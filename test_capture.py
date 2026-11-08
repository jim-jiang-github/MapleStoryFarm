# -*- coding: utf-8 -*-
"""Screen capture FPS benchmark: mss vs dxcam, targeting the game client area."""
import time
import ctypes
import ctypes.wintypes as wt
import os

import win32gui

OUT_DIR = ".image"
os.makedirs(OUT_DIR, exist_ok=True)

ctypes.windll.user32.SetProcessDPIAware()  # avoid DPI virtualization coords

GAME_TITLE = "冒险岛怀旧服"

hwnd = win32gui.FindWindow(None, GAME_TITLE)
if not hwnd:
    raise SystemExit(f"window not found: {GAME_TITLE}")

win32gui.SetForegroundWindow(hwnd)
time.sleep(0.5)

left, top, right, bottom = win32gui.GetClientRect(hwnd)
origin = win32gui.ClientToScreen(hwnd, (0, 0))
region = {
    "left": origin[0],
    "top": origin[1],
    "width": right - left,
    "height": bottom - top,
}
print(f"hwnd={hwnd} client={region['width']}x{region['height']} at ({region['left']},{region['top']})")


def bench(name, grab, seconds=5.0):
    n = 0
    t0 = time.perf_counter()
    last = None
    while True:
        last = grab()
        n += 1
        dt = time.perf_counter() - t0
        if dt >= seconds:
            break
    fps = n / dt
    print(f"[{name}] {n} frames in {dt:.2f}s -> {fps:.1f} FPS "
          f"({1000/fps:.2f} ms/frame), shape={getattr(last, 'shape', None) or (last.width if last else '?')}")
    return fps, last


# ---------- mss ----------
import mss

with mss.mss() as sct:
    fps_mss, shot = bench("mss    ", lambda: sct.grab(region))
    import mss.tools
    mss.tools.to_png(shot.rgb, shot.size, output=os.path.join(OUT_DIR, "frame_mss.png"))

# ---------- dxcam ----------
try:
    import dxcam

    cam = dxcam.create(output_idx=0, output_color="BGR")
    if cam is None:
        print("[dxcam ] create() returned None (device busy?)")
    else:
        dregion = (region["left"], region["top"],
                   region["left"] + region["width"], region["top"] + region["height"])

        def sync_grab():
            f = cam.grab(region=dregion)
            return f if f is not None else False

        fps_dx, frame = bench("dxcam sync grab", sync_grab)
        cam.start(target_fps=0, region=dregion)
        time.sleep(0.3)

        def latest():
            f = cam.get_latest_frame()
            return f if f is not None else False

        fps_bg, frame = bench("dxcam bg thread", latest, seconds=3.0)
        cam.stop()
        cam.release()
        if frame is not False:
            import cv2
            cv2.imwrite(os.path.join(OUT_DIR, "frame_dxcam.png"), frame)
except Exception as e:
    print(f"[dxcam ] failed: {type(e).__name__}: {e}")

print("done")
