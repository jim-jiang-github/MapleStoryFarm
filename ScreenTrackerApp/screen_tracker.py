# -*- coding: utf-8 -*-
"""屏幕区域追踪器: 框选桌面上一块区域 (推荐 750x500 比例), 自动识别并鼠标跟随其中目标.

用途: 把要追踪的内容 (如测谎仪游戏窗口/任意含白团/漩涡的画面) 摆到框选区域内,
     鼠标会平滑地跟随识别到的目标 (white_shape / vortex).

操作:
  1. 启动后全屏截图上拖拽画框 (Enter/空格确认, C 取消退出)
  2. 开始追踪: 真实鼠标通过 150Hz 临界阻尼弹簧平滑跟随目标
  3. F12 = 全局退出 (随时, 包括框选阶段)
  4. F2  = 追踪中重新框选区域

坐标约定: 识别在 750x500 标准画布 (模型训练分辨率) 上进行, 鼠标坐标按区域实际
大小缩放回去, 因此框选区域不必精确 750x500 (但比例接近 3:2 识别效果最好).

用法: 双击 Run_ScreenTracker.bat, 或 python screen_tracker.py [--weights ...] [--preview]
"""
import argparse
import ctypes
import os
import sys
import threading
import time

import cv2
import numpy as np

try:
    import mss
except ImportError:
    print("缺少依赖 mss: pip install mss")
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
user32 = ctypes.windll.user32
from track_core import YoloTracker

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 750, 500          # 模型标准画布
VK_QUIT = 0x7B           # F12
VK_RESEL = 0x71          # F2
QUIT = {"flag": False}


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def get_cursor_pos():
    pt = POINT(0, 0)
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def key_pressed(vk):
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0


def watchdog():
    """全局 F12 退出看护: 任何阶段 (含框选阻塞期) 按下即退出进程."""
    while True:
        if key_pressed(VK_QUIT):
            QUIT["flag"] = True
            os._exit(0)
        time.sleep(0.05)


class SmoothMouse:
    """150Hz 后台线程 + 临界阻尼弹簧插值: 光标连续平滑地跟随目标, 像人手."""

    def __init__(self, ox, oy, sx=1.0, sy=1.0, hz=150.0, tau=0.07):
        self.ox, self.oy = ox, oy          # 区域原点 (屏幕物理像素)
        self.sx, self.sy = sx, sy          # 画布 -> 屏幕缩放
        self.dt = 1.0 / hz
        c = 2.0 / tau
        self.k = (c / 2.0) ** 2
        self.c = c
        gx, gy = get_cursor_pos()
        self.x, self.y = (gx - ox) / max(sx, 1e-6), (gy - oy) / max(sy, 1e-6)
        self.vx = self.vy = 0.0
        self.tx, self.ty = self.x, self.y
        self.running = True
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def set_target(self, x, y):
        with self.lock:
            self.tx, self.ty = float(x), float(y)

    def _loop(self):
        while self.running:
            with self.lock:
                ax = self.k * (self.tx - self.x) - self.c * self.vx
                ay = self.k * (self.ty - self.y) - self.c * self.vy
                self.vx += ax * self.dt
                self.vy += ay * self.dt
                self.x += self.vx * self.dt
                self.y += self.vy * self.dt
                sxp = int(round(self.ox + self.x * self.sx))
                syp = int(round(self.oy + self.y * self.sy))
            user32.SetCursorPos(sxp, syp)
            time.sleep(self.dt)

    def stop(self):
        self.running = False


def select_region():
    """全屏截图 + cv2.selectROI 框选. 返回 (x, y, w, h) 物理像素, 取消返回 None."""
    with mss.mss() as sct:
        mon = sct.monitors[0]
        raw = sct.grab(mon)
    img = np.asarray(raw)[:, :, :3].copy()           # BGRA -> BGR
    scr_h, scr_w = img.shape[:2]
    # 说明文字
    tip = img.copy()
    cv2.rectangle(tip, (0, 0), (scr_w, 44), (30, 30, 30), -1)
    cv2.putText(tip, "Drag a box (3:2 ratio best)  |  ENTER/SPACE=confirm  C=cancel  F12=quit",
                (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    cv2.namedWindow("select_region", cv2.WINDOW_NORMAL)
    cv2.setWindowProperty("select_region", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    r = cv2.selectROI("select_region", tip, False, False)
    cv2.destroyAllWindows()
    x, y, w, h = [int(v) for v in r]
    if w < 60 or h < 40:
        return None
    return x, y, w, h


def run_tracker(model, region, preview=False):
    x, y, w, h = region
    sx, sy = w / W, h / H                              # 画布 -> 屏幕 缩放
    mover = SmoothMouse(x, y, sx, sy)
    trk = YoloTracker(fps=15.0)
    mon = {"left": x, "top": y, "width": w, "height": h}
    print(f"[区域] ({x},{y}) {w}x{h}  (识别画布 {W}x{H})")
    print("[追踪] 鼠标已接管, F12=退出  F2=重新框选")
    sct = mss.mss()
    dt_ema = 1 / 15.0
    t_stat = time.time()
    n_frame = n_det = 0
    try:
        while not QUIT["flag"]:
            if key_pressed(VK_RESEL):
                print("[重选] F2 -> 重新框选")
                return "reselect"
            raw = sct.grab(mon)
            frame = np.asarray(raw)[:, :, :3]
            if frame.shape[1] != W or frame.shape[0] != H:
                frame = cv2.resize(frame, (W, H))
            # YOLO 检测
            r = model.predict(frame, imgsz=640, conf=0.10, verbose=False)[0]
            dets = []
            for b in r.boxes:
                cls = int(b.cls[0])
                x1, y1, x2, y2 = b.xyxy[0].tolist()
                dets.append(((x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1,
                             float(b.conf[0]), cls))
            st = trk.update(dets)
            # 追踪器实际帧率自适应 (速度量纲 px/帧 依赖 fps)
            t_now = time.time()
            dt_ema = 0.9 * dt_ema + 0.1 * max(1e-3, t_now - getattr(run_tracker, "_t_prev", t_now - dt_ema))
            run_tracker._t_prev = t_now
            trk.fps = 1.0 / dt_ema
            if trk.pos is not None:
                mover.set_target(trk.pos[0], trk.pos[1])
            n_frame += 1
            n_det += len(dets)
            # 状态打印 (2s 一次)
            if t_now - t_stat >= 2.0:
                if trk.pos is not None:
                    mpx = x + trk.pos[0] * sx
                    mpy = y + trk.pos[1] * sy
                    pos_s = f"pos=({mpx:.0f},{mpy:.0f}) 画布({trk.pos[0]:.0f},{trk.pos[1]:.0f})"
                else:
                    pos_s = "pos=丢失"
                print(f"  [{st:6s}] {pos_s}  {n_det/max(1,n_frame):.1f} det/帧  "
                      f"{n_frame/max(1,t_now-t_stat):.0f} fps")
                t_stat, n_frame, n_det = t_now, 0, 0
            if preview:
                vis = frame.copy()
                for d in dets:
                    color = [(60, 200, 60), (50, 50, 230), (120, 120, 120)][d[5] % 3]
                    cv2.circle(vis, (int(d[0]), int(d[1])), 8, color, 2)
                    cv2.putText(vis, f"{d[4]:.2f}", (int(d[0]) - 10, int(d[1]) - 10),
                                0, 0.5, color, 1)
                if trk.pos is not None:
                    cv2.drawMarker(vis, (int(trk.pos[0]), int(trk.pos[1])),
                                   (0, 255, 255), cv2.MARKER_CROSS, 22, 2)
                cv2.imshow("preview (F12 quit)", vis)
                cv2.waitKey(1)
    finally:
        mover.stop()
    return "quit"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=os.path.join(HERE, "best_ft3.pt"))
    ap.add_argument("--preview", action="store_true", help="显示识别预览小窗 (默认关)")
    args = ap.parse_args()

    # 高 DPI: 截图与 SetCursorPos 统一用物理像素
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()

    if not os.path.exists(args.weights):
        print(f"[错误] 模型不存在: {args.weights}")
        sys.exit(1)
    from ultralytics import YOLO
    model = YOLO(args.weights)
    print(f"[模型] {args.weights}")
    print("[启动] 请在屏幕上框选追踪区域 (推荐 3:2 比例, 如 750x500)")

    threading.Thread(target=watchdog, daemon=True).start()

    while not QUIT["flag"]:
        region = select_region()
        if region is None:
            print("[取消] 未选择有效区域, 退出")
            return
        rc = run_tracker(model, region, preview=args.preview)
        if rc != "reselect":
            break
    print("[退出] screen_tracker 结束")


if __name__ == "__main__":
    main()
