# -*- coding: utf-8 -*-
"""Game window capture -> MJPEG web preview.

Run:  python webui/server.py
Open: http://127.0.0.1:8765
"""
import asyncio
import collections
import ctypes
import json
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import cv2
import mss
import numpy as np
import uvicorn
import win32con
import win32gui
from fastapi import Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse


ctypes.windll.user32.SetProcessDPIAware()

SCREENSHOT = getattr(mss, "MSS", None) or getattr(mss, "mss")

WINDOW_TITLE = "冒险岛怀旧服"
PREVIEW_W = 320          # 小方格宽度（px）
STREAM_W = 1024          # 推流分辨率宽度：预览可放大到千px级，1024 仍有清晰余量
DEFAULT_FPS = 30
JPEG_QUALITY = 72
# 默认监听所有网卡：手机连同一个 WiFi 时直接走局域网地址最顺畅（往返几毫秒，
# 对比免费穿透的 1 秒往返是降维打击）。只想给本机用就 set PREVIEW_HOST=127.0.0.1。
HOST = (os.environ.get("PREVIEW_HOST") or "0.0.0.0").strip() or "0.0.0.0"
PORT = int(os.environ.get("PREVIEW_PORT") or 8765)
DETECT_CONF = 0.35
# 两种部署配置，实测差异（30 帧真实画面，含 UI 排除+去重，对比 best.pt@960）：
#   速度: INT8 384x672  ~44ms/22FPS  recall 83.6%  多检 0.8/帧
#   精度: INT8 544x960  ~96ms/10FPS  recall 89.6%  多检 0.3/帧
MODES = {
    "fast": {"dir": "model_int8_fast_openvino_model", "imgsz": (384, 672),
             "conf": DETECT_CONF, "label": "速度"},
    "accurate": {"dir": "model_int8_openvino_model", "imgsz": (544, 960),
                 "conf": DETECT_CONF, "label": "精度"},
}
OV_MODEL_DIR = MODES["fast"]["dir"]
DETECT_IMGSZ = MODES["fast"]["imgsz"]

# ---------------- UI 排除区（可在 WebUI 框选 / 保存） ----------------
# 归一化坐标（占客户区宽高的比例，与分辨率无关），存在同目录 masks.json 里。
MASK_FILE = Path(__file__).with_name("masks.json")
MASK_GRAY = 114                 # 与 YOLO letterbox 填充值一致，对模型最"无感"
MIN_MASK_SIZE = 0.01            # 最小边长（归一化），防止手抖画出零面积小框
DEFAULT_MASKS = [
    {"id": "m1", "x1": 0.00, "y1": 0.00, "x2": 0.17, "y2": 0.26},   # 左上小地图
    {"id": "m2", "x1": 0.00, "y1": 0.40, "x2": 0.07, "y2": 0.70},   # 左侧任务灯泡 / 向导列
    {"id": "m3", "x1": 0.00, "y1": 0.86, "x2": 1.00, "y2": 1.00},   # 底部状态栏 + 聊天条
    {"id": "m4", "x1": 0.66, "y1": 0.68, "x2": 1.00, "y2": 0.91},   # 右下快捷栏 / 商城
    {"id": "m5", "x1": 0.66, "y1": 0.00, "x2": 1.00, "y2": 0.12},   # 右上任务提示
]

mask_lock = threading.Lock()    # 保护 UI_MASKS 的读改写 + 落盘
UI_MASKS = []                   # [{"id","x1","y1","x2","y2"}]


def _clamp01(v):
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


def _clean_mask(raw, fallback_id=""):
    """把任意输入整理成一个合法区域；太小或坐标非法返回 None。"""
    if not isinstance(raw, dict):
        return None
    try:
        x1 = _clamp01(float(raw["x1"]))
        y1 = _clamp01(float(raw["y1"]))
        x2 = _clamp01(float(raw["x2"]))
        y2 = _clamp01(float(raw["y2"]))
    except (KeyError, TypeError, ValueError):
        return None
    if any(v != v for v in (x1, y1, x2, y2)):        # NaN
        return None
    x1, x2 = min(x1, x2), max(x1, x2)
    y1, y2 = min(y1, y2), max(y1, y2)
    if x2 - x1 < MIN_MASK_SIZE or y2 - y1 < MIN_MASK_SIZE:
        return None
    return {"id": str(raw.get("id") or fallback_id),
            "x1": round(x1, 4), "y1": round(y1, 4),
            "x2": round(x2, 4), "y2": round(y2, 4)}


def load_masks():
    if MASK_FILE.exists():
        try:
            data = json.loads(MASK_FILE.read_text(encoding="utf-8"))
            items = data.get("masks", []) if isinstance(data, dict) else data
            out, seen = [], set()
            for i, raw in enumerate(items):
                m = _clean_mask(raw, f"m{i + 1}")
                if m is None or m["id"] in seen:
                    continue
                seen.add(m["id"])
                out.append(m)
            return out
        except Exception as e:  # noqa: BLE001
            print(f"[mask] 读取 {MASK_FILE.name} 失败，改用默认区域: {e}")
    return [dict(m) for m in DEFAULT_MASKS]


def save_masks(masks):
    """先写临时文件再原子替换，避免写一半断电留下坏 JSON。"""
    tmp = MASK_FILE.with_name(MASK_FILE.name + ".tmp")
    tmp.write_text(json.dumps({"masks": masks}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(MASK_FILE)


def mask_snapshot():
    with mask_lock:
        return [dict(m) for m in UI_MASKS]


def mask_rects():
    """给推理热路径用的 (x1,y1,x2,y2) 快照，加锁一次取出。"""
    with mask_lock:
        return [(m["x1"], m["y1"], m["x2"], m["y2"]) for m in UI_MASKS]


def _next_mask_id():
    existing = {m["id"] for m in UI_MASKS}
    n = 1
    while f"m{n}" in existing:
        n += 1
    return f"m{n}"


UI_MASKS = load_masks()


def apply_ui_mask(frame, rects=None):
    """把这些区域涂成中性灰：模型根本看不到固定 UI，从根上消灭"灯泡变蝙蝠"。"""
    h, w = frame.shape[:2]
    for fx1, fy1, fx2, fy2 in (rects if rects is not None else mask_rects()):
        frame[int(fy1 * h):int(fy2 * h), int(fx1 * w):int(fx2 * w)] = MASK_GRAY
    return frame


def in_ui_mask(box, w, h, rects=None):
    """检测框中心落在排除区内则丢弃（涂灰之外的双保险）。"""
    cx = (box[0] + box[2]) / 2 / w
    cy = (box[1] + box[3]) / 2 / h
    for fx1, fy1, fx2, fy2 in (rects if rects is not None else mask_rects()):
        if fx1 <= cx <= fx2 and fy1 <= cy <= fy2:
            return True
    return False


def find_window():
    hwnd = win32gui.FindWindow(None, WINDOW_TITLE)
    if hwnd and win32gui.IsWindowVisible(hwnd):
        return hwnd
    hits = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h):
            t = win32gui.GetWindowText(h)
            if t and (WINDOW_TITLE in t or "冒险岛" in t):
                hits.append(h)

    win32gui.EnumWindows(cb, None)
    return hits[0] if hits else None


def client_region(hwnd):
    if win32gui.IsIconic(hwnd):
        return None
    l, t, r, b = win32gui.GetClientRect(hwnd)
    if r - l < 64 or b - t < 64:
        return None
    x, y = win32gui.ClientToScreen(hwnd, (0, 0))
    return {"left": x, "top": y, "width": r - l, "height": b - t}


def _wait_foreground(hwnd, timeout=0.6):
    u32 = ctypes.windll.user32
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if u32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.03)
    return False


def bring_to_front():
    """把游戏窗口抬到最前并获得焦点。

    游戏用 DX 渲染，PrintWindow 抓不到内容（实测返回全黑），被别的窗口挡住时
    mss 抓到的就是挡在前面的窗口。所以凡是要截图的地方都得先前置。

    Windows 的"前台锁"会静默忽略 SetForegroundWindow（返回值 0，前台不变）。
    实测这台机器上 AttachThreadInput 跨进程会 Access denied（错误 5），真正管用
    的是：先模拟按一下 ALT —— 系统认为"用户刚有输入"，随后那次 SetForegroundWindow
    就放行。所以按"直接切 → ALT 唤醒 → 置顶兜底"三级依次尝试。

    返回 (ok, 说明文字)。
    """
    hwnd = find_window()
    if not hwnd:
        return False, "未找到游戏窗口"
    u32 = ctypes.windll.user32
    SWP = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.2)
        if u32.GetForegroundWindow() == hwnd:
            return True, "已在前台"

        # 1) 直接切（游戏刚被用过时这一步就够）
        u32.SetForegroundWindow(hwnd)
        if _wait_foreground(hwnd, 0.35):
            return True, "已前置"

        # 2) 模拟一次 ALT：解除前台锁，系统放行紧接着的切换
        u32.keybd_event(win32con.VK_MENU, 0, 0, 0)
        u32.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)
        u32.SetForegroundWindow(hwnd)
        if _wait_foreground(hwnd, 0.45):
            return True, "已前置"

        # 3) 置顶再取消置顶，最后再切一次
        u32.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, SWP)
        u32.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, SWP)
        u32.SetForegroundWindow(hwnd)
        if _wait_foreground(hwnd, 0.45):
            return True, "已前置"
        return False, "前置被系统拒绝（游戏是否以管理员身份运行？手动点一下游戏窗口即可）"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def placeholder(msg=b"NO WINDOW"):
    img = np.full((360, 640, 3), 24, np.uint8)
    cv2.putText(img, msg.decode(), (40, 190), cv2.FONT_HERSHEY_DUPLEX,
                1.0, (120, 120, 120), 1, cv2.LINE_AA)
    return img


class Grabber(threading.Thread):
    """持续抓取游戏客户区，保留最新一帧供 Web 层取用。"""

    def __init__(self, target_fps=90):
        super().__init__(daemon=True)
        self.interval = 1.0 / target_fps
        self.lock = threading.Lock()
        self.frame = placeholder()
        self.window_found = False
        self.client_size = (0, 0)
        self.capture_fps = 0.0
        self.stream_fps = 0.0
        self.frames_sent = 0
        self.last_frame_ts = 0.0
        self.error = ""
        self.running = True

    def snapshot(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else placeholder()

    def stamp(self):
        """最新一帧的时间戳。判断"抓到的是不是前置之后的新帧"用。"""
        return self.last_frame_ts

    def wait_fresh(self, since, timeout=1.5):
        """等到出现比 since 更新的一帧；超时返回 False。"""
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            if self.last_frame_ts > since:
                return True
            time.sleep(0.01)
        return False

    def run(self):
        with SCREENSHOT() as sct:
            count, t0 = 0, time.perf_counter()
            while self.running:
                tick = time.perf_counter()
                hwnd = find_window()
                region = client_region(hwnd) if hwnd else None
                if region is None:
                    self.window_found = False
                    self.error = "窗口未找到或已最小化" if not hwnd else "客户区尺寸异常"
                    with self.lock:
                        self.frame = placeholder()
                        self.client_size = (0, 0)
                    time.sleep(0.25)
                    continue

                try:
                    raw = sct.grab(region)
                    frame = np.asarray(raw)[:, :, :3].copy()
                except Exception as e:  # noqa: BLE001
                    self.error = f"{type(e).__name__}: {e}"
                    time.sleep(0.2)
                    continue

                with self.lock:
                    self.frame = frame
                    self.window_found = True
                    self.client_size = (region["width"], region["height"])
                    self.last_frame_ts = time.time()
                    self.error = ""
                count += 1
                dt = time.perf_counter() - t0
                if dt >= 1.0:
                    self.capture_fps, count, t0 = count / dt, 0, time.perf_counter()

                spare = self.interval - (time.perf_counter() - tick)
                if spare > 0:
                    time.sleep(spare)

    def note_sent(self):
        self.frames_sent += 1


grabber = Grabber()
grabber.start()

# ---------------- 状态条识别（HP / MP / EXP） ----------------
# 三条是固定 UI：位置固定、颜色固定、只沿水平方向变化。所以不走训练/推理，
# 直接按"色相 + 饱和度"逐列扫描，既准（逐像素）又快（3 个 ~160x15 的 ROI，
# 亚毫秒级），也完全不受模型召回率影响。
BAR_FILE = Path(__file__).with_name("bars.json")
BAR_KINDS = ("hp", "mp", "exp")
BAR_LABEL = {"hp": "HP", "mp": "MP", "exp": "EXP"}
BAR_HUE_TOL = 30        # 色相容差（OpenCV 色相 0~180，对应 0~360°）
BAR_S_MIN, BAR_V_MIN = 90, 90   # 填充色一定"够艳够亮"，空槽是浅灰(S≈0)或深色(V<80)
BAR_EDGE_V = 110        # 槽内（含右侧浅灰空槽）V>=150，槽外 HUD 底板 V≈68，取中间值
BAR_JUMP_MIN = 12       # 相邻列中位色的跳变阈值（BGR 三通道差之和），用来判断"是不是一条槽"

# 默认坐标：1366×768 怀旧服实测（归一到客户区宽高）。
# 实测三条槽（含左右 1px 边框）：红 465-631 / 蓝 633-799 / 黄绿 809-975，都在 y 747-763。
# 注意存的必须是"整条槽"而不是彩色填充的范围——满血时填充会一直顶到槽底，
# 只框填充的话分母会偏小，读数永远 100%（这就是 HP 95.9% 读成 100% 的原因）。
# measure_bar 里还有一层自动外扩兜底，标定时只框住填充也能自动补齐。
DEFAULT_BARS = {
    "hp": {"x1": 0.3404, "y1": 0.9727, "x2": 0.4619, "y2": 0.9935, "hue": 1},
    "mp": {"x1": 0.4634, "y1": 0.9727, "x2": 0.5849, "y2": 0.9935, "hue": 103},
    "exp": {"x1": 0.5922, "y1": 0.9727, "x2": 0.7138, "y2": 0.9935, "hue": 33},
}

bar_lock = threading.Lock()
BAR_CFG = {}
# 坐标是谁定的：auto = 自动定位（分辨率/客户端变了可以自己重定位）；manual = 用户手框过，不再自动覆盖
BAR_META = {"source": "auto", "res": None}


def _clean_hue(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:                                  # NaN
        return None
    # OpenCV 色相 0~180，允许端点外的值归一回来
    f = f % 180.0
    return round(f, 1)


def _clean_rect(raw):
    """把坐标整理成合法归一化矩形；太小或非法返回 None。"""
    if not isinstance(raw, dict):
        return None
    try:
        x1 = _clamp01(float(raw["x1"]))
        y1 = _clamp01(float(raw["y1"]))
        x2 = _clamp01(float(raw["x2"]))
        y2 = _clamp01(float(raw["y2"]))
    except (KeyError, TypeError, ValueError):
        return None
    if any(v != v for v in (x1, y1, x2, y2)):
        return None
    x1, x2 = min(x1, x2), max(x1, x2)
    y1, y2 = min(y1, y2), max(y1, y2)
    if x2 - x1 < 0.01 or y2 - y1 < 0.003:
        return None
    return {"x1": round(x1, 4), "y1": round(y1, 4),
            "x2": round(x2, 4), "y2": round(y2, 4)}


def load_bars():
    cfg = {k: dict(v) for k, v in DEFAULT_BARS.items()}
    meta = {"source": "auto", "res": None}
    if BAR_FILE.exists():
        try:
            data = json.loads(BAR_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if data.get("source") in ("auto", "manual"):
                    meta["source"] = data["source"]
                res = data.get("res")
                if isinstance(res, (list, tuple)) and len(res) == 2:
                    meta["res"] = [int(res[0]), int(res[1])]
            raw = data.get("bars", {}) if isinstance(data, dict) else {}
            for k in BAR_KINDS:
                item = raw.get(k)
                if not isinstance(item, dict):
                    continue
                rect = _clean_rect(item)
                if rect is None:
                    continue
                cfg[k] = {**rect, "hue": _clean_hue(item.get("hue"))}
        except Exception as e:  # noqa: BLE001
            print(f"[bars] 读取 {BAR_FILE.name} 失败，改用默认坐标: {e}")
    BAR_META.update(meta)
    return cfg


def save_bars():
    tmp = BAR_FILE.with_name(BAR_FILE.name + ".tmp")
    with bar_lock:
        data = {k: dict(v) for k, v in BAR_CFG.items()}
        meta = dict(BAR_META)
    tmp.write_text(json.dumps({"source": meta.get("source", "auto"),
                               "res": meta.get("res"),
                               "bars": data}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(BAR_FILE)


def mark_bars(source=None, res=None):
    """记录坐标来源和定这套坐标时的客户区分辨率。"""
    with bar_lock:
        if source in ("auto", "manual"):
            BAR_META["source"] = source
        if res is not None:
            BAR_META["res"] = [int(res[0]), int(res[1])]


BAR_CFG = load_bars()


def bar_snapshot():
    with bar_lock:
        return {k: dict(v) for k, v in BAR_CFG.items()}


def _roi_px(rect, w, h):
    x1 = int(round(rect["x1"] * w))
    x2 = int(round(rect["x2"] * w))
    y1 = int(round(rect["y1"] * h))
    y2 = int(round(rect["y2"] * h))
    x1 = max(0, min(x1, w - 1))
    y1 = max(0, min(y1, h - 1))
    x2 = max(x1 + 1, min(x2, w))
    y2 = max(y1 + 1, min(y2, h))
    return x1, y1, x2, y2


def slot_bounds(frame, x1, y1, x2, y2):
    """把框自动外扩成"整条槽"的左右边界（像素，含左右边框）。

    条内不管填充色还是右侧浅灰空槽都够亮（V>=150），槽外是 HUD 深色底板（V≈68），
    中间还夹着 1px 全黑的分隔列。所以按列亮度找"包含框中心的连续亮区"，两端就是槽的边框。
    这样即使用户只框住左边的彩色填充（满血时最容易这么框），分母也还是整条槽——
    否则空槽被排除在外，读数会永远卡在 100%（HP 562/586 被读成 100% 就是这个坑）。

    找不到可信的槽（亮区顶到搜索窗边界，说明外面也亮，不可信）就原样返回。
    """
    h, w = frame.shape[:2]
    pad = max(24, int(w * 0.16))
    wx1 = max(0, x1 - pad)
    wx2 = min(w, x2 + pad)
    a = y1 + int((y2 - y1) * 0.2)
    b = max(a + 1, y2 - int((y2 - y1) * 0.2))
    if b - a < 2 or wx2 - wx1 < 8:
        return x1, x2
    v = cv2.cvtColor(frame[a:b, wx1:wx2], cv2.COLOR_BGR2HSV)[..., 2]
    colv = np.median(v, axis=0)
    bright = colv >= BAR_EDGE_V
    c = min(max((x1 + x2) // 2 - wx1, 0), len(colv) - 1)
    if not bright[c]:
        idx = np.flatnonzero(bright)     # 框中心正好压在暗处，取最近的亮列重新起步
        if idx.size == 0:
            return x1, x2
        c = int(idx[np.argmin(np.abs(idx - c))])
    lo = c
    while lo > 0 and bright[lo - 1]:
        lo -= 1
    hi = c
    while hi < len(colv) - 1 and bright[hi + 1]:
        hi += 1
    if hi - lo < 8 or (lo == 0 and wx1 == 0) or (hi == len(colv) - 1 and wx2 == w):
        return x1, x2
    return wx1 + lo, wx1 + hi


def _runs_of(mask, minlen=30):
    """把布尔序列切成连续 True 段，返回 [(start, end)]（含端点）。"""
    out, s = [], None
    for i, b in enumerate(mask):
        if b and s is None:
            s = i
        elif not b and s is not None:
            if i - s >= minlen:
                out.append((s, i - 1))
            s = None
    if s is not None and len(mask) - s >= minlen:
        out.append((s, len(mask) - 1))
    return out


def find_bar_slots(frame):
    """在画面底部自动找三条并排的细长槽（HP / MP / EXP 的固定排版）。

    换客户端、换分辨率、换皮肤时不用手动标定——前提是"三条横排、等宽、靠屏幕下方"。
    做法：在底部 20% 高度逐行找亮段（V>=110），挑三条宽度接近、间隔很小、细长
    （宽高比>=5、高<=4.5%画面高）的相邻段，再按三条中心列向上/向下延伸出高度。
    候选取"最贴底"的那组，所以状态条被 UI 抬高一点也照样能找到。

    返回 [(x1, y1, x2, y2), ...]（像素，含边框，从左到右），找不到返回 None。
    """
    h, w = frame.shape[:2]
    v = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)[..., 2]
    best = None
    for y in range(h - 1, int(h * 0.80) - 1, -1):
        rs = [r for r in _runs_of(v[y] >= BAR_EDGE_V, 30) if r[1] - r[0] + 1 <= w * 0.4]
        for i in range(len(rs) - 2):
            trio = rs[i:i + 3]
            ws = [t[1] - t[0] + 1 for t in trio]
            gaps = [trio[j + 1][0] - trio[j][1] - 1 for j in range(2)]
            if not all(0 <= g <= max(6, int(0.12 * max(ws))) for g in gaps):
                continue
            spread = max(ws) - min(ws)
            if spread > 0.3 * max(ws) or sum(ws) < w * 0.12:
                continue
            cols = [(a + b) // 2 for a, b in trio]
            y1 = y2 = y
            while y1 - 1 >= 0 and all(v[y1 - 1, c] >= BAR_EDGE_V for c in cols):
                y1 -= 1
            while y2 + 1 < h and all(v[y2 + 1, c] >= BAR_EDGE_V for c in cols):
                y2 += 1
            hh = y2 - y1 + 1
            if not (5 <= hh <= h * 0.045) or min(ws) / hh < 5:
                continue
            score = (abs(y2 - (h - 1)), spread / max(ws))   # 先求贴底，再求等宽
            if best is None or score < best[0]:
                best = (score, trio, y1, y2)
    if not best:
        return None
    _, trio, y1, y2 = best
    return [(a, y1, b, y2) for a, b in trio]


def autolocate_bars(frame):
    """自动定位三条槽，按 HP / MP / EXP 从左到右写入配置。返回 (ok, 说明, 配置)。"""
    slots = find_bar_slots(frame)
    if not slots:
        return False, "没在画面底部找到三条并排的槽，请手动标定", None
    h, w = frame.shape[:2]
    cfg = {}
    hues = {}
    for kind, (x1, y1, x2, y2) in zip(BAR_KINDS, slots):
        rect = {"x1": round(x1 / w, 4), "y1": round(y1 / h, 4),
                "x2": round(x2 / w, 4), "y2": round(y2 / h, 4)}
        hue = sample_bar_hue(frame, rect)
        hues[kind] = hue
        if hue is None:                       # 空条采样不到，退回该条默认色相
            hue = DEFAULT_BARS[kind]["hue"]
        cfg[kind] = {**rect, "hue": hue}
    # 三条全采样不到颜色 -> 多半是把别的东西（白底对话框之类）当成了槽，别覆盖原配置
    if all(v is None for v in hues.values()):
        return False, "找到的三段没有填充色，可能是误判，请手动标定", None
    ok, why = bars_valid(frame, cfg)           # 几何对了但指错地方，也别写进去
    if not ok:
        return False, f"定位结果没通过自检：{why}", None
    with bar_lock:
        BAR_CFG.clear()
        BAR_CFG.update(cfg)
    mark_bars(source="auto", res=(w, h))
    save_bars()
    return True, "已自动定位", cfg


def sample_bar_hue(frame, rect):
    """从条内左侧 1/4 采样填充色相。

    三条都是从左往右填充，左侧几乎总有颜色，所以标定那一刻顺手把颜色学下来，
    之后就不用猜"这个条是红是蓝"。采样不到（空条/框歪了）返回 None，测量时
    自动退化为纯饱和度+亮度判定。
    """
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = _roi_px(rect, w, h)
    x1, x2 = slot_bounds(frame, x1, y1, x2, y2)
    x2s = max(x1 + 1, x1 + max(2, (x2 - x1) // 4))
    sub = frame[y1:y2, x1:x2s]
    if sub.size == 0:
        return None
    hsv = cv2.cvtColor(sub, cv2.COLOR_BGR2HSV)
    m = (hsv[..., 1] > BAR_S_MIN) & (hsv[..., 2] > BAR_V_MIN)
    if int(m.sum()) < 8:
        return None
    return _clean_hue(np.median(hsv[..., 0][m]))


def measure_bar(frame, rect, hue=None):
    """返回填充比例 0~1；ROI 无效返回 None。

    先把框自动补齐成整条槽（slot_bounds），再切成一列列：每列看中间 60% 高度的
    像素，够艳够亮（且色相对得上填充色）就算已填充。然后从左边数连续填充列，
    允许中间断 2 列（条上的高光/渐变会把个别列盖掉），断更多就认为到了空槽。
    分母是槽的内部宽度（去掉左右各 1px 边框），不是彩色填充的宽度。
    """
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = _roi_px(rect, w, h)
    if x2 - x1 < 8 or y2 - y1 < 2:
        return None
    sx1, sx2 = slot_bounds(frame, x1, y1, x2, y2)
    x1, x2 = sx1 + 1, sx2                # 去掉左右 1px 边框，numpy 切片末端正好排掉右框
    if x2 - x1 < 4:
        return None
    hsv = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
    Hh = hsv[..., 0].astype(np.int16)
    S = hsv[..., 1].astype(np.int16)
    V = hsv[..., 2].astype(np.int16)

    hh = Hh.shape[0]
    a = int(hh * 0.2)
    b = max(a + 1, int(hh * 0.8))
    Hh, S, V = Hh[a:b], S[a:b], V[a:b]

    m = (S > BAR_S_MIN) & (V > BAR_V_MIN)
    if hue is not None:
        d = np.abs(Hh - int(round(hue)))
        m &= np.minimum(d, 180 - d) <= BAR_HUE_TOL
    col = m.mean(axis=0) > 0.5

    n = len(col)
    if n == 0:
        return None
    filled, gap, started = 0, 0, False
    for v in col:
        if v:
            if started:
                filled += gap          # 断口两侧都是填充 -> 断口本身也算填充
            started = True
            filled += 1
            gap = 0
        elif started:
            gap += 1
            if gap > 2:
                break
    return min(1.0, filled / n)


def bars_valid(frame, cfg=None):
    """自检：手上这套坐标现在还是不是"三条并排的槽"。返回 (ok, 原因)。

    纯颜色/几何判据，不认字也不认皮肤：
      1. 槽宽 —— 真槽被 slot_bounds 撑出来最多比框宽一点；指到一片亮底板时会撑到几百像素。
      2. 横向跳变 —— 真槽横向只有"填充 → 空槽"一处颜色跳变；指错地方时整条都在花。
      3. 三条等宽 —— 三条槽是同一套 UI 画的，宽度必然接近。
    自检只用来判断"该不该重新自动定位"，不会改任何配置。
    """
    cfg = bar_snapshot() if cfg is None else cfg
    h, w = frame.shape[:2]
    widths = []
    for kind in BAR_KINDS:
        rect = cfg.get(kind)
        if not rect:
            return False, f"{BAR_LABEL[kind]} 还没有坐标"
        x1, y1, x2, y2 = _roi_px(rect, w, h)
        if x2 - x1 < 8 or y2 - y1 < 2:
            return False, f"{BAR_LABEL[kind]} 的框太小"
        sx1, sx2 = slot_bounds(frame, x1, y1, x2, y2)
        ws = sx2 - sx1
        if ws > max(int((x2 - x1) * 2.2), (x2 - x1) + 40):
            return False, f"{BAR_LABEL[kind]} 附近不是一条槽"
        sub = frame[y1:y2, sx1 + 1:sx2]
        if sub.shape[1] < 8 or sub.shape[0] < 2:
            return False, f"{BAR_LABEL[kind]} 裁出来的区域太小"
        med = np.median(sub.astype(np.int16), axis=0)          # 每列中位色
        d = np.abs(np.diff(med, axis=0)).sum(axis=1)
        d = np.convolve(d, np.ones(3, np.float32) / 3, mode="same")
        jumps = int((d > BAR_JUMP_MIN).sum())
        if jumps > max(4, int(ws * 0.15)):
            return False, f"{BAR_LABEL[kind]} 那一带颜色太碎"
        widths.append(ws)
    if widths and max(widths) - min(widths) > 0.3 * max(widths):
        return False, "三条槽宽度差太多"
    return True, ""


def bars_overlay_frame(frame, thresh=0.5):
    """把状态条的定位结果画在画面上（只给人看，不参与测量）。

    三种线各说一件事：
      白框  —— 参与判定的 ROI（bars.json 里存的那个框，实测会再自动外扩成整条槽）
      洋红框 —— slot_bounds 找出来的"整条槽"，也就是百分比的分母
      黄竖线 —— 当前算出来的填充终点（位置 = 读数）
    """
    out = frame.copy()
    h, w = out.shape[:2]
    cfg = bar_snapshot()
    for kind in BAR_KINDS:
        rect = cfg.get(kind)
        if not rect:
            continue
        x1, y1, x2, y2 = _roi_px(rect, w, h)
        try:
            val = measure_bar(frame, rect, rect.get("hue"))
        except Exception:  # noqa: BLE001
            val = None
        sx1, sx2 = slot_bounds(frame, x1, y1, x2, y2)
        top, bot = max(0, y1 - 2), min(h - 1, y2 + 2)
        # 槽（分母）：洋红，画在条上下的空白处，不压条本身
        cv2.rectangle(out, (sx1, top), (sx2 - 1, bot), (255, 0, 255), 1)
        # 填充终点：黄竖线
        if val is not None:
            fx = int(round(sx1 + 1 + val * max(1, sx2 - sx1 - 2)))
            cv2.line(out, (fx, max(0, y1 - 5)), (fx, min(h - 1, y2 + 5)), (0, 255, 255), 1)
        # ROI：白描边（画在框外一圈，因为条本身可能是白的/红的）
        cv2.rectangle(out, (max(0, x1 - 1), max(0, y1 - 1)),
                      (min(w - 1, x2 + 1), min(h - 1, y2 + 1)), (255, 255, 255), 1)
    return out


def bars_debug_frame(frame):
    """给前端/自检用：把三个 ROI 和逐列判定结果画出来。"""
    out = frame.copy()
    cfg = bar_snapshot()
    h, w = out.shape[:2]
    for kind in BAR_KINDS:
        rect = cfg.get(kind)
        if not rect:
            continue
        x1, y1, x2, y2 = _roi_px(rect, w, h)
        cv2.rectangle(out, (x1, y1), (x2, y2), (255, 0, 255), 1)
        cv2.putText(out, kind.upper(), (x1, max(10, y1 - 3)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 0, 255), 1, cv2.LINE_AA)
    return out


bar_state = {
    "enabled": True,
    "values": {k: None for k in BAR_KINDS},
    "updated": 0.0,
    "error": "",
    "note": "",          # 自检/自动定位的结果说明，给前端显示
    "fps": 0.0,
}
_bar_thread_started = [False]

BAR_CHECK_EVERY = 20        # 每 20 帧（约 2s）做一次自检
BAR_RETRY_SEC = 5.0         # 自检不过时，最多每 5s 重定位一次，别把 CPU 吃满


def maybe_autolocate(frame):
    """坐标来源是 auto 时，自己判断要不要重新定位。

    触发条件：客户区分辨率变了，或者周期自检判定现在这套坐标已经不像三条槽
    （换客户端/换皮肤/窗口被识别成别的尺寸都会撞上）。手动标定过就不动，只提示。
    """
    h, w = frame.shape[:2]
    source = BAR_META.get("source", "auto")
    res = BAR_META.get("res")
    moved = bool(res) and (abs(res[0] - w) > 2 or abs(res[1] - h) > 2)
    ok, why = bars_valid(frame, bar_snapshot())
    if ok and not moved:
        bar_state["note"] = "自动定位有效" if source == "auto" else "手动标定有效"
        return False
    if source == "manual":
        bar_state["note"] = "手动标定的坐标对不上这个画面，可点「自动定位」" if not ok else "手动标定有效"
        return False
    if time.time() < bar_state.get("_next_try", 0.0):
        return False
    bar_state["_next_try"] = time.time() + BAR_RETRY_SEC
    ok2, msg, _ = autolocate_bars(frame)
    bar_state["note"] = msg if ok2 else f"自动定位失败：{msg}"
    return ok2


def bars_loop():
    prev = {}
    n, t0 = 0, time.perf_counter()
    tick = 0
    while True:
        if not bar_state["enabled"] or not grabber.window_found:
            time.sleep(0.25)
            continue
        frame = grabber.snapshot()
        tick += 1
        if tick == 1 or tick % BAR_CHECK_EVERY == 1:
            try:
                if maybe_autolocate(frame):
                    prev.clear()
                    continue           # 换了一套坐标，这一帧作废，下一帧用新坐标算
            except Exception as e:  # noqa: BLE001
                bar_state["error"] = f"autolocate: {type(e).__name__}: {e}"
        cfg = bar_snapshot()
        vals = {}
        for kind in BAR_KINDS:
            rect = cfg.get(kind)
            try:
                p = measure_bar(frame, rect, rect.get("hue")) if rect else None
            except Exception as e:  # noqa: BLE001
                bar_state["error"] = f"{kind}: {type(e).__name__}: {e}"
                p = None
            if p is not None:
                last = prev.get(kind)
                # 掉血要立刻反映（alpha 大），回血慢慢跟上（alpha 小）
                alpha = 0.9 if last is None or p < last else 0.35
                p = p if last is None else last + (p - last) * alpha
                prev[kind] = p
                vals[kind] = round(p * 100, 1)
            else:
                vals[kind] = None
        with bar_lock:
            bar_state["values"] = vals
            bar_state["updated"] = time.time()
        n += 1
        dt = time.perf_counter() - t0
        if dt >= 2.0:
            bar_state["fps"], n, t0 = n / dt, 0, time.perf_counter()
        time.sleep(0.1)


def ensure_bars_thread():
    if _bar_thread_started[0]:
        return
    _bar_thread_started[0] = True
    threading.Thread(target=bars_loop, daemon=True, name="bars").start()


ensure_bars_thread()


# ---------------- 画面运动（判断挂机还在不在打怪） ----------------
"""远端只能看到一张张低清快照时，"画面在不在动"比帧率更有用：
人眼很难从两张相差 2 秒的静图看出角色是不是卡住了，但差分分数一眼就知道。
代价是 1 秒一次缩略图差分，占不到 1% CPU。"""
motion_state = {
    "score": 0.0, "moving": False, "idle_sec": 0.0,
    "last_move": 0.0, "hist": collections.deque(maxlen=60), "ready": False,
}
_move_lock = threading.Lock()


def motion_loop():
    prev = None
    warm = 2          # 头两帧不算：启动瞬间是"占位图→真实画面"，会被误判成剧烈运动
    while True:
        try:
            frame = grabber.snapshot()
            if frame is None or getattr(frame, "size", 0) == 0:
                time.sleep(1.0)
                continue
            small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90),
                               interpolation=cv2.INTER_AREA)
            score = 0.0
            if prev is not None and prev.shape == small.shape and warm <= 0:
                score = float(np.abs(small.astype(np.int16) - prev.astype(np.int16)).mean()) / 255.0
            warm -= 1
            prev = small
            now = time.time()
            with _move_lock:
                motion_state["score"] = round(score, 5)
                motion_state["ready"] = True
                # 0.4% 的像素级差异就算"在动"：挂机时人物/伤害数字的变化远大于这个
                if score >= 0.004:
                    motion_state["moving"] = True
                    motion_state["last_move"] = now
                    motion_state["idle_sec"] = 0.0
                else:
                    motion_state["moving"] = False
                    motion_state["idle_sec"] = round(now - motion_state["last_move"], 1) \
                        if motion_state["last_move"] else 0.0
                motion_state["hist"].append(round(score, 4))
        except Exception as e:      # 抓屏失败不该拖死整个服务
            motion_state["ready"] = False
            motion_state["error"] = str(e)[:120]
        time.sleep(1.0)


threading.Thread(target=motion_loop, daemon=True, name="motion").start()




# ---------------- 检测（OpenVINO） ----------------
det_state = {
    "enabled": False,
    "loading": False,
    "ready": False,
    "annotated": None,
    "infer_ms": 0.0,
    "det_fps": 0.0,
    "counts": {},
    "filter": "",        # 类别过滤：空串=全部；非空=只画名字包含该词的目标
    "lock": None,        # 锁定目标 {"tid","name","conf"} 或 None
    "mask": True,        # UI 排除区开关：涂掉固定 UI 区域再推理
    "mode": "fast",      # fast(20+FPS) / accurate(精度优先)
    "error": "",
}
det_lock = threading.Lock()
det_thread_started = [False]     # 检测线程只启动一次（内部按 mode 热切换模型）
# IoUTracker 等定义在下方，实例化延后到类定义之后

BOX_COLOR = (96, 220, 64)      # BGR 亮绿
TXT_BG = (40, 40, 40)
LOCK_COLOR = (36, 96, 255)     # BGR 橙红: 锁定目标

PALETTE = [(96, 220, 64), (64, 160, 255), (255, 160, 64), (255, 96, 224),
           (64, 224, 224), (160, 96, 255), (64, 255, 160), (224, 224, 64)]


def iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


def overlap_min(a, b):
    """交集 / 较小框面积：判断"框套框"的重复检测比 IoU 更有效。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    iw = max(0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    amin = min((ax2 - ax1) * (ay2 - ay1), (bx2 - bx1) * (by2 - by1))
    return inter / amin if amin > 0 else 0.0


def class_color(name):
    """颜色由类名决定：同一种怪永远同一个颜色，不会随 ID 变化而闪色。"""
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xFFFFFF
    return PALETTE[h % len(PALETTE)]


def dedupe_dets(dets, thr=0.6):
    """同一类名的重叠框只保留置信度最高者（模型的类 ID 别名会重复检出同一只怪）。"""
    out = []
    for d in sorted(dets, key=lambda d: -d[2]):
        if all(d[1] != o[1] or overlap_min(d[0], o[0]) < thr for o in out):
            out.append(d)
    return out


class IoUTracker:
    """轻量 tracking-by-detection：
    - 同类目标按 IoU 贪心匹配，给每只怪稳定 track_id
    - min_hits: 连续命中 N 帧才确认为真实目标（只闪 1~2 帧的瞬现目标直接丢弃）
    - max_age: 丢失 N 帧内保留轨迹（检测偶尔漏一帧不闪断）
    - 类别投票: 轨迹类别由历史帧投票决定，确认后需明显领先才能改判（防止树→蜗牛→树）
    - EMA 平滑框坐标（防逐帧抖动）
    """

    def __init__(self, iou_thr=0.3, min_hits=3, max_age=8, ema=0.6,
                 shift_gate=0.15, shift_max=320.0, class_margin=6,
                 resurrect_frames=25):
        self.iou_thr = iou_thr
        self.min_hits = min_hits
        self.max_age = max_age
        self.ema = ema
        self.shift_gate = shift_gate   # 相位相关响应低于此值视为无有效平移
        self.shift_max = shift_max     # 平移量超过此值视为切屏，不补偿
        self.class_margin = class_margin  # 已确认轨迹改判类别所需的领先票数
        self.resurrect_frames = resurrect_frames  # 轨迹消失后多久内可复用原 ID
        self.tracks = {}
        self.lost = {}                 # tid -> {"name","box","age"} 复活缓存
        self.next_id = 1
        self.pending = 0               # 尚未确认的轨迹数（被瞬现过滤掉的候选）

    def update(self, dets, shift=(0.0, 0.0)):
        """dets: [(box, name, conf)], shift: 画面内容位移(相机补偿)

        统一按“类名”而不是类 ID 匹配：模型里存在多个类 ID 指向同一种怪
        （如 1210100_Pig / 9300058_Pig），按类 ID 匹配会把同一只怪拆成多条轨迹。
        """
        # 相机补偿：整幅画面平移时，先把旧轨迹的框跟着平移再匹配
        dx, dy = shift
        if abs(dx) + abs(dy) > 0.01:
            for tr in self.tracks.values():
                b = tr["box"]
                tr["box"] = [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]
        tids = list(self.tracks.keys())
        used_t, used_d = set(), set()

        # 第一级: 同类 IoU 贪心匹配（静止/慢速目标）
        pairs = []
        for t in tids:
            tr = self.tracks[t]
            for di, (box, name, conf) in enumerate(dets):
                if name != tr["name"]:
                    continue
                v = iou(tr["box"], box)
                if v >= self.iou_thr:
                    pairs.append((v, t, di))
        pairs.sort(reverse=True)
        for v, t, di in pairs:
            if t in used_t or di in used_d:
                continue
            used_t.add(t)
            used_d.add(di)
            self._apply(t, dets[di])

        # 第二级: 中心点距离兜底（快速移动目标）
        pairs2 = []
        for t in tids:
            if t in used_t:
                continue
            tr = self.tracks[t]
            cx = (tr["box"][0] + tr["box"][2]) / 2
            cy = (tr["box"][1] + tr["box"][3]) / 2
            bw, bh = tr["box"][2] - tr["box"][0], tr["box"][3] - tr["box"][1]
            for di, (box, name, conf) in enumerate(dets):
                if di in used_d:
                    continue
                radius = max(80.0, 1.2 * (bw + bh))
                dcx = (box[0] + box[2]) / 2 - cx
                dcy = (box[1] + box[3]) / 2 - cy
                dist = (dcx * dcx + dcy * dcy) ** 0.5
                if dist <= radius:
                    pairs2.append((-dist, t, di))   # 负距离 = 近者优先
        pairs2.sort()
        for nd, t, di in pairs2:
            if t in used_t or di in used_d:
                continue
            used_t.add(t)
            used_d.add(di)
            self._apply(t, dets[di])

        # 先进先出创建新轨迹；优先复活刚消失的同类轨迹（保持 ID 与颜色不变）
        for di, (box, name, conf) in enumerate(dets):
            if di in used_d:
                continue
            tid = self._take_lost(box, name)
            if tid is None:
                tid = self.next_id
                self.next_id += 1
            self.tracks[tid] = {"box": list(box), "name": name,
                                "conf": conf, "hits": 1, "streak": 1, "misses": 0,
                                "confirmed": False, "votes": {name: 1}}
            used_t.add(tid)   # 新建轨迹本帧视为已匹配，否则会被误判为漏检

        dead = []
        for t, tr in self.tracks.items():
            if t not in used_t:
                tr["misses"] += 1
                tr["streak"] = 0          # 连续命中被打断；已确认状态不撤销
                if tr["misses"] > self.max_age:
                    dead.append(t)
        for t in dead:
            tr = self.tracks.pop(t)
            self.lost[t] = {"name": tr["name"], "box": tr["box"], "age": 0}
        for t in list(self.lost):
            self.lost[t]["age"] += 1
            if self.lost[t]["age"] > self.resurrect_frames:
                del self.lost[t]

        self.pending = sum(1 for tr in self.tracks.values() if not tr["confirmed"])
        return [dict(tr, tid=t) for t, tr in self.tracks.items() if tr["confirmed"]]

    def _take_lost(self, box, name):
        """在复活缓存里找同类别、位置接近的已消失轨迹，复用它的 ID。"""
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        bw, bh = box[2] - box[0], box[3] - box[1]
        best, best_d = None, None
        for tid, tr in self.lost.items():
            if tr["name"] != name:
                continue
            lx = (tr["box"][0] + tr["box"][2]) / 2
            ly = (tr["box"][1] + tr["box"][3]) / 2
            d = ((cx - lx) ** 2 + (cy - ly) ** 2) ** 0.5
            if d <= max(120.0, 1.0 * (bw + bh)) and (best_d is None or d < best_d):
                best, best_d = tid, d
        if best is not None:
            del self.lost[best]
        return best

    def _apply(self, t, det):
        box, name, conf = det
        tr = self.tracks[t]
        b, e = tr["box"], self.ema
        tr["box"] = [e * box[k] + (1 - e) * b[k] for k in range(4)]
        tr["conf"] = e * conf + (1 - e) * tr["conf"]
        tr["hits"] += 1
        tr["streak"] += 1
        tr["misses"] = 0
        self._vote(tr, name)
        if tr["streak"] >= self.min_hits:
            tr["confirmed"] = True

    def _vote(self, tr, name):
        """类别投票：票数最高的类作为轨迹类别；已确认的轨迹改判需明显领先。"""
        votes = tr["votes"]
        votes[name] = votes.get(name, 0) + 1
        if sum(votes.values()) > 60:      # 滑窗式衰减，避免老票永久主导
            for k in votes:
                votes[k] = max(1, votes[k] // 2)
        best = max(votes, key=votes.get)
        cur = tr["name"]
        if best == cur:
            return
        if not tr["confirmed"] or votes[best] >= votes.get(cur, 0) + self.class_margin:
            tr["name"] = best


def draw_tracks(frame, tracks, locked_tid=None):
    img = frame.copy()
    for tr in tracks:
        x1, y1, x2, y2 = map(int, tr["box"])
        locked = locked_tid is not None and tr["tid"] == locked_tid
        # 颜色按怪种固定：同一只怪的框永远不会因为 ID 变化而换色
        color = LOCK_COLOR if locked else class_color(tr["name"])
        th = 3 if locked else 2
        cv2.rectangle(img, (x1, y1), (x2, y2), color, th)
        tag = ("LOCK " if locked else "") + f"#{tr['tid']} {tr['name']} {tr['conf']:.2f}"
        (tw, th2), _ = cv2.getTextSize(tag, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        ty = y1 - 5 if y1 - 5 > th2 else y2 + th2 + 5
        cv2.rectangle(img, (x1, ty - th2 - 4), (x1 + tw + 4, ty + 2), TXT_BG, -1)
        cv2.putText(img, tag, (x1 + 2, ty - 1), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, color, 1, cv2.LINE_AA)
    return img


def est_camera_shift(frame, prev_small, sw, sh):
    """相位相关估计整幅画面的平移（视角滚动补偿）。返回全分辨率坐标系的 (dx, dy)。"""
    if prev_small is None:
        return (0.0, 0.0)
    h, w = frame.shape[:2]
    if abs(w - sw * 4) > sw or abs(h - sh * 4) > sh:   # 客户区尺寸突变，不补偿
        return (0.0, 0.0)
    cur = cv2.cvtColor(cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_AREA),
                       cv2.COLOR_BGR2GRAY).astype(np.float32)
    win = cv2.createHanningWindow((sw, sh), cv2.CV_32F)
    (dx, dy), resp = cv2.phaseCorrelate(prev_small, cur, win)
    if resp < 0.12:          # 响应弱 = 画面内容剧变（战斗特效/切换场景），不补偿
        return (0.0, 0.0)
    dx, dy = dx * (w / sw), dy * (h / sh)   # 缩放回全分辨率坐标
    if abs(dx) > 400 or abs(dy) > 400:   # 位移过大视为切屏
        return (0.0, 0.0)
    return (dx, dy)


def pick_lock(tracks, prev_tid):
    """锁定策略：优先延续上一帧的锁定目标（防横跳）；
    新锁定要求轨迹成熟（hits>=5）且置信度达标（>=0.45），按面积×置信度选最优。"""
    if not tracks:
        return None
    by_id = {t["tid"]: t for t in tracks}
    if prev_tid in by_id:
        return prev_tid
    best, best_score = None, -1.0
    for t in tracks:
        if t["hits"] < 5 or t["conf"] < 0.45:
            continue
        x1, y1, x2, y2 = t["box"]
        score = (x2 - x1) * (y2 - y1) * t["conf"]
        if score > best_score:
            best, best_score = t["tid"], score
    if best is None:   # 没有成熟目标时退而选置信度最高的，保证始终有锁
        t = max(tracks, key=lambda t: t["conf"])
        best = t["tid"]
    return best


tracker = IoUTracker()


def detect_loop():
    import warnings
    warnings.filterwarnings("ignore")
    from ultralytics import YOLO

    models = {}          # mode -> YOLO（切换回来不用重新加载）

    def get_model(mode):
        if mode not in models:
            models[mode] = YOLO(MODES[mode]["dir"], task="detect")
        return models[mode]

    try:
        get_model(det_state["mode"])
    except Exception as e:  # noqa: BLE001
        det_state["error"] = f"模型加载失败: {type(e).__name__}: {e}"
        det_state["loading"] = False
        return
    det_state["ready"] = True
    det_state["loading"] = False
    print(f"detector ready ({MODES[det_state['mode']]['dir']})")

    n, t0 = 0, time.perf_counter()
    prev_small = None   # 相机补偿用：上一帧缩略灰度图
    SMALL_W, SMALL_H = 342, 192

    while True:
        if not det_state["enabled"]:
            time.sleep(0.2)
            continue
        if not grabber.window_found:
            time.sleep(0.3)
            continue

        mode = det_state["mode"]
        cfg = MODES[mode]
        try:
            model = get_model(mode)      # 首次切到该模式时才加载
            det_state["loading"] = False
        except Exception as e:  # noqa: BLE001
            det_state["error"] = f"模型加载失败: {type(e).__name__}: {e}"
            time.sleep(1.0)
            continue

        frame = grabber.snapshot()
        tick = time.perf_counter()
        rects = mask_rects() if det_state["mask"] else None
        try:
            # 推理用涂掉 UI 的副本；画框画在原始帧上，画面里 UI 照常可见
            infer_src = apply_ui_mask(frame.copy(), rects) if rects else frame
            res = model.predict(infer_src, imgsz=cfg["imgsz"],
                                conf=cfg["conf"], verbose=False, device="cpu")[0]
        except Exception as e:  # noqa: BLE001
            det_state["error"] = f"推理失败: {type(e).__name__}: {e}"
            time.sleep(0.5)
            continue

        h, w = frame.shape[:2]
        flt = det_state["filter"]
        flt_l = flt.lower()
        names = res.names
        raw = []
        for xyxy, conf, cls in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist(),
                                   res.boxes.cls.tolist()):
            name = names[int(cls)].split("_", 1)[-1]
            if flt_l and flt_l not in name.lower():
                continue
            box = xyxy
            if rects and in_ui_mask(box, w, h, rects):          # 双保险
                continue
            raw.append((box, name, float(conf)))
        # 模型的类 ID 别名会让同一只怪被检出多个框，先按类名去重
        dets = dedupe_dets(raw)

        tracks = tracker.update(dets, shift=est_camera_shift(frame, prev_small,
                                                             SMALL_W, SMALL_H))
        prev_small = cv2.cvtColor(cv2.resize(frame, (SMALL_W, SMALL_H),
                                             interpolation=cv2.INTER_AREA),
                                  cv2.COLOR_BGR2GRAY).astype(np.float32)
        with det_lock:
            prev = det_state["lock"]["tid"] if det_state["lock"] else None
        locked_tid = pick_lock(tracks, prev)

        counts = {}
        for tr in tracks:
            counts[tr["name"]] = counts.get(tr["name"], 0) + 1
        lock_info = None
        for tr in tracks:
            if tr["tid"] == locked_tid:
                lock_info = {"tid": tr["tid"], "name": tr["name"],
                             "conf": round(tr["conf"], 2)}

        annotated = draw_tracks(frame, tracks, locked_tid)

        with det_lock:
            det_state["annotated"] = annotated
            det_state["infer_ms"] = (time.perf_counter() - tick) * 1000
            det_state["counts"] = counts
            det_state["lock"] = lock_info
            det_state["error"] = ""
        n += 1
        dt = time.perf_counter() - t0
        if dt >= 2.0:
            det_state["det_fps"], n, t0 = n / dt, 0, time.perf_counter()


app = FastAPI(title="Capture Preview")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
INDEX = Path(__file__).with_name("index.html")

# 外网访问时的口令门（可选）：设了环境变量 PREVIEW_TOKEN 才生效，本地用不受影响。
# 理由：这个页面等于"看你的屏幕 + 能把游戏窗口提到前台"，把 URL 直接公开风险太大。
PREVIEW_TOKEN = (os.environ.get("PREVIEW_TOKEN") or "").strip()


def _is_loopback(host):
    return host in ("127.0.0.1", "::1", "localhost")


@app.middleware("http")
async def _token_gate(request, call_next):
    # 本机自己看不折腾：从 127.0.0.1 打开直接放行；局域网/外网一律要口令。
    if not PREVIEW_TOKEN or _is_loopback(getattr(request.client, "host", "")):
        return await call_next(request)
    if request.query_params.get("k") == PREVIEW_TOKEN:
        resp = await call_next(request)
        resp.set_cookie("pv", PREVIEW_TOKEN, max_age=7 * 86400, httponly=True, samesite="lax")
        return resp
    if request.cookies.get("pv") == PREVIEW_TOKEN:
        return await call_next(request)
    return Response("需要访问口令：在地址后面加 ?k=你的口令 再打开一次", status_code=401,
                    media_type="text/plain; charset=utf-8")


def encode(frame, width=None, quality=JPEG_QUALITY):
    h, w = frame.shape[:2]
    if width and w > width:
        frame = cv2.resize(frame, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(
        INDEX.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


# 手机监控页：只取 640px 缩略图 + 几十字节状态，2 秒一次约 50 kbps。
# 免费隧道只剩 200 kbps 时主页面（H.264）会很勉强，这一页却完全够用。
MOBILE = Path(__file__).with_name("m.html")


@app.get("/m", response_class=HTMLResponse)
async def mobile():
    try:
        html = MOBILE.read_text(encoding="utf-8")
    except OSError:
        return HTMLResponse("<h1>m.html 不见了</h1>", status_code=500)
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/motion")
async def api_motion():
    with _move_lock:
        return JSONResponse({
            "score": motion_state["score"],
            "moving": motion_state["moving"],
            "idle_sec": motion_state["idle_sec"],
            "hist": list(motion_state["hist"]),
            "ready": motion_state["ready"],
        })



@app.get("/api/ver")
async def html_ver():
    """index.html 的版本号（mtime+size）。前端轮询它，改完页面自动刷新，
    免得对着浏览器的旧缓存以为改动没生效。"""
    try:
        st = INDEX.stat()
        return JSONResponse({"ver": f"{int(st.st_mtime)}-{st.st_size}"})
    except OSError as e:
        return JSONResponse({"ver": "", "error": str(e)})


@app.get("/api/stats")
async def stats():
    age = (time.time() - grabber.last_frame_ts) * 1000 if grabber.last_frame_ts else -1
    with det_lock:
        counts = dict(det_state["counts"])
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
    return JSONResponse({
        "window_found": grabber.window_found,
        "window_title": WINDOW_TITLE,
        "client_size": list(grabber.client_size),
        "capture_fps": round(grabber.capture_fps, 1),
        "stream_fps": round(grabber.stream_fps, 1),
        "frame_age_ms": round(age, 0),
        "error": grabber.error,
        "detect_enabled": det_state["enabled"],
        "detect_loading": det_state["loading"],
        "detect_ready": det_state["ready"],
        "infer_ms": round(det_state["infer_ms"], 0),
        "det_fps": round(det_state["det_fps"], 1),
        "targets": sum(counts.values()),
        "pending": tracker.pending,
        "top_classes": [f"{k}×{v}" for k, v in top],
        "detect_error": det_state["error"],
        "filter": det_state["filter"],
        "lock": det_state["lock"],
        "mode": det_state["mode"],
        "mode_label": MODES[det_state["mode"]]["label"],
        "mask": det_state["mask"],
        "bars": dict(bar_state["values"]),
        "bars_fps": round(bar_state["fps"], 1),
    })


# ---------- 状态条（HP / MP / EXP） ----------
@app.get("/api/bars")
async def bars_get():
    with bar_lock:
        vals = dict(bar_state["values"])
        source = BAR_META.get("source", "auto")
    return JSONResponse({
        "bars": bar_snapshot(),
        "values": vals,
        "enabled": bar_state["enabled"],
        "fps": round(bar_state["fps"], 1),
        "updated": round(bar_state["updated"], 3),
        "labels": BAR_LABEL,
        "source": source,
        "note": bar_state["note"],
        "error": bar_state["error"],
    })


@app.post("/api/bars/set")
async def bars_set(payload: dict = Body(...)):
    """保存某一条的框选范围，并从当前画面自动学一个填充色相。

    顺手把框自动补齐成整条槽（用户直觉上会沿着彩色填充拖，满血时那样框会切掉右侧空槽）。
    """
    kind = str(payload.get("kind", "")).lower()
    if kind not in BAR_KINDS:
        return JSONResponse({"ok": False, "msg": f"kind 必须是 {BAR_KINDS}"}, status_code=400)
    rect = _clean_rect(payload)
    if rect is None:
        return JSONResponse({"ok": False, "msg": "框太小或坐标非法"}, status_code=400)

    frame = grabber.snapshot()
    h, w = frame.shape[:2]
    px1, py1, px2, py2 = _roi_px(rect, w, h)
    sx1, sx2 = slot_bounds(frame, px1, py1, px2, py2)
    if (sx1, sx2) != (px1, px2):
        rect = {**rect, "x1": round(sx1 / w, 4), "x2": round(sx2 / w, 4)}

    hue = sample_bar_hue(frame, rect)
    if hue is None:                       # 采样不到就沿用原来的色相，别把已有配置搞丢
        hue = bar_snapshot().get(kind, {}).get("hue")
    with bar_lock:
        BAR_CFG[kind] = {**rect, "hue": hue}
    mark_bars(source="manual", res=(w, h))   # 手框过的坐标不再被自动定位覆盖
    save_bars()
    return JSONResponse({"ok": True, "kind": kind, "rect": rect, "hue": hue})


@app.post("/api/bars/reset")
async def bars_reset():
    with bar_lock:
        BAR_CFG.clear()
        BAR_CFG.update({k: dict(v) for k, v in DEFAULT_BARS.items()})
    mark_bars(source="auto", res=None)
    save_bars()
    return JSONResponse({"ok": True, "bars": bar_snapshot()})


@app.post("/api/bars/autolocate")
async def bars_autolocate(focus: bool = True):
    """在当前画面上自动找三条槽并写入配置（换客户端/换分辨率用）。

    默认先前置游戏窗口——被别的窗口挡住时抓到的是遮挡窗口，底部根本没有那三条槽。
    """
    if focus:
        stamp = grabber.stamp()
        ok, _ = await asyncio.to_thread(bring_to_front)
        if ok:
            await asyncio.to_thread(grabber.wait_fresh, stamp, 1.5)
        await asyncio.sleep(0.15)          # 等遮罩/过渡动画过去
    ok, msg, cfg = await asyncio.to_thread(autolocate_bars, grabber.snapshot())
    if not ok:
        return JSONResponse({"ok": False, "msg": msg})
    return JSONResponse({"ok": True, "msg": msg, "bars": cfg})


@app.get("/api/bars/preview")
async def bars_preview():
    """把三个 ROI 画在当前画面上，用来核对框得准不准。"""
    jpg = encode(bars_debug_frame(grabber.snapshot()), width=None, quality=90)
    return Response(content=jpg, media_type="image/jpeg")


@app.get("/api/detect/filter")
async def detect_filter(name: str = ""):
    """设置类别过滤：name 为空显示全部，否则只画名字包含 name 的目标（不区分大小写）。"""
    det_state["filter"] = name.strip()
    return JSONResponse({"filter": det_state["filter"]})


@app.get("/api/detect/mask")
async def detect_mask(on: int = 1):
    """UI 排除区开关：on=1 涂掉固定 UI 再推理（默认），on=0 全画面检测。"""
    det_state["mask"] = bool(on)
    return JSONResponse({"mask": det_state["mask"]})


# ---------- 排除区配置（WebUI 框选 / 选择 / 删除 / 持久化） ----------
@app.get("/api/masks")
async def masks_get():
    return JSONResponse({"masks": mask_snapshot(), "on": det_state["mask"]})


@app.post("/api/masks/add")
async def masks_add(payload: dict = Body(...)):
    m = _clean_mask(payload)
    if m is None:
        return JSONResponse({"error": "区域太小或坐标非法", "masks": mask_snapshot()},
                            status_code=400)
    with mask_lock:
        m["id"] = _next_mask_id()
        UI_MASKS.append(m)
        snap = [dict(x) for x in UI_MASKS]
    try:
        save_masks(snap)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"写入 {MASK_FILE.name} 失败: {e}", "masks": snap},
                            status_code=500)
    return JSONResponse({"masks": snap, "id": m["id"]})


@app.post("/api/masks/update")
async def masks_update(payload: dict = Body(...)):
    """移动 / 缩放已有区域。"""
    mid = str(payload.get("id") or "")
    m = _clean_mask(payload, mid)
    if m is None:
        return JSONResponse({"error": "区域太小或坐标非法", "masks": mask_snapshot()},
                            status_code=400)
    with mask_lock:
        for i, cur in enumerate(UI_MASKS):
            if cur["id"] == mid:
                UI_MASKS[i] = m
                break
        snap = [dict(x) for x in UI_MASKS]
    try:
        save_masks(snap)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"写入 {MASK_FILE.name} 失败: {e}", "masks": snap},
                            status_code=500)
    return JSONResponse({"masks": snap, "id": mid})


@app.post("/api/masks/delete")
async def masks_delete(payload: dict = Body(...)):
    mid = str(payload.get("id") or "")
    with mask_lock:
        keep = [m for m in UI_MASKS if m["id"] != mid]
        removed = len(keep) != len(UI_MASKS)
        UI_MASKS[:] = keep
        snap = [dict(x) for x in UI_MASKS]
    if removed:
        try:
            save_masks(snap)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"写入 {MASK_FILE.name} 失败: {e}", "masks": snap},
                                status_code=500)
    return JSONResponse({"masks": snap, "removed": removed})


@app.post("/api/masks/clear")
async def masks_clear():
    with mask_lock:
        UI_MASKS.clear()
    try:
        save_masks([])
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"写入 {MASK_FILE.name} 失败: {e}", "masks": []},
                            status_code=500)
    return JSONResponse({"masks": []})


@app.post("/api/masks/reset")
async def masks_reset():
    """恢复内置的一整套默认区域（小地图/灯泡列/底栏/快捷栏/任务提示）。"""
    with mask_lock:
        UI_MASKS[:] = [dict(m) for m in DEFAULT_MASKS]
        snap = [dict(x) for x in UI_MASKS]
    try:
        save_masks(snap)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"写入 {MASK_FILE.name} 失败: {e}", "masks": snap},
                            status_code=500)
    return JSONResponse({"masks": snap})



@app.get("/api/detect/mode")
async def detect_mode(m: str = "fast"):
    """切换部署配置：fast=384x672/20+FPS，accurate=544x960/精度优先。"""
    if m in MODES:
        det_state["mode"] = m
        det_state["loading"] = True      # 首次切到该模式需要加载模型
    return JSONResponse({"mode": det_state["mode"], **MODES[det_state["mode"]]})


@app.get("/api/detect/toggle")
async def detect_toggle():
    det_state["enabled"] = not det_state["enabled"]
    if det_state["enabled"] and not det_thread_started[0]:
        det_thread_started[0] = True
        det_state["loading"] = not det_state["ready"]
        threading.Thread(target=detect_loop, daemon=True).start()
    if not det_state["enabled"]:
        with det_lock:
            det_state["annotated"] = None
            det_state["counts"] = {}
            det_state["lock"] = None
        tracker.tracks.clear()
    return JSONResponse({"enabled": det_state["enabled"], "loading": det_state["loading"]})


@app.get("/api/window/focus")
async def window_focus():
    """把游戏窗口抬到前台（前端按钮 / 脚本截图前调用）。"""
    ok, msg = await asyncio.to_thread(bring_to_front)
    return JSONResponse({"ok": ok, "msg": msg, "window_found": grabber.window_found})


@app.get("/api/snapshot")
async def snapshot(focus: int = 0, fresh: int = 1, bars: int = 0,
                   w: int = 0, q: int = 0):
    """抓一张当前画面。

    focus=1 先前置游戏窗口，然后等到一帧"前置之后"的新画面再返回——否则拿到的
    是上一秒的旧帧（挡住时的画面），这是最容易踩的坑。
    """
    focus_msg = ""
    focus_ok = False
    if focus:
        stamp = grabber.stamp()
        focus_ok, focus_msg = await asyncio.to_thread(bring_to_front)
        if fresh and focus_ok:
            await asyncio.to_thread(grabber.wait_fresh, stamp, 1.5)
    frame = grabber.snapshot()
    if bars:
        frame = await asyncio.to_thread(bars_overlay_frame, frame)
    jpg = encode(frame, width=(w or None), quality=(q or 92))
    # 响应头只能是 latin-1，所以这里放英文状态；中文说明走 /api/window/focus 的 JSON
    state = "skip" if not focus else ("ok" if focus_ok else "fail")
    return Response(content=jpg, media_type="image/jpeg",
                    headers={"Content-Disposition": "inline; filename=snapshot.jpg",
                             "X-Focus": state})


@app.get("/stream")
async def stream(bars: int = 0):
    async def gen():
        count, t0 = 0, time.perf_counter()
        while True:
            with det_lock:
                ann = det_state["annotated"] if det_state["enabled"] else None
            src = ann if ann is not None else grabber.snapshot()
            if bars:
                src = bars_overlay_frame(src)
            jpg = encode(src)
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            grabber.note_sent()
            count += 1
            dt = time.perf_counter() - t0
            if dt >= 1.0:
                grabber.stream_fps, count, t0 = count / dt, 0, time.perf_counter()
            await asyncio.sleep(1.0 / DEFAULT_FPS)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


def port_busy(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def lan_ipv4():
    """本机在局域网里的地址（手机连同一个 WiFi 时该用这个，而不是穿透域名）。"""
    ips = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    try:                                  # 兜底：拿默认出口网卡的地址
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("223.5.5.5", 80))
        ip = probe.getsockname()[0]
        probe.close()
        if not ip.startswith("127.") and ip not in ips:
            ips.insert(0, ip)
    except OSError:
        pass
    return ips


if __name__ == "__main__":
    local = f"http://127.0.0.1:{PORT}"
    want_open = "--open" in sys.argv

    if port_busy("127.0.0.1", PORT):
        print(f"already running -> {local}")
        if want_open:
            webbrowser.open(local)
        raise SystemExit(0)

    print(f"preview -> {local}   (Ctrl+C to stop)")
    if HOST in ("0.0.0.0", "::"):
        for ip in lan_ipv4():
            tail = f"/?k={PREVIEW_TOKEN}" if PREVIEW_TOKEN else "/"
            print(f"手机(同 WiFi) -> http://{ip}:{PORT}{tail}   ← 在家就用这个，最流畅")
    if want_open:
        threading.Timer(1.2, lambda: webbrowser.open(local)).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
