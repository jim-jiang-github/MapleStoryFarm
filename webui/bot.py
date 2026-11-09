# -*- coding: utf-8 -*-
"""自动打怪（挂机）：读检测结果，决定按哪个键。

为什么单独一个文件：这段代码全是「什么时候按什么」。跟抓屏、推理混在一起，
出问题时分不清是识别错了还是按键节奏错了。这里只负责：拿一份当前状态快照 →
按优先级决定动作 → 发按键。识别、跟踪、血条都在 server.py 里，通过 get_state
回调拿过来。

优先级（每 20ms 一轮）：
  1) 血低于门限 → 喝药（有独立冷却，默认 3 秒）
  2) 全程按 Z 拾取（默认 130ms 一次，无脑一直按）
  3) 有目标且与攻击区域相交（宽 range_px × 高 range_h 的矩形，画面上画的就是
     它，与怪物框有交集才出手）→ 转向 + 攻击（攻击有冷却，默认 420ms）。
     头顶差得不多的怪（<= jump_reach）会先跳起来（Alt）再打。
     range_side=front（单体技能只打身前）时矩形只朝目标那侧展开，且每刀前都
     重新点一次方向键对正——朝向读不到，宁可按一下也不空砍
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
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

try:
    import paths as apppaths            # 和 server.py 共用同一套资源/数据目录
except ImportError:                     # 单独 import 本模块时补一下 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import paths as apppaths

CONF_FILE = apppaths.data_file("botconf.json")

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

# 备用药水键（前端抓的是 KeyboardEvent.code）只认这一批——游戏里真能绑键用的。
CODE_VK = {
    **{f"Key{chr(65 + i)}": 0x41 + i for i in range(26)},        # A..Z
    **{f"Digit{i}": 0x30 + i for i in range(10)},                # 主键盘 0..9
    **{f"Numpad{i}": 0x60 + i for i in range(10)},               # 小键盘 0..9
    **{f"F{i}": 0x70 + i - 1 for i in range(1, 13)},             # F1..F12
    "ShiftLeft": 0xA0, "ShiftRight": 0xA1,
    "ControlLeft": 0xA2, "ControlRight": 0xA3,
    "AltLeft": 0xA4, "AltRight": 0xA5,
    "Space": 0x20, "Enter": 0x0D,
    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22,
    "Semicolon": 0xBA, "Quote": 0xDE, "Comma": 0xBC, "Period": 0xBE,
    "Slash": 0xBF, "Backslash": 0xDC, "BracketLeft": 0xDB, "BracketRight": 0xDD,
    "Minus": 0xBD, "Equal": 0xBB,
}
# 这些键的扫描码不带扩展标志会被游戏当成小键盘那套
CODE_EXTENDED = {"Insert", "Delete", "Home", "End", "PageUp", "PageDown"}


def _key_label(name):
    """状态栏里给人看的键名。"""
    if name in POT_LABEL:
        return POT_LABEL[name]
    if name.startswith("Key"):
        return name[3:]
    if name.startswith("Digit"):
        return name[5:]
    if name.startswith("Numpad"):
        return "小" + name[6:]
    return {"Insert": "Ins", "Delete": "Del", "PageUp": "PgUp",
            "PageDown": "PgDn", "ShiftLeft": "LShift", "ShiftRight": "RShift",
            "ControlLeft": "LCtrl", "ControlRight": "RCtrl",
            "Space": "空格", "Enter": "回车"}.get(name, name)

# 转身后等多久再攻击（毫秒）。方向键和 Ctrl 挨着发出去，游戏会在同一帧里
# 一起处理，结果往往按「转身前」的朝向出招——看着就是朝着反方向挥刀。
TURN_SETTLE_MS = 130
TURN_TAP_MS = 50          # 转身时方向键按住多久：够游戏认一次转向，又不至于真的迈一步
JUMP_COOLDOWN_MS = 900    # 两次跳的最小间隔：起跳+落地+站稳要这么久，比这快就是原地抽搐

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


def _scan_for(name):
    """主键位查表；备用快捷键（KeyboardEvent.code）运行时换算并缓存。"""
    s = _SCAN.get(name)
    if s is None:
        vk = CODE_VK.get(name)
        if vk is None:
            raise KeyError(name)
        s = _SCAN[name] = (int(vk), int(_u32.MapVirtualKeyW(vk, 0)))
    return s


def send_key(name, down):
    """发一次按键（down=True 按下，False 抬起）。名字见 VK 和 CODE_VK。

    返回 SendInput 的返回值：<1 表示系统没接受这次注入（比如被 UIPI 拦了）。
    """
    _, sc = _scan_for(name)
    flags = KEYEVENTF_SCANCODE | (0 if down else KEYEVENTF_KEYUP)
    if name in EXTENDED or name in CODE_EXTENDED:
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
    RETRY_MAX_SEC = 60.0  # 一直连不上就退避到这个间隔，别每 5 秒扫一遍串口

    def __init__(self, mode="auto", port=""):
        self._lock = threading.Lock()
        self.mode = mode
        self.req_port = port or ""      # 配置里要求的串口（可能为空＝自动找）
        self.hid = None                 # 真的连上了才有，端口号从 self.hid.port 读
        self.err = ""
        self._next_try = 0.0
        self._wait = self.RETRY_SEC
        self._retry_thread = None
        self._apply(mode, self.req_port)
        self._start_retry()

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
            self._wait = self.RETRY_SEC
            if mode == "soft":
                return
            try:
                self.hid = self._open_hid()
            except Exception as e:  # noqa: BLE001
                self.err = str(e)
                self._next_try = time.time() + self.RETRY_SEC

    def _start_retry(self):
        if self._retry_thread is not None and self._retry_thread.is_alive():
            return
        self._retry_thread = threading.Thread(
            target=self._retry_loop, daemon=True, name="hid-retry")
        self._retry_thread.start()

    def _backoff(self):
        self._next_try = time.time() + self._wait
        self._wait = min(self.RETRY_MAX_SEC, self._wait * 1.7)

    def _retry_loop(self):
        """在后台补连硬件，绝不放进按键路径。

        没有小板时 `find_port()` 要挨个串口 probe（每个 0.25s 超时），以前是在
        `__call__` 里同步做的——按键一次卡几百毫秒，走位就变成一顿一顿，
        看着就像「软件注入没生效」。现在按键直接走软件，连不连得上以后台为准。
        """
        while True:
            time.sleep(0.5)
            if self.hid is not None or self.mode == "soft":
                continue
            if time.time() < self._next_try:
                continue
            try:
                k = self._open_hid()
            except Exception as e:  # noqa: BLE001
                self.err = str(e)
                self._backoff()
                continue
            with self._lock:
                if self.hid is None:          # 这期间没被别人抢先连上才装进去
                    self.hid, self.err = k, ""
                    self._wait = self.RETRY_SEC
                else:
                    try:
                        k.close()
                    except Exception:  # noqa: BLE001
                        pass

    def configure(self, mode, port):
        """切换出口。配置没变就什么都不做——不能因为改了个血线就重连串口。"""
        port = port or ""
        if mode == self.mode and port == self.req_port:
            return False
        self._apply(mode, port)
        return True

    # ---- 对外 ----
    def __call__(self, name, down):
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
                    self._wait = self.RETRY_SEC
                    self._next_try = time.time() + self.RETRY_SEC
        n = send_key(name, down)
        if n < 1:
            # SendInput 被系统拒了。最常见的原因是游戏跑在管理员权限下，
            # UIPI 不允许低权限进程往它的窗口注入输入——这时候按键一点效果都没有，
            # 必须报出来，不然面板还显示「软件注入」像在正常工作。
            self.err = "软件注入被系统拦下：游戏以管理员身份运行，请用管理员身份启动服务"
            return False, "soft"
        return True, "soft"

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
        return "软件注入（未检测到 CH9329）"



# ---------------- 配置 ----------------
DEFAULTS = {
    "on": False,
    "hp_pct": 40.0,      # 血低于这个百分比就喝药
    "pot_sec": 3.0,      # 两次喝药的最小间隔（秒）
    "pot_key": "pot8",   # 用哪个键喝药：pot8 / pot9 / pot0
    "pot_chain": ["", "", "", ""],
    # ↑ 备用红药键 1~4：主键按了血量没回到线以上（药空了）就按这个顺序换着喝
    "mp_pct": 30.0,      # 蓝低于这个百分比就喝蓝药
    "mp_key": "pot9",    # 用哪个键喝蓝药：pot8 / pot9 / pot0
    "mp_chain": ["", "", "", ""],
    # ↑ 备用蓝药键 1~4：主键按了蓝量没回到线以上就按这个顺序换着喝
    "atk_key": "ControlLeft",   # 攻击键（KeyboardEvent.code 名，面板上可改）
    "jump_key": "AltLeft",      # 跳跃键
    "pick_key": "KeyZ",         # 拾取键
    "range_px": 100.0,   # 攻击区域宽：人物中心左右各这么多像素
    "range_h": 120.0,    # 攻击区域高：以人物身体为中心上下各占一半（画面上画的就是它）
    "range_side": "both",  # 攻击方向：both 左右两侧 / front 只打身前（单体技能）
    "jump_reach": 140.0, # 跳跃可攻击高度：头顶的怪离攻击区不超过它就跳起来打
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
# 药水键兼容旧的 pot8/pot9/pot0（VK 名），新的都用 KeyboardEvent.code 名
_KEY_NAMES = tuple(CODE_VK) + POT_KEYS
ENUMS = {"pot_key": _KEY_NAMES, "mp_key": _KEY_NAMES,
         "atk_key": tuple(CODE_VK), "jump_key": tuple(CODE_VK),
         "pick_key": tuple(CODE_VK),
         "input": ("auto", "hid", "soft"), "range_side": ("both", "front")}

CLAMP = {
    "hp_pct": (10.0, 95.0),
    "mp_pct": (10.0, 95.0),
    "pot_sec": (0.5, 30.0),
    "range_px": (40.0, 600.0),
    "range_h": (40.0, 400.0),
    "jump_reach": (60.0, 300.0),
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
        elif isinstance(dv, list):
            # 药水键链（红/蓝）：最多跟默认一样长，只收认识的键名，不认识的清空
            n = len(dv)
            if isinstance(v, list):
                out[k] = [x if (x == "" or x in CODE_VK or x in VK) else ""
                          for x in (str(s).strip() for s in v)][:n]
                out[k] += [""] * (n - len(out[k]))
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
    for k in ("atk_ms", "pick_ms", "dy_tol", "range_h", "jump_reach"):   # 毫秒/像素：整数更好读
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


def char_body_box(box):
    """名字牌框 -> 人物身体框 [x1,y1,x2,y2]（浮点）。

    这个游戏把名字牌挂在角色**脚下**，身体在牌子上方（实测 16px 高的牌子
    对应约 60px 高的人物，脚底离牌顶 8px），身宽约为身高的 0.9（人形比高瘦，
    bw 是半宽）。换分辨率/缩放时牌子等比变高变矮，所以全部按牌高归一。
    攻击区域、识别框、攻击范围带用的都是这一个函数——改这里就全跟着变了
    （server.py 通过 botmod.char_body_box 引用，别在两边各写一份）。
    """
    x1, y1, x2, y2 = (float(v) for v in box)
    ph = max(8.0, y2 - y1)
    k = ph / 16.0
    body_h = 60.0 * k
    gap = 8.0 * k
    bw = max(10.0, body_h * 0.45)
    cx = (x1 + x2) / 2.0
    return [cx - bw, y1 - gap - body_h, cx + bw, y1 - gap]


def _intersects(a, b):
    """两个 [x1,y1,x2,y2] 框是否有交集（ touching 不算，得真叠上）。"""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def attack_zone(char_box, range_px, range_h, face=0, side="both"):
    """攻击判定区：以人物身体中心为原点、上下各 range_h/2 的矩形。

    横向按攻击方向展开：
      side="both"  左右各 range_px（多数技能，两侧都能打到）
      side="front" 只朝 face 那一侧展开 range_px（单体技能只能打正前方）。
                   face=0（朝向未知）时按两侧算——不知道朝哪就别假装知道，
                   让判定宽一点，等转身后再按单侧精确算。
    server 的 draw_attack_range 画在画面上的就是它——所见即所打。
    """
    b = char_body_box(char_box)
    cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
    hh = float(range_h) / 2.0
    w = float(range_px)
    if side == "front" and face:
        if face > 0:
            return [cx, cy - hh, cx + w, cy + hh]
        return [cx - w, cy - hh, cx, cy + hh]
    return [cx - w, cy - hh, cx + w, cy + hh]


def in_attack_zone(char_box, mon_box, range_px, range_h, face=0, side="both"):
    return _intersects(attack_zone(char_box, range_px, range_h, face, side), mon_box)


def pick_target(char_box, monsters, range_px, range_h, jump_reach, dy_tol):
    """挑一个「值得处理」的目标：带内按水平距离排序，同距取分数高的。

    纵向候选带 = 攻击区域向上扩 jump_reach（跳起来也够得着的层）、向下扩
    dy_tol（下一层：走下平台沿也算一条路）。带外的怪压根不进入决策——
    隔着两层平台的怪当成目标，只会让角色对着空气挥刀或者原地发呆。
    到了带内**打不打**由 _step 的攻击区域矩形判断（头顶够得着的会跳）。
    """
    if not char_box or not monsters:
        return None
    zone = attack_zone(char_box, range_px, range_h)
    v_top = zone[1] - float(jump_reach)
    v_bot = _cy(char_box) + float(dy_tol)
    cx = _cx(char_box)
    best, best_d = None, None
    for m in monsters:
        box = m.get("box")
        if not box or len(box) != 4:
            continue
        if not (box[1] < v_bot and box[3] > v_top):
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
        self._status = {"act": "空闲", "target": "", "hp": None, "face": 0,
                        "atk": 0, "pot": 0, "pick": 0, "jmp": 0, "err": ""}
        self._thread = None
        self._held = 0            # 当前按住的左右方向：-1 / 0 / 1
        self._down = set()        # 当前真的按住没松的键（别对没按的键发抬起）
        self._face = 0            # 上一次转向的方向（攻击朝这一侧）
        self._t_turn = 0.0        # 上一次转身的时刻：转完要等一帧再打，否则打的是旧朝向
        self._was_fg = False      # 上一轮游戏是否在前台（重新拿回焦点时朝向不可信）
        self._t_pot = 0.0
        self._pot_key = None      # 当前在用的药水键（喝空了会顺着备用链往下换）
        self._pot_wait = False    # 刚按过药水键，正在等血量回到线以上
        self._pot_hp0 = 0.0       # 按下那一刻的血量：比它高就算喝到了
        self._t_mpot = 0.0
        self._mp_key = None       # 蓝药侧的状态，和上面三个一一对应
        self._mp_wait = False
        self._mp_mp0 = 0.0
        self._t_atk = 0.0
        self._t_jump = 0.0
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
            # 朝向跟着状态一起出：server 画攻击区域要按真实朝向画单侧，
            # 不然「正前方」模式画出来的框和实际判定对不上。
            self._status["face"] = self._face

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

    POT_VERIFY_MS = 800      # 按下药水键后等多久确认「血量回来没有」

    def _pot_chain(self, cfg):
        """喝药尝试顺序：主键在最前，备用 1~4 只留设了值的，去重。"""
        seen, order = set(), []
        for k in [cfg["pot_key"], *cfg.get("pot_chain", [])]:
            if k and k not in seen:
                seen.add(k)
                order.append(k)
        return order or [cfg["pot_key"]]

    def _mp_chain(self, cfg):
        """喝蓝药尝试顺序：和红药同一套规则。"""
        seen, order = set(), []
        for k in [cfg["mp_key"], *cfg.get("mp_chain", [])]:
            if k and k not in seen:
                seen.add(k)
                order.append(k)
        return order or [cfg["mp_key"]]

    def _pot_step(self, cfg, hp, now):
        """喝药状态机。

        正常：血量 < 血线且过了喝药间隔 → 按当前药水键，进入 0.8s 验证期。
        验证：血量比按下那一刻高（哪怕还在血线下）＝喝到了，键不变；
              到点没涨 ＝ 这一格的药空了 → 立刻换备用键的下一格接着按，
              不等喝药间隔——药空了还等 3 秒人早没了。
        兜底：整条链绕完一圈（含主键全空）就停手，回到正常间隔再从头试。
        """
        chain = self._pot_chain(cfg)
        if self._pot_key not in chain:
            self._pot_key = chain[0]          # 首次 / 配置刚改过
        thr = cfg["hp_pct"]
        press = False
        if self._pot_wait:
            if hp > self._pot_hp0 + 0.3:      # 回血了＝喝到了，键留用
                self._pot_wait = False
                self._set_status(act=f"喝药 {_key_label(self._pot_key)} 生效", err="")
            elif (now - self._t_pot) * 1000.0 >= self.POT_VERIFY_MS:
                self._pot_wait = False
                idx = chain.index(self._pot_key)
                if idx == len(chain) - 1:     # 到 5 号（或只有主键）还没喝上
                    self._pot_key = chain[0]  # 整条链空转了一圈，歇完从头再来
                    self._set_status(act="药水键全按过一轮都没回血，歇一会儿再试", err="药可能喝完了")
                else:
                    self._pot_key = chain[idx + 1]
                    self._set_status(act=f"{_key_label(chain[idx])} 没反应，换 {_key_label(self._pot_key)}", err="")
                    press = True              # 空瓶不等人，立刻按下一格
        elif hp < thr and now - self._t_pot >= cfg["pot_sec"]:
            press = True
        if press:
            self._tap(self._pot_key)
            self._t_pot = now
            self._pot_hp0 = hp
            self._pot_wait = True
            self._count("pot")
            self._set_status(act=f"喝药 {_key_label(self._pot_key)}", err="")

    def _mpot_step(self, cfg, mp, now):
        """喝蓝药状态机：和 _pot_step 同一套状态机，只是量的是蓝、按键是 mp_key。

        正常：蓝量 < 蓝线且过了喝药间隔 → 按当前蓝药键，进入 0.8s 验证期。
        验证：蓝量比按下那一刻高＝喝到了；到点没涨＝这格药空了 → 换备用键接着按。
        兜底：整条链绕完一圈就停手，回到正常间隔再从头试。
        """
        chain = self._mp_chain(cfg)
        if self._mp_key not in chain:
            self._mp_key = chain[0]
        thr = cfg["mp_pct"]
        press = False
        if self._mp_wait:
            if mp > self._mp_mp0 + 0.3:
                self._mp_wait = False
                self._set_status(act=f"喝蓝 {_key_label(self._mp_key)} 生效", err="")
            elif (now - self._t_mpot) * 1000.0 >= self.POT_VERIFY_MS:
                self._mp_wait = False
                idx = chain.index(self._mp_key)
                if idx == len(chain) - 1:
                    self._mp_key = chain[0]
                    self._set_status(act="蓝药键全按过一轮都没回蓝，歇一会儿再试", err="蓝药可能喝完了")
                else:
                    self._mp_key = chain[idx + 1]
                    self._set_status(act=f"{_key_label(chain[idx])} 没反应，换 {_key_label(self._mp_key)}", err="")
                    press = True
        elif mp < thr and now - self._t_mpot >= cfg["pot_sec"]:
            press = True
        if press:
            self._tap(self._mp_key)
            self._t_mpot = now
            self._mp_mp0 = mp
            self._mp_wait = True
            self._set_status(act=f"喝蓝 {_key_label(self._mp_key)}", err="")

    def _step(self, cfg, now=None):
        """跑一轮决策。now 只有测试会传（假时钟），线上留空取真实时间。"""
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

        now = time.time() if now is None else float(now)

        # 1) 喝药：低于血线按当前药水键；按完没见血量涨回来就顺着备用键换
        if hp is not None:
            self._pot_step(cfg, hp, now)

        # 1.5) 喝蓝：低于蓝线按蓝药键，逻辑和红药完全一致
        mp = st.get("mp")
        self._set_status(mp=mp)
        if mp is not None:
            self._mpot_step(cfg, mp, now)

        # 2) 拾取：无脑一直按，跟其它动作不冲突（不同键）
        if cfg["pick"] and (now - self._t_pick) * 1000.0 >= cfg["pick_ms"]:
            self._tap(cfg["pick_key"], 25)
            self._t_pick = now
            self._count("pick")

        char = st.get("char")
        if not char:
            self._set_dir(0)
            self._face = 0            # 人都没定位到，朝向记忆一并作废
            self._set_status(act="找不到人物", target="")
            return

        tgt = pick_target(char, st.get("monsters") or [],
                          cfg["range_px"], cfg["range_h"], cfg["jump_reach"], cfg["dy_tol"])
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

        # 出不出手看攻击区域：画面上那个矩形（宽 range_px × 高 range_h）与怪物
        # 框有交集才打。之前只比水平中心距，上下层的怪水平上很近、实际够不着，
        # 人物就朝空气挥刀。现在拆开看：
        #   水平交不上 → 走过去；
        #   水平挨着、怪物悬在头顶且差得不多（<= jump_reach）→ 跳起来打：跳完
        #     人物升高、攻击区域跟着上移，交上集就走下面的正常攻击流程；
        #   水平挨着但高度交不上（下一层/差太多）→ 站住，走过去也白走。
        #
        # 攻击方向 front（单体技能只能打身前）：矩形只朝目标那一侧展开。判定
        # 用 side（假设已面向目标），绘制用真实朝向（server 读 status["face"]）——
        # 出招前那一步「对正」就是把假设变成事实。
        mode = cfg["range_side"]
        zone = attack_zone(char, cfg["range_px"], cfg["range_h"], side, mode)
        mb = tgt["box"]
        x_hit = zone[0] < mb[2] and mb[0] < zone[2]
        y_hit = zone[1] < mb[3] and mb[1] < zone[3]
        atk_ready = (now - self._t_atk) * 1000.0 >= cfg["atk_ms"]
        settling = (now - self._t_turn) * 1000.0 < TURN_SETTLE_MS
        # 上一次转身之后还没出过手 = 朝向只是「记得对」，不是「刚确认过」。
        # 被击退、复活、你自己手动走两步都会让记忆失准；单侧攻击失准就是空砍，
        # 所以正前方模式下每刀前都重新点一次方向键对正（转身 50ms + 等 130ms
        # 本来就塞得进 420ms 的攻击冷却里，几乎不损攻速）。
        calibrated = self._t_turn > self._t_atk
        if not (x_hit and y_hit):
            if x_hit:
                gap = zone[1] - mb[3]        # >0：怪物整个悬在攻击区上方
                if 0 < gap <= cfg["jump_reach"]:
                    self._set_dir(0)
                    jump_ready = (now - self._t_jump) * 1000.0 >= JUMP_COOLDOWN_MS
                    # 跳起来那一刀也是朝正前方的：正前方模式先对正再起跳，
                    # 别跳起来砍反方向。两侧模式朝向不影响命中，直接跳更干脆。
                    if jump_ready and mode == "front" and (self._face != side
                                                           or not calibrated):
                        self._tap("right" if side > 0 else "left", TURN_TAP_MS)
                        self._face = side
                        self._t_turn = now
                        self._set_status(act="转身朝目标", err="")
                        return
                    if jump_ready:
                        self._tap(cfg["jump_key"])
                        self._t_jump = now
                        self._count("jmp")
                    self._set_status(act="跳起攻击", err="")
                else:
                    self._set_dir(0)
                    self._set_status(act="高度够不着")
            elif cfg["move"]:
                self._set_dir(side)
                self._face = side
                self._set_status(act="走向目标")
            else:
                self._set_dir(0)
                self._set_status(act="目标太远")
            return

        # 进范围了：先站住，再转向目标，然后按节奏攻击
        self._set_dir(0)
        if self._face != side or (mode == "front" and atk_ready and not calibrated):
            # 这个游戏只能靠按方向键转身。轻点一下位置几乎不动，但朝向会变。
            # 转完这一轮**不打**：转身和攻击挨着发，游戏会按旧朝向出招。
            self._tap("right" if side > 0 else "left", TURN_TAP_MS)
            self._face = side
            self._t_turn = now
            self._set_status(act="转身朝目标", err="")
            return
        if settling:
            # 刚转完身，等游戏把朝向渲染出来再出手
            self._set_status(act="等朝向生效")
            return
        if atk_ready:
            self._tap(cfg["atk_key"])
            self._t_atk = now
            self._count("atk")
            self._set_status(act="攻击", err="")
        else:
            self._set_status(act="等冷却")
