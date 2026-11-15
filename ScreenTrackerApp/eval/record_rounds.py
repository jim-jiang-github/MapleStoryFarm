# -*- coding: utf-8 -*-
"""录制 Unity 局: PrintWindow 抓帧 + telemetry 对齐, 供离线开发旋转跟踪器.

输出: out/video.avi (750x500) + out/telemetry.jsonl (每抓帧一行, 含最新遥测)
用法: python tools/record_rounds.py --rounds 6 --out kaggle_out/rot_dev
"""
import argparse
import collections
import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time

import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect_dataset import find_hwnd_by_pid, print_window, user32

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXE = os.path.join(ROOT, "Builds", "LieDetector", "LieDetector.exe")


class StreamClient:
    """单连接: reader 线程持续收 telemetry, 主线程发命令等 ack."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.sock.settimeout(None)
        self.buf = b""
        self.latest = None
        self.acks = collections.deque()
        self.lock = threading.Lock()
        self.dead = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                d = self.sock.recv(65536)
            except OSError:
                break
            if not d:
                break
            self.buf += d
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if "mainShape" in obj:
                    with self.lock:
                        self.latest = obj
                else:
                    self.acks.append(obj)
        self.dead = True

    def send(self, line):
        self.sock.sendall((line + "\n").encode("utf-8"))

    def cmd(self, line, timeout=8):
        n0 = len(self.acks)
        self.send(line)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if len(self.acks) > n0:
                return self.acks[-1]
            time.sleep(0.01)
        return None

    def snap(self):
        with self.lock:
            return dict(self.latest) if self.latest else None


def record(out_dir, rounds, port, fps):
    os.makedirs(out_dir, exist_ok=True)
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
    cli.send("stream 60")          # 60hz 遥测, 抓帧时取最新
    time.sleep(0.5)

    W, H = 750, 500
    vw = cv2.VideoWriter(os.path.join(out_dir, "video.avi"),
                         cv2.VideoWriter_fourcc(*"MJPG"), fps, (W, H))
    log = open(os.path.join(out_dir, "telemetry.jsonl"), "w", encoding="utf-8")

    idx = 0
    t_start = time.time()
    try:
        for rr in range(rounds):
            cli.cmd("replay")
            cli.send("mouse -100 -100")
            # 等到本局结束 (result/done), 再多录 2.9s 结算
            seen_result = False
            t_round = time.time()
            while time.time() - t_round < 30:
                img = print_window(hwnd)
                tel = cli.snap()
                if img is not None:
                    if img.shape[0] != H or img.shape[1] != W:
                        img = cv2.resize(img, (W, H))
                    vw.write(img[:, :, ::-1])       # RGB->BGR
                    log.write(json.dumps({
                        "idx": idx, "round": rr,
                        "tel": tel,
                        "ts": time.time() - t_start,
                    }, ensure_ascii=False) + "\n")
                    log.flush()
                    idx += 1
                if tel:
                    ph = tel.get("game", {}).get("phase")
                    if ph in ("result", "done"):
                        seen_result = True
                    elif seen_result and ph not in ("result", "done"):
                        break                        # 进了下一局/idle, 本局录完
                time.sleep(1.0 / fps)
            print(f"round {rr+1}/{rounds}: 累计 {idx} 帧", flush=True)
    finally:
        log.close()
        vw.release()
        cli.send("stream off")
        try:
            proc.terminate()
        except Exception:
            pass
    print(f"完成: {idx} 帧 -> {out_dir}", flush=True)
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--out", default="kaggle_out/rot_dev")
    ap.add_argument("--port", type=int, default=17762)
    ap.add_argument("--fps", type=int, default=30)
    args = ap.parse_args()
    record(args.out, args.rounds, args.port, args.fps)


if __name__ == "__main__":
    main()
