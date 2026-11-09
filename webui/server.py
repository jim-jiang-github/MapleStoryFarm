# -*- coding: utf-8 -*-
"""Game window capture -> MJPEG web preview.

Run:  python webui/server.py
Open: http://127.0.0.1:8765
"""
import asyncio
import base64
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


# 抓屏坐标必须是物理像素，这一步要在抓屏/窗口调用之前做。
# 不做的话，改过系统缩放后 GetClientRect 会返回放大后的值（实测 1366x768 被报成
# 2049x1152），而 mss 按物理像素抓图，抓到的区域比窗口大一圈 → 画面混进别的窗口、
# 人物框和屏蔽区全错位（表现就是「重启一下就好了」）。
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:  # noqa: BLE001  老系统没有这个函数
    ctypes.windll.user32.SetProcessDPIAware()

# 打包成 exe 后控制台默认是 GBK(936)，print 的中文会变成一片乱码
# （「手机(同 WiFi)」变成 `\xca\xd6\xbb\xfa`）。把控制台代码页和 Python 输出
# 一起切到 UTF-8，两边一致才不会花。
if getattr(sys, "frozen", False):
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  没有控制台（重定向）时无所谓
        pass


import cv2
import mss
import numpy as np
import uvicorn
import win32con
import win32gui
from fastapi import Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

# 跟踪模块与本文件同目录；直接 `python webui/server.py` 时它已在 sys.path 里，
# 用 -m 方式启动时补一下，免得为了一个 import 去改运行方式
sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as apppaths  # noqa: E402  （见 webui/paths.py：源码/打包后的资源与数据目录）
from track import Tracker  # noqa: E402  （见 webui/track.py：卡尔曼 + 匈牙利 + ByteTrack）
import bot as botmod  # noqa: E402  （见 webui/bot.py：挂机按键与决策）


SCREENSHOT = getattr(mss, "MSS", None) or getattr(mss, "mss")

WINDOW_TITLE = "冒险岛怀旧服"
PREVIEW_W = 320          # 小方格宽度（px）
STREAM_W = 1024          # 推流分辨率宽度：预览可放大到千px级，1024 仍有清晰余量
DEFAULT_FPS = 30
JPEG_QUALITY = 72
JPEG_STREAM_Q = 85      # 实时流用：比 72 清楚，但按显示尺寸缩放后体积只有原来的 1/3
# 默认监听所有网卡：手机连同一个 WiFi 时直接走局域网地址最顺畅（往返几毫秒，
# 对比免费穿透的 1 秒往返是降维打击）。只想给本机用就 set PREVIEW_HOST=127.0.0.1。
HOST = (os.environ.get("PREVIEW_HOST") or "0.0.0.0").strip() or "0.0.0.0"
PORT = int(os.environ.get("PREVIEW_PORT") or 8765)
DETECT_CONF = 0.35
# 送进跟踪器的低分门限。推理阈值压到这里、而不是 0.35，是为了 ByteTrack 的第二阶段：
# 0.10~0.35 的框不新建轨迹，只在第一轮没配上的轨迹上补配一次，
# 检测器偶尔掉分（遮挡、特效、远处小目标）时不至于直接丢目标。
# 界面上看到的框仍是跟踪器确认过的，噪声框不会冒出来。
LOW_DETECT_CONF = 0.10
# 显示门限：置信度低于这个值的轨迹不画框、不计数、不参与锁定。
# 跟踪器内部仍用低分框续命（上面那段），所以「看不见」不等于「跟丢了」——
# 阈值调高只是把 0.2/0.3 这种拿不准的框藏起来，不影响已经确认的目标继续被跟踪。
# 这个值可以在页面上用滑块实时改（/api/detect/conf），存在同目录的 detconf.json。
SHOW_CONF_DEFAULT = float(os.environ.get("PREVIEW_SHOW_CONF") or 0.80)
SHOW_HITS_DEFAULT = int(os.environ.get("PREVIEW_MIN_HITS") or 6)
SHOW_CONF_FILE = apppaths.data_file("detconf.json")
# ---- 显示稳定（解决「冒出来一两帧又消失」）----
# 置信度高不等于目标真实：误检偶尔也会连着两帧拿到 0.7。原来「连续 2 帧」就确认，
# 于是这种误检会被画两帧再消失。现在显示端另加三道闸：
#   1) show_hits：累计命中这么多帧才允许第一次出现（默认 6 帧 ≈ 0.2~0.3 秒）
#   2) DISPLAY_MAX_MISS：连续 2 帧没观测到就不再画，避免幽灵框停在原地
#      （这时框用的是卡尔曼预测 + 上一次达标的分数，所以标签上的数字仍然 >= 门限）
#   3) 同一只怪被两条轨迹重复框住时只画一条（见 DUP_TRACK_IOU）
# 门限对「显示」是硬约束：只要这一帧量测到 conf < show_conf，立刻撤框，没有任何
# 按秒/按帧的宽限。之前留过 0.45 秒和 2 帧两种宽限，结果是 conf 在门限附近反复
# 横跳的轨迹（0.58/0.60 来回）几乎一直挂着低分框，看上去像门限没生效。
# 跟踪器内部不受影响，仍然 max_age 帧内保留轨迹，随时可以接回。
DISPLAY_MAX_MISS = 2
# 两条轨迹框到同一只怪（检测偶尔分裂 / 类别名不一致）时，IoU 超过它就只留一条
DUP_TRACK_IOU = 0.55
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
MASK_FILE = apppaths.data_file("masks.json")
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
    """等到游戏真的成了前台窗口。

    最小化也占着「前台」这个名分，但那种前台一帧都抓不到——所以必须连
    IsIconic 一起判断，否则会把「还原失败」当成成功报给用户。
    """
    u32 = ctypes.windll.user32
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if u32.GetForegroundWindow() == hwnd and not win32gui.IsIconic(hwnd):
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
    was_min = bool(win32gui.IsIconic(hwnd))
    ok_msg = "已还原并前置" if was_min else "已前置"
    try:
        if was_min:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.2)
        if u32.GetForegroundWindow() == hwnd and not win32gui.IsIconic(hwnd):
            return True, ok_msg

        # 1) 直接切（游戏刚被用过时这一步就够）
        u32.SetForegroundWindow(hwnd)
        if _wait_foreground(hwnd, 0.35):
            return True, ok_msg

        # 2) 模拟一次 ALT：解除前台锁，系统放行紧接着的切换
        u32.keybd_event(win32con.VK_MENU, 0, 0, 0)
        u32.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)
        u32.SetForegroundWindow(hwnd)
        if _wait_foreground(hwnd, 0.45):
            return True, ok_msg

        # 3) 置顶再取消置顶，最后再切一次
        u32.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, SWP)
        u32.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, SWP)
        u32.SetForegroundWindow(hwnd)
        if _wait_foreground(hwnd, 0.45):
            return True, ok_msg
        # 这里要分开报：最小化还还原不了，基本就是游戏跑在管理员权限下、当前
        # 进程够不着它的窗口（UIPI 拦 ShowWindow），手动点任务栏一下就成。
        if win32gui.IsIconic(hwnd):
            return False, "窗口仍是最小化：游戏以管理员身份运行，脚本无权还原，请手动点任务栏"
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
        self.window_minimized = False
        self.client_size = (0, 0)
        self.capture_fps = 0.0
        self.stream_fps = 0.0
        self.last_sent_wall = 0.0
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
                    # 三种情况差别很大，别混成一句「客户区尺寸异常」：
                    # 窗口没了 / 窗口最小化（能一键还原）/ 窗口在但尺寸不可用
                    if not hwnd:
                        self.window_minimized = False
                        self.error = "未找到游戏窗口（标题含「冒险岛」）"
                    elif win32gui.IsIconic(hwnd):
                        self.window_minimized = True
                        self.error = "游戏窗口已最小化，还原后才有画面"
                    else:
                        self.window_minimized = False
                        self.error = "游戏客户区尺寸异常（窗口可能被完全遮住或还在加载）"
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
                    self.window_minimized = False
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
        self.last_sent_wall = time.time()


grabber = Grabber()
grabber.start()

# ---------------- 状态条识别（HP / MP / EXP） ----------------
# 三条是固定 UI：位置固定、颜色固定、只沿水平方向变化。所以不走训练/推理，
# 直接按"色相 + 饱和度"逐列扫描，既准（逐像素）又快（3 个 ~160x15 的 ROI，
# 亚毫秒级），也完全不受模型召回率影响。
BAR_FILE = apppaths.data_file("bars.json")
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
    # 深色皮肤的槽（EXP 常见：空槽是暗的、不"亮"）会让 slot_bounds 把"彩色填充段"
    # 误当成整条槽 —— 分母缩到填充宽度，读数永远 100%。槽不可能比 ROI 还窄，
    # 遇到这种明显缩水的就退回用 ROI 当分母。
    if sx2 - sx1 < (x2 - x1) * 0.6:
        sx1, sx2 = x1, x2
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
    "lock_box": None,    # 上次锁定的框：目标短暂丢失时用它就近接回，而不是跳去别的怪
    "lock_ts": 0.0,      # 最后一次锁上的时间（超过 3 秒就不再用 lock_box 找）
    "mask": True,        # UI 排除区开关：涂掉固定 UI 区域再推理
    "max_fps": 0,        # 推理帧率上限：0=不限（跑满）；5~30 按目标帧率给循环踩刹车
    "next_infer": 0.0,   # 下一帧推理的计划时刻（节流用，按计划推进以吸收 sleep 误差）
    "mode": "fast",      # fast(20+FPS) / accurate(精度优先)
    "show_conf": SHOW_CONF_DEFAULT,   # 显示门限：低于它的目标不画框/不计数
    "show_hits": SHOW_HITS_DEFAULT,   # 稳定帧数：累计命中这么多帧才允许第一次出现
    "shown": {},         # tid -> {"first": 首次出现时间, "last": 最近显示时间}，用于最短显示时长
    "vis": [],           # 这一帧真正画出来的轨迹（给挂机脚本挑目标用）
    "error": "",
    # 冷启动阶段：点开「识别」不等于马上有框——导入推理库、加载/编译模型、跑通
    # 第一帧，这几步合起来要几秒到十几秒，期间面板上什么都看不到，很容易以为
    # 「按钮没生效」。stage 用来给面板报进度，ready 只在**首帧推理成功后**才置位。
    "stage": "idle",     # idle / import / load / warmup / ready / error
    "det_t0": 0.0,       # 这次冷启动的开始时刻（perf_counter）
    "preloaded": False,  # 服务启动时的后台预热是否已完成（点开就秒出）
    # 预热是静默的（失败不影响点开时再试），但「为什么没预热成」要留痕：
    # 打包成 exe 后最容易缺的就是动态库，没有这条只能靠猜。
    "preload_error": "",
}
det_lock = threading.Lock()
# 已加载的模型：mode -> YOLO。放到模块级而不是循环里的局部变量——线程崩了重拉、
# 或者切模式再切回来，都能直接复用，不用再花十几秒重新编译 OpenVINO。
det_models = {}
# 检测线程句柄：以前是个「只启动一次」的布尔位，线程要是崩了（比如推理库导入
# 失败）就再也拉不起来，只能重启服务。现在存句柄，死了允许重新拉起。
det_thread = [None]

# 冷启动的三步，只用来显示「现在卡在哪一步」。
# 百分比不按阶段加权——各阶段实际耗时波动太大（磁盘缓存、CPU 负载），加权会在
# 阶段切换时一下跳十几二十个点，看着像数字瞎编。改成按「已用时间 / 预期总时长」
# 线性推进，超时停在 96%，100% 只留给首帧真的跑通那一刻。
DET_STAGES = {"import": "导入推理库", "load": "加载模型", "warmup": "首帧检测"}
DET_EXPECT_S = 12.0      # 本机实测冷启动 10~15 秒（导入 ultralytics 占大头）


def _det_stage(name):
    det_state["stage"] = name


def det_progress() -> dict:
    """面板用的冷启动进度：阶段是真的，百分比是按总耗时估的。"""
    st = det_state
    started = st["det_t0"]
    elapsed_ms = (time.perf_counter() - started) * 1000 if started else 0
    if not st["enabled"]:
        return {"stage": "idle", "label": "", "pct": 0,
                "elapsed_ms": 0, "waiting": ""}
    if st["error"]:
        return {"stage": "error", "label": "启动失败", "pct": 100,
                "elapsed_ms": round(elapsed_ms), "waiting": st["error"]}
    # 模型好了但窗口不在：识别循环在空转，用户看到的就是「开了没反应」。
    # 导入库/加载模型这两步跟窗口无关，别拿「等窗口」把阶段盖掉。
    wait = ("" if grabber.window_found or st["stage"] in ("import", "load")
            else "找不到游戏窗口")
    if st["stage"] == "ready":
        return {"stage": "ready", "label": "就绪", "pct": 100,
                "elapsed_ms": round(elapsed_ms), "waiting": wait}
    pct = min(96, round(elapsed_ms / 1000.0 / DET_EXPECT_S * 96))
    if wait and st["stage"] == "warmup":
        # 模型已经装好、卡在「抓不到游戏画面」这一步：进度别再往上爬，
        # 否则看着像马上好，实际是在空等窗口
        pct = min(pct, 40)
    return {"stage": st["stage"], "label": DET_STAGES.get(st["stage"], "准备中"),
            "pct": pct, "elapsed_ms": round(elapsed_ms), "waiting": wait}


def _clamp_conf(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return SHOW_CONF_DEFAULT
    return round(min(0.95, max(0.05, v)), 2)


def _clamp_hits(v) -> int:
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return SHOW_HITS_DEFAULT
    return min(12, max(1, v))


def _clamp_fps(v) -> int:
    """推理帧率上限：0 = 不限（检测循环跑满），否则钳到 5~30。

    这个值不改变单帧耗时，只是让循环在两次推理之间睡一会儿——省的是 CPU 占用，
    不是让某一次推理变快。降到 10 就是「每秒推理 10 次」，显示端本来也不是每帧
    都刷新框，所以看上去差别很小。
    """
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    return min(30, max(5, v))


def _load_det_conf() -> dict:
    """读取上次调的显示参数；文件坏了/没写过就用默认值。"""
    try:
        raw = json.loads(SHOW_CONF_FILE.read_text("utf-8"))
    except Exception:
        raw = {}
    return {"show_conf": _clamp_conf(raw.get("show_conf", SHOW_CONF_DEFAULT)),
            "show_hits": _clamp_hits(raw.get("show_hits", SHOW_HITS_DEFAULT)),
            "max_fps": _clamp_fps(raw.get("max_fps", 0))}


def _save_det_conf() -> None:
    try:
        SHOW_CONF_FILE.write_text(json.dumps(
            {"show_conf": det_state["show_conf"], "show_hits": det_state["show_hits"],
             "max_fps": det_state["max_fps"]}
        ), encoding="utf-8")
    except Exception:
        pass          # 落盘失败不影响当前会话，只是重启后回到旧值


det_state.update(_load_det_conf())
# Tracker（见 webui/track.py）实例化延后到导入之后

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


def dedupe_dets(dets, thr=0.6, dup_thr=0.85):
    """重叠框只保留置信度最高者。

    thr    ：同类名重叠到这个程度就算重复（模型的类 ID 别名会重复检出同一只怪）。
    dup_thr：不同类名也可以判重复 —— 模型偶尔把同一只怪认成两种怪（树/蘑菇/石头），
             两个框几乎重合，各跟一条轨迹，画面就是一只怪两个框、两个读数。
    """
    out = []
    for d in sorted(dets, key=lambda d: -d[2]):
        dup = False
        for o in out:
            ov = overlap_min(d[0], o[0])
            if (d[1] == o[1] and ov >= thr) or ov >= dup_thr:
                dup = True
                break
        if not dup:
            out.append(d)
    return out


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


def pick_lock(tracks, prev_tid, prev_box=None):
    """锁定策略（按优先级）：

    1. 上一帧的锁定目标还在 —— 直接延续，这是锁定稳定的根本；
    2. 它刚丢了 —— 优先接回「离它最后位置最近、同类、且位置合理」的那条轨迹，
       而不是画面里最大/最显眼的那只。怪被遮挡、跳出画面边缘再回来时，
       这一步决定了锁会不会跳到别人身上；
    3. 都没有 —— 按面积×置信度选一只成熟目标；
    4. 仍没有 —— 选置信度最高的，保证界面上始终有锁。
    """
    if not tracks:
        return None
    by_id = {t["tid"]: t for t in tracks}
    if prev_tid in by_id:
        return prev_tid

    # 2) 就近接回：用预测的移动速度放大一点搜索半径，快速移动的怪也能接上
    if prev_box is not None:
        cx = (prev_box[0] + prev_box[2]) / 2
        cy = (prev_box[1] + prev_box[3]) / 2
        bw = prev_box[2] - prev_box[0]
        bh = prev_box[3] - prev_box[1]
        radius = max(180.0, 2.0 * (bw + bh))
        best, best_d = None, None
        for t in tracks:
            if t["conf"] < 0.35:
                continue
            b = t["box"]
            d = (((b[0] + b[2]) / 2 - cx) ** 2 + ((b[1] + b[3]) / 2 - cy) ** 2) ** 0.5
            if d <= radius and (best_d is None or d < best_d):
                best, best_d = t["tid"], d
        if best is not None:
            return best

    # 3) 面积×置信度：大的、近的优先
    best, best_score = None, -1.0
    for t in tracks:
        if t["hits"] < 5 or t["conf"] < 0.45:
            continue
        x1, y1, x2, y2 = t["box"]
        score = (x2 - x1) * (y2 - y1) * t["conf"]
        if score > best_score:
            best, best_score = t["tid"], score
    if best is None:   # 4) 没有成熟目标时退而选置信度最高的，保证始终有锁
        best = max(tracks, key=lambda t: t["conf"])["tid"]
    return best


tracker = Tracker()


def pick_visible(tracks, shown, now, show_conf, show_hits, prev_tid=None):
    """这一帧该画出来的轨迹，同时就地维护 shown（tid -> {"ok", "last"}）。

    规则（见 SHOW_CONF/DISPLAY_MAX_MISS 附近的说明）：
      1) 这一帧量测到的 conf < show_conf —— 直接不画，没有宽限。门限是硬约束；
      2) 首次出现要攒够 show_hits 帧命中 —— 连续两帧的误检攒不够；
      3) 连续 DISPLAY_MAX_MISS 帧没观测到就不画 —— 不留停在原地的幽灵框；
      4) 同一只怪被两条轨迹重复框住时只留一条（DUP_TRACK_IOU）。
    画出来的轨迹 conf 恒 >= show_conf，标签上的数字不会低于门限。
    """
    cand = []
    for tr in tracks:
        if tr["misses"] > DISPLAY_MAX_MISS:      # 3) 幽灵框
            continue
        if tr["conf"] < show_conf:               # 1) 硬门限
            continue
        if shown.get(tr["tid"]) is None and tr["hits"] < show_hits:   # 2) 首次确认
            continue
        cand.append(tr)

    # 4) 重复轨迹抑制：锁定目标优先，其次置信度高的
    cand.sort(key=lambda t: (t["tid"] != prev_tid, -t["conf"]))
    vis = []
    for tr in cand:
        if any(iou(tr["box"], k["box"]) > DUP_TRACK_IOU for k in vis):
            continue
        vis.append(tr)

    keep = {tr["tid"] for tr in vis}
    for tr in vis:
        shown[tr["tid"]] = {"ok": now, "last": now}
    for tid in [k for k in shown if k not in keep]:
        shown.pop(tid, None)                     # 没画出来就清记录，下次重新确认
    return vis


# ---------------- 人物定位（名字牌模板匹配） ----------------
# 2D 客户端里名字牌是叠在画面上的 UI 文本，像素尺寸不随分辨率变（换分辨率它还是
# 那么高），所以拿 WebUI 上框出来的一小块名字牌当模板，用 TM_CCOEFF_NORMED 在
# 整幅画面里找最强响应就能定位人物——比找人形轮廓稳得多，也不吃模型。
#
# 全画面直搜 1366x768 要 15~25ms，所以分两级：半分辨率粗搜（~4ms）+ 命中点附近
# 按原尺寸精修（~2ms）。跑在独立线程里按 15fps 走，不占检测线程的推理时间。
CHAR_TPL_FILE = apppaths.data_file("chartpl.png")
CHAR_CONF_FILE = apppaths.data_file("charconf.json")
CHAR_THRESH_DEFAULT = 0.80
CHAR_MIN_W, CHAR_MIN_H = 24, 8      # 比这还小的框多半是框歪了
CHAR_LOST_AFTER = 0.6               # 超过这么久没命中就不再画标记（秒）
CHAR_FPS = 15
# 怪物框与人物身体框的重叠比例（交集/较小框面积）超过它，就当这只怪「和人物
# 完全重叠」：人挡怪/怪挡人时 YOLO 的框会乱抖，锁定和挂机跟着乱选目标，不如
# 当没看见。人物丢了（模板没匹配上）时不做这个过滤。
CHAR_OVERLAP_HIDE = 0.8
# 尺度只在「分数确实很硬」时采信：画面里没有这块图时，随便一个尺度也能刷到 0.7 上下
# 的假高分，按它把模板压扁，之后就永远匹配不上了。名字牌真身在 0.95 以上，所以
# 0.88 这条线足够区分「真找到了、只是尺度差了 2%」和「画面里压根没有人」。
CHAR_SCALE_MIN_SCORE = 0.88
CHAR_SCALE_COARSE_MIN = 0.80    # 半分辨率粗扫的采信线（会回全图复核）

# 人物只可能出现在画面中部的一条**横向带**里：左右能顶满，上下范围很小（脚下有
# 平台、上面是小地图/任务栏）。把匹配收窄到这条带：上下 HUD 里那些同名字样天然
# 被排除，候选数量掉一个量级，误匹配和耗时一起降。存归一化上下边界，跟着窗口走。
CHAR_ROI_DEFAULT = (0.08, 0.90)
CHAR_ROI_MIN_H = 0.05          # 带子最窄 5% 画面高，再窄就没意义了
CHAR_ROI_EDGE_PX = 14          # 前端拖动时的边缘命中像素（只给前端参考，服务端不用）

char_lock = threading.Lock()
char_state = {
    "enabled": True,
    "thresh": CHAR_THRESH_DEFAULT,
    "tpl": None,          # 模板原图（灰度，未裁紧；换尺度时由它重算）
    "tpl_w": None,        # 裁紧 + 平滑后的白字掩码：实际参与匹配的就是它
    "size": (0, 0),
    "box": None,          # 最近一次命中的框 [x1,y1,x2,y2]（全分辨率）
    "dir": "above",       # 箭头恒朝上（名字牌在脚下）。原来的「朝下/朝上」选项已去掉。
    "scale": 1.0,         # 匹配用的缩放（上传的图和实际画面像素尺度不一致时自动标定）
    "score": 0.0,
    "ts": 0.0,
    "misses": 0,
    "pending": None,      # 嫌疑跳变候选 (box, hits)：攒够帧数才切过去
    "guard": 0,           # 当前 pending 的命中帧数（0 = 正常跟踪）
    "frame_size": None,   # 标定/上次重标时的画面尺寸 (W, H)：只有它变了才允许改尺度
    "roi": CHAR_ROI_DEFAULT,   # 搜索范围的归一化上下边界 (y1, y2)；左右恒为整宽
    "error": "",
}
# 找人的线程句柄。以前用一个 [False] 布尔记「起过了」，一旦线程因为异常挂了，
# 这个标记还是 True，之后就不再有人去匹配了 —— 表现就是「清掉模板之后再传图，
# 人物标记再也不回来」。改成看 is_alive()，挂了能自己重新拉起来。
char_thread = None


def _clamp_thresh(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return CHAR_THRESH_DEFAULT
    return round(min(0.95, max(0.30, v)), 2)


def _clamp_roi(y1, y2):
    """归一化上下边界 → 合法 (y1, y2)：都在 0~1，且带子至少 CHAR_ROI_MIN_H 高。

    允许传反（用户从上往下拖），这里自动换过来；带子太窄就往外顶到最小高度，
    别让一次误拖把范围压成一条缝、什么都匹配不到。
    """
    try:
        a, b = float(y1), float(y2)
    except (TypeError, ValueError):
        return CHAR_ROI_DEFAULT
    if a != a or b != b:                 # NaN
        return CHAR_ROI_DEFAULT
    a, b = min(0.98, max(0.0, a)), min(1.0, max(0.02, b))
    if b < a:
        a, b = b, a
    if b - a < CHAR_ROI_MIN_H:
        b = min(1.0, a + CHAR_ROI_MIN_H)
        a = max(0.0, b - CHAR_ROI_MIN_H)
    return (round(a, 4), round(b, 4))


def _save_char_conf():
    try:
        CHAR_CONF_FILE.write_text(json.dumps(
            {"enabled": char_state["enabled"], "thresh": char_state["thresh"],
             "scale": char_state["scale"], "roi": list(char_state["roi"])}
        ), encoding="utf-8")
    except Exception:
        pass


def _load_char_conf():
    try:
        raw = json.loads(CHAR_CONF_FILE.read_text("utf-8"))
    except Exception:
        raw = {}
    char_state["enabled"] = bool(raw.get("enabled", True))
    char_state["thresh"] = _clamp_thresh(raw.get("thresh", CHAR_THRESH_DEFAULT))
    char_state["dir"] = "above"      # 箭头固定朝上：旧配置文件里的 dir 一律忽略
    try:
        char_state["scale"] = min(1.6, max(0.25, float(raw.get("scale", 1.0))))
    except (TypeError, ValueError):
        char_state["scale"] = 1.0
    roi = raw.get("roi")
    if isinstance(roi, (list, tuple)) and len(roi) == 2:
        char_state["roi"] = _clamp_roi(roi[0], roi[1])


CHAR_WHITE_THR = 185       # 帧里「白字」的亮度下限
CHAR_TXT_THR = 150         # 模板里白字的亮度下限
CHAR_BLUR_SIGMA = 1.0      # 匹配前的高斯平滑（把 1~2px 笔画糊到 3~4px，峰变宽）
# 白字图里 99% 是 0（黑）。直接做 TM_CCOEFF_NORMED，那些全黑的小窗口方差≈0，
# 归一化时除出来一堆 1.000 的假峰（实测 top5 全是 1.000，真身只有 0.958 排不上）。
# 给两边都加 0.02 的底噪，常值窗口的相关系数就退回 0，假峰全消失。
CHAR_EPS = 0.02
CHAR_LOCAL_RADIUS = 60     # 锁定后先在上一帧位置 ±60px 里找
CHAR_WIDE_RADIUS = 200     # 找不到再放宽到 ±200（跳跃/瞬移，不改走全图）
CHAR_REACQ_MISS = 6        # 连续 miss 这么多帧才回退全图重找
CHAR_DIST_PENALTY = 0.05   # 多候选时对「离上次位置远」的轻微惩罚
# 屏幕 HUD 区（底部状态栏/顶部/左右两条）里也印着玩家名字，而且那处白字的形状和
# 名字牌完全一样（实测分数 0.92，只比真身低 0.06）。人物走出画面时全图搜索会锁到
# 那儿去，看起来就是「框突然跳到左下角」。落在这些区域里的候选要额外扣分。
CHAR_HUD_PENALTY = 0.12
CHAR_HUD_BOTTOM = 0.93     # y > 93%H
CHAR_HUD_TOP = 0.05        # y < 5%H
CHAR_HUD_SIDE = 0.09       # x < 9%W 或 x > 91%W（左侧小地图 / 右侧任务栏）

# ---- 人物框的防抖 / 防跳变 ----
# 局部搜索已经把位置约束在上一帧附近，但「附近」在 ±200px 宽搜时足够装下另一行
# 同名白字（小地图里的自己、队伍栏、聊天窗冒出的同名字）；一次误匹配就会把框甩到
# 那边去，下一帧又甩回来，看着像「标记被别的东西引走了」。所以对框本身再加三道闸：
CHAR_DEADZONE_PX = 2.5     # 位移小于这个当没动（匹配峰在原地 ±1px 抖，别让框跟着颤）
CHAR_SMOOTH_PX = 12.0      # 这么小的位移做 EMA，把抖动吃掉；更大的直接跟手
CHAR_SMOOTH_ALPHA = 0.60   # EMA 系数：越小越稳、越滞后（0.6 时 7px/帧 的慢走滞后约 5px）
CHAR_JUMP_PX = 90.0        # 单帧位移超过它就算「跳变」：正常跑动一帧只走 10~40px
CHAR_JUMP_CONFIRM = 4      # 跳变位置要连续这么多帧落在同一处，才承认是真瞬移（约 0.27s）


def _white_map(gray, thr=None):
    """帧 -> 白字似然图。

    为什么不用灰度/边缘做相关：名字牌是纯白细笔画，底下是滚动的地面和特效，同一行字
    相邻两帧的灰度分能从 1.00 掉到 0.77；灰度还要求背景亮度也一致（实测模板背景均值
    19、帧里 136，直接灰度匹配峰值只有 0.74，而且误检能到 0.64，根本分不开）。

    这里只保留「亮且细」的像素：顶帽 = 原图 - 开运算。开运算会把大块亮区（云、白墙、
    地面高光）留下来，相减后归零；只有 1~2px 宽的笔画会被保留。这样背景怎么变都不影响，
    剩下的纯白细结构里，模板的形状就是唯一线索。
    """
    thr = CHAR_WHITE_THR if thr is None else thr
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    opened = cv2.morphologyEx(gray, cv2.MORPH_OPEN, k)
    top = cv2.subtract(gray, opened)
    m = ((gray >= thr) & (top >= 18)).astype(np.float32)
    if m.sum() < 8:                       # 这一块压根没有白字
        return m + CHAR_EPS
    return cv2.GaussianBlur(m, (0, 0), CHAR_BLUR_SIGMA) + CHAR_EPS


def _text_spread(gray):
    """白字在图上铺开的比例 (宽比, 高比)。两者都很大 = 传的是整张截图，不是名字牌。"""
    m = (gray >= CHAR_TXT_THR)
    if int(m.sum()) < 25:
        return 0.0, 0.0
    ys, xs = np.where(m)
    h, w = gray.shape[:2]
    return ((xs.max() - xs.min() + 1) / float(w), (ys.max() - ys.min() + 1) / float(h))


def _char_tpl_mask(gray):
    """模板灰度图 -> (裁紧后的灰度, 白字掩码[未模糊])。

    上传的图往往带一圈背景（实测用户那张 20px 高里有 3 行纯背景），背景会把相关分数
    拉低、还会把框撑大。这里按白字外接框裁紧，只留 2px 余量。
    """
    m = (gray >= CHAR_TXT_THR).astype(np.uint8)
    if int(m.sum()) >= 25:
        ys, xs = np.where(m > 0)
        pad = 2
        y0, y1 = max(0, int(ys.min()) - pad), min(gray.shape[0], int(ys.max()) + 1 + pad)
        x0, x1 = max(0, int(xs.min()) - pad), min(gray.shape[1], int(xs.max()) + 1 + pad)
        gray = gray[y0:y1, x0:x1]
        m = (gray >= CHAR_TXT_THR).astype(np.uint8)
    if int(m.sum()) < 20:                 # 不是纯白字（灰字/描边字）：按分位数兜底
        t = float(np.percentile(gray, 90))
        m = (gray >= t).astype(np.uint8)
    return gray, m.astype(np.float32)


def _tpl_mask_at(gray, scale=1.0):
    """按指定缩放生成匹配用的模板（裁紧 + 缩放 + 平滑）。"""
    _, m = _char_tpl_mask(gray)
    if abs(scale - 1.0) > 0.005:
        w = max(8, int(round(m.shape[1] * scale)))
        h = max(5, int(round(m.shape[0] * scale)))
        m = cv2.resize(m, (w, h), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(m, (0, 0), CHAR_BLUR_SIGMA) + CHAR_EPS


def _gray_from_image(raw):
    """bytes -> 灰度图。解码失败返回 None。"""
    buf = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        return None
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _decode_data_url(s):
    """data:image/png;base64,xxx -> bytes。只认 data URL，避免读任意路径。"""
    if not isinstance(s, str) or "," not in s:
        return None
    head, b64 = s.split(",", 1)
    if "base64" not in head:
        return None
    try:
        return base64.b64decode(b64)
    except Exception:
        return None


def _set_char_tpl(gray, scale=1.0):
    """装上模板。gray 存原图（换尺度时重算用），tpl_w 是实际参与匹配的掩码。"""
    tpl_w = _tpl_mask_at(gray, scale)
    # 缩得太小就别认这个尺度：掩码只剩十几个像素时，画面里随便一小块白边都能刷到
    # 0.8 以上，框会画到血条边、EXP 条那种地方去。宁可回 1:1。
    if tpl_w.shape[1] < CHAR_MIN_W or tpl_w.shape[0] < CHAR_MIN_H:
        scale = 1.0
        tpl_w = _tpl_mask_at(gray, scale)
    h, w = tpl_w.shape[:2]
    with char_lock:
        char_state["tpl"] = gray
        char_state["tpl_w"] = tpl_w
        char_state["size"] = (w, h)
        char_state["scale"] = scale
        char_state["box"] = None
        char_state["score"] = 0.0
        char_state["misses"] = 0
        char_state["pending"] = None
        char_state["guard"] = 0
        char_state["error"] = ""


def _probe_scale(fw, gray, sc):
    """在某个尺度上试一次，返回 (score, scale, 显著性) 或 None。

    显著性 = 最高分 - 99.9 分位分（降采样算，够用且快）。
    """
    H, W = fw.shape[:2]
    t = _tpl_mask_at(gray, sc)
    # 尺寸下限用 CHAR_MIN_W/CHAR_MIN_H：比这更小的掩码匹配出来的峰毫无意义
    # （21×6 的小块在血条边缘能刷到 0.83，之前就是这么把框画到血条上去的）
    if (t.shape[0] < CHAR_MIN_H or t.shape[1] < CHAR_MIN_W
            or t.shape[0] >= H or t.shape[1] >= W):
        return None
    r = cv2.matchTemplate(fw, t, cv2.TM_CCOEFF_NORMED)
    _, mx, _, _ = cv2.minMaxLoc(r)
    return float(mx), sc, 1.0          # 第三项留给以后换度量用，CCOEFF 不需要


def _char_scale_sweep(gray, frame_gray):
    """找一个让模板和画面像素尺度对得上的缩放，返回 (score, scale)。

    名字牌是 2D UI，同一台机器稳定运行时 1:1 就够；但换窗口大小、换分辨率、或者
    上传的图是从别的分辨率截图（4K、手机截图）裁的，尺度能差一倍。实测尺度差 2%
    分数就从 0.98 掉到 0.93，差 8% 直接跳到别处。

    全程在**全分辨率**上扫：曾经试过半分辨率粗扫省时间，但名字牌在半分辨率下只剩
    十几像素宽，笔画糊成一片，0.5 和 0.32 分数一样 —— 扫出来的尺度全是错的。
    全图扫 0.40~1.60（步长 4%）约 200ms，只在「上传」和「跟丢 4 秒后重标」时跑，
    不进每帧路径。
    """
    ry1, ry2 = _char_roi(frame_gray.shape)
    fw = _white_map(frame_gray[ry1:ry2])   # 只在人物可能出现的带子里扫，跟 find_char 一致
    H, W = fw.shape[:2]
    best = None                          # (score, scale, sig)
    for sc in np.arange(0.40, 1.621, 0.04):
        got = _probe_scale(fw, gray, round(float(sc), 3))
        if got is not None and (best is None or got[0] > best[0]):
            best = got
    if best is None:
        return 0.0, 1.0
    if best[0] >= CHAR_SCALE_COARSE_MIN:
        # 在最佳尺度附近再细调一次，免得 4% 的步长把分数压在 0.93 这种半高不高的位置
        for d in (-0.03, -0.015, 0.015, 0.03):
            got = _probe_scale(fw, gray, round(best[1] + d, 3))
            if got is not None and got[0] > best[0]:
                best = got
    if best[0] < CHAR_SCALE_MIN_SCORE:
        return best[0], 1.0              # 峰不够突出 / 分数不够硬：保持 1:1
    return best[0], round(best[1], 3)


def _install_char_tpl(gray):
    """装模板并顺手标定尺度：返回 (score, scale)。"""
    frame = grabber.snapshot()
    fg = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    score, scale = _char_scale_sweep(gray, fg)
    _set_char_tpl(gray, scale)
    with char_lock:
        char_state["scale"] = scale
        # 记住标定时的画面尺寸：以后只有这个尺寸变了，才允许重标尺度
        char_state["frame_size"] = (fg.shape[1], fg.shape[0])
    _save_char_conf()
    # 立刻在整幅画面上跑一次，命中就把标记点上，前端马上能看到
    box, sc2 = find_char(frame)
    with char_lock:
        char_state["score"] = round(sc2, 3)
        if sc2 >= char_state["thresh"]:
            char_state["box"] = box
            char_state["ts"] = time.time()
            char_state["misses"] = 0
    ensure_char_thread()
    return sc2, scale


def load_char_tpl():
    if not CHAR_TPL_FILE.exists():
        return
    try:
        buf = np.frombuffer(CHAR_TPL_FILE.read_bytes(), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    except Exception:
        img = None
    if img is None or not img.size or img.shape[1] < CHAR_MIN_W:
        return
    _set_char_tpl(img, char_state["scale"])


def _best_peaks(res, n=5, rx=24, ry=8):
    """取 n 个非极大峰（分数降序），就地抹掉邻域。返回 [(score, x, y)]。"""
    out = []
    r = res.copy()
    for _ in range(n):
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx <= 0:
            break
        out.append((float(mx), int(loc[0]), int(loc[1])))
        x, y = loc
        r[max(0, y - ry):y + ry + 1, max(0, x - rx):x + rx + 1] = -1.0
    return out


def _local_search(g, tpl_w, box, radius):
    """在 box 周围 ±radius 里搜一次，返回 (box, score) 或 None（峰压在边界上）。"""
    th, tw = tpl_w.shape[:2]
    H, W = g.shape[:2]
    x1, y1 = max(0, int(box[0]) - radius), max(0, int(box[1]) - radius)
    x2, y2 = min(W, int(box[2]) + radius), min(H, int(box[3]) + radius)
    roi = _white_map(g[y1:y2, x1:x2])
    if roi.shape[0] < th or roi.shape[1] < tw:
        return None
    res = cv2.matchTemplate(roi, tpl_w, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    # 峰压在 ROI 边上：人已经走出去了，这一级的结果不可信
    if (loc[0] <= 1 or loc[1] <= 1 or
            loc[0] >= res.shape[1] - 2 or loc[1] >= res.shape[0] - 2):
        return None
    bx, by = x1 + int(loc[0]), y1 + int(loc[1])
    return [bx, by, bx + tw, by + th], float(mx)


def _hud_penalty(box, W, H):
    """候选落在 HUD 区里就扣分（人物的名字牌不会在状态栏里）。"""
    cx = (box[0] + box[2]) / 2.0 / W
    cy = (box[1] + box[3]) / 2.0 / H
    pen = 0.0
    if cy > CHAR_HUD_BOTTOM or cy < CHAR_HUD_TOP:
        pen += CHAR_HUD_PENALTY
    if cx < CHAR_HUD_SIDE or cx > 1.0 - CHAR_HUD_SIDE:
        pen += CHAR_HUD_PENALTY
    return pen


def _char_roi(shape):
    """当前搜索范围的像素行区间 (y1, y2)。左右恒为整宽。

    归一化存、按帧换算：换窗口大小后同一份配置还成立。
    """
    with char_lock:
        a, b = char_state["roi"]
    H = int(shape[0])
    y1 = max(0, min(H - 2, int(round(float(a) * H))))
    y2 = max(y1 + 2, min(H, int(round(float(b) * H))))
    return y1, y2


def find_char(frame):
    """在当前画面里找名字牌。返回 (box, score)；box 为全分辨率像素坐标。

    只在「人物可能出现的横向带」（char_state["roi"]，默认 8%~90% 高、整宽）里找：
    上面是小地图/任务栏、下面是血条，那些地方也印着玩家名字，天然被排除在外。
    带内再做三级策略（前两级都不到 0.2ms，只有真丢了才走带内全搜）：
      1) 局部 ±60px —— 正常跟踪，逐帧位移一般 < 20px，一定落在里面；
      2) 局部 ±200px —— 跳跃/瞬移/被击退时兜住，避免「闪一下重新找」；
      3) 带内全搜 —— 取前 5 个峰，按「分数 - 位置惩罚」排序。

    为什么不全图直搜：画面里还有别处长得一模一样的白字（左下角 LV 栏就印着同一个
    名字，分数 0.92），全图搜很容易被它抢走；局部搜索 + 范围收窄天然排除了这种情况。
    """
    with char_lock:
        tpl_w = char_state["tpl_w"]
        last = char_state["box"]
        misses = char_state["misses"]
        thresh = char_state["thresh"]
    if tpl_w is None:
        return None, 0.0
    th, tw = tpl_w.shape[:2]
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    H, W = g.shape[:2]
    if th >= H or tw >= W:
        return None, 0.0

    ry1, ry2 = _char_roi(g.shape)
    band = g[ry1:ry2]                    # 只在这一条里找
    bh, bw = band.shape[:2]
    if th >= bh or tw >= bw:
        return None, 0.0

    # 1) / 2) 局部跟踪：命中且过门限就算数，不必回到全搜
    if last is not None and misses <= CHAR_REACQ_MISS:
        lb = [last[0], last[1] - ry1, last[2], last[3] - ry1]    # 换成带内坐标
        for radius in (CHAR_LOCAL_RADIUS, CHAR_WIDE_RADIUS):
            hit = _local_search(band, tpl_w, lb, radius)
            if hit is not None and hit[1] >= thresh:
                b = hit[0]
                return [b[0], b[1] + ry1, b[2], b[3] + ry1], hit[1]
            if hit is not None and radius == CHAR_WIDE_RADIUS:
                break      # 宽搜都只有低分：别再拿低分位置凑数

    # 3) 带内全搜
    fw = _white_map(band)
    res = cv2.matchTemplate(fw, tpl_w, cv2.TM_CCOEFF_NORMED)
    cands = _best_peaks(res, 5)
    if not cands:
        return None, 0.0
    if last is not None:
        ref = ((last[0] + last[2]) / 2.0, (last[1] + last[3]) / 2.0 - ry1)
    else:
        ref = (bw / 2.0, bh * 0.55)      # 首次：带子里偏中下（人物脚踩平台）
    diag = (bw * bw + bh * bh) ** 0.5
    best = None
    for sc, x, y in cands:
        box = [x, y + ry1, x + tw, y + th + ry1]
        d = (((x + tw / 2.0) - ref[0]) ** 2 + ((y + th / 2.0) - ref[1]) ** 2) ** 0.5
        val = sc - CHAR_DIST_PENALTY * (d / diag) - _hud_penalty(box, W, H)
        if best is None or val > best[0]:
            best = (val, sc, x, y)
    val, sc, x, y = best
    # 判定用「扣掉位置惩罚后的分」：真身在带子中部，扣完还是 0.95 左右；HUD 里那行
    # 同名文字 0.84 要扣 0.24 只剩 0.60，直接判不通过。显示出去的仍是原始匹配分。
    if val < thresh:
        return None, float(sc)
    return [x, y + ry1, x + tw, y + th + ry1], float(sc)


def _bbox_center(b):
    return ((b[0] + b[2]) * 0.5, (b[1] + b[3]) * 0.5)


def _bbox_dist(a, b):
    ax, ay = _bbox_center(a)
    bx, by = _bbox_center(b)
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


def _bbox_int(b):
    """框一律存整数像素。

    EMA 平滑出来的是浮点，float 的框写回 char_state 之后，下一帧 _local_search 会拿它
    做 g[y1:y2] 切片 —— TypeError: slice indices must be integers。异常被 char_loop
    的 try 吞掉，表现为「人物标记突然不动了、error 一直刷」。
    """
    return [int(round(float(v))) for v in b]


def _bbox_blend(cur, new, k):
    return _bbox_int([cur[i] + (new[i] - cur[i]) * k for i in range(4)])


def chase_char_box(cur, new, pending):
    """给人物框加防抖 / 防跳变。返回 (这一帧显示的框, 新 pending, 事件)。

    cur     上一帧显示的框（None = 还没锁定）
    new     这一帧匹配到的框
    pending 正在等确认的跳变候选 (box, hits)，没有就是 None

    事件只用于日志和面板提示：
      first  首次锁定（拿到模板后第一次命中）
      track  正常跟随（含小位移平滑）
      hold   疑似跳到别处，这一帧不认，继续按住旧框
      switch 跳变候选连续确认够了，承认是真瞬移/被击退

    三道闸：
      1) 死区：位移 < CHAR_DEADZONE_PX 当作没动。匹配峰本来就在原地 ±1px 抖，
         不设死区的话框会一直微颤。
      2) 小位移 EMA：位移 < CHAR_SMOOTH_PX 时按 CHAR_SMOOTH_ALPHA 融合。慢速走动
         时框走得比匹配结果稳，快速移动时（> 12px/帧）直接跟手，不拖后腿。
      3) 跳变确认：位移 > CHAR_JUMP_PX 一律先按住旧框，同一处再连续出现
         CHAR_JUMP_CONFIRM 帧才切过去。这一步就是「别被别处的同名白字引走」——
         一次误匹配只会让框停在原地，不会甩出去再甩回来。
    """
    if cur is None:
        return _bbox_int(new), None, "first"
    d = _bbox_dist(cur, new)
    if d <= CHAR_DEADZONE_PX:
        return _bbox_int(cur), None, "track"
    if d <= CHAR_JUMP_PX:
        k = CHAR_SMOOTH_ALPHA if d <= CHAR_SMOOTH_PX else 1.0
        return _bbox_blend(cur, new, k), None, "track"
    hits = 1
    if pending is not None and _bbox_dist(pending[0], new) <= CHAR_JUMP_PX:
        hits = pending[1] + 1          # 连续落在同一处：可能是真瞬移
    if hits >= CHAR_JUMP_CONFIRM:
        return _bbox_int(new), None, "switch"
    return _bbox_int(cur), (_bbox_int(new), hits), "hold"


def _auto_recalibrate(frame):
    """长时间跟丢后重标尺度 —— 只在**窗口尺寸真的变了**时才做。

    以前只要跟丢 4 秒就重扫一遍，结果人物不在画面里时，扫描在半空里凑出一个 0.88
    的假峰（比如把 54×15 的模板压到 21×6），把好模板改成坏的：之后不但找不着人，
    还会在血条边缘画个 ME 0.83。名字牌是 2D UI，像素尺寸只跟窗口大小/分辨率有关，
    窗口尺寸没变就没有理由改尺度。
    """
    with char_lock:
        tpl = char_state["tpl"]
        old = char_state["scale"]
        prev_size = char_state["frame_size"]
    if tpl is None:
        return False
    H, W = frame.shape[:2]
    if prev_size is None:
        # 第一次进来只记尺寸，不改模板：此时手里没有「变化」这个证据
        with char_lock:
            char_state["frame_size"] = (W, H)
        return False
    if prev_size == (W, H):
        return False                      # 窗口没变过，尺度不可能变
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    score, scale = _char_scale_sweep(tpl, gray)
    if score < CHAR_SCALE_MIN_SCORE or abs(scale - old) <= 0.02:
        return False
    _set_char_tpl(tpl, scale)
    with char_lock:
        char_state["scale"] = scale
        char_state["score"] = round(score, 3)
    _save_char_conf()
    return True


def char_loop():
    """独立线程：按 CHAR_FPS 找人，结果写进 char_state 供画面叠加用。"""
    while True:
        with char_lock:
            enabled, has_tpl = char_state["enabled"], char_state["tpl"] is not None
        if not enabled or not has_tpl or not grabber.window_found:
            time.sleep(0.25)
            continue
        try:
            frame = grabber.snapshot()
            box, score = find_char(frame)
        except Exception as e:  # noqa: BLE001
            # 整轮包起来：以前 snapshot 在 try 外面，取帧一抛异常线程就静悄悄死了
            with char_lock:
                char_state["error"] = f"{type(e).__name__}: {e}"
            time.sleep(0.5)
            continue
        now = time.time()
        with char_lock:
            char_state["error"] = ""
            char_state["score"] = round(score, 3)
            if box is not None and score >= char_state["thresh"]:
                shown, pend, ev = chase_char_box(
                    char_state["box"], box, char_state["pending"])
                char_state["box"] = shown
                char_state["pending"] = pend
                char_state["guard"] = pend[1] if pend else 0
                char_state["misses"] = 0
                # hold 不续命：一直按着旧框说明这次匹配没被采信，CHAR_LOST_AFTER
                # 到点就把标记收掉，别在旧位置留一个「幽灵 ME」。
                if ev != "hold":
                    char_state["ts"] = now
            else:
                char_state["pending"] = None
                char_state["guard"] = 0
                char_state["misses"] += 1
                if char_state["misses"] > CHAR_REACQ_MISS:   # 连续没命中就当丢了
                    char_state["box"] = None
                    char_state["ts"] = 0.0
        # 丢了 4 秒还没回来：可能是窗口大小/分辨率变了，重标一次尺度
        with char_lock:
            lost_long = char_state["misses"] > CHAR_FPS * 4
        if lost_long:
            try:
                _auto_recalibrate(frame)
            except Exception:  # noqa: BLE001
                pass
            with char_lock:
                char_state["misses"] = 0
        time.sleep(1.0 / CHAR_FPS)


CHAR_COLOR = (255, 200, 0)      # BGR 天蓝，和识别框（绿/红/怪种色）都区分得开


# ---------------- 自动打怪（挂机） ----------------
# 决策全在 webui/bot.py（什么时候按哪个键）。这里只做三件事：
#   1) 给它喂一份状态快照（人物框 / 血 / 这一帧画出来的怪 / 游戏在不在前台）
#   2) 把攻击范围画到画面上，方便对着画面调距离
#   3) 提供开关和参数接口
BOT_RANGE_ON = (0, 190, 255)    # BGR 橙黄：挂机在跑
BOT_RANGE_OFF = (110, 110, 110)  # 灰：挂机关着也画，不然没法调「多远算够得着」


def _game_foreground():
    """游戏窗口是不是当前前台窗口。

    这是挂机脚本最要紧的一道闸：不是前台就绝不发按键，否则等于往你正在用的
    窗口里打字、按方向键。
    """
    hwnd = find_window()
    if not hwnd:
        return False
    # 用 win32gui 而不是 ctypes.windll：后者默认按 c_int 返回，64 位句柄会被
    # 截成 32 位，和 find_window() 给的整数对不上，闸门就永远判成「不在前台」。
    return int(win32gui.GetForegroundWindow()) == int(hwnd)


def _bot_state():
    with char_lock:
        cbox = char_state["box"]
        cts = char_state["ts"]
    if cbox is not None and (cts <= 0 or time.time() - cts > CHAR_LOST_AFTER):
        cbox = None                      # 人物已经丢了，别拿旧位置继续打
    with bar_lock:
        hp = bar_state["values"].get("hp")
    with det_lock:
        mons = list(det_state["vis"])
    return {"fg": _game_foreground(), "hp": hp, "char": cbox, "monsters": mons}


game_bot = botmod.Bot(_bot_state)
# 上次关着/开着都不自动续跑：重启服务器就自己开始按键盘太吓人了。
# 参数（血线、距离…）照旧从 botconf.json 读，只有开关强制回关。
game_bot.update_cfg({"on": False}, save=False)


def _dash(img, p1, p2, color, thick=1, seg=9):
    """画虚线：cv2 没有现成的虚线，按长度切段画。"""
    x1, y1 = p1
    x2, y2 = p2
    n = int(max(abs(x2 - x1), abs(y2 - y1)) / (seg * 2)) + 1
    for i in range(n):
        t0 = 2 * i / (2 * n)
        t1 = (2 * i + 1) / (2 * n)
        cv2.line(img, (int(x1 + (x2 - x1) * t0), int(y1 + (y2 - y1) * t0)),
                 (int(x1 + (x2 - x1) * t1), int(y1 + (y2 - y1) * t1)),
                 color, thick, cv2.LINE_AA)


# 身体框换算的唯一实现在 bot.py（决策与画图共用一份），这里起个别名保持可读
char_body_box = botmod.char_body_box


def draw_attack_range(img, box):
    """在人物旁边画出攻击区域：以身体中心为原点、上下各 range_h/2 的矩形
    （上下边虚线、左右实线）。

    横向按攻击方向展开：`both` 左右各 range_px；`front` 只朝人物**当前朝向**
    那一侧展开（单体技能只能打身前）。朝向来自 bot 状态里的 `face`，读不到
    （0）就画成两侧——不知道朝哪就别假装知道。

    画的不是「水平够得着多远」的示意带了——bot 出不出手就是按这个矩形与怪物
    框求交判断的（in_attack_zone），所见即所打。上下层够不着的怪，把高度调小
    就不会再被打。
    """
    cfg, st = game_bot.snapshot()
    r = int(cfg["range_px"])
    rh = int(cfg["range_h"])
    if r <= 0 or rh <= 0:
        return img
    mode = cfg.get("range_side", "both")
    face = int(st.get("face") or 0)
    on = cfg["on"]
    color = BOT_RANGE_ON if on else BOT_RANGE_OFF
    thick = 2 if on else 1
    h, w = img.shape[:2]
    b = char_body_box(box)
    cx = int((b[0] + b[2]) / 2)
    cy = int((b[1] + b[3]) / 2)
    half = rh // 2
    if mode == "front" and face:
        if face > 0:                      # 朝右：从身体中心往右
            left, right = cx, cx + r
        else:                             # 朝左
            left, right = cx - r, cx
    else:
        left, right = cx - r, cx + r
    left, right = max(2, left), min(w - 3, right)
    if right - left < 6:                  # 贴着画面边缘时别画成一条线
        right = min(w - 3, left + 6)
    top, bot = max(2, cy - half), min(h - 3, cy + half)
    _dash(img, (left, top), (right, top), color, thick)
    _dash(img, (left, bot), (right, bot), color, thick)
    # 两端竖线用实线，一眼能看出边界在哪
    cv2.line(img, (left, top - 6), (left, bot + 6), color, thick, cv2.LINE_AA)
    cv2.line(img, (right, top - 6), (right, bot + 6), color, thick, cv2.LINE_AA)
    cv2.line(img, (cx, cy - 5), (cx, cy + 5), color, thick, cv2.LINE_AA)
    # 箭头表示打哪边：两侧 / 朝右 / 朝左 / 朝向未知（正前方模式还没转身）
    arrow = ""
    if mode == "front":
        arrow = " ->" if face > 0 else (" <-" if face < 0 else " ?")
    tag = f"ATK {r}x{rh}{arrow}" if on else f"ATK {r}x{rh}{arrow} (关)"
    (tw_, th_), _ = cv2.getTextSize(tag, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
    # 标签贴在矩形外侧的空地上：正前方模式只有半个框，往里放会压住人物本体
    if mode == "front" and face > 0:
        tx = right + 6
    elif mode == "front" and face < 0:
        tx = left - tw_ - 6
    else:
        tx = left + 4
    tx = max(4, min(tx, w - tw_ - 6))
    ty = min(bot - 2, max(th_ + 3, cy + th_ // 2))
    cv2.rectangle(img, (tx - 3, ty - th_ - 3), (tx + tw_ + 3, ty + 2), TXT_BG, -1)
    cv2.putText(img, tag, (tx, ty - 1), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                color, 1, cv2.LINE_AA)
    return img


def overlay_char(img):
    """把人物标记画到画面上（原地修改并返回）。

    人物用和怪物同款的识别框样式：由名字牌框推导出身体框，整框框住人物本体，
    顶上挂 ME 标签（分数即模板匹配置信度）。名字牌本身再画个 1px 细框，方便
    确认模板匹配到的到底是哪块像素。以前的「小框 + 箭头指向身体」不要了——
    框直接盖住身体就不需要箭头再指一遍。
    """
    with char_lock:
        box = char_state["box"]
        score = char_state["score"]
        ts = char_state["ts"]
    if box is None or ts <= 0 or time.time() - ts > CHAR_LOST_AFTER:
        return img
    h, w = img.shape[:2]
    # 名字牌细框（1px）：模板实际命中的位置，标定/换图时用来肉眼核对
    px1, py1, px2, py2 = (int(v) for v in box)
    cv2.rectangle(img, (max(1, px1 - 2), max(1, py1 - 2)),
                  (min(w - 2, px2 + 2), min(h - 2, py2 + 2)), CHAR_COLOR, 1)
    # 身体框：和 draw_tracks 里的怪物框同一个画法（2px 矩形 + 底色标签）
    b = char_body_box(box)
    x1, y1 = max(1, int(b[0])), max(1, int(b[1]))
    x2, y2 = min(w - 2, int(b[2])), min(h - 2, int(b[3]))
    cv2.rectangle(img, (x1 - 2, y1 - 2), (x2 + 2, y2 + 2), CHAR_COLOR, 2)
    tag = f"ME {score:.2f}"
    (tw_, th_), _ = cv2.getTextSize(tag, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
    ty = y1 - 6 if y1 - 6 > th_ else y2 + th_ + 6
    cv2.rectangle(img, (x1 - 4, ty - th_ - 4), (x1 - 4 + tw_ + 4, ty + 2), TXT_BG, -1)
    cv2.putText(img, tag, (x1 - 2, ty - 1), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                CHAR_COLOR, 1, cv2.LINE_AA)
    # 攻击范围：挂在人物标记上一起画，所以手机上看到的就是后台真正用的那个距离
    try:
        draw_attack_range(img, box)
    except Exception:  # noqa: BLE001
        pass                     # 画不画得出来都不该影响推流
    return img


def ensure_char_thread():
    """确保找人的线程活着。线程挂了（异常/被回收）就重新拉一个。

    之前这里用一次性布尔标记，线程一死就再也不会有新的——模板明明在、开关也开着，
    人物标记却永远不出现，看着就像「功能被我点坏了」。
    """
    global char_thread
    if char_thread is not None and char_thread.is_alive():
        return
    char_thread = threading.Thread(target=char_loop, daemon=True)
    char_thread.start()


_load_char_conf()
load_char_tpl()
if char_state["tpl"] is not None:
    ensure_char_thread()


_YOLO = [None]              # 懒加载的 ultralytics.YOLO 类（导入一次就缓存）
_det_load_lock = threading.Lock()   # 预加载线程和检测线程可能同时想装模型


def _yolo_cls():
    if _YOLO[0] is None:
        _det_stage("import")
        import warnings
        warnings.filterwarnings("ignore")
        from ultralytics import YOLO  # noqa: PLC0415  故意延迟到线程里导入（约 5 秒）
        _YOLO[0] = YOLO
    return _YOLO[0]


def _ensure_model(mode):
    """取（必要时加载）某个模式的模型，模块级缓存，多线程共用一把锁。"""
    if mode in det_models:
        return det_models[mode]
    with _det_load_lock:
        if mode not in det_models:      # 双检：等锁期间可能已被别的线程装好
            cls = _yolo_cls()
            _det_stage("load")
            det_models[mode] = cls(apppaths.model_dir(MODES[mode]["dir"]), task="detect")
    return det_models[mode]


def preload_det():
    """服务启动后后台预热：导入推理库 + 装模型 + 用一张空图跑通首帧。

    首次点开「识别」原本要从零等 10~15 秒（导入 ultralytics 占一半，OpenVINO
    首次编译占另一半）。预先跑一遍之后，点开基本是秒出，进度条一闪而过。
    失败就安静退出：用户点开时检测线程会再试一次，并把自己的报错显示到面板上。
    """
    try:
        mode = det_state["mode"]
        model = _ensure_model(mode)
        if det_state["enabled"]:
            return                      # 用户已经点开了，让检测线程自己去跑首帧
        _det_stage("warmup")
        cfg = MODES[mode]
        # 尺寸无所谓：predict 会按 imgsz 缩放，这里只要把推理引擎建起来
        model.predict(np.zeros((768, 1366, 3), np.uint8), imgsz=cfg["imgsz"],
                      conf=min(cfg["conf"], LOW_DETECT_CONF),
                      verbose=False, device="cpu")
        det_state["preloaded"] = True
        print(f"detector preloaded ({cfg['dir']})")
    except Exception as e:  # noqa: BLE001
        # 预加载失败不影响正常流程，但要把原因留下来：exe 换机器最常见的失败
        # 就是缺 dll / 模型路径不对，面板和 /api/stats 都能查到。
        det_state["preload_error"] = f"{type(e).__name__}: {e}"
        print(f"preload failed: {det_state['preload_error']}")
    finally:
        if not det_state["enabled"]:
            _det_stage("idle")


def detect_loop():
    _det_stage("import")
    try:
        _ensure_model(det_state["mode"])
    except Exception as e:  # noqa: BLE001
        det_state["error"] = f"模型加载失败: {type(e).__name__}: {e}"
        det_state["loading"] = False
        _det_stage("error")
        return
    # 模型装好了，但 ready 要等第一帧真的跑出结果——那一步也可能要一两秒，
    # 提前置 ready 会让面板显示「0 ms · 0 帧/秒」，看着像坏了。
    _det_stage("warmup")
    print(f"detector loaded ({MODES[det_state['mode']]['dir']})")

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
            # 首次切到某个模式时要现加载模型，这也算一次冷启动：进度条重走一遍
            fresh = mode not in det_models
            if fresh:
                det_state["ready"] = False
                det_state["loading"] = True
                det_state["det_t0"] = time.perf_counter()
            model = _ensure_model(mode)
        except Exception as e:  # noqa: BLE001
            det_state["error"] = f"模型加载失败: {type(e).__name__}: {e}"
            det_state["loading"] = False
            _det_stage("error")
            time.sleep(1.0)
            continue

        # 推理节流：max_fps>0 时按目标帧率给循环踩刹车。刹车期间不抓帧、不跑跟踪器，
        # 画面上保持上一次的标注（本来就只在每次推理后更新一次，不是每帧都变）。
        # 排程按「下一帧的计划时刻」推进，而不是按「上次睡完的时刻」——Windows 的
        # time.sleep 精度约 15.6ms，每睡一次都可能多睡一格；若拿实际时刻当基准，
        # 这点误差会一帧一帧累加（实测设 15 只能跑到 7.9）。按计划时刻推进则下一帧
        # 少睡一点就补回来了，平均速率才不会掉一半。
        tgt = det_state["max_fps"]
        if tgt > 0:
            period = 1.0 / tgt
            nxt = det_state["next_infer"]
            now = time.perf_counter()
            if not nxt or nxt < now - 1.0:      # 首次 / 刚从暂停恢复：重新对齐
                nxt = now
            if now < nxt:
                time.sleep(min(nxt - now, 0.25))
                continue
            # 落后太多（推理本身比 period 还慢）就别攒欠账，从当前时刻重新起算
            det_state["next_infer"] = max(nxt + period, now)

        frame = grabber.snapshot()
        tick = time.perf_counter()
        rects = mask_rects() if det_state["mask"] else None
        try:
            # 推理用涂掉 UI 的副本；画框画在原始帧上，画面里 UI 照常可见
            infer_src = apply_ui_mask(frame.copy(), rects) if rects else frame
            res = model.predict(infer_src, imgsz=cfg["imgsz"],
                                conf=min(cfg["conf"], LOW_DETECT_CONF),
                                verbose=False, device="cpu")[0]
        except Exception as e:  # noqa: BLE001
            det_state["error"] = f"推理失败: {type(e).__name__}: {e}"
            det_state["loading"] = False
            _det_stage("error")
            time.sleep(0.5)
            continue

        if not det_state["ready"] or det_state["stage"] == "error":
            # 首帧推理通了才算真的就绪：面板的进度条到这里才走满。
            # 之前偶发失败（过图瞬间抓不到帧之类）也会自动恢复，不用重启。
            det_state["ready"] = True
            det_state["loading"] = False
            det_state["error"] = ""
            _det_stage("ready")
            print("detector ready")

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
        now = time.time()
        with det_lock:
            prev = det_state["lock"]["tid"] if det_state["lock"] else None
            prev_box = det_state["lock_box"]
            stale = now - det_state["lock_ts"] > 3.0
            show_conf = det_state["show_conf"]
            show_hits = det_state["show_hits"]
            shown = {k: dict(v) for k, v in det_state["shown"].items()}
        if stale:                      # 锁定已经断了 3 秒，别再拿旧位置去找
            prev_box = None

        vis = pick_visible(tracks, shown, now, show_conf, show_hits, prev)

        # 和人物完全重叠的怪不当目标：人挡怪/怪挡人时 YOLO 的框会乱抖，锁定和
        # 挂机跟着乱选。过滤放在「这一帧画谁」这一层——显示、计数、锁定、挂机
        # 挑目标用的都是同一份 vis，不会出现「画面上没有、挂机却在打」的分裂。
        # 人物刚被挡住/丢失时不滤：宁可多显示，也别拿旧位置瞎删。
        with char_lock:
            cbox = char_state["box"]
            cts = char_state["ts"]
        if cbox is not None and cts > 0 and now - cts <= CHAR_LOST_AFTER:
            cbody = char_body_box(cbox)
            vis = [tr for tr in vis
                   if overlap_min(tr["box"], cbody) < CHAR_OVERLAP_HIDE]

        locked_tid = pick_lock(vis, prev, prev_box)

        counts = {}
        for tr in vis:
            counts[tr["name"]] = counts.get(tr["name"], 0) + 1
        lock_info, lock_box = None, None
        for tr in vis:
            if tr["tid"] == locked_tid:
                lock_info = {"tid": tr["tid"], "name": tr["name"],
                             "conf": round(tr["conf"], 2)}
                lock_box = list(tr["box"])

        annotated = draw_tracks(frame, vis, locked_tid)

        with det_lock:
            det_state["annotated"] = annotated
            det_state["infer_ms"] = (time.perf_counter() - tick) * 1000
            det_state["counts"] = counts
            det_state["shown"] = shown
            # 挂机脚本要看「这一帧画出来的怪在哪」。只留框/名字/分数，别把整个
            # 轨迹 dict 塞进来（里面有卡尔曼状态，每帧拷贝没必要）
            det_state["vis"] = [{"tid": tr["tid"], "name": tr["name"],
                                 "box": [round(float(v), 1) for v in tr["box"]],
                                 "conf": round(float(tr["conf"]), 3)} for tr in vis]
            det_state["lock"] = lock_info
            if lock_box is not None:   # 锁上了才更新；丢了保留最后位置，便于下次接回同一只
                det_state["lock_box"] = lock_box
                det_state["lock_ts"] = time.time()
            det_state["error"] = ""
        n += 1
        dt = time.perf_counter() - t0
        if dt >= 2.0:
            det_state["det_fps"], n, t0 = n / dt, 0, time.perf_counter()


# 服务一起来就在后台把模型热好（不推理真画面，只把库导入、模型编译、引擎建起来）。
# 这样用户点「识别」时不用先干等十几秒——等待被挪到了启动阶段，而那个阶段本来
# 就在等窗口/等页面打开。失败没关系，点开时检测线程会再试一次并显示报错。
threading.Thread(target=preload_det, daemon=True, name="det-preload").start()


app = FastAPI(title="Capture Preview")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
INDEX = apppaths.res_file("index.html")

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
MOBILE = apppaths.res_file("m.html")


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
    # 没人看流时 /stream 的计时循环不跑，stream_fps 会冻在上次的值上（看着像"还在推 30 帧"）。
    # 用最后一帧发出的时间判断新鲜度，超过 1.5 秒就当 0。
    sent_age = time.time() - grabber.last_sent_wall if grabber.last_sent_wall else -1
    stream_fps = round(grabber.stream_fps, 1) if 0 <= sent_age < 1.5 else 0.0
    with det_lock:
        counts = dict(det_state["counts"])
    dp = det_progress()
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
    return JSONResponse({
        "window_found": grabber.window_found,
        "window_minimized": grabber.window_minimized,
        "window_title": WINDOW_TITLE,
        "client_size": list(grabber.client_size),
        "capture_fps": round(grabber.capture_fps, 1),
        "stream_fps": stream_fps,
        "frame_age_ms": round(age, 0),
        "error": grabber.error,
        "detect_enabled": det_state["enabled"],
        "detect_loading": det_state["loading"],
        "detect_ready": det_state["ready"],
        "detect_stage": dp["stage"],
        "detect_stage_label": dp["label"],
        "detect_progress": dp["pct"],
        "detect_elapsed_ms": dp["elapsed_ms"],
        "detect_waiting": dp["waiting"],
        "infer_ms": round(det_state["infer_ms"], 0),
        "det_fps": round(det_state["det_fps"], 1),
        "targets": sum(counts.values()),
        "show_conf": det_state["show_conf"],
        "show_hits": det_state["show_hits"],
        "max_fps": det_state["max_fps"],
        "bot_on": game_bot.snapshot()[0]["on"],
        "pending": tracker.pending,
        "top_classes": [f"{k}×{v}" for k, v in top],
        "detect_error": det_state["error"],
        "detect_preloaded": det_state["preloaded"],
        "detect_preload_error": det_state["preload_error"],
        "filter": det_state["filter"],
        "lock": det_state["lock"],
        "mode": det_state["mode"],
        "mode_label": MODES[det_state["mode"]]["label"],
        "mask": det_state["mask"],
        "bars": dict(bar_state["values"]),
        "bars_fps": round(bar_state["fps"], 1),
        "char": {
            "has_tpl": char_state["tpl"] is not None,
            "enabled": char_state["enabled"],
            "thresh": char_state["thresh"],
            "score": char_state["score"],
            "age": (round(time.time() - char_state["ts"], 2)
                    if char_state["ts"] else None),
            "size": list(char_state["size"]),
            "dir": char_state["dir"],
            "scale": char_state["scale"],
            "guard": char_state["guard"],   # >0：疑似跳变，正在攒帧确认
            "roi": list(char_state["roi"]),   # 搜索范围（归一化 y1,y2；整宽）
        },
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


@app.get("/api/detect/conf")
async def detect_conf(v: float = -1):
    """显示门限（0.05~0.95）。不传 v 只读当前值，传了立即生效并存盘。

    改的是「什么样的框算识别到」：低于门限的目标不画框、不计数、不参与锁定，
    但跟踪器仍在内部跟着它，所以画面上不会出现「框跳来跳去」的抖动。
    """
    if v >= 0:
        val = _clamp_conf(v)
        with det_lock:
            det_state["show_conf"] = val
        _save_det_conf()
    return JSONResponse({"show_conf": det_state["show_conf"], "show_hits": det_state["show_hits"]})


@app.get("/api/detect/stability")
async def detect_stability(v: int = -1):
    """稳定帧数（1~12）：累计命中这么多帧才允许第一次出现在画面上。

    调大 → 误检更少，但真目标出现得稍晚；调小 → 更快，但更容易闪过一两个假框。
    不传 v 只读当前值。
    """
    if v >= 0:
        val = _clamp_hits(v)
        with det_lock:
            det_state["show_hits"] = val
            det_state["shown"] = {}       # 规则变了，重新攒
        _save_det_conf()
    return JSONResponse({"show_conf": det_state["show_conf"], "show_hits": det_state["show_hits"]})


@app.get("/api/detect/mask")
async def detect_mask(on: int = 1):
    """UI 排除区开关：on=1 涂掉固定 UI 再推理（默认），on=0 全画面检测。"""
    det_state["mask"] = bool(on)
    return JSONResponse({"mask": det_state["mask"]})


@app.get("/api/detect/fps")
async def detect_fps(v: int = -1):
    """推理帧率上限：0 = 不限（跑满），5~30 按目标帧率给检测循环踩刹车。

    省的是 CPU 占用而不是单帧耗时：YOLO 的 imgsz 固定，单次推理永远是那么多计算量，
    这个开关只是决定「每秒推理几次」。降到 10 约等于把检测线程的 CPU 砍到原来的
    1/3（按实测 66ms/帧算，跑满约 15FPS）。不传 v 只读当前值。
    """
    if v >= 0:
        val = _clamp_fps(v)
        with det_lock:
            det_state["max_fps"] = val
            det_state["next_infer"] = 0.0    # 立刻按新节拍重新排程，不用等上一档走完
        _save_det_conf()
    return JSONResponse({"max_fps": det_state["max_fps"], "det_fps": round(det_state["det_fps"], 1)})


# ---------------- 挂机（自动打怪）接口 ----------------
@app.get("/api/bot")
async def bot_get():
    """当前挂机参数 + 这一轮在干什么（面板上「状态」那一行就是这里来的）。"""
    cfg, st = game_bot.snapshot()
    with char_lock:
        has_char = char_state["tpl"] is not None
    with bar_lock:
        hp = bar_state["values"].get("hp")
    return JSONResponse({
        "cfg": cfg, "status": st, "hp": hp,
        "input": game_bot.input_info(),      # 现在实际用硬件还是软件注入
        "foreground": _game_foreground(),
        "has_char": has_char,
        "detect_on": det_state["enabled"],
        "window_found": grabber.window_found,
    })


@app.post("/api/bot/set")
async def bot_set(payload: dict = Body(...)):
    """改挂机参数（可只传一部分）。传了 on 就走开关那条路，保证线程起停一致。"""
    data = dict(payload or {})
    on = data.pop("on", None)
    cfg = game_bot.update_cfg(data)
    if on is not None:
        cfg["on"] = game_bot.set_on(on)
    return JSONResponse({"cfg": cfg})


@app.post("/api/bot/toggle")
async def bot_toggle():
    cfg, _ = game_bot.snapshot()
    on = game_bot.set_on(not cfg["on"])
    if not on:
        game_bot.release_all()      # 关掉时把按住的键全部松开，别让角色一直跑
    cfg, st = game_bot.snapshot()
    return JSONResponse({"on": on, "cfg": cfg, "status": st})


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
        # 切到没加载过的模式要现编译模型（十几秒），此时也得让进度条出现；
        # 切回已加载过的模式则瞬间生效，不打扰。
        if det_state["enabled"] and m not in det_models:
            det_state["loading"] = True
            det_state["ready"] = False
            det_state["det_t0"] = time.perf_counter()
            det_state["stage"] = "load"
    return JSONResponse({"mode": det_state["mode"], **MODES[det_state["mode"]]})


@app.get("/api/detect/toggle")
async def detect_toggle():
    det_state["enabled"] = not det_state["enabled"]
    if det_state["enabled"]:
        th = det_thread[0]
        if th is None or not th.is_alive():
            # 第一次开，或上一次线程崩了（导入失败/推理异常退出）：重新拉起并重走进度
            det_state["error"] = ""
            det_state["det_t0"] = time.perf_counter()
            det_state["loading"] = not det_state["ready"]
            det_state["stage"] = "ready" if det_state["ready"] else "import"
            th = threading.Thread(target=detect_loop, daemon=True, name="detect")
            det_thread[0] = th
            th.start()
    else:
        with det_lock:
            det_state["annotated"] = None
            det_state["counts"] = {}
            det_state["lock"] = None
            det_state["lock_box"] = None
            det_state["lock_ts"] = 0.0
            det_state["shown"] = {}
        tracker.reset()
    return JSONResponse({"enabled": det_state["enabled"], "loading": det_state["loading"],
                         **{k: v for k, v in det_progress().items() if k != "waiting"}})


@app.get("/api/detect/progress")
async def detect_progress():
    """冷启动进度（轻量接口）：面板在加载期间高频轮询它，就绪即停。

    返回的 pct 是按阶段耗时估的，不是真实百分比——import/load 在 C++ 里跑，
    没有回调能报精细进度。阶段切换是真的，所以进度会一站一站往前走。
    """
    p = det_progress()
    p["enabled"] = det_state["enabled"]
    p["ready"] = det_state["ready"]
    p["loading"] = det_state["loading"]
    p["window_found"] = grabber.window_found
    # 服务启动时的后台预热（用户还没点识别）也让人能看到，排查「点了半天没反应」用
    p["preloading"] = (not det_state["enabled"]) and det_state["stage"] in ("import", "load", "warmup")
    return JSONResponse(p)


@app.get("/api/window/focus")
async def window_focus():
    """把游戏窗口抬到前台（前端按钮 / 脚本截图前调用）。"""
    ok, msg = await asyncio.to_thread(bring_to_front)
    return JSONResponse({"ok": ok, "msg": msg, "window_found": grabber.window_found})


# ---------------- 人物定位接口 ----------------
@app.get("/api/char")
async def char_status():
    with char_lock:
        ts = char_state["ts"]
        return JSONResponse({
            "has_tpl": char_state["tpl"] is not None,
            "enabled": char_state["enabled"],
            "thresh": char_state["thresh"],
            "score": char_state["score"],
            "box": char_state["box"],
            "age": round(time.time() - ts, 2) if ts else None,
            "size": list(char_state["size"]),
            "dir": char_state["dir"],
            "scale": char_state["scale"],
            "guard": char_state["guard"],
            "roi": list(char_state["roi"]),   # 搜索范围（归一化上下边界，整宽）
            "error": char_state["error"],
            "thread": _char_thread_alive(),   # 找人线程是否活着（排查「标记不动了」）
        })


@app.post("/api/char/template")
async def char_set_template(payload: dict = Body(...)):
    """用前端框出来的归一化矩形，从**当前原始画面**裁一块当名字牌模板。

    注意是从原始帧裁、不是从推流画面裁：推流画面叠了识别框和血条标注，
    裁进来模板就把那些线也一起认了。
    """
    try:
        x1, y1, x2, y2 = (float(payload[k]) for k in ("x1", "y1", "x2", "y2"))
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "msg": "坐标不合法"}, status_code=400)
    frame = grabber.snapshot()
    h, w = frame.shape[:2]
    px1, px2 = sorted((int(round(x1 * w)), int(round(x2 * w))))
    py1, py2 = sorted((int(round(y1 * h)), int(round(y2 * h))))
    px1, py1 = max(0, px1), max(0, py1)
    px2, py2 = min(w, px2), min(h, py2)
    cw, ch = px2 - px1, py2 - py1
    if cw < CHAR_MIN_W or ch < CHAR_MIN_H:
        return JSONResponse({"ok": False,
                             "msg": f"框太小（{cw}×{ch}），至少要 {CHAR_MIN_W}×{CHAR_MIN_H}"},
                            status_code=400)
    gray = cv2.cvtColor(frame[py1:py2, px1:px2], cv2.COLOR_BGR2GRAY)
    ok, buf = cv2.imencode(".png", gray)
    if not ok:
        return JSONResponse({"ok": False, "msg": "模板编码失败"}, status_code=500)
    CHAR_TPL_FILE.write_bytes(buf.tobytes())
    score, scale = _install_char_tpl(gray)
    return JSONResponse({"ok": True, "size": [cw, ch], "score": round(score, 3),
                         "scale": scale, "thresh": char_state["thresh"]})


@app.post("/api/char/upload")
async def char_upload(payload: dict = Body(...)):
    """上传一张人物名字牌的小图当模板（前端传 data URL，不用 multipart 依赖）。

    收到图之后会在当前画面上扫一遍缩放尺度（截图可能来自别的分辨率），把模板
    重采样到和画面一致的像素尺度，再跑一次匹配，命中就立刻把标记点上。
    """
    raw = _decode_data_url(payload.get("image"))
    if not raw:
        return JSONResponse({"ok": False, "msg": "图片数据不合法"}, status_code=400)
    gray = await asyncio.to_thread(_gray_from_image, raw)
    if gray is None:
        return JSONResponse({"ok": False, "msg": "这张图解析不了（要 png/jpg/webp）"},
                            status_code=400)
    h, w = gray.shape[:2]
    if w < CHAR_MIN_W or h < CHAR_MIN_H:
        return JSONResponse({"ok": False,
                             "msg": f"图太小（{w}×{h}），至少要 {CHAR_MIN_W}×{CHAR_MIN_H}"},
                            status_code=400)
    if w > 600:                      # 大图先缩一下省内存
        gray = cv2.resize(gray, (600, max(8, int(round(h * 600 / w)))),
                          interpolation=cv2.INTER_AREA)
        h, w = gray.shape[:2]
    # 传整张截图是最常见的坑：白字散布在全图，模板就等于「整个画面的文字布局」，
    # 匹配出来的位置必然是乱的。这种情况直接拒掉，让用户只截名字那一小块。
    wr, hr = _text_spread(gray)
    if w >= 400 and h >= 250 and wr > 0.85 and hr > 0.85:
        return JSONResponse({"ok": False,
                             "msg": "这张图里的白字铺满了整幅画面，看起来是整张截图。"
                                    "请只截「人物名字」那一小块（长条形，几厘米宽就够）"},
                            status_code=400)
    ok, buf = cv2.imencode(".png", gray)
    if not ok:
        return JSONResponse({"ok": False, "msg": "模板编码失败"}, status_code=500)
    CHAR_TPL_FILE.write_bytes(buf.tobytes())
    score, scale = await asyncio.to_thread(_install_char_tpl, gray)
    return JSONResponse({"ok": True, "size": [w, h], "score": round(score, 3),
                         "scale": scale, "thresh": char_state["thresh"]})


def _char_thread_alive():
    return bool(char_thread is not None and char_thread.is_alive())


@app.delete("/api/char/template")
async def char_del_template():
    try:
        CHAR_TPL_FILE.unlink()
    except FileNotFoundError:
        pass
    with char_lock:
        # 模板没了就什么都不剩：tpl/tpl_w/框/分数/计时全清空，否则刷新页面会带着
        # 上次的框和分数出现（load_char_tpl 只在 tpl 为空时跳过，但 stats 会漏旧值）
        char_state["tpl"] = None
        char_state["tpl_w"] = None
        char_state["size"] = (0, 0)
        char_state["box"] = None
        char_state["score"] = 0.0
        char_state["ts"] = 0.0
        char_state["misses"] = 0
        char_state["pending"] = None
        char_state["guard"] = 0
        char_state["error"] = ""
    ensure_char_thread()     # 线程要是已经挂了，这里顺手拉回来
    return JSONResponse({"ok": True, "thread": _char_thread_alive()})


@app.get("/api/char/template.png")
async def char_tpl_png():
    if not CHAR_TPL_FILE.exists():
        return Response(status_code=404)
    return Response(CHAR_TPL_FILE.read_bytes(), media_type="image/png",
                    headers={"Cache-Control": "no-cache"})


@app.get("/api/char/conf")
async def char_conf(v: float = 0, enabled: int = -1, dir: str = "",
                    roi: str = ""):
    """人物匹配阈值 / 开关 / 搜索范围。

    v        阈值（0.30~0.95）
    enabled  0/1 才改开关
    roi      "y1,y2" 归一化上下边界（左右恒为整宽），如 "0.38,0.70"
    dir      已废弃：箭头固定朝上。参数留着是为了老页面不会 422。
    """
    changed = False
    if v:
        char_state["thresh"] = _clamp_thresh(v)
        changed = True
    if enabled in (0, 1):
        char_state["enabled"] = bool(enabled)
        changed = True
    char_state["dir"] = "above"
    if roi:
        parts = [x for x in roi.replace("%", "").split(",") if x.strip() != ""]
        new_roi = None
        if len(parts) == 2:
            try:
                new_roi = _clamp_roi(float(parts[0]), float(parts[1]))
            except (TypeError, ValueError):
                new_roi = None
        if new_roi is not None and new_roi != char_state["roi"]:
            char_state["roi"] = new_roi
            changed = True
            # 范围一变，上一帧的框八成落在新带子外面。清掉它重新搜：
            # 否则「局部跟踪」会一直围着带外的旧位置打转。
            with char_lock:
                char_state["box"] = None
                char_state["pending"] = None
                char_state["guard"] = 0
    if changed:
        _save_char_conf()
        with char_lock:
            char_state["misses"] = 0
    return JSONResponse({"thresh": char_state["thresh"], "enabled": char_state["enabled"],
                         "dir": char_state["dir"], "roi": list(char_state["roi"])})


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
    frame = overlay_char(frame)         # 人物标记跟识别开关无关，始终画（有模板才画）
    jpg = encode(frame, width=(w or None), quality=(q or 92))
    # 响应头只能是 latin-1，所以这里放英文状态；中文说明走 /api/window/focus 的 JSON
    state = "skip" if not focus else ("ok" if focus_ok else "fail")
    return Response(content=jpg, media_type="image/jpeg",
                    headers={"Content-Disposition": "inline; filename=snapshot.jpg",
                             "X-Focus": state})


@app.get("/stream")
async def stream(bars: int = 0, w: int = 0, q: int = 0):
    """MJPEG 实时画面（本地 / 局域网用）。

    w / q 由前端按"实际显示尺寸"给：原始画面 1366x768 每帧 200KB 以上，
    浏览器每帧都要完整解一次，本机同时跑游戏和检测时就会掉帧、看着一顿一顿。
    按显示尺寸推（一般 1000px 左右）+ q85，体积降到 1/3，速度反而提上来。
    """
    async def gen():
        count, t0 = 0, time.perf_counter()
        width = w if 320 <= w <= 4096 else None
        quality = q if 40 <= q <= 95 else JPEG_STREAM_Q
        # 按目标时间戳排帧：以前是"编完再睡 1/30"，编码本身的 5~8ms 白搭进去，
        # 26fps 上不去。改成先算好下一帧该发的时刻，耗时就补差值，能稳在 30。
        nxt = time.perf_counter()
        while True:
            with det_lock:
                ann = det_state["annotated"] if det_state["enabled"] else None
            if ann is not None:
                # annotated 是多帧共享的，要画人物标记得先复制一份，否则多个客户端会叠画
                src = ann.copy() if char_state["box"] is not None else ann
            else:
                src = grabber.snapshot()      # 已经是副本
            src = overlay_char(src)
            if bars:
                src = await asyncio.to_thread(bars_overlay_frame, src)
            # 编解码放线程：cv2 会释放 GIL，别把事件循环堵住（否则血条/统计请求排队）
            jpg = await asyncio.to_thread(encode, src, width, quality)
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            grabber.note_sent()
            count += 1
            dt = time.perf_counter() - t0
            if dt >= 1.0:
                grabber.stream_fps, count, t0 = count / dt, 0, time.perf_counter()
            nxt += 1.0 / DEFAULT_FPS
            delay = nxt - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                nxt = time.perf_counter()      # 落后了就别补帧，重新对齐

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
