# -*- coding: utf-8 -*-
"""旋转跟踪方案闭环验证: Unity 工具 + 遥测 GT 对坐标.

每局: replay -> 持续抓帧 -> YOLO 检测 -> 匀速追踪器 -> 对比遥测 mainShape.center
目标: >=N_VALID 次隐身期验证, 命中率(<=25px) >= 95%
用法: python tools/rot_eval_unity.py --weights kaggle_out/best.pt --rounds 25
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect_dataset import find_hwnd_by_pid, print_window, user32
from record_rounds import StreamClient
from track_core import YoloTracker

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXE = os.path.join(ROOT, "Builds", "LieDetector", "LieDetector.exe")
NEAR = 25.0


def run_eval(weights, rounds, port, out_dir, min_valid):
    os.makedirs(out_dir, exist_ok=True)
    from ultralytics import YOLO
    model = YOLO(weights)

    env = dict(os.environ, LIE_PORT=str(port))
    proc = subprocess.Popen([EXE], cwd=os.path.dirname(EXE), env=env)
    hwnd = None
    for _ in range(60):
        time.sleep(1)
        hwnd = find_hwnd_by_pid(proc.pid)
        if hwnd:
            break
    if not hwnd:
        raise RuntimeError("未找到游戏窗口")
    time.sleep(3)
    user32.SetWindowPos(hwnd, 0, 60, 60, 0, 0, 0x0001 | 0x0002)
    time.sleep(1)

    cli = StreamClient(port)
    cli.cmd("hello")
    cli.send("mouse -100 -100")
    cli.cmd("pause off")
    cli.send("stream 60")
    time.sleep(0.5)

    W, H = 750, 500
    recs = []
    vw = cv2.VideoWriter(os.path.join(out_dir, "eval_track.avi"),
                         cv2.VideoWriter_fourcc(*"MJPG"), 12, (W, H))
    trk = YoloTracker(fps=10.0)
    t_start = time.time()
    n_frames = 0
    fps_n = 0
    try:
        for rr in range(rounds):
            cli.cmd("replay")
            cli.send("mouse -100 -100")
            trk = YoloTracker(fps=10.0)      # 每局重开(新图形)
            seen_result = False
            t_round = time.time()
            while time.time() - t_round < 30:
                img = print_window(hwnd)
                if img is None:
                    time.sleep(0.05)
                    continue
                if img.shape[0] != H or img.shape[1] != W:
                    img = cv2.resize(img, (W, H))
                tel = cli.snap()
                # ---- YOLO 检测 ----
                r = model.predict(img, imgsz=640, conf=0.10, verbose=False)[0]
                dets = []
                for b in r.boxes:
                    c = int(b.cls[0])
                    if c in (0, 1):
                        x1, y1, x2, y2 = b.xyxy[0].tolist()
                        dets.append(((x1 + x2) / 2, (y1 + y2) / 2,
                                     x2 - x1, y2 - y1, float(b.conf[0]), c))
                st = trk.update(dets)
                n_frames += 1
                fps_n += 1
                # ---- 记录验证 ----
                if tel and trk.pos is not None:
                    ms = tel.get("mainShape") or {}
                    c = ms.get("center") or {}
                    ph = tel.get("game", {}).get("phase", "?")
                    if c.get("x") is not None and ph in ("count", "fade", "hidden"):
                        err = float(np.hypot(trk.pos[0] - c["x"], trk.pos[1] - c["y"]))
                        recs.append(dict(idx=n_frames, round=rr, phase=ph, err=err,
                                         x=trk.pos[0], y=trk.pos[1],
                                         gx=c["x"], gy=c["y"], st=st,
                                         dets=[[round(d[0], 1), round(d[1], 1), round(d[4], 3), d[5]] for d in dets],
                                         det_conf=max([d[4] for d in dets], default=0.0)))
                # ---- 画叠加(轨迹+当前点) ----
                if tel:
                    ph = tel.get("game", {}).get("phase", "?")
                    if ph in ("result", "done"):
                        seen_result = True
                    elif seen_result:
                        break
                f = img[:, :, ::-1].copy()
                for rec in recs[-400:]:
                    if rec["round"] == rr:
                        cv2.circle(f, (int(rec["x"]), int(rec["y"])), 2, (0, 220, 60), -1)
                if trk.pos is not None:
                    px, py = int(trk.pos[0]), int(trk.pos[1])
                    cv2.circle(f, (px, py), 5, (0, 255, 255), -1)
                    cv2.line(f, (px - 9, py), (px + 9, py), (0, 255, 255), 1)
                    cv2.line(f, (px, py - 9), (px, py + 9), (0, 255, 255), 1)
                cv2.putText(f, f"{trk.state} {st} r{rr+1}", (10, 480),
                            0, 0.6, (255, 215, 0), 2)
                vw.write(f)
                time.sleep(0.045)          # ~12fps
    finally:
        vw.release()
        cli.send("stream off")
        try:
            proc.terminate()
        except Exception:
            pass

    # ---- 统计 ----
    with open(os.path.join(out_dir, "eval_records.jsonl"), "w", encoding="utf-8") as fp:
        for rec in recs:
            fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
    dt = time.time() - t_start
    print(f"\n== 闭环验证: {rounds} 局 / {n_frames} 帧 / {dt:.0f}s ({fps_n/dt:.1f} fps) ==")
    for ph in ("count", "fade", "hidden"):
        phr = [r for r in recs if r["phase"] == ph]
        if phr:
            errs = np.array([r["err"] for r in phr])
            print(f"  [{ph:6s}] n={len(phr):5d}  命中(≤{NEAR:.0f}px) {(errs <= NEAR).mean()*100:5.1f}%  "
                  f"中位 {np.median(errs):5.1f}px  p90 {np.percentile(errs, 90):6.1f}px")
    hid = [r for r in recs if r["phase"] == "hidden"]
    if hid:
        errs = np.array([r["err"] for r in hid])
        rate = (errs <= NEAR).mean() * 100
        print(f"\n  隐身期验证次数 {len(hid)} (目标 >= {min_valid}), 命中率 {rate:.1f}% (目标 >= 95%)")
        print("  " + ("✅ 达标" if (len(hid) >= min_valid and rate >= 95) else "❌ 未达标"))
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=os.path.join(ROOT, "kaggle_out", "best.pt"))
    ap.add_argument("--rounds", type=int, default=25)
    ap.add_argument("--port", type=int, default=17785)
    ap.add_argument("--out", default="kaggle_out/rot_eval")
    ap.add_argument("--min-valid", type=int, default=1000)
    args = ap.parse_args()
    run_eval(args.weights, args.rounds, args.port, args.out, args.min_valid)


if __name__ == "__main__":
    main()
