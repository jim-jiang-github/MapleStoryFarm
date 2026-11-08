# -*- coding: utf-8 -*-
"""Capture calibration frames from the game and quantize the OpenVINO model to INT8."""
import ctypes
import os
import time
import warnings

warnings.filterwarnings("ignore")
ctypes.windll.user32.SetProcessDPIAware()

import cv2
import mss
import numpy as np
import openvino as ov
import win32gui

SRC_DIR = "model_544x960_openvino_model"
OUT_DIR = "model_int8_openvino_model"
CALIB_DIR = ".image/calib"
IN_W, IN_H = 960, 544
N_FRAMES = 60


def grab_calibration():
    os.makedirs(CALIB_DIR, exist_ok=True)
    hwnd = win32gui.FindWindow(None, "冒险岛怀旧服")
    if hwnd:
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.8)
    l, t, r, b = win32gui.GetClientRect(hwnd)
    x, y = win32gui.ClientToScreen(hwnd, (0, 0))
    region = {"left": x, "top": y, "width": r - l, "height": b - t}
    paths = []
    with mss.mss() as sct:
        for i in range(N_FRAMES):
            frame = np.asarray(sct.grab(region))[:, :, :3]
            p = os.path.join(CALIB_DIR, f"calib_{i:03d}.jpg")
            cv2.imwrite(p, frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            paths.append(p)
            time.sleep(0.08)
    print(f"calibration frames: {len(paths)}")
    return paths


def to_tensor(path):
    img = cv2.imread(path)
    img = cv2.resize(img, (IN_W, IN_H), interpolation=cv2.INTER_LINEAR)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return np.ascontiguousarray(img.transpose(2, 0, 1)[None]).astype(np.float32) / 255.0


if __name__ == "__main__":
    files = grab_calibration()
    import nncf

    core = ov.Core()
    model = core.read_model(os.path.join(SRC_DIR, "best.xml"))
    dataset = nncf.Dataset(files, to_tensor)
    print("quantizing (subset=50, MIXED)...")
    q_model = nncf.quantize(model, dataset, subset_size=50, preset=nncf.QuantizationPreset.MIXED)
    os.makedirs(OUT_DIR, exist_ok=True)
    ov.save_model(q_model, os.path.join(OUT_DIR, "best.xml"), compress_to_fp16=True)
    import shutil
    shutil.copy(os.path.join(SRC_DIR, "metadata.yaml"), os.path.join(OUT_DIR, "metadata.yaml"))
    print("INT8 model ->", OUT_DIR)
