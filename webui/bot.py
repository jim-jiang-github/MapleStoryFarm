# -*- coding: utf-8 -*-
"""自动打怪（挂机）：读检测结果，决定按哪个键。

为什么单独一个文件：这段代码全是「什么时候按什么」。跟抓屏、推理混在一起，
出问题时分不清是识别错了还是按键节奏错了。这里只负责：拿一份当前状态快照 →
按优先级决定动作 → 发按键。识别、跟踪、血条都在 server.py 里，通过 get_state
回调拿过来。

优先级（每 20ms 一轮）：
  1) 血低于门限 → 喝药（有独立冷却，默认 3 秒）
  2) 全程按 Z 拾取（默认 130ms 一次，无脑一直按）
  3) 有目标且在攻击距离内 → 转向 + 攻击（攻击有冷却，默认 420ms）
  4) 有目标但在范围外 → 朝它走（只按左右，这个场景没有纵向移动）
  5) 视野内没目标 → 左右巡逻（可选，默认开）

安全前提，必须遵守：
  **只有游戏窗口在前台时才发按键。** 否则等于往当前获得焦点的窗口里打字/点鼠标。
  一旦失焦立刻松开所有按住的键，避免方向键卡在按下状态导致角色一直跑。

按键出口有两个（见 Sender）：
  hid  = CH9329F 硬件（串口转 USB HID），目标机收到的是真实 USB 键盘报文；
  soft = SendInput + 扫描码（KEYEVENTF_SCANCODE），系统里带 LLKHF_INJECTED 标记。
游戏多半走 DirectInput/Raw Input，读的是扫描码 —— PostMessage 那种窗口消息它不看；
而带反外挂的游戏还会把带注入标记的按键认出来。所以优先硬件。
"""
from __future__ import annotations

import ctypes
import json
import threading
import time
from ctypes import wintypes
from pathlib import Path

CONF_FILE = Path(__file__).with_name("botconf.json")

# ---------------- 键位 ----------------
# 用户的设置：Ctrl 攻击、Alt 跳、方向键（只会用到左右）、8/9/0 喝药、Z 拾取。
# 这里存虚拟键码（VK），扫描码运行时用 MapVirtualKeyW 换，免得手抄扫描码抄错。
VK = {
    "attack": 0x11,   # Ctrl
    "jump": 0x12,     # Alt
    "left": 0x25,
    "right": 0x27,
    "pick": 0x5A,     # Z
    "pot8": 0x38,
    "pot9": 0x39,
    "pot0": 0x30,
}
# 方向键是「扩展键」，不加 KEYEVENTF_EXTENDEDKEY 的话游戏收到的是小键盘那一套
EXTENDED = {"left", "right"}
POT_KEYS = ("pot8", "pot9", "pot0")
POT_LABEL = {"pot8": "8", "pot9": "9", "pot0": "0"}

# 转身后等多久再攻击（毫秒）。方向键和 Ctrl 挨着发出去，游戏会在同一帧里
# 一起处理，结果往往按「转身前」的朝向出招——看着就是朝着反方向挥刀。
TURN_SETTLE_MS = 130
TURN_TAP_MS = 50          # 转身时方向键按住多久：够游戏认一次转向，又不至于真的迈一步

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

_u32 = ctypes.WinDLL("user32", use_last_error=True)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _scan_of(name):
    vk = VK[name]
    sc = _u32.MapVirtualKeyW(vk, 0)      # MAPVK_VK_TO_VSC
    return int(vk), int(sc)


_SCAN = {name: _scan_of(name) for name in VK}


def send_key(name, down):
    """发一次按键（down=True 按下，False 抬起）。名字见 VK。

    返回 SendInput 的返回值：<1 表示系统没接受这次注入（比如被 UIPI 拦了）。
    """
    _, sc = _SCAN[name]
    flags = KEYEVENTF_SCANCODE | (0 if down else KEYEVENTF_KEYUP)
    if name in EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = _INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(0, sc, flags, 0, 0)))
    return _u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


# ---------------- 按键出口 ----------------
class Sender:
    """把按键发出去，优先走硬件。

    hid  = CH9329F（串口转 USB HID）：对目标机就是一个普通 USB 键盘，
           没有注入标记，DirectInput / 反外挂都当真人操作。
    soft = SendInput：不用额外硬件，但按键带 LLKHF_INJECTED，可能被游戏无视或识别。
    auto = 有硬件用硬件；找不到硬件、或者运行中掉线，就回落到软件注入并记下原因。

    掉线回落是刻意的：宁可降级继续跑，也不要整个脚本哑掉。面板会显示当前实际在用哪个。
    """

    RETRY_SEC = 5.0      # 硬件掉线后多久再试着连一次

    def __init__(self, mode="auto", port=""):
        self._lock = threading.Lock()
        self.mode = mode
        self.req_port = port or ""      # 配置里要求的串口（可能为空＝自动找）
        self.hid = None                 # 真的连上了才有，端口号从 self.hid.port 读
        self.err = ""
        self._next_try = 0.0
        self._apply(mode, self.req_port)

    # ---- 硬件 ----
    def _open_hid(self):
        import hidinput                      # 延迟导入：没有 pyserial 也能跑软件注入
        p = self.req_port or hidinput.find_port()
        if not p:
            raise hidinput.HidUnavailable("没有串口应答 CH9329（小板没插？驱动没装？）")
        k = hidinput.HidKeyboard(p)
        try:
            k.info()                         # 确认对面真的是 CH9329，而不是碰巧占用的串口
        except Exception:
            k.close()
            raise
        return k

    def _apply(self, mode, port):
        with self._lock:
            if self.hid is not None:
                try:
                    self.hid.close()         # 关之前会补一帧全零，别把键留在按下状态
                except Exception:  # noqa: BLE001
                    pass
                self.hid = None
            self.mode, self.req_port = mode, port or ""
            self.err = ""
            if mode == "soft":
                return
            try:
                self.hid = self._open_hid()
            except Exception as e:  # noqa: BLE001
                self.err = str(e)
                self._next_try = time.time() + self.RETRY_SEC

    def _maybe_retry(self):
        if self.mode == "soft" or self.hid is not None:
            return
        if time.time() < self._next_try:
            return
        try:
            k = self._open_hid()
        except Exception as e:  # noqa: BLE001
            self.err = str(e)
            self._next_try = time.time() + self.RETRY_SEC
            return
        with self._lock:
            self.hid, self.err = k, ""

    def configure(self, mode, port):
        """切换出口。配置没变就什么都不做——不能因为改了个血线就重连串口。"""
        port = port or ""
        if mode == self.mode and port == self.req_port:
            return False
        self._apply(mode, port)
        return True

    # ---- 对外 ----
    def __call__(self, name, down):
        if self.hid is None:
            self._maybe_retry()
        k = self.hid
        if k is not None:
            try:
                k(name, down)
                return True, "hid"
            except Exception as e:  # noqa: BLE001
                # 硬件没了（拔线/串口被抢）。回落软件注入，并记下来让面板看得见。
                with self._lock:
                    self.err = f"硬件断开，已回落软件注入: {e}"
                    try:
                        k.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self.hid = None
                    self._next_try = time.time() + self.RETRY_SEC
        return send_key(name, down) >= 1, "soft"

    def release_all(self):
        k = self.hid
        if k is not None:
            try:
                k.release_all()
            except Exception:  # noqa: BLE001
                pass

    def describe(self):
        """面板上显示「现在到底在用哪个出口」。"""
        if self.hid is not None:
            return f"硬件 {self.hid.port}"
        if self.mode == "soft":
            return "软件注入"
        return "软件注入（硬件不可用）"



# ---------------- 配置 ----------------
DEFAULTS = {
    "on": False,
    "hp_pct": 60.0,      # 血低于这个百分比就喝药
    "pot_sec": 3.0,      # 两次喝药的最小间隔（秒）
    "pot_key": "pot8",   # 用哪个键喝药：pot8 / pot9 / pot0
    "range_px": 160.0,   # 攻击距离：人物中心到怪物中心的水平像素
    "atk_ms": 420,       # 两次攻击的最小间隔（毫秒）
    "pick": True,        # 全程按 Z 拾取
    "pick_ms": 130,      # 拾取按键间隔（毫秒）
    "patrol": True,      # 视野里没怪时左右来回走
    "patrol_sec": 2.5,   # 巡逻换向间隔（秒）
    "move": True,        # 怪在攻击距离外时走过去
    "dy_tol": 150,       # 纵向容差：同一条平台上的怪才算目标（像素）
    "input": "auto",     # 按键出口：auto 优先硬件 / hid 只认硬件 / soft 只用软件注入
    "hid_port": "",      # 硬件串口，留空自动扫描（CH9329 通常挂在 CH343 那个口上）
}

# 字符串型配置的合法取值，非法就退回默认，免得写进一个谁都不认识的模式
ENUMS = {"pot_key": POT_KEYS, "input": ("auto", "hid", "soft")}

CLAMP = {
    "hp_pct": (10.0, 95.0),
    "pot_sec": (0.5, 30.0),
    "range_px": (40.0, 600.0),
    "atk_ms": (120, 3000),
    "pick_ms": (60, 2000),
    "patrol_sec": (0.5, 20.0),
    "dy_tol": (40, 400),
}


def clamp_cfg(raw):
    """整理一份配置：非法值退回默认，数值钳到合理区间。"""
    out = dict(DEFAULTS)
    for k, dv in DEFAULTS.items():
        if k not in raw:
            continue
        v = raw[k]
        if isinstance(dv, bool):
            out[k] = bool(v)
        elif isinstance(dv, str):
            if k == "hid_port":
                out[k] = str(v or "").strip().upper()[:16]
            else:
                out[k] = v if v in ENUMS.get(k, ()) else dv
        else:
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v != v:                      # NaN
                continue
            lo, hi = CLAMP.get(k, (dv, dv))
            out[k] = round(min(hi, max(lo, v)), 2)
    for k in ("atk_ms", "pick_ms", "dy_tol"):   # 毫秒/像素：整数更好读
        out[k] = int(out[k])
    return out


def load_cfg():
    if CONF_FILE.exists():
        try:
            return clamp_cfg(json.loads(CONF_FILE.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            print(f"[bot] 读取 {CONF_FILE.name} 失败，用默认值: {e}")
    return dict(DEFAULTS)


def save_cfg(cfg):
    try:
        CONF_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[bot] 写 {CONF_FILE.name} 失败: {e}")


# ---------------- 决策 ----------------
def _cx(box):
    return (float(box[0]) + float(box[2])) / 2


def _cy(box):
    return (float(box[1]) + float(box[3])) / 2


def pick_target(char_box, monsters, range_px, dy_tol):
    """挑一个目标：先在纵向容差内按水平距离排序，同等距离时取分数高的。

    纵向容差是为了不隔着平台打怪——只按左右走位，上下层的怪根本够不着，
    把它们算成目标只会让角色在原地朝一堵墙挥刀。
    """
    if not char_box or not monsters:
        return None
    cx, cy = _cx(char_box), _cy(char_box)
    best, best_d = None, None
    for m in monsters:
        box = m.get("box")
        if not box or len(box) != 4:
            continue
        dy = abs(_cy(box) - cy)
        if dy > dy_tol:
            continue
        d = abs(_cx(box) - cx)
        score = (d, -float(m.get("conf", 0)))
        if best_d is None or score < best_d:
            best, best_d = m, score
    return best


class Bot:
    """一个线程 + 一份配置 + 一份状态。

    get_state() 由 server.py 提供，返回：
        {"fg": bool,            # 游戏窗口是否在前台
         "hp": float | None,    # 血量百分比 0~100，读不到就是 None
         "char": [x1,y1,x2,y2] | None,      # 人物名字牌框（全分辨率像素）
         "monsters": [{"name","box","conf"}]}
    按键通过 self.sender(name, down) 发，测试时可以换成记录器。
    """

    TICK = 0.02          # 主循环节拍
    TAP_MS = 55          # 「点一下」的按住时长：够游戏识别成一次按键，又不至于走出一步

    def __init__(self, get_state, sender=None):
        self.get_state = get_state
        self.cfg = load_cfg()
        # sender 传进来时是测试用的假出口（只记录事件，不发真按键）；
        # 不传就按配置开真出口：优先 CH9329 硬件，连不上才软件注入。
        self.sender = sender or Sender(self.cfg["input"], self.cfg["hid_port"])
        self._lock = threading.Lock()
        self._status = {"act": "空闲", "target": "", "hp": None,
                        "atk": 0, "pot": 0, "pick": 0, "err": ""}
        self._thread = None
        self._held = 0            # 当前按住的左右方向：-1 / 0 / 1
        self._down = set()        # 当前真的按住没松的键（别对没按的键发抬起）
        self._face = 0            # 上一次转向的方向（攻击朝这一侧）
        self._t_turn = 0.0        # 上一次转身的时刻：转完要等一帧再打，否则打的是旧朝向
        self._was_fg = False      # 上一轮游戏是否在前台（重新拿回焦点时朝向不可信）
        self._t_pot = 0.0
        self._t_atk = 0.0
        self._t_pick = 0.0
        self._t_patrol = 0.0
        self._patrol_dir = 1

    # ---- 对外 ----
    def snapshot(self):
        with self._lock:
            return dict(self.cfg), dict(self._status)

    def update_cfg(self, patch, save=True):
        with self._lock:
            merged = dict(self.cfg)
            merged.update(patch or {})
            self.cfg = clamp_cfg(merged)
            cfg = dict(self.cfg)
        if save:
            save_cfg(cfg)
        # 换按键出口：先把手上的键松开（硬件那边关串口时会补一帧全零，
        # 软件注入的键得自己抬），再重连。测试用的假出口没有 configure，就跳过。
        if hasattr(self.sender, "configure"):
            self.release_all()
            try:
                self.sender.configure(cfg["input"], cfg["hid_port"])
            except Exception as e:  # noqa: BLE001
                self._set_status(err=f"切换输入方式失败: {e}")
        return cfg

    def input_info(self):
        """面板上「按键出口」那几行：现在实际用的是什么、为什么退到软件。"""
        s = self.sender
        if not hasattr(s, "describe"):
            return {"now": "测试替身", "err": "", "mode": "test", "port": ""}
        return {"now": s.describe(), "err": s.err, "mode": s.mode,
                "port": s.hid.port if s.hid is not None else s.req_port}

    def set_on(self, on, save=True):
        on = bool(on)
        self.update_cfg({"on": on}, save=save)
        if on:
            self._face = 0            # 刚开：不知道角色现在朝哪，第一下必须转身
            self._t_turn = 0.0
            self._ensure_thread()
        else:
            self.release_all()
        return on

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="bot", daemon=True)
        self._thread.start()

    # ---- 按键封装 ----
    def _press(self, name):
        self.sender(name, True)
        self._down.add(name)

    def _release(self, name):
        # 只抬真的按着的键：失焦时每轮都会调一次 release_all，无脑发抬起会有两个坏处
        # ——每 20ms 空转 8 次 SendInput，更糟的是你在别的窗口按着 Ctrl，会被它按掉。
        if name in self._down:
            self._down.discard(name)
            self.sender(name, False)

    def _tap(self, name, ms=None):
        self._press(name)
        time.sleep((ms or self.TAP_MS) / 1000.0)
        self._release(name)

    def _set_dir(self, want):
        """把「按住的左右」调整到 want（-1 左 / 0 松 / 1 右）。"""
        if want == self._held:
            return
        if self._held == -1:
            self._release("left")
        elif self._held == 1:
            self._release("right")
        self._held = 0
        if want == -1:
            self._press("left")
        elif want == 1:
            self._press("right")
        self._held = want

    def release_all(self):
        for name in list(self._down):
            self.sender(name, False)
            self._down.discard(name)
        self._held = 0

    # ---- 状态 ----
    def _set_status(self, **kw):
        with self._lock:
            self._status.update(kw)

    def _count(self, key):
        with self._lock:
            self._status[key] += 1

    # ---- 主循环 ----
    def _loop(self):
        while True:
            with self._lock:
                cfg = dict(self.cfg)
            if not cfg["on"]:
                self.release_all()
                self._set_status(act="已停止")
                time.sleep(0.1)
                continue
            try:
                self._step(cfg)
            except Exception as e:  # noqa: BLE001
                # 单帧出错不能让整个线程死掉：松键、记下来、下一轮继续
                self.release_all()
                self._set_status(act="出错", err=f"{type(e).__name__}: {e}")
            time.sleep(self.TICK)

    def _step(self, cfg):
        st = self.get_state() or {}
        hp = st.get("hp")
        self._set_status(hp=hp)
        if hp is not None and hp <= 0:
            # 死了：复活点出生时朝向右，之前的朝向记忆不可信
            self._face = 0

        # 失焦：绝对不能发按键（会打进别的窗口），同时把按住的键松开
        if not st.get("fg"):
            self.release_all()
            self._was_fg = False
            self._set_status(act="游戏不在前台", target="", err="")
            return
        if not self._was_fg:
            # 重新拿回焦点：这期间你可能手动走过、转过，朝向记忆作废，下一轮先转身
            self._was_fg = True
            self._face = 0

        now = time.time()

        # 1) 喝药：读不到血量就不动，别蒙着眼睛按
        if hp is not None and hp < cfg["hp_pct"] and now - self._t_pot >= cfg["pot_sec"]:
            self._tap(cfg["pot_key"])
            self._t_pot = now
            self._count("pot")
            self._set_status(act=f"喝药 {POT_LABEL[cfg['pot_key']]}", err="")

        # 2) 拾取：无脑一直按，跟其它动作不冲突（不同键）
        if cfg["pick"] and (now - self._t_pick) * 1000.0 >= cfg["pick_ms"]:
            self._tap("pick", 25)
            self._t_pick = now
            self._count("pick")

        char = st.get("char")
        if not char:
            self._set_dir(0)
            self._face = 0            # 人都没定位到，朝向记忆一并作废
            self._set_status(act="找不到人物", target="")
            return

        tgt = pick_target(char, st.get("monsters") or [], cfg["range_px"], cfg["dy_tol"])
        if not tgt:
            self._set_status(target="")
            if cfg["patrol"]:
                if now - self._t_patrol >= cfg["patrol_sec"]:
                    self._patrol_dir = -self._patrol_dir
                    self._t_patrol = now
                self._set_dir(self._patrol_dir)
                self._face = self._patrol_dir
                self._set_status(act="巡逻找怪")
            else:
                self._set_dir(0)
                self._set_status(act="守株待兔")
            return

        name = tgt.get("name", "?")
        dx = _cx(tgt["box"]) - _cx(char)
        side = 1 if dx >= 0 else -1
        dist = abs(dx)
        self._set_status(target=f"{name} {int(dist)}px")

        if dist > cfg["range_px"]:
            if cfg["move"]:
                self._set_dir(side)
                self._face = side
                self._set_status(act="走向目标")
            else:
                self._set_dir(0)
                self._set_status(act="目标太远")
            return

        # 进范围了：先站住，再转向目标，然后按节奏攻击
        self._set_dir(0)
        if self._face != side:
            # 这个游戏只能靠按方向键转身。轻点一下位置几乎不动，但朝向会变。
            # 转完这一轮**不打**：转身和攻击挨着发，游戏会按旧朝向出招。
            self._tap("right" if side > 0 else "left", TURN_TAP_MS)
            self._face = side
            self._t_turn = now
            self._set_status(act="转身朝目标", err="")
            return
        if (now - self._t_turn) * 1000.0 < TURN_SETTLE_MS:
            # 刚转完身，等游戏把朝向渲染出来再出手
            self._set_status(act="等朝向生效")
            return
        if (now - self._t_atk) * 1000.0 >= cfg["atk_ms"]:
            self._tap("attack")
            self._t_atk = now
            self._count("atk")
            self._set_status(act="攻击", err="")
        else:
            self._set_status(act="等冷却")
