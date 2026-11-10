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


# ---------------- S0 前台闸门的口径 ----------------
# 回归：远程桌面最小化/断开时 GetForegroundWindow() 返回 0，以前被当成
# 「不在前台」→ 挂机整段停摆。现在只有「别的窗口真的在前台」才拦。
print("[S0] 前台闸门口径（远程断开不停摆）")

_real_fg = srv._game_foreground
_gw = srv.win32gui.GetForegroundWindow
_ic = srv.win32gui.IsIconic
_iv = srv.win32gui.IsWindowVisible
srv.find_window = lambda: 4321
srv.win32gui.IsWindowVisible = lambda h: True
srv.win32gui.IsIconic = lambda h: False
srv.win32gui.GetForegroundWindow = lambda: 4321
check("前台就是游戏 → 放行", _real_fg() is True)
srv.win32gui.GetForegroundWindow = lambda: 0
check("没人持焦（远程最小化/断开）→ 放行", _real_fg() is True)
srv.win32gui.GetForegroundWindow = lambda: 9999
check("别的窗口在前台 → 拦住（防误注入）", _real_fg() is False)
srv.win32gui.GetForegroundWindow = lambda: 4321
srv.win32gui.IsIconic = lambda h: True
check("游戏最小化 → 拦住（一帧都抓不到）", _real_fg() is False)
srv.find_window = lambda: None
srv.win32gui.GetForegroundWindow, srv.win32gui.IsIconic = _gw, _ic
srv.win32gui.IsWindowVisible = _iv

# ---------------- S0b 局域网地址：虚拟网卡不能当成「手机能用」的入口 ----------------
# 回归：Hyper-V 的 vEthernet（WSL / Default Switch，典型 172.x）、VMware、
# VirtualBox、VPN 的 tun/tap 也都是「本机 IPv4」，gethostbyname_ex 一视同仁
# 吐出来，启动横幅照着打两行，用户还得猜哪个才对——而照着虚拟网卡敲的
# 那个永远连不上。
print("[S0b] 局域网地址滤掉虚拟网卡")
_ni = srv.netinfo
check("虚拟网卡上的地址被判为不可用", not _ni._usable("172.25.16.1", {"172.25.16.1"}))
check("没在虚拟集合里的地址放行", _ni._usable("192.168.31.176", {"172.25.16.1"}))
check("回环地址判掉", not _ni._usable("127.0.0.1", set()))
check("169.254 自立地址判掉（没拿到 DHCP，谁都连不上）",
      not _ni._usable("169.254.10.2", set()))
check("空地址判掉", not _ni._usable("", set()))
check("接口名 vEthernet (Default Switch) 认得出是虚拟的",
      any(h in "vethernet (default switch)" for h in _ni._VIRTUAL_HINTS))
check("接口名 Wi-Fi 不会被误判成虚拟",
      not any(h in "wi-fi" for h in _ni._VIRTUAL_HINTS))
check("lan_ipv4 里不会混进回环 / 自立地址",
      all(not ip.startswith(("127.", "169.254.")) for ip in _ni.lan_ipv4()),
      _ni.lan_ipv4())

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


def make_scene(hp=0.6, mp=0.8, exp=0.3, mobs=(), with_char=True, char_dx=0):
    """合成一帧。mobs: [(类名, (x, y, w, h))] 画面绝对坐标。char_dx：名字牌水平位移。"""
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
        cv2.putText(img, CHAR_TEXT, (CHAR_XY[0] + char_dx, CHAR_XY[1]),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
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
check("人物识别状态行存在（识别出错/多久没认到都直接写在面板上）",
      b'id="charStat"' in html and b'paintCharStat' in html)
check("技能范围是滑块（宽/上/下），数字输入框只允许技能 CD 那一个",
      b'skgrid' in html and b'class="skr"' in html and b'data-a="w"' in html
      and html.count(b'type="number"') == html.count(b'class="skcdv"'))
check("方向改成两种：以人物为中心 / 正前方",
      "以人物为中心".encode() in html and "正前方".encode() in html)
check("卡片里不再画示意图（范围只画在实时画面上，不重复占一块画布）",
      b"skmini" not in html)
check("「以地面范围放大」按钮仍是删掉的状态（两套范围互不派生）",
      b"skgrow" not in html)
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
check("stats.char 带 error / box（面板那句状态话就是靠它）",
      stats["char"].get("error") == "" and stats["char"].get("box") is not None,
      (stats["char"].get("error"), stats["char"].get("box")))

# 识别区不只是搜索窗，还是挂机走位的边界（bot.walk_x_limits）。人一旦被挤到
# 识别区边上、名字牌只剩半个在框内，框内匹配永远不到分——丢人时横向外扩才捞得回来。
print("[S3b] 识别区边上丢了人 → 横向外扩重找")
_roi0 = list(srv.char_state["roi"])
with srv.char_lock:
    srv.char_state["roi"] = [0.50, 0.08, 0.95, 0.90]      # 名字牌中心 0.454 明确在框外
    srv.char_state["box"], srv.char_state["ts"] = None, 0.0
    srv.char_state["misses"] = srv.CHAR_REACQ_MISS + 2
_scene = make_scene(hp=0.99, mp=0.80, exp=0.30)
_box, _sc = srv.find_char(_scene)
check("名字牌落在识别区左边外面 → 外扩后仍能认出",
      _box is not None and _sc >= 0.8, (_box, _sc))
_pad0 = srv.CHAR_LOST_PAD_X
srv.CHAR_LOST_PAD_X = 0.0
_box0, _sc0 = srv.find_char(_scene)
srv.CHAR_LOST_PAD_X = _pad0
check("不外扩就真找不到（证明起作用的就是这一条）", _box0 is None, (_box0, _sc0))
check("左右惩罚只在候选跑出识别区时才扣（框内贴边不再冤枉）",
      srv._hud_penalty([10, 300, 60, 320], W, H, (0.0, 1.0)) == 0.0
      and srv._hud_penalty([10, 300, 60, 320], W, H, (0.20, 1.0)) > 0.0
      and srv._hud_penalty([10, 300, 60, 320], W, H, None) == 0.0)
check("bot 状态里带识别区（走位边界就是按它算的）",
      srv._bot_state().get("roi") == [0.50, 0.08, 0.95, 0.90],
      srv._bot_state().get("roi"))
with srv.char_lock:
    srv.char_state["roi"] = _roi0
    srv.char_state["misses"] = 0

# 上一帧的框跑到识别区外面时，旧代码会切出空矩阵把 morphologyEx 打炸；异常被
# char_loop 吞掉后 misses 不涨、box 不清，下一轮拿同一个坏框再炸一次 —— 永久卡死。
# 现场：识别区右边界 1097，框中心 1204（人走到识别区外了），两分钟不刷新。
print("[S3c] 上一帧的框跑到识别区外 → 不许抛异常，而且要跟得住")
_true_box, _ = srv.find_char(make_scene(hp=0.99, mp=0.80, exp=0.30))
check("先正常认出人（拿真框做基准）", _true_box is not None, _true_box)
with srv.char_lock:
    _roi_bak = list(srv.char_state["roi"])
    srv.char_state["roi"] = [0.02, 0.08, 0.30, 0.90]      # 右边界 410，真框在 620 附近
    srv.char_state["box"] = list(_true_box)
    srv.char_state["misses"] = 0
try:
    _b2, _s2 = srv.find_char(make_scene(hp=0.99, mp=0.80, exp=0.30))
    _raised = None
except Exception as _e:                                   # noqa: BLE001
    _b2, _s2, _raised = None, 0.0, _e
check("框在识别区外时 find_char 不抛异常", _raised is None, _raised)
check("仍然跟得住（搜索块并上了上一帧的框）", _b2 is not None and _s2 >= 0.8, (_b2, _s2))
check("搜索块把识别区外的那只框并进来了",
      srv._search_band((H, W), _true_box)[2] > _true_box[2],
      (srv._search_band((H, W), _true_box), _true_box))
check("空切片只返回 None，不会打炸 morphologyEx",
      srv._local_search(np.full((100, 100), 30, np.uint8), srv.char_state["tpl_w"],
                        [500, 500, 554, 515], 60) is None)
check("跟踪允许掉 0.06 分（名字牌被压住一角），但位置必须在 8px 内",
      srv._track_ok(([100, 100, 154, 115], 0.76), [100, 100, 154, 115], 0.80)
      and not srv._track_ok(([130, 100, 184, 115], 0.76), [100, 100, 154, 115], 0.80)
      and not srv._track_ok(([100, 100, 154, 115], 0.70), [100, 100, 154, 115], 0.80))
with srv.char_lock:
    srv.char_state["roi"] = _roi_bak
    srv.char_state["box"] = None
    srv.char_state["misses"] = 0


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
    "pick": False, "patrol": False, "move": True})
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

# 方向只有两种：全方位 / 身前；旧的固定单边值自动并成身前
post("/api/bot/set", {"skills": [
    {"key": "KeyS", "type": "single", "dir": "left",
     "ground": [150, 150, 60, 60], "jump": False, "jrect": [150, 150, 220, 140]}]})
check("旧值 left → 迁移成 front（没有固定单边的技能），左偏移收成 0",
      srv.game_bot.cfg["skills"][0]["dir"] == "front"
      and srv.game_bot.cfg["skills"][0]["ground"][0] == 0,
      srv.game_bot.cfg["skills"][0])

# 方向：身前单向 → 左右两半边都是感知范围，出手前先转身朝怪
post("/api/bot/set", {"skills": [
    {"key": "KeyS", "type": "single", "dir": "front",
     "ground": [150, 150, 60, 60], "jump": False, "jrect": [150, 150, 220, 140]}]})
set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30,
                     mobs=[mob("mob_Blue_Snail", -80, 0)]))
mark_events()
check("身前技能：左边的怪照打（先点左对正再出 KeyS）", wait_until(
    lambda: "left" in press_keys(new_events())
            and "KeyS" in press_keys(new_events()), timeout=8),
    detail=str(new_events()[-14:]))
post("/api/bot/set", {"skills": SKILLS})

# 朝向跟人物实际位移走：无怪+巡逻关（bot 不发任何键），合成画面里把人物
# 挪动，身前技能的朝向也得跟着翻——用户手动转向时框不能僵在旧的一边
post("/api/bot/set", {"patrol": False})
set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30, mobs=[]))
time.sleep(0.6)
face0 = srv.game_bot.snapshot()[1]["face"]
set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30, mobs=[], char_dx=40))
check("人物右移 40px → 朝向翻成右（face=1）", wait_until(
    lambda: srv.game_bot.snapshot()[1]["face"] == 1, timeout=8),
    detail=f"face0={face0} now={srv.game_bot.snapshot()[1]['face']}")
set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30, mobs=[], char_dx=-50))
check("人物左移 → 朝向翻成左（face=-1）", wait_until(
    lambda: srv.game_bot.snapshot()[1]["face"] == -1, timeout=8),
    detail=str(srv.game_bot.snapshot()[1]))
set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30, mobs=[]))
post("/api/bot/set", {"patrol": True})

# 丢人兜底端到端：画面里没有人物（识别不到）→ 默认锚在画面正中心，
# 靠近中心的怪照打，不整段停摆（窗口都没了才认栽停手）
set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30, with_char=False,
                     mobs=[("mob_Blue_Snail", (700, 350, 40, 30))]))
mark_events()
check("丢人时靠近画面中心的怪照打（中心兜底锚点）", wait_until(
    lambda: "KeyS" in press_keys(new_events()), timeout=10),
    detail=str(new_events()[-12:]))

# 画框回归（用户实测「技能框都没了」的两条根因）：
#   ① 身前技能左右不对称（左30 右170）时，朝左必须整体镜像——不许砍成 30px 窄条
#   ② 挂机关着也得是看得见的颜色——灰线在岩石背景上等于隐形
print("  -- 画框回归：身前技能朝左镜像 / 挂机关着也可见")
_cfg0, _ = srv.game_bot.snapshot()
_flat = np.full((720, 1280, 3), 150, np.uint8)
_box = [600.0, 380.0, 640.0, 392.0]


def _sk_span(face, on):
    srv.game_bot.cfg["on"] = on
    srv.game_bot.cfg["skills"] = [{"key": "KeyS", "type": "single", "dir": "front",
                                   "ground": [0, 200, 60, 60], "jump": False,
                                   "jrect": [0, 200, 100, 60]}]
    srv.game_bot._status["face"] = face
    im = _flat.copy()
    srv.draw_skill_ranges(im, _box)
    xs = np.nonzero(np.any(im != 150, axis=2))[1]
    return None if len(xs) == 0 else (int(xs.min()), int(xs.max()))


_sp_r, _sp_l = _sk_span(1, True), _sk_span(-1, True)
check("朝右框宽 ≈200（人物中心往右 200）",
      _sp_r and 190 <= _sp_r[1] - _sp_r[0] <= 250, _sp_r)
check("朝左框同样宽 ≈200（整体镜像，不是 30px 窄条）",
      _sp_l and 190 <= _sp_l[1] - _sp_l[0] <= 250, _sp_l)
check("朝左的框确实翻到了左边（左边界比朝右更靠左）",
      _sp_l and _sp_r and _sp_l[0] < _sp_r[0] - 100, (_sp_l, _sp_r))
_sp_off = _sk_span(1, False)
check("挂机关着画框依旧可见（不再是灰线隐形）", _sp_off is not None, _sp_off)
srv.game_bot.cfg["on"] = _cfg0["on"]
srv.game_bot.cfg["skills"] = _cfg0["skills"]
srv.game_bot._status["face"] = 0

# 换分辨率 / 换系统缩放：技能范围按 base_w 等比，画框和判定用同一个系数
print("  -- 画框回归：换画面尺寸后技能框等比（base_w）")


def _sk_span_at(w, h, base_w):
    srv.game_bot.cfg["on"] = True
    srv.game_bot.cfg["base_w"] = base_w
    srv.game_bot.cfg["skills"] = [{"key": "KeyS", "type": "single", "dir": "both",
                                   "ground": [100, 100, 60, 60], "jump": False,
                                   "jrect": [100, 100, 100, 60]}]
    im = np.full((h, w, 3), 150, np.uint8)
    bx = [w / 2 - 20, h / 2 - 6, w / 2 + 20, h / 2 + 6]
    srv.draw_skill_ranges(im, bx)
    xs = np.nonzero(np.any(im != 150, axis=2))[1]
    return None if len(xs) == 0 else int(xs.max() - xs.min())


_s1 = _sk_span_at(1280, 720, 1280)
_s2 = _sk_span_at(1920, 1080, 1280)
_s3 = _sk_span_at(1920, 1080, 0)
check("基准宽度下框宽 ≈200（±100）", _s1 and 190 <= _s1 <= 240, _s1)
check("画面放大 1.5 倍后框宽 ≈300（按 base_w 等比）",
      _s1 and _s2 and abs(_s2 - _s1 * 1.5) <= 25, (_s1, _s2))
check("base_w 未知（老配置）时 1:1，框还是 ≈200",
      _s3 and 190 <= _s3 <= 240, _s3)
# 保存那一刻的画面宽度就是基准：前端拖框用的也是当前画面像素，两边天然对齐
_cw = srv.grabber.client_size[0]
_r = post("/api/bot/set", {"skills": [{"key": "KeyS", "type": "aoe", "dir": "both",
                                       "ground": [100, 100, 60, 60], "jump": False}]})
check("保存技能时把当时的画面宽度记成 base_w",
      _cw > 0 and _r[1]["cfg"]["base_w"] == _cw,
      (_cw, _r[1]["cfg"].get("base_w")))
srv.game_bot.cfg["base_w"] = _cfg0.get("base_w", 0)
srv.game_bot.cfg["on"] = _cfg0["on"]
srv.game_bot.cfg["skills"] = _cfg0["skills"]

# 面板上点选某个技能 → 画面上只画它的框（其余先不画，调范围时不被糊住）。
# 选中的是「配置里的第几张卡」，和面板 SK 编号同一个口径——中间夹一张没设
# 键位的卡也占一个编号，不能把后面的顶上来。
print("  -- 画框回归：只画选中的那个技能")


def _sk_focus_span(focus):
    srv.game_bot.cfg["on"] = True
    srv.game_bot.cfg["base_w"] = 1280
    srv.game_bot.cfg["skills"] = [
        {"key": "KeyA", "type": "aoe", "dir": "center", "ground": [100, 100, 60, 60],
         "jump": False, "jrect": [100, 100, 100, 60]},
        {"key": "", "type": "aoe", "dir": "center", "ground": [200, 200, 60, 60],
         "jump": False, "jrect": [200, 200, 100, 60]},     # 没录键位：只占编号，不画
        {"key": "KeyC", "type": "aoe", "dir": "center", "ground": [300, 300, 60, 60],
         "jump": False, "jrect": [300, 300, 100, 60]},
    ]
    with srv.ui_lock:
        srv.ui_state["skill_focus"] = focus
    im = np.full((720, 1280, 3), 150, np.uint8)
    srv.draw_skill_ranges(im, [600, 380, 640, 392])
    xs = np.nonzero(np.any(im != 150, axis=2))[1]
    return None if len(xs) == 0 else (int(xs.min()), int(xs.max()))


_fa_all, _fa_1, _fa_mid, _fa_3 = (_sk_focus_span(-1), _sk_focus_span(0),
                                 _sk_focus_span(1), _sk_focus_span(2))
check("没选中 → 两个有键位的框都画（一直画到 300px 那个）",
      _fa_all and _fa_all[1] - _fa_all[0] > 400, _fa_all)
check("选中第 1 张 → 只剩窄框（跨度 < 250）",
      _fa_1 and _fa_1[1] - _fa_1[0] < 250, _fa_1)
check("选中第 3 张 → 画的是那个宽框（中间那张没键位没串位）",
      _fa_3 and _fa_3[1] - _fa_3[0] > 500 and _fa_3[1] > _fa_1[1] + 100,
      (_fa_1, _fa_3))
check("选中的是没设键位的那张 → 什么都不画（不瞎画别人的）",
      _fa_mid is None, _fa_mid)
check("选中值越界（技能被删了）→ 退回全画",
      _sk_focus_span(9) == _fa_all, _sk_focus_span(9))
with srv.ui_lock:
    srv.ui_state["skill_focus"] = -1
srv.game_bot.cfg["base_w"] = _cfg0.get("base_w", 0)
srv.game_bot.cfg["on"] = _cfg0["on"]
srv.game_bot.cfg["skills"] = _cfg0["skills"]

# 丢人兜底已改：识别不到人物时 bot 不再拿「画面中心」假锚点继续打（改成
# 往识别区中间走回来），所以 ME 标记和技能框都不画——画了只会让人误以为
# 还在按那个位置打
print("  -- 画框回归：丢人时 ME 和技能框都不画（假锚点已废除）")
with srv.char_lock:
    _cb, _ct = srv.char_state["box"], srv.char_state["ts"]
    srv.char_state["box"], srv.char_state["ts"] = None, 0.0
srv.game_bot.cfg["skills"] = [{"key": "KeyS", "type": "single", "dir": "both",
                               "ground": [150, 150, 60, 60], "jump": False,
                               "jrect": [150, 150, 220, 140]}]
im = _flat.copy()
srv.overlay_char(im)
ys, xs = np.nonzero(np.any(im != 150, axis=2))
check("丢人时什么都不画（整帧保持原样）", len(xs) == 0, len(xs))
srv.game_bot.cfg["skills"] = _cfg0["skills"]
with srv.char_lock:
    srv.char_state["box"], srv.char_state["ts"] = _cb, _ct

# 朝向：挂机关着的时候靠「手动走位的位移」观察（server.observe_face，找人线程里
# 一直在跑），身前技能的框跟着翻——用户实测的诉求：按方向键走，框得跟着翻
print("  -- 画框回归：挂机关着手动走，身前技能框跟着翻")
with srv.char_lock:
    _face0, _anc0 = srv.char_state["face"], srv.char_state["face_anchor"]
    srv.char_state["face"], srv.char_state["face_anchor"] = 0, None
srv.game_bot.cfg["on"] = False
srv.game_bot.cfg["base_w"] = 0              # 1:1，数字好核对
srv.game_bot._status["face"] = 0            # bot 那份朝向是停着的旧值，不该被用到
srv.game_bot.cfg["skills"] = [{"key": "KeyS", "type": "single", "dir": "front",
                               "ground": [0, 200, 60, 60], "jump": False,
                               "jrect": [0, 200, 100, 60]}]


def _span_face():
    im = _flat.copy()
    srv.draw_skill_ranges(im, _box)
    xs = np.nonzero(np.any(im != 150, axis=2))[1]
    return None if len(xs) == 0 else (int(xs.min()), int(xs.max()))


srv.observe_face(_box)                                      # 第一帧只立锚点
_f0 = _span_face()
srv.observe_face([_box[0] + 40, _box[1], _box[2] + 40, _box[3]])   # 往右挪 40px
_fr = _span_face()
srv.observe_face([_box[0] - 60, _box[1], _box[2] - 60, _box[3]])   # 再往左挪 100px
_fl = _span_face()
check("朝向还没读到：按朝右画，框只在人物右侧（不再偷偷对称展开）",
      _f0 and _f0[1] - _f0[0] <= 260 and _f0[0] > 560, _f0)
check("往右走 40px → 朝右：框还在右边，不翻",
      _fr and _f0 and abs(_fr[0] - _f0[0]) < 50, (_f0, _fr))
check("往左走 → 朝左：框整体镜像翻到左边", _fl and _fr and _fl[1] < _fr[1] - 80,
      (_fr, _fl))

# 用户真实的配置：方向=正前方 + 宽 140（只够得着身前那半边）。以前镜像只对
# dir=front 生效，这种框朝左时纹丝不动 —— 就是用户看到的「框不跟着朝向」。
srv.game_bot.cfg["skills"] = [{"key": "KeyS", "type": "single", "dir": "front",
                               "ground": [0, 140, 55, 31], "jump": False,
                               "jrect": [0, 153, 103, 42]}]
with srv.char_lock:
    srv.char_state["face"], srv.char_state["face_anchor"] = 0, None
srv.observe_face(_box)
_b0 = _span_face()
srv.observe_face([_box[0] + 40, _box[1], _box[2] + 40, _box[3]])
_br = _span_face()
srv.observe_face([_box[0] - 60, _box[1], _box[2] - 60, _box[3]])
_bl = _span_face()
check("正前方 + 宽140：朝向未知时按朝右画，只往右边伸（不当成两边都够）",
      _b0 and _b0[1] - _b0[0] < 200 and _b0[0] > 560, _b0)
check("正前方 + 宽140：往右走 → 框还在人物右边",
      _br and _b0 and abs(_br[0] - _b0[0]) < 20 and abs(_br[1] - _b0[1]) < 20,
      (_b0, _br))
check("正前方 + 宽140：往左走 → 框整体镜像翻到左边",
      _bl and _br and _bl[1] < _br[1] - 80, (_br, _bl))
srv.game_bot.cfg["skills"] = [{"key": "KeyS", "type": "single", "dir": "front",
                               "ground": [30, 170, 60, 60], "jump": False,
                               "jrect": [30, 170, 100, 60]}]

# 端到端：真靠找人线程发现「名字牌往右挪了」（不是手工喂 observe_face）
with srv.char_lock:
    srv.char_state["face"], srv.char_state["face_anchor"] = 0, None
set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30, char_dx=0))
check("先认出人物、立起朝向观测锚点",
      wait_until(lambda: srv.char_state["face_anchor"] is not None, timeout=8))
set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30, char_dx=45))
check("名字牌往右挪 → 观察到的朝向变成右", wait_until(
    lambda: srv.char_state["face"] == 1, timeout=8), srv.char_state["face"])

with srv.char_lock:
    srv.char_state["face"], srv.char_state["face_anchor"] = _face0, _anc0
srv.game_bot.cfg["base_w"] = _cfg0.get("base_w", 0)
srv.game_bot.cfg["on"] = _cfg0["on"]
srv.game_bot.cfg["skills"] = _cfg0["skills"]

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
check("非法键清空 / 非法类型退回 aoe / 范围钳到 0~600 并收成左右相等",
      sk[0]["key"] == "" and sk[0]["type"] == "aoe" and sk[0]["dir"] == "center"
      and sk[0]["ground"] == [600, 600, 60, 60], sk[0])
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

# ---------------- S9 尺度自愈（窗口尺寸变了） ----------------
print("[S9] 窗口变大 → 尺度重标（用户实测：模板 1366 截的，游戏拉到 2049 名字牌是 1.5 倍）")
set_frame(make_scene(hp=0.99, mp=0.80, exp=0.30))
check("基线：能正常找到人物", wait_until(
    lambda: get("/api/char")[1]["box"] is not None, timeout=8))
_base = get("/api/char")[1]
check("基线：标定宽度 = 当前画面宽 1366", _base["calib_w"] == 1366, _base["calib_w"])
# 整幅画面放大 1.5 倍：名字牌跟着变大，等价于把游戏窗口从 1366 拉到 2049。
# 尺度还停在旧值 → 真实环境里匹配分掉到 0.6、框消失（合成字没那么脆，
# 这里不靠「框丢不丢」触发，直接调 char_loop 里那个自愈函数）
_big = cv2.resize(make_scene(hp=0.99, mp=0.80, exp=0.30),
                  None, fx=1.5, fy=1.5, interpolation=cv2.INTER_LINEAR)
set_frame(_big)
time.sleep(0.4)
# 面板上那个「重标尺度」按钮已经删了（换窗口时这段逻辑本来就会自己跑）：
# 直接调自愈函数，验的还是同一段逻辑
_ok = srv._auto_recalibrate(_big)
check("尺度自愈：窗口变大后重标成功", _ok is True, _ok)
check("尺度换到了 ≈1.5 倍",
      abs(srv.char_state["scale"] - _base["scale"] * 1.5) < 0.12,
      f"base={_base['scale']} new={srv.char_state['scale']}")
check("标定宽度更新成新画面宽", srv.char_state["calib_w"] == int(_big.shape[1]),
      srv.char_state["calib_w"])
check("重标后 ME 框还在", wait_until(
    lambda: get("/api/char")[1]["box"] is not None, timeout=8),
    detail=str(get("/api/char")[1]))
# 窗口尺寸没再变 → 什么都不做（不然每 4 秒一轮全尺度扫描，白烧 CPU）
_sc = srv.char_state["scale"]
check("窗口没再变时不去瞎扫（返回 False、尺度不动）",
      srv._auto_recalibrate(_big) is False and srv.char_state["scale"] == _sc,
      srv.char_state["scale"])

# ---------------- S10 抓屏优先级：窗口直读 > 抓屏幕 ----------------
print("[S10] 抓屏优先级：先读游戏窗口绘制表面（原生像素），失败才退回抓屏幕")
class _FakeSct:
    """假装是 mss 的抓屏对象：返回什么由测试说了算。"""
    def __init__(self, img=None, boom=False):
        self.img, self.boom, self.calls = img, boom, 0
    def grab(self, region):
        self.calls += 1
        if self.boom:
            raise RuntimeError("BitBlt: Access is denied.")
        return self.img.copy()

_REGION = {"left": 0, "top": 0, "width": 1366, "height": 768}
_real_grab_window = srv.grab_window
try:
    # ① 窗口直读成功 → 用它，且客户区尺寸跟着帧走（不是 GetClientRect 的值）
    srv.grab_window = lambda hwnd: np.full((768, 1366, 3), 40, np.uint8)
    g = srv.grabber
    g.win_fails, g.screen_until = 0, 0.0
    fake = _FakeSct(np.full((768, 2049, 3), 90, np.uint8))
    got = g._grab(fake, 1, _REGION)
    check("窗口直读成功时优先用它", got is not None and got[1] == "window", got and got[1])
    check("窗口直读时不去抓屏幕", fake.calls == 0, fake.calls)
    check("帧尺寸来自帧本身（原生像素）", got[0].shape[:2] == (768, 1366), got[0].shape)

    # ② 窗口直读抛异常 →退回抓屏幕，帧尺寸换成屏幕抓到的
    def _boom(hwnd):
        raise RuntimeError("BitBlt 失败（错误码 5）")
    srv.grab_window = _boom
    g.win_fails, g.screen_until = 0, 0.0
    fake = _FakeSct(np.full((1152, 2049, 3), 90, np.uint8))
    got = g._grab(fake, 1, _REGION)
    check("窗口直读失败时退回抓屏幕", got is not None and got[1] == "screen", got and got[1])
    check("退回时用的是屏幕帧的尺寸", got[0].shape[:2] == (1152, 2049), got[0].shape)

    # ③ 窗口全黑（游戏没在渲染）不能当有效帧
    srv.grab_window = lambda hwnd: np.zeros((768, 1366, 3), np.uint8)
    g.win_fails, g.screen_until = 0, 0.0
    fake = _FakeSct(np.full((768, 1366, 3), 90, np.uint8))
    got = g._grab(fake, 1, _REGION)
    check("窗口画面全黑时不当真，改用屏幕", got is not None and got[1] == "screen",
          got and got[1])

    # ④ 两条路都失败 → 返回 None，且错误信息里两条原因都在
    srv.grab_window = _boom
    g.win_fails, g.screen_until = 0, 0.0
    got = g._grab(_FakeSct(boom=True), 1, _REGION)
    check("两条路都失败时返回 None", got is None, got)
    check("错误信息里同时写了窗口和屏幕的原因",
          "窗口" in g.error and "屏幕" in g.error and "Access is denied" in g.error,
          g.error)
finally:
    srv.grab_window = _real_grab_window
    srv.grabber.win_fails, srv.grabber.screen_until = 0, 0.0
    srv.grabber.error = ""

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
