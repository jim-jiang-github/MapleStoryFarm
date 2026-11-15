# -*- coding: utf-8 -*-
"""
LieDetector YOLO 数据采集脚本
- 启动 N 个游戏实例 (LIE_PORT=17751..)，平铺窗口
- 每局: replay -> 循环 [pause off -> 短暂运行 -> pause on 冻结 -> 截图 + state]
  标签与像素在同一冻结帧上, 逐像素对齐
- 输出 YOLO 格式: images/ labels/ + manifest.jsonl
类别: 0=white_shape (count/fade, 主图形可见)  1=vortex (hidden 阶段主图形位置)
      2=bg_shape (背景平铺图形)
"""
import argparse, ctypes, json, os, socket, subprocess, sys, time
from ctypes import wintypes

import numpy as np
from PIL import Image

# ---------------- Win32 ----------------
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

user32.SetProcessDPIAware()
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)

PW_RENDERFULLCONTENT = 0x00000002


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", ctypes.c_uint32 * 3)]


def find_hwnd_by_pid(pid):
    """找属于 pid 的可见顶层窗口"""
    result = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            r = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            if buf.value == "LieDetector" and (r.right - r.left) > 100:
                result.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return result[0] if result else None


def window_client_rect(hwnd):
    """返回 (window rect, client 相对窗口原点的偏移, client w/h)"""
    wr = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    pt = POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    cx, cy = pt.x - wr.left, pt.y - wr.top
    cr = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(cr))
    cw, ch = cr.right - cr.left, cr.bottom - cr.top
    return wr, (cx, cy), (cw, ch)


def print_window(hwnd):
    """PrintWindow 抓整窗 -> numpy BGRA, 裁出客户区, 返回 RGB numpy (失败返回 None)"""
    wr, (cx, cy), (cw, ch) = window_client_rect(hwnd)
    w, h = wr.right - wr.left, wr.bottom - wr.top
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h
    bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
    bmi.bmiHeader.biCompression = 0
    buf = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(buf), None, 0)
    if not hbmp or not buf.value:
        gdi32.DeleteObject(hbmp); gdi32.DeleteDC(mem); user32.ReleaseDC(hwnd, hdc)
        return None
    old = gdi32.SelectObject(mem, hbmp)
    ok = user32.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT)
    gdi32.SelectObject(mem, old)
    img = None
    if ok:
        size = w * h * 4
        raw = (ctypes.c_ubyte * size).from_address(buf.value)
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
        crop = arr[cy:cy + ch, cx:cx + cw, :3]        # BGRA -> 取 BGR
        img = crop[:, :, ::-1].copy()                 # -> RGB
    gdi32.DeleteObject(hbmp); gdi32.DeleteDC(mem); user32.ReleaseDC(hwnd, hdc)
    return img


# ---------------- TCP CLI ----------------
class Cli:
    def __init__(self, port, timeout=10):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""

    def send(self, line):
        self.sock.sendall((line + "\n").encode("utf-8"))

    def read_line(self, timeout=10):
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            d = self.sock.recv(65536)
            if not d:
                raise ConnectionError("closed")
            self.buf += d
        i = self.buf.index(b"\n")
        line, self.buf = self.buf[:i], self.buf[i + 1:]
        return line.decode("utf-8")

    def cmd(self, c, timeout=10):
        self.send(c)
        return self.read_line(timeout)

    def state(self, timeout=15):
        """发送 state, 跳过 ack 行, 返回帧 json"""
        self.send("state")
        deadline = time.time() + timeout
        while time.time() < deadline:
            obj = json.loads(self.read_line(max(1, deadline - time.time())))
            if obj.get("schema") == "lie.telemetry/1":
                return obj
        raise TimeoutError("no state frame")

    def close(self):
        try: self.sock.close()
        except Exception: pass


# ---------------- 标签 ----------------
CANVAS_W, CANVAS_H = 750.0, 500.0


def frame_to_yolo(frame, cw, ch):
    """帧 json -> YOLO 行列表 + 调试信息。cw/ch = 截图客户区像素"""
    sx, sy = cw / CANVAS_W, ch / CANVAS_H
    phase = frame["game"]["phase"]
    ms = frame.get("mainShape", {})
    labels = []

    def add_box(cls, bx, by, bw, bh):
        x0, y0 = bx, by
        x1, y1 = bx + bw, by + bh
        x0c, y0c = max(0.0, x0), max(0.0, y0)
        x1c, y1c = min(CANVAS_W, x1), min(CANVAS_H, y1)
        if x1c - x0c < 8 or y1c - y0c < 8:
            return
        cxn = ((x0c + x1c) / 2) / CANVAS_W
        cyn = ((y0c + y1c) / 2) / CANVAS_H
        wn = (x1c - x0c) / CANVAS_W
        hn = (y1c - y0c) / CANVAS_H
        labels.append(f"{cls} {cxn:.6f} {cyn:.6f} {wn:.6f} {hn:.6f}")

    if ms and ms.get("visible") and phase in ("idle", "count", "fade"):
        b = ms["bbox"]
        add_box(0, b["x"], b["y"], b["w"], b["h"])
    elif phase == "hidden":
        b = ms["bbox"]
        add_box(1, b["x"], b["y"], b["w"], b["h"])

    for bs in frame.get("backgroundShapes", []):
        b = bs["bbox"]
        add_box(2, b["x"], b["y"], b["w"], b["h"])

    # 背景图形很多且慢, 隔帧采样一部分以控制实例量 (主图形不裁剪)
    if phase == "hidden" and frame["time"]["t"] % 2 >= 1:
        pass
    return labels, phase


# ---------------- 采集 ----------------
def collect_instance(idx, port, exe, out_dir, rounds, dt, log):
    img_dir = os.path.join(out_dir, "images")
    lbl_dir = os.path.join(out_dir, "labels")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(lbl_dir, exist_ok=True)
    manifest = open(os.path.join(out_dir, f"manifest_inst{idx}.jsonl"), "a", encoding="utf-8")

    # 启动进程
    env = dict(os.environ, LIE_PORT=str(port))
    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe), env=env)
    hwnd = None
    for _ in range(120):
        time.sleep(1)
        hwnd = find_hwnd_by_pid(proc.pid)
        if hwnd:
            break
    if not hwnd:
        raise RuntimeError(f"inst{idx}: 未找到游戏窗口")
    time.sleep(3)

    # 平铺位置: 每列 960px, 两行
    col, row = idx % 2, (idx // 2) % 2
    user32.SetWindowPos(hwnd, 0, col * 960 + (20 if col else 0),
                        row * 520 + (60 if row else 0), 0, 0, 0x0001 | 0x0002)  # SWP_NOSIZE|SWP_NOZORDER
    time.sleep(1)

    cli = Cli(port)
    hello = json.loads(cli.cmd("hello"))
    log(f"inst{idx}: port={port} hwnd={hwnd} pid={proc.pid} hello ok")

    _, (cx_off, cy_off), (cw, ch) = window_client_rect(hwnd)
    log(f"inst{idx}: client={cw}x{ch} (offset {cx_off},{cy_off})")
    cli.send("mouse -100 -100")   # 屏蔽真实鼠标
    time.sleep(0.3)
    cli.cmd("pause off")
    time.sleep(0.3)

    n_frames = 0
    try:
        for rr in range(rounds):
            cli.cmd("replay")
            cli.send("mouse -100 -100")
            seq = 0
            t0 = time.time()
            while True:
                cli.send("pause off")
                time.sleep(dt)
                cli.send("pause on")
                frame = cli.state()
                phase = frame["game"]["phase"]
                if phase == "result":
                    break
                if phase == "done":
                    break
                labels, ph = frame_to_yolo(frame, cw, ch)
                img = print_window(hwnd)
                if img is None:
                    log(f"inst{idx}: PrintWindow 失败, 跳过")
                    continue
                name = f"i{idx}_r{rr:03d}_{seq:04d}"
                Image.fromarray(img).save(os.path.join(img_dir, name + ".jpg"),
                                          quality=88, subsampling=1)
                with open(os.path.join(lbl_dir, name + ".txt"), "w") as f:
                    f.write("\n".join(labels) + ("\n" if labels else ""))
                manifest.write(json.dumps({
                    "image": name + ".jpg", "instance": idx, "round_local": rr,
                    "round_game": frame["game"]["round"], "phase": ph,
                    "t": frame["time"]["t"], "fps": frame["time"]["fps"],
                    "n_labels": len(labels),
                    "main_center": frame["mainShape"]["center"],
                }, ensure_ascii=False) + "\n")
                n_frames += 1
                seq += 1
            log(f"inst{idx}: 轮 {rr+1}/{rounds} 完成, {seq} 帧, 耗时 {time.time()-t0:.1f}s")
    finally:
        manifest.close()
        cli.close()
        try: proc.terminate()
        except Exception: pass
    return n_frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", type=int, default=2)
    ap.add_argument("--port0", type=int, default=17751)
    ap.add_argument("--rounds", type=int, default=225, help="每个实例采集的轮数")
    ap.add_argument("--dt", type=float, default=0.07, help="每帧运行时长(秒)")
    ap.add_argument("--out", default="dataset_raw")
    ap.add_argument("--exe", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                  "Builds", "LieDetector", "LieDetector.exe"))
    ap.add_argument("--calib", action="store_true", help="校准模式: 只抓 3 帧出叠加图")
    args = ap.parse_args()

    exe = os.path.abspath(args.exe)
    if not os.path.exists(exe):
        print("找不到 exe:", exe); sys.exit(1)
    os.makedirs(args.out, exist_ok=True)

    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    if args.calib:
        # 校准: 单实例, 抓 3 帧 (count/fade/hidden 各阶段), 画标签框存叠加图
        import subprocess as sp
        env = dict(os.environ, LIE_PORT=str(args.port0))
        proc = sp.Popen([exe], cwd=os.path.dirname(exe), env=env)
        hwnd = None
        for _ in range(120):
            time.sleep(1)
            hwnd = find_hwnd_by_pid(proc.pid)
            if hwnd: break
        if not hwnd: print("未找到窗口"); sys.exit(1)
        time.sleep(3)
        user32.SetWindowPos(hwnd, 0, 60, 60, 0, 0, 0x0001 | 0x0002)
        cli = Cli(args.port0)
        _, (_, _), (cw, ch) = window_client_rect(hwnd)
        print(f"client={cw}x{ch}")
        cli.send("mouse -100 -100")
        cli.cmd("pause off"); time.sleep(0.3)
        cli.cmd("replay")
        cli.send("mouse -100 -100")
        saved = 0
        phase_seen = set()
        try:
            while saved < 8:
                cli.send("pause off"); time.sleep(args.dt); cli.send("pause on")
                fr = cli.state()
                ph = fr["game"]["phase"]
                if ph == "result": break
                if ph in phase_seen and ph != "hidden":
                    if len(phase_seen) >= 3: continue
                img = print_window(hwnd)
                if img is None:
                    print("PrintWindow 失败"); continue
                labels, ph2 = frame_to_yolo(fr, cw, ch)
                # 画框: 0红 1紫 2青
                colors = {0: (255, 60, 60), 1: (220, 60, 255), 2: (60, 230, 230)}
                from PIL import ImageDraw
                pi = Image.fromarray(img)
                dr = ImageDraw.Draw(pi)
                sx, sy = cw / CANVAS_W, ch / CANVAS_H
                def draw(cls, b):
                    x0, y0 = b["x"] * sx, b["y"] * sy
                    dr.rectangle([x0, y0, x0 + b["w"] * sx, y0 + b["h"] * sy],
                                 outline=colors[cls], width=2)
                ms = fr.get("mainShape", {})
                if ms.get("visible") and ph in ("idle", "count", "fade"):
                    draw(0, ms["bbox"])
                elif ph == "hidden":
                    draw(1, ms["bbox"])
                for bs in fr.get("backgroundShapes", []):
                    draw(2, bs["bbox"])
                fn = os.path.join(args.out, f"calib_{ph}_{saved}.png")
                pi.save(fn)
                print(f"saved {fn} phase={ph} labels={len(labels)} t={fr['time']['t']}")
                phase_seen.add(ph)
                saved += 1
        finally:
            cli.close(); proc.terminate()
        return

    # 正式采集: 多实例并行 (每实例一个线程)
    import threading
    results = {}
    def run(i, port):
        try:
            n = collect_instance(i, port, exe, args.out, args.rounds, args.dt, log)
            results[i] = n
            log(f"inst{i}: 共 {n} 帧")
        except Exception as e:
            log(f"inst{i}: 失败 {e}")
            results[i] = -1

    threads = []
    for i in range(args.instances):
        t = threading.Thread(target=run, args=(i, args.port0 + i))
        t.start(); threads.append(t)
        time.sleep(5)   # 错开启动
    for t in threads:
        t.join()
    log(f"全部完成: {results}")


if __name__ == "__main__":
    main()
