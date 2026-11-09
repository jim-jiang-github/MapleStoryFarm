# -*- coding: utf-8 -*-
"""无头全链路自测：合成画面 + 假 YOLO + 录音按键，把服务端功能整个跑一遍。

不依赖：游戏窗口、YOLO 模型（ultralytics 不会被 import）、CH9329 硬件、
真实按键注入（绝不往系统发键）。可以在任何时候跑，包括游戏开着的时候。

用法（以后每次改完 webui/ 都跑一遍，提交前必须全绿）：
    python tools/test_headless.py

原理：
  - MAPLE_DATA_DIR=临时目录  → bars.json / botconf.json / masks.json 等全部
    指到临时目录，跑多少遍都不碰仓库里真实的那几份配置；
  - MAPLE_HEADLESS=1         → 跳过启动时的模型预热线程；
  - 停掉真实抓屏线程，改由测试往 grabber 里塞合成画面（暗底 + 三条状态槽
    + 白字名字牌 + 标记色怪方块）；
  - srv._YOLO 塞一个假 YOLO：按标记色扫描画面返回检测框，格式与真模型一致，
    走的还是真实的检测→去重→跟踪→锁定→显示全链路；
  - game_bot.sender 换成录音器，只记录 (时刻, 键名, 按下/抬起)。

分段：
  [S0] 替身注入与线程接管
  [S1] 静态页与只读 API
  [S2] 血条测量（合成画面 → /api/bars → EXP 记录）
  [S3] 人物模板上传 → 定位 → body_c
  [S4] 检测全链路（假 YOLO → 计数/过滤/门限/稳定帧/屏蔽区）
  [S5] 挂机端到端（真实线程 + 录音按键：技能/群攻偏好/跳放/走路/拾取/喝药链/喝蓝）
  [S6] 配置校验与持久化
  [S7] 真实 HTTP 冒烟（uvicorn 线程 + MJPEG 流）
  [S8] 既有单测套件（test_bot / test_charbox / test_sender / test_detprog / test_hidinput）
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(tempfile.mkdtemp(prefix="maple-headless-"))
# ↓ 必须在 import server 之前设好：paths / 预热线程都在 import 时就定了
os.environ["MAPLE_DATA_DIR"] = str(DATA)
os.environ["MAPLE_HEADLESS"] = "1"

sys.path.insert(0, str(ROOT / "webui"))
import cv2  # noqa: E402
import server as srv  # noqa: E402
import bot as botmod  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

ok = 0
fail = 0


def check(title, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {title}")
    else:
        fail += 1
        print(f"  FAIL {title}" + (f"  <- {detail}" if detail else ""))


def wait_until(cond, timeout=8.0, interval=0.05):
    """轮询等条件成立；到点没成立返回 False（check 交给调用方报细节）。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    return cond()


def _try(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return False


# ---------------- S0 替身注入 ----------------
print("[S0] 替身注入（合成画面 / 假 YOLO / 录音按键）")

srv.find_window = lambda: None          # 全程「没有游戏窗口」，绝不碰真实桌面
srv.grabber.running = False             # 停掉真实抓屏，画面改由测试喂
srv.grabber.join(timeout=2)
check("真实抓屏线程已停", not srv.grabber.is_alive())
srv._game_foreground = lambda: True     # 挂机测试全程视为「游戏在前台」

EVENTS = []
EVENTS_LOCK = threading.Lock()


def recorder(name, down):
    with EVENTS_LOCK:
        EVENTS.append((time.monotonic(), str(name), bool(down)))


srv.game_bot.sender = recorder
check("按键出口换成录音器（不会真按键盘）",
      srv.game_bot.input_info()["mode"] == "test")

# 假 YOLO：按标记色扫描画面。名字带 mob_ 前缀（真实模型也是这么命名的，
# 检测循环里 name.split("_",1)[-1] 剥前缀）。
MOB_BGR = {
    "mob_Blue_Snail": (255, 0, 255),       # 品红
    "mob_Blue_Mushroom": (0, 255, 255),    # 黄
    "mob_Orange_Mushroom": (0, 128, 255),  # 橙
}


class _BoxList:
    def __init__(self, data):
        self.data = data

    def tolist(self):
        return list(self.data)


class _Boxes:
    def __init__(self, xyxy, conf, cls):
        self.xyxy, self.conf, self.cls = _BoxList(xyxy), _BoxList(conf), _BoxList(cls)


class _Result:
    def __init__(self, names, boxes):
        self.names, self.boxes = names, boxes


class FakeYOLO:
    """接口与 ultralytics.YOLO 对齐：_ensure_model 会拿它实例化模型。"""

    def __init__(self, path, task=None):
        self.names = dict(enumerate(MOB_BGR))

    def predict(self, img, **kw):
        xyxy, conf, cls = [], [], []
        for i, color in enumerate(MOB_BGR.values()):
            m = np.all(img == np.array(color, np.uint8), axis=2)
            if not m.any():
                continue
            # 同色的怪可能有好几只：按连通域拆开，一只一个框（整图 min/max
            # 会把两只怪并成一个大框）
            ncc, _, stats, _ = cv2.connectedComponentsWithStats(
                m.astype(np.uint8), connectivity=8)
            for ci in range(1, ncc):
                x, y, w, h, area = stats[ci]
                if area < 40:                 # 抗锯齿碎屑不算怪
                    continue
                xyxy.append([float(x), float(y), float(x + w), float(y + h)])
                conf.append(0.92)
                cls.append(i)
        return [_Result(self.names, _Boxes(xyxy, conf, cls))]


srv._YOLO[0] = FakeYOLO
check("假 YOLO 已注入（真实模型不会被加载）", True)

W, H = 1366, 768
FALLBACK_HUE = {"hp": 1, "mp": 103, "exp": 33}


def _hue_bgr(hue):
    px = np.uint8([[[max(0, min(179, int(hue))), 255, 255]]])
    return tuple(int(v) for v in cv2.cvtColor(px, cv2.COLOR_HSV2BGR)[0, 0])


CHAR_TEXT = "HERO"
CHAR_XY = (600, 400)          # 名字牌文字基线左下角（画面绝对坐标）
CHAR_TW, CHAR_TH = cv2.getTextSize(CHAR_TEXT, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)[0]
CHAR_TPL_PX = (CHAR_XY[0] - 2, CHAR_XY[1] - CHAR_TH - 2,
               CHAR_XY[0] + CHAR_TW + 2, CHAR_XY[1] + 2)   # 上传模板用的框


def make_scene(hp=0.6, mp=0.8, exp=0.3, mobs=(), with_char=True):
    """合成一帧。mobs: [(类名, (x, y, w, h))] 画面绝对坐标。"""
    img = np.full((H, W, 3), 40, np.uint8)
    bars = srv.bar_snapshot()
    for kind, frac in (("hp", hp), ("mp", mp), ("exp", exp)):
        r = bars.get(kind)
        if not r:
            continue
        x1, y1 = int(r["x1"] * W), int(r["y1"] * H)
        x2, y2 = int(r["x2"] * W), int(r["y2"] * H)
        if x2 - x1 < 8 or y2 - y1 < 4:
            continue
        cv2.rectangle(img, (x1 - 3, y1 - 3), (x2 + 3, y2 + 3), (85, 85, 85), -1)
        cv2.rectangle(img, (x1, y1), (x2, y2), (200, 200, 200), -1)
        fw = max(1, int((x2 - x1) * min(1.0, max(0.0, frac))))
        cv2.rectangle(img, (x1, y1), (x1 + fw, y2),
                      _hue_bgr(r.get("hue") or FALLBACK_HUE[kind]), -1)
    if with_char:
        cv2.putText(img, CHAR_TEXT, CHAR_XY, cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 255, 255), 1, cv2.LINE_AA)
    for name, (x, y, w, h) in mobs:
        cv2.rectangle(img, (int(x), int(y)), (int(x + w), int(y + h)),
                      MOB_BGR[name], -1)
    return img


def set_frame(img):
    with srv.grabber.lock:
        srv.grabber.frame = img
        srv.grabber.window_found = True
        srv.grabber.window_minimized = False
        srv.grabber.client_size = (img.shape[1], img.shape[0])
        srv.grabber.last_frame_ts = time.time()
        srv.grabber.error = ""


def det_vis():
    with srv.det_lock:
        return list(srv.det_state["vis"])


def det_counts():
    with srv.det_lock:
        return dict(srv.det_state["counts"])


client = TestClient(srv.app)


def get(path, **kw):
    r = client.get(path, **kw)
    return r.status_code, (r.json() if "json" in r.headers.get("content-type", "")
                           else r.content)


def post(path, payload=None):
    r = client.post(path, json=payload or {})
    return r.status_code, (r.json() if "json" in r.headers.get("content-type", "")
                           else r.content)


def press_keys(evs):
    """事件流里「按下」的键，按时间排序（拾取/方向抬起这些杂项也保留）。"""
    return [n for _, n, d in evs if d]


def wait_press(key, timeout=6.0):
    return wait_until(lambda: any(n == key and d for _, n, d in new_events()),
                      timeout=timeout)


NEW_MARK = [0]


def new_events():
    with EVENTS_LOCK:
        return list(EVENTS[NEW_MARK[0]:])


def mark_events():
    with EVENTS_LOCK:
        NEW_MARK[0] = len(EVENTS)


# ---------------- S1 静态页与只读 API ----------------
print("[S1] 静态页与只读 API")
code, html = get("/")
check("GET / 200", code == 200)
check("首页包含技能面板（skillList）", b"skillList" in html, html[:80] if isinstance(html, bytes) else "")
check("首页不再有旧的攻击距离滑块（rngRange）", b"rngRange" not in html)
code, mhtml = get("/m")
check("GET /m 200", code == 200 and len(mhtml) > 500)
code, ver = get("/api/ver")
check("GET /api/ver 200", code == 200 and isinstance(ver, dict))
code, motion = get("/api/motion")
check("GET /api/motion 200", code == 200 and isinstance(motion, dict))
code, stats = get("/api/stats")
check("GET /api/stats 200 且含 bot_on/window_found",
      code == 200 and "bot_on" in stats and "window_found" in stats)
check("stats.char 带 body_c（技能范围编辑的锚点）",
      "body_c" in stats.get("char", {}))
code, snap = get("/api/snapshot")
check("GET /api/snapshot 返回 JPEG", code == 200 and bytes(snap[:2]) == b"\xff\xd8")
code, focus = get("/api/window/focus")
check("无窗口时 /api/window/focus 报 ok=False",
      code == 200 and focus.get("ok") is False, focus)

# ---------------- S2 血条测量 ----------------
print("[S2] 合成画面 → 血条测量")
set_frame(make_scene(hp=0.60, mp=0.80, exp=0.30))
check("HP 读数 ≈ 60%", wait_until(
    lambda: (get("/api/bars")[1]["values"]["hp"] or 0) and
            abs(get("/api/bars")[1]["values"]["hp"] - 60) < 4),
    detail=str(get("/api/bars")[1]["values"]))
check("MP 读数 ≈ 80%", wait_until(
    lambda: abs((get("/api/bars")[1]["values"]["mp"] or 0) - 80) < 4),
    detail=str(get("/api/bars")[1]["values"]))
check("EXP 读数 ≈ 30%", wait_until(
    lambda: abs((get("/api/bars")[1]["values"]["exp"] or 0) - 30) < 4),
    detail=str(get("/api/bars")[1]["values"]))

set_frame(make_scene(hp=0.25))      # 掉血要立刻反映
check("掉血到 25% 读数跟上（EMA 快降）", wait_until(
    lambda: abs((get("/api/bars")[1]["values"]["hp"] or 0) - 25) < 4, timeout=4),
    detail=str(get("/api/bars")[1]["values"]))

code, expd = get("/api/exp", params={"ms": 60000})
check("EXP 走势记到 30（5 秒一个采样点，等它来）", wait_until(
    lambda: any(abs(p[1] - 30) < 4 for p in
                get("/api/exp", params={"ms": 60000})[1]["points"]),
    timeout=15),
    detail=str(get("/api/exp", params={"ms": 60000})[1]["points"][-3:]))

code, r = post("/api/bars/set", {"kind": "hp", "x1": 0.34, "y1": 0.972,
                                 "x2": 0.46, "y2": 0.994})
check("手框血条 → ok 且采到色相", code == 200 and r.get("ok") is True
      and isinstance(r.get("hue"), (int, float)), r)
code, r = post("/api/bars/reset")
check("POST /api/bars/reset 200", code == 200)
set_frame(make_scene(hp=0.60))
check("重置后仍能读到血量", wait_until(
    lambda: abs((get("/api/bars")[1]["values"]["hp"] or 0) - 60) < 5))

# ---------------- S3 人物定位 ----------------
print("[S3] 人物模板上传 → 定位")
set_frame(make_scene(hp=0.60, mp=0.80, exp=0.30, mobs=()))
tx1, ty1, tx2, ty2 = CHAR_TPL_PX
code, r = post("/api/char/template", {
    "x1": tx1 / W, "y1": ty1 / H, "x2": tx2 / W, "y2": ty2 / H})
check("上传名字牌模板 ok", code == 200 and r.get("ok") is True, r)
check("人物定位到名字牌（score ≥ 0.8 且框落在文字上）", wait_until(
    lambda: (lambda c: c.get("has_tpl") and c.get("box") and c["score"] >= 0.8
             and abs(c["box"][0] - CHAR_XY[0]) < 12
             and abs(c["box"][3] - CHAR_XY[1]) < 12)
            (get("/api/char")[1])))
code, png = get("/api/char/template.png")
check("GET /api/char/template.png 返回 PNG",
      code == 200 and bytes(png[:4]) == b"\x89PNG")

CHAR_BOX = get("/api/char")[1]["box"]
body = botmod.char_body_box(CHAR_BOX)
BCX, BCY = (body[0] + body[2]) / 2, (body[1] + body[3]) / 2
code, stats = get("/api/stats")
check("stats.char.body_c = 身体中心", code == 200 and
      stats["char"]["body_c"] and
      abs(stats["char"]["body_c"][0] - BCX) < 1.5 and
      abs(stats["char"]["body_c"][1] - BCY) < 1.5,
      stats["char"].get("body_c"))


def mob(name, dx, dy, w=40, h=30):
    return (name, (int(BCX + dx - w / 2), int(BCY + dy - h / 2), w, h))


# ---------------- S4 检测全链路 ----------------
print("[S4] 假 YOLO 检测全链路")
post("/api/masks/clear")                       # 临时目录复制来的真实屏蔽区先清掉
get("/api/detect/mask", params={"on": 0})      # 屏蔽区测试放最后单独做
get("/api/detect/stability", params={"v": 2})  # 2 帧稳定就显示，测试节奏快
get("/api/detect/fps", params={"v": 30})

set_frame(make_scene(mobs=[mob("mob_Blue_Snail", 80, 0),
                           mob("mob_Blue_Snail", -120, 0),
                           mob("mob_Blue_Mushroom", 10, -80)]))
code, r = get("/api/detect/toggle")
check("打开识别", code == 200 and r["enabled"] is True, r)
check("3 只怪全部识别（2 蜗牛 + 1 蘑菇）", wait_until(
    lambda: det_counts().get("Blue_Snail") == 2 and
            det_counts().get("Blue_Mushroom") == 1, timeout=12),
    detail=str(det_counts()))
vis = det_vis()
check("检测框和合成位置对得上（第一只蜗牛 x≈BCX+80）",
      any(abs((b["box"][0] + b["box"][2]) / 2 - (BCX + 80)) < 6 for b in vis),
      vis)

get("/api/detect/filter", params={"name": "Mushroom"})
check("类别过滤：只剩蘑菇", wait_until(
    lambda: det_counts() == {"Blue_Mushroom": 1}, timeout=6), str(det_counts()))
get("/api/detect/filter", params={"name": ""})
check("清空过滤：3 只全回来", wait_until(
    lambda: sum(det_counts().values()) == 3, timeout=6), str(det_counts()))

get("/api/detect/conf", params={"v": 0.95})    # 假模型 conf=0.92，门限抬高就没了
check("显示门限 0.95 → 全部滤掉", wait_until(
    lambda: not det_vis(), timeout=6), str(det_counts()))
get("/api/detect/conf", params={"v": 0.5})
check("门限降回 0.5 → 全部回来", wait_until(
    lambda: sum(det_counts().values()) == 3, timeout=6), str(det_counts()))

# 屏蔽区：把第一只蜗牛盖住 → 推理前涂掉，画面上不该再有它
get("/api/detect/mask", params={"on": 1})
cx = (BCX + 80) / W                       # 以怪框中心为遮罩中心，盖全
cy = BCY / H
code, r = post("/api/masks/add", {"x1": cx - 0.03, "y1": cy - 0.06,
                                  "x2": cx + 0.03, "y2": cy + 0.06})
check("添加屏蔽区 ok", code == 200 and r.get("id"), r)
check("屏蔽区里的怪被排除（2 只）", wait_until(
    lambda: sum(det_counts().values()) == 2, timeout=8), str(det_counts()))
mid = r["id"]
code, r = post("/api/masks/delete", {"id": mid})
check("删除屏蔽区 → 3 只回来", code == 200 and wait_until(
    lambda: sum(det_counts().values()) == 3, timeout=8), str(det_counts()))

# ---------------- S5 挂机端到端 ----------------
print("[S5] 挂机端到端（录音按键）")
SKILLS = [
    {"key": "KeyS", "type": "single", "ground": [150, 150, 60, 60],
     "jump": True, "jrect": [150, 150, 220, 140]},
    {"key": "KeyD", "type": "aoe", "ground": [100, 100, 60, 60],
     "jump": False, "jrect": [100, 100, 120, 60]},
]
code, r = post("/api/bot/set", {
    "skills": SKILLS, "pot_key": "Digit1",
    "pot_chain": ["Digit2", "Digit3", "Digit4", "Digit5"],
    "mp_key": "Digit9", "mp_chain": [], "pot_sec": 0.5, "hp_pct": 40, "mp_pct": 30,
    "atk_ms": 200, "pick": False, "patrol": False, "move": True})
check("挂机参数下发 ok（技能表原样保存）",
      code == 200 and [s["key"] for s in r["cfg"]["skills"]] == ["KeyS", "KeyD"], r)

set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30,
                     mobs=[mob("mob_Blue_Snail", 80, 0)]))
check("等检测稳定（1 只蜗牛）", wait_until(
    lambda: sum(det_counts().values()) == 1, timeout=8), str(det_counts()))
mark_events()
code, r = post("/api/bot/set", {"on": True})
check("挂机开启", code == 200 and r["cfg"]["on"] is True)
check("单只怪 → 单体技能 KeyS 出手", wait_press("KeyS"),
      detail=str(new_events()[-12:]))
check("出手前先轻点方向对正（老流程保留）",
      "right" in press_keys(new_events()[:6]), str(new_events()[:8]))

set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30,
                     mobs=[mob("mob_Blue_Snail", 80, 0),
                           mob("mob_Blue_Snail", -90, 0)]))
mark_events()
check("两只怪 → 优先群攻 KeyD", wait_until(
    lambda: "KeyD" in press_keys(new_events()), timeout=8),
    detail=str(new_events()[-12:]))

set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30,
                     mobs=[mob("mob_Blue_Snail", 80, 0)]))
mark_events()
check("回到单只 → 换回单体 KeyS", wait_until(
    lambda: "KeyS" in press_keys(new_events()), timeout=8),
    detail=str(new_events()[-12:]))

# 跳放：怪在跳放框里、地面框外（头顶 140px）
set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30,
                     mobs=[mob("mob_Blue_Snail", 50, -140)]))


def jump_pair():
    evs = new_events()
    for i, (t, n, d) in enumerate(evs):
        if d and n == srv.game_bot.cfg["jump_key"]:
            for t2, n2, d2 in evs[i + 1:]:
                if d2 and n2 == "KeyS":
                    return 0.05 <= t2 - t <= 0.5
    return False


mark_events()
check("跳放连招：跳 → ~100ms → 技能键", wait_until(jump_pair, timeout=10),
      detail=str(new_events()[-16:]))

# 走路：怪在所有技能框之外（右侧 400px），但还在候选带的高度范围内
set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30,
                     mobs=[mob("mob_Blue_Snail", 400, 0)]))
mark_events()
check("范围外的怪 → 按住 right 走过去", wait_until(
    lambda: "right" in press_keys(new_events()), timeout=8),
    detail=str(new_events()[-12:]))

# 拾取
set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30, mobs=[]))
mark_events()
post("/api/bot/set", {"pick": True})
check("拾取开 → Z 连按", wait_until(
    lambda: press_keys(new_events()).count("KeyZ") >= 4, timeout=4),
    detail=str(new_events()[-10:]))
post("/api/bot/set", {"pick": False})

# 喝红药：按了就回血 → 键留用
set_frame(make_scene(hp=0.20, mp=0.80, exp=0.30, mobs=[]))
mark_events()
check("血 20% → 按主药键 Digit1", wait_press("Digit1"),
      detail=str(new_events()[-8:]))
set_frame(make_scene(hp=0.90, mp=0.80, exp=0.30, mobs=[]))
time.sleep(1.6)
check("血涨回来 → 判定喝到，不换键（没有 Digit2..Digit5）",
      not any(k in press_keys(new_events())
              for k in ("Digit2", "Digit3", "Digit4", "Digit5")),
      str(new_events()[-12:]))

# 喝红药：一直不回血 → 顺着链换 Digit1→Digit5
set_frame(make_scene(hp=0.20, mp=0.80, exp=0.30, mobs=[]))
mark_events()
CHAIN = ["Digit1", "Digit2", "Digit3", "Digit4", "Digit5"]


def chain_order():
    seen = []
    for k in press_keys(new_events()):
        if k in CHAIN and (not seen or seen[-1] != k):
            seen.append(k)
    return len(seen) >= 5 and seen[:5] == CHAIN


check("药空了 → 换链 Digit1→Digit2→…→Digit5",
      wait_until(chain_order, timeout=12),
      detail=str([k for k in press_keys(new_events())
                  if k.startswith("Digit")]))
set_frame(make_scene(hp=0.90, mp=0.80, exp=0.30, mobs=[]))

# 喝蓝
set_frame(make_scene(hp=0.90, mp=0.10, exp=0.30, mobs=[]))
mark_events()
check("蓝 10% → 按蓝药键 Digit9", wait_press("Digit9"),
      detail=str(new_events()[-8:]))
set_frame(make_scene(hp=0.90, mp=0.80, exp=0.30, mobs=[]))
time.sleep(1.2)

code, r = post("/api/bot/set", {"on": False})
check("挂机关闭", code == 200 and r["cfg"]["on"] is False)

# ---------------- S6 配置校验与持久化 ----------------
print("[S6] 配置校验与持久化")
code, r = post("/api/bot/set", {"skills": [
    {"key": "NotAKey", "type": "zzz", "ground": [9999, -5, 60, 60],
     "jump": True, "jrect": [100, 100, 200, 60]}] * 9})
sk = r["cfg"]["skills"]
check("技能列表最多 8 个", len(sk) == 8, len(sk))
check("非法键清空 / 非法类型退回 aoe / 范围钳到 0~600",
      sk[0]["key"] == "" and sk[0]["type"] == "aoe"
      and sk[0]["ground"] == [600, 0, 60, 60], sk[0])
code, r = post("/api/bot/set", {"hp_pct": 999, "pot_key": "zzz", "pot_sec": 0})
check("数值钳位 / 非法药键退回默认",
      r["cfg"]["hp_pct"] == 95.0 and r["cfg"]["pot_key"] == "pot8"
      and r["cfg"]["pot_sec"] == 0.5, r["cfg"])

post("/api/bot/set", {"skills": SKILLS})
saved = json.loads((DATA / "botconf.json").read_text(encoding="utf-8"))
check("技能配置已落盘到临时数据目录",
      [s["key"] for s in saved["skills"]] == ["KeyS", "KeyD"], saved.get("skills"))
check("数据目录确实隔离（不在仓库 webui/ 下）",
      DATA.resolve() != (ROOT / "webui").resolve())

# ---------------- S7 真实 HTTP 冒烟 ----------------
print("[S7] 真实 HTTP 冒烟（uvicorn 线程 + MJPEG）")
import requests  # noqa: E402
import uvicorn   # noqa: E402

s = socket.socket()
s.bind(("127.0.0.1", 0))
PORT = s.getsockname()[1]
s.close()
ucfg = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="error")
uv = threading.Thread(target=lambda: uvicorn.Server(ucfg).run(), daemon=True)
uv.start()
base = f"http://127.0.0.1:{PORT}"
check("uvicorn 起来了", wait_until(
    lambda: _try(lambda: requests.get(base + "/api/ver", timeout=1).ok), timeout=10))
r = requests.get(base + "/", timeout=5)
check("真实 GET / 200 且含技能面板", r.ok and "skillList" in r.text)
r = requests.get(base + "/api/snapshot", timeout=5)
check("真实 /api/snapshot 返回 JPEG", r.ok and r.content[:2] == b"\xff\xd8")
chunk = b""
try:
    with requests.get(base + "/stream", stream=True, timeout=6) as rs:
        for piece in rs.iter_content(4096):
            chunk += piece
            if len(chunk) > 48 * 1024:
                break
except requests.exceptions.RequestException:
    pass
check("MJPEG /stream 持续输出 JPEG 帧",
      b"\xff\xd8" in chunk and len(chunk) > 8192, f"{len(chunk)}B")
get("/api/detect/toggle")          # 收尾：识别关掉

# ---------------- S8 既有单测套件 ----------------
print("[S8] 既有单测套件")
for t in ("test_bot.py", "test_charbox.py", "test_sender.py",
          "test_detprog.py", "test_hidinput.py"):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / t)],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    tail = (r.stdout or "").strip().splitlines()[-1:] + (r.stderr or "").strip().splitlines()[-1:]
    check(f"{t} 通过", r.returncode == 0, " | ".join(tail))

print(f"\n{'=' * 46}\n无头自测：{ok} ok / {fail} fail"
      + ("" if fail == 0 else f"  ← 有 {fail} 项没过！"))
sys.exit(1 if fail else 0)
