# -*- coding: utf-8 -*-
"""端到端鼠标闭环测试: 追踪器 -> 真实鼠标 (SetCursorPos) -> 游戏自身悬停判定.

无叠加窗口: 直接看 Unity 游戏窗口, 真实光标跟着追踪器跑 (判定也用真实鼠标).
游戏规则: hidden 阶段光标在图形内累计悬停 4.6s -> success; 10.5s 超时 -> fail.
快捷键: Q (全局) 退出.
产物: out/rounds.jsonl (逐局 verdict + 追踪统计) + 控制台汇总.

用法: python tools/mouse_eval.py --rounds 200 --out kaggle_out/mouse_eval_200
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
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
W, H = 750, 500
NEAR = 25.0
VK_QUIT = 0x7B        # F12 退出 (Q 会在用户打字时误触)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def get_cursor_pos():
    pt = POINT(0, 0)
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


class SmoothMouse:
    """150Hz 后台线程 + 临界阻尼弹簧插值: 光标连续平滑地追踪 10Hz 的目标, 像人手.

    主线程只喂 set_target(画布坐标); 线程内部积分并 SetCursorPos (屏幕坐标).
    """

    def __init__(self, ox, oy, hz=150.0, tau=0.07):
        self.ox, self.oy = ox, oy
        self.dt = 1.0 / hz
        c = 2.0 / tau                       # 阻尼系数 (tau = 收敛时间常数, 秒)
        self.k = (c / 2.0) ** 2             # 临界阻尼刚度
        self.c = c
        sx, sy = get_cursor_pos()
        self.x, self.y = float(sx - ox), float(sy - oy)   # 从当前光标位置平滑出发
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
                sx, sy = int(round(self.x)), int(round(self.y))
            user32.SetCursorPos(self.ox + sx, self.oy + sy)
            time.sleep(self.dt)

    def stop(self):
        self.running = False


def client_origin(hwnd):
    pt = POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def key_pressed(vk):
    return (user32.GetAsyncKeyState(vk) & 0x8000) != 0


class GameSession:
    """游戏进程 + TCP 连接, 支持断线重连 / 进程重启."""

    def __init__(self, port):
        self.port = port
        self.proc = None
        self.hwnd = None
        self.cli = None
        self.launch()

    def launch(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                time.sleep(1)
            except Exception:
                pass
        env = dict(os.environ, LIE_PORT=str(self.port))
        self.proc = subprocess.Popen([EXE], cwd=os.path.dirname(EXE), env=env)
        self.hwnd = None
        for _ in range(60):
            time.sleep(1)
            self.hwnd = find_hwnd_by_pid(self.proc.pid)
            if self.hwnd:
                break
        if not self.hwnd:
            raise RuntimeError("游戏窗口未出现")
        time.sleep(3)
        user32.SetWindowPos(self.hwnd, 0, 40, 40, 0, 0, 0x0001 | 0x0002)
        time.sleep(1)
        self.cli = StreamClient(self.port)
        self.cli.cmd("hello")
        self.cli.cmd("pause off")
        self.cli.send("stream 60")
        time.sleep(0.5)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def ensure(self):
        """连接/进程异常则重启, 返回 client (可能重建)."""
        if not self.alive() or self.cli.dead:
            print("[重启] 游戏进程/连接异常, 重新拉起")
            self.launch()
        return self.cli

    def close(self):
        try:
            if self.cli:
                self.cli.send("stream off")
                time.sleep(0.2)
        except Exception:
            pass
        try:
            if self.proc:
                self.proc.terminate()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=os.path.join(ROOT, "kaggle_out", "ft3v6", "best_ft3.pt"))
    ap.add_argument("--port", type=int, default=17787)
    ap.add_argument("--rounds", type=int, default=200)
    ap.add_argument("--out", default=os.path.join(ROOT, "kaggle_out", "mouse_eval_200"))
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    log_path = os.path.join(args.out, "rounds.jsonl")
    logf = open(log_path, "w", encoding="utf-8")

    from ultralytics import YOLO
    model = YOLO(args.weights)
    print(f"[模型] {args.weights}")

    gs = GameSession(args.port)
    ox, oy = client_origin(gs.hwnd)
    mover = SmoothMouse(ox, oy)
    print("[鼠标] 平滑插值线程已启动 (150Hz 弹簧), 按全局 F12 键退出")

    results = []
    t0 = time.time()
    nf = 0
    n_hit = n_tot = 0
    fails_detail = []
    try:
        for rr in range(1, args.rounds + 1):
            if rr > 1:
                try:
                    gs.ensure().cmd("replay")
                except OSError:
                    gs.launch()
                    gs.cli.cmd("replay")
                ox, oy = client_origin(gs.hwnd)
                mover.ox, mover.oy = ox, oy
            time.sleep(0.3)
            trk = YoloTracker(fps=10.0)
            verdict = None
            t_v = None
            hid_n = hid_hit = 0
            hid_errs = []
            t_round = time.time()
            while time.time() - t_round < 60:
                if key_pressed(VK_QUIT):
                    raise KeyboardInterrupt
                img = print_window(gs.hwnd)
                if img is None:
                    time.sleep(0.05)
                    continue
                if img.shape[0] != H or img.shape[1] != W:
                    img = cv2.resize(img, (W, H))
                try:
                    tel = gs.cli.snap()
                except Exception:
                    tel = None
                ph = (tel or {}).get("game", {}).get("phase", "?")

                r = model.predict(img, imgsz=640, conf=0.10, verbose=False)[0]
                dets = []
                for b in r.boxes:
                    cls = int(b.cls[0])
                    x1, y1, x2, y2 = b.xyxy[0].tolist()
                    dets.append(((x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1,
                                 float(b.conf[0]), cls))
                st = trk.update(dets)
                nf += 1

                # ---- 真实鼠标 = 追踪器位置 (弹簧插值, 平滑人手) ----
                if trk.pos is not None:
                    mover.set_target(trk.pos[0], trk.pos[1])

                # ---- 统计 ----
                if tel:
                    c = (tel.get("mainShape") or {}).get("center") or {}
                    if c.get("x") is not None and ph == "hidden" and trk.pos is not None:
                        err = float(np.hypot(trk.pos[0] - c["x"], trk.pos[1] - c["y"]))
                        hid_n += 1
                        hid_hit += (err <= NEAR)
                        hid_errs.append(err)
                        n_tot += 1
                        n_hit += (err <= NEAR)
                    v = tel.get("game", {}).get("verdict")
                    if v is not None and verdict is None:
                        verdict = v
                        t_v = time.time()
                    # verdict 已出且进入 done -> 本局结束 (立即重启下一局, 不傻等超时);
                    # 兜底: verdict 出来 8 秒后还没到 done 也结束
                    if verdict is not None and (ph == "done" or time.time() - t_v > 8):
                        break
                time.sleep(0.045)

            wins = sum(1 for x in results if x["verdict"] == "success")
            rate = hid_hit / max(1, hid_n) * 100
            med = float(np.median(hid_errs)) if hid_errs else -1
            rec = dict(round=rr, verdict=verdict or "fail", hidden_n=hid_n,
                       hidden_hit=hid_hit, hidden_rate=round(rate, 1),
                       err_med=round(med, 1), t=round(time.time() - t_round, 1))
            results.append(rec)
            logf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            logf.flush()
            print(f"[局 {rr:3d}/{args.rounds}] {rec['verdict']:8s} "
                  f"追踪 {rate:5.1f}% (n={hid_n}) med {med:4.1f}px  "
                  f"总胜 {wins}/{len(results)}  {time.time()-t0:.0f}s", flush=True)
            if verdict != "success":
                fails_detail.append(rec)
    except KeyboardInterrupt:
        print("\n[Q 键退出]")
    finally:
        mover.stop()
        gs.close()
        logf.close()

    wins = sum(1 for x in results if x["verdict"] == "success")
    print(f"\n== 鼠标闭环测试完成: {len(results)} 局, 游戏判定成功 {wins} "
          f"({wins / max(1, len(results)) * 100:.1f}%) ==")
    if n_tot:
        print(f"   hidden 追踪命中率(<=25px): {n_hit}/{n_tot} = {n_hit / n_tot * 100:.1f}%")
    if fails_detail:
        print("   失败局:")
        for f in fails_detail:
            print(f"     局{f['round']}: 追踪 {f['hidden_rate']}% n={f['hidden_n']} med={f['err_med']}px")


if __name__ == "__main__":
    main()
