# -*- coding: utf-8 -*-
"""
cv_target.py — 屏幕/窗口目标识别（本机截图 + 模板匹配 / 红色条分析），可联动 ESP 键盘控制台

为什么分两种模式：
  1) template —— 模板匹配。目标"整体长相固定"时最准：拿一张小图当模板，多尺度搜索。
  2) bar      —— 红色条检测。适合"血条/进度条"这种**长度会变**的元素。
     血条变短时模板匹配必然失效（像素对不上），所以用颜色+形状判断，
     并输出"填充比例"（当前血条大概几个百分点）。
  3) both     —— 先模板定位，再在命中框里算红色条填充比例。

截图来源（三选一，优先级：--image > --window > 全屏）：
  --window "冒险岛怀旧服"   只截这个窗口的客户区（跟着窗口移动/改大小走）
  --image shot.png         直接读一张图（离线调参用，不必开着游戏）
  （都不给）                截主屏全屏

常用流程：
  # 0) 先在线框选，把游戏里那个元素做成模板（拖鼠标框住它，回车确认）
  python cv_target.py --window "冒险岛怀旧服" --calibrate

  # 1) 单次识别，打印 JSON（带 --debug 会存一张画了框的图，肉眼核对）
  python cv_target.py --window "冒险岛怀旧服" --once --debug debug.png

  # 2) 持续监控：每 0.3 秒找一次；找到就先按一次 d（1.5 秒内不重复按）
  python cv_target.py --window "冒险岛怀旧服" --loop 0.3 --key d --cooldown 1.5

  # 3) 不靠模板，只找窗口里的红色横条（血条类）
  python cv_target.py --window "冒险岛怀旧服" --mode bar --once

退出码：命中 0 / 未命中 1 / 参数或运行错误 3
依赖：opencv-python、numpy、mss、pywin32（均已在本机 Python310 可用）

窗口识别（不依赖坐标硬编码）：
  python cv_target.py --list-windows                      # 列出所有可见窗口
  python cv_target.py --wininfo --window "冒险岛怀旧服"    # 识别这个窗口：位置/客户区/是否前台/是否被挡
  python cv_target.py --window "冒险岛怀旧服" --shot win.png   # 按窗口现截一张，核对识别对不对
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import cv2
import numpy as np
import mss

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_DIR = os.path.join(HERE, "templates")
DEFAULT_TPL = os.path.join(TPL_DIR, "target.png")
ESP_TAP = "http://127.0.0.1:8080/api/tap?combo="


# ---------------- 截图来源 ----------------
def dpi_aware():
    """让进程按物理像素取窗口坐标，否则 Win32 返回逻辑坐标、和 mss 截图对不齐"""
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 每显示器 DPI 感知
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _root(hwnd):
    import ctypes
    try:
        return ctypes.windll.user32.GetAncestor(hwnd, 2)   # GA_ROOT
    except Exception:
        return hwnd


def list_windows(min_w=200, min_h=100):
    """所有可见、有标题、面积够大的顶层窗口"""
    import win32gui
    out = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return
        r = win32gui.GetWindowRect(hwnd)
        w, h = r[2] - r[0], r[3] - r[1]
        if w < min_w or h < min_h:
            return
        out.append({"hwnd": hwnd, "title": title, "rect": [r[0], r[1], w, h],
                    "minimized": bool(win32gui.IsIconic(hwnd))})

    win32gui.EnumWindows(cb, None)
    out.sort(key=lambda d: -(d["rect"][2] * d["rect"][3]))
    return out


def wininfo(title_sub):
    """识别窗口：几何、客户区、前台、遮挡、DPI"""
    import ctypes
    import win32gui
    dpi_aware()
    hwnd, title, (cx, cy, cw, ch) = find_window(title_sub)
    r = win32gui.GetWindowRect(hwnd)
    fg = _root(win32gui.GetForegroundWindow()) == hwnd
    # 客户区中心点上是哪个窗口？不是自己就说明被挡了
    px, py = cx + cw // 2, cy + ch // 2
    at = win32gui.WindowFromPoint((px, py))
    covered = bool(at) and _root(at) != hwnd
    try:
        dpi = int(ctypes.windll.user32.GetDpiForWindow(hwnd))
    except Exception:
        dpi = None
    info = {
        "title": title, "hwnd": hwnd,
        "window_rect": [r[0], r[1], r[2] - r[0], r[3] - r[1]],
        "client_rect": [cx, cy, cw, ch],
        "client_origin": [cx, cy],
        "minimized": bool(win32gui.IsIconic(hwnd)),
        "foreground": fg,
        "covered_at_center": covered,
        "covered_by": win32gui.GetWindowText(at) if covered else None,
        "dpi": dpi,
        "dpi_scale": round(dpi / 96.0, 3) if dpi else None,
    }
    return info


def grab(x, y, w, h):
    """截屏幕上一块矩形 -> BGR"""
    with mss.mss() as sct:
        mon = {"left": int(x), "top": int(y), "width": int(w), "height": int(h)}
        raw = np.array(sct.grab(mon))          # BGRA
    return cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)


def find_window(title_sub):
    """按标题子串找窗口，返回 (hwnd, 标题, (屏幕x, 屏幕y, 宽, 高))"""
    import win32gui
    hits = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if t and title_sub in t:
                hits.append((hwnd, t))

    win32gui.EnumWindows(cb, None)
    if not hits:
        sys.exit("[错误] 没找到标题包含“%s”的窗口" % title_sub)
    hwnd, title = hits[0]
    if win32gui.IsIconic(hwnd):
        sys.exit("[错误] 窗口“%s”已最小化，请先恢复" % title)
    x, y = win32gui.ClientToScreen(hwnd, (0, 0))
    w, h = win32gui.GetClientRect(hwnd)[2:]
    return hwnd, title, (x, y, w, h)


def focus_window(hwnd):
    """把窗口提到最前（截图截的是屏幕像素，窗口被遮挡就会截到别的东西）"""
    import win32gui
    win32gui.ShowWindow(hwnd, 9)      # SW_RESTORE
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.3)


def get_source(args):
    """返回 (图像, 该图像左上角在屏幕上的坐标)"""
    if args.image:
        img = cv2.imdecode(np.fromfile(args.image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            sys.exit("[错误] 图片读取失败: %s" % args.image)
        return img, (0, 0)
    if args.window:
        hwnd, title, (wx, wy, ww, wh) = find_window(args.window)
        if args.focus:
            focus_window(hwnd)
        if args.region:
            rx, ry, rw, rh = args.region
            return grab(wx + rx, wy + ry, rw, rh), (wx + rx, wy + ry)
        return grab(wx, wy, ww, wh), (wx, wy)
    if args.region:
        rx, ry, rw, rh = args.region
        return grab(rx, ry, rw, rh), (rx, ry)
    with mss.mss() as sct:
        m = sct.monitors[0]
    return grab(m["left"], m["top"], m["width"], m["height"]), (m["left"], m["top"])


def load_tpl(path):
    if not os.path.isfile(path):
        sys.exit("[错误] 模板不存在: %s（先用 --calibrate 或 --shot 造一个）" % path)
    t = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if t is None:
        sys.exit("[错误] 模板读取失败: %s" % path)
    return t


# ---------------- 模板匹配 ----------------
def parse_scales(args):
    if args.scale_range:
        lo, hi, step = [float(v) for v in args.scale_range.split(",")]
        out, sc = [], lo
        while sc <= hi + 1e-9:
            out.append(round(sc, 3))
            sc += step
        return out
    return [args.scale]


def match_best(screen, tpl, scales, thresh):
    best = None
    sh, sw = screen.shape[:2]
    for sc in scales:
        if sc <= 0:
            continue
        t = cv2.resize(tpl, None, fx=sc, fy=sc,
                       interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_LINEAR)
        th, tw = t.shape[:2]
        if th > sh or tw > sw or th < 4 or tw < 4:
            continue
        res = cv2.matchTemplate(screen, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if best is None or mx > best["score"]:
            best = {"score": float(mx), "x": int(loc[0]), "y": int(loc[1]),
                    "w": int(tw), "h": int(th), "scale": float(sc)}
    if best is None:
        return {"found": False, "score": 0.0, "reason": "模板比截图大，或缩放非法"}
    best["found"] = best["score"] >= thresh
    return best


# ---------------- 红色条分析 ----------------
def red_masks(bgr):
    """(亮红, 暗红)：亮红≈已填充，暗红≈血条底槽"""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    bright = (cv2.inRange(hsv, (0, 130, 130), (12, 255, 255)) |
              cv2.inRange(hsv, (168, 130, 130), (180, 255, 255)))
    dark = (cv2.inRange(hsv, (0, 90, 35), (18, 255, 129)) |
            cv2.inRange(hsv, (162, 90, 35), (180, 255, 129)))
    k = np.ones((3, 3), np.uint8)
    return cv2.morphologyEx(bright, cv2.MORPH_CLOSE, k), cv2.morphologyEx(dark, cv2.MORPH_CLOSE, k)


def bar_fill(bgr):
    """小图里红色条的填充比例：取红像素最多的那一行，亮红/全部红"""
    bright, dark = red_masks(bgr)
    any_red = cv2.bitwise_or(bright, dark)
    row_any = (any_red > 0).sum(axis=1)
    if row_any.max() == 0:
        return None
    y = int(row_any.argmax())
    total = int(row_any[y])
    if total < 8:
        return None
    return round(int((bright[y] > 0).sum()) / float(total), 3)


def find_bars(screen, offset=(0, 0), min_w=24, min_ratio=4.0):
    bright, dark = red_masks(screen)
    any_red = cv2.bitwise_or(bright, dark)
    cnts, _ = cv2.findContours(any_red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if w < min_w or h < 2 or w / float(h) < min_ratio:
            continue
        tot = int((any_red[y:y + h, x:x + w] > 0).sum())
        fil = int((bright[y:y + h, x:x + w] > 0).sum())
        out.append({"x": x + offset[0], "y": y + offset[1], "w": w, "h": h,
                    "fill": round(fil / float(max(1, tot)), 3)})
    out.sort(key=lambda d: -d["w"])
    return out


# ---------------- 联动 ESP 控制台 ----------------
def esp_tap(key):
    url = ESP_TAP + urllib.parse.quote(key)
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            body = r.read().decode("utf-8", "ignore")
        return '"ok": true' in body
    except Exception as e:
        print("[警告] 触发按键失败: %s" % e, file=sys.stderr)
        return False


# ---------------- 调试图 ----------------
def save_debug(screen, hit, path, bars=None):
    vis = screen.copy()
    if hit and hit.get("found"):
        cv2.rectangle(vis, (hit["x"], hit["y"]),
                      (hit["x"] + hit["w"], hit["y"] + hit["h"]), (0, 255, 0), 2)
        cv2.putText(vis, "%.3f" % hit["score"], (hit["x"], max(12, hit["y"] - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
    for b in (bars or []):
        cv2.rectangle(vis, (b["x"], b["y"]), (b["x"] + b["w"], b["y"] + b["h"]), (0, 200, 255), 1)
    cv2.imencode(".png", vis)[1].tofile(path)


# ---------------- 单次识别 ----------------
def detect(args):
    t0 = time.time()
    screen, off = get_source(args)
    res = {"found": False, "shot_size": [screen.shape[1], screen.shape[0]]}

    if args.mode in ("template", "both"):
        hit = match_best(screen, load_tpl(args.tpl), parse_scales(args), args.thresh)
        res.update({"score": round(hit["score"], 4), "scale": hit.get("scale"),
                    "found": hit["found"], "hit": hit})
        if hit["score"] > 0:
            res["box_local"] = [hit["x"], hit["y"], hit["w"], hit["h"]]
            res["box"] = [hit["x"] + off[0], hit["y"] + off[1], hit["w"], hit["h"]]
            res["center"] = [hit["x"] + off[0] + hit["w"] // 2, hit["y"] + off[1] + hit["h"] // 2]
        if hit["score"] > 0:
            crop = screen[max(0, hit["y"]):hit["y"] + hit["h"],
                          max(0, hit["x"]):hit["x"] + hit["w"]]
            res["fill"] = bar_fill(crop)

    if args.mode in ("bar", "both"):
        bars = find_bars(screen, off, args.min_w)
        res["bars"] = bars[:5]
        if args.mode == "bar":
            res["found"] = bool(bars)
            if bars:
                b = bars[0]
                res["box"] = [b["x"], b["y"], b["w"], b["h"]]
                res["center"] = [b["x"] + b["w"] // 2, b["y"] + b["h"] // 2]
                res["fill"] = b["fill"]

    res["took_ms"] = int((time.time() - t0) * 1000)
    if args.debug:
        save_debug(screen, res.get("hit"), args.debug, res.get("bars"))
    return res


# ---------------- 在线框选校准 ----------------
def calibrate(args):
    """截一张图，弹窗让人用鼠标框住目标，存成模板"""
    img, _off = get_source(args)
    import tkinter as tk
    from PIL import Image, ImageTk

    h, w = img.shape[:2]
    scale = min(1.0, 1200.0 / w, 760.0 / h)
    disp = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) \
        if scale < 1.0 else img.copy()

    root = tk.Tk()
    root.title("框住要识别的元素：按住左键拖出矩形 → 回车确认 / Esc 取消")
    canvas = tk.Canvas(root, width=disp.shape[1], height=disp.shape[0], cursor="cross")
    canvas.pack()
    photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)))
    canvas.create_image(0, 0, anchor="nw", image=photo)

    st = {"x": 0, "y": 0, "rect": None}
    rect_id = [None]

    def on_down(e):
        st["x"], st["y"] = e.x, e.y
        if rect_id[0]:
            canvas.delete(rect_id[0])
        rect_id[0] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#00ff88", width=2)

    def on_drag(e):
        if rect_id[0]:
            canvas.coords(rect_id[0], st["x"], st["y"], e.x, e.y)

    def on_up(e):
        x0, y0 = min(st["x"], e.x), min(st["y"], e.y)
        x1, y1 = max(st["x"], e.x), max(st["y"], e.y)
        st["rect"] = (x0, y0, x1 - x0, y1 - y0)

    def on_ok(e=None):
        if not st["rect"] or st["rect"][2] < 6 or st["rect"][3] < 4:
            print("[提示] 框太小或没框，重新拖一次")
            return
        x, y, ww, hh = st["rect"]
        ox = int(round(x / scale))
        oy = int(round(y / scale))
        ow = int(round(ww / scale))
        oh = int(round(hh / scale))
        crop = img[oy:oy + oh, ox:ox + ow].copy()
        os.makedirs(TPL_DIR, exist_ok=True)
        out = args.tpl
        cv2.imencode(".png", crop)[1].tofile(out)
        print("已保存模板: %s  %dx%d" % (out, crop.shape[1], crop.shape[0]))
        print("红色条填充比例: %s" % bar_fill(crop))
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_down)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_up)
    root.bind("<Return>", on_ok)
    root.bind("<Escape>", lambda e: root.destroy())
    root.mainloop()
    return 0 if os.path.isfile(args.tpl) else 1


# ---------------- 自检 ----------------
def selftest(args):
    tpl = load_tpl(args.tpl)
    print("模板: %s  %dx%d" % (args.tpl, tpl.shape[1], tpl.shape[0]))
    print("红色条填充比例(模板自身): %s" % bar_fill(tpl))
    canvas = np.zeros((600, 900, 3), np.uint8)
    canvas[:] = (30, 30, 30)
    ox, oy = 317, 241
    canvas[oy:oy + tpl.shape[0], ox:ox + tpl.shape[1]] = tpl
    hit = match_best(canvas, tpl, [1.0], 0.9)
    print("合成画面定位: found=%s 期望(%d,%d) 实得(%d,%d) 分数 %.4f" %
          (hit["found"], ox, oy, hit["x"], hit["y"], hit["score"]))
    b = find_bars(canvas)
    print("红色条检测: %s" % (b[:3] if b else "无"))
    ok = hit["found"] and abs(hit["x"] - ox) <= 1 and abs(hit["y"] - oy) <= 1
    print("自检: %s" % ("通过" if ok else "失败"))
    return 0 if ok else 2


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser(description="窗口/屏幕目标识别（模板/红色条），可联动 ESP 控制台")
    ap.add_argument("--window", help="窗口标题子串，例如 冒险岛怀旧服")
    ap.add_argument("--list-windows", action="store_true", help="列出所有可见窗口")
    ap.add_argument("--wininfo", action="store_true", help="识别指定窗口的位置/客户区/前台/遮挡")
    ap.add_argument("--image", help="读一张图片当输入（离线调参）")
    ap.add_argument("--region", help="只搜该区域 x,y,w,h（给了 --window 时按窗口内坐标）")
    ap.add_argument("--focus", action="store_true", help="截图前把窗口提到最前")
    ap.add_argument("--mode", choices=["template", "bar", "both"], default="both")
    ap.add_argument("--tpl", default=DEFAULT_TPL, help="模板路径")
    ap.add_argument("--thresh", type=float, default=0.75, help="模板匹配阈值 0~1")
    ap.add_argument("--scale", type=float, default=1.0, help="单尺度")
    ap.add_argument("--scale-range", help="多尺度 低,高,步长 例如 0.9,1.3,0.05")
    ap.add_argument("--min-w", type=int, default=24, help="红色条最小宽度")
    ap.add_argument("--once", action="store_true", help="只识别一次")
    ap.add_argument("--loop", type=float, help="循环间隔(秒)")
    ap.add_argument("--key", help="命中后按一次这个键，如 d / ctrl+c")
    ap.add_argument("--trigger", choices=["found", "absent"], default="found")
    ap.add_argument("--cooldown", type=float, default=1.0, help="触发最小间隔(秒)")
    ap.add_argument("--debug", help="输出画框图路径")
    ap.add_argument("--shot", help="只截一张图保存到该路径")
    ap.add_argument("--calibrate", action="store_true", help="框选生成模板")
    ap.add_argument("--selftest", action="store_true", help="离线自检")
    ap.add_argument("--exit-on-hit", action="store_true", help="命中即退出")
    args = ap.parse_args()

    dpi_aware()

    if args.list_windows:
        print(json.dumps(list_windows(), ensure_ascii=False, indent=2))
        return 0

    if args.wininfo:
        if not args.window:
            sys.exit("[错误] --wininfo 需要配合 --window \"标题片段\" 使用")
        print(json.dumps(wininfo(args.window), ensure_ascii=False, indent=2))
        return 0

    if args.region:
        args.region = tuple(int(v) for v in args.region.split(","))
        if len(args.region) != 4:
            sys.exit("[错误] --region 需要 x,y,w,h 四个数")

    if args.shot:
        img, off = get_source(args)
        cv2.imencode(".png", img)[1].tofile(args.shot)
        print("已保存: %s  %dx%d  屏幕原点=%s" % (args.shot, img.shape[1], img.shape[0], off))
        return 0

    if args.calibrate:
        return calibrate(args)

    if args.selftest:
        return selftest(args)

    last_fire = 0.0
    while True:
        try:
            res = detect(args)
        except SystemExit:
            raise
        except Exception as e:
            print("[错误] %s" % e, file=sys.stderr)
            return 3
        print(json.dumps(res, ensure_ascii=False))

        fire = res["found"] if args.trigger == "found" else (not res["found"])
        if fire and args.key and (time.time() - last_fire) >= args.cooldown:
            ok = esp_tap(args.key)
            last_fire = time.time()
            print("[触发] KEY %s -> %s" % (args.key, "已发送" if ok else "失败"), file=sys.stderr)

        if args.exit_on_hit and fire:
            return 0
        if not args.loop:
            return 0 if res["found"] else 1
        time.sleep(max(0.05, args.loop))


if __name__ == "__main__":
    sys.exit(main())
