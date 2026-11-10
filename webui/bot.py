# -*- coding: utf-8 -*-
"""自动打怪（挂机）：读检测结果，决定按哪个键。

为什么单独一个文件：这段代码全是「什么时候按什么」。跟抓屏、推理混在一起，
出问题时分不清是识别错了还是按键节奏错了。这里只负责：拿一份当前状态快照 →
按优先级决定动作 → 发按键。识别、跟踪、血条都在 server.py 里，通过 get_state
回调拿过来。

优先级（每 20ms 一轮）：
  1) 血低于门限 → 喝药（有独立冷却，默认 3 秒）
  2) 全程按拾取键（默认 130ms 一次，无脑一直按）
  3) 技能选择：按列表顺序（= 优先级）逐个算判定框（skill_rects，画面上画的
     就是它们，与怪物框有交集才算罩到）。怪 ≥2 优先群攻、只有 1 只优先单体。
     地面框罩到 → 转向 + 按技能键（默认 420ms 冷却）；只在跳放框里 →
     跳（Alt）→ 100ms → 技能键。范围只盖一边的技能每刀前重新点一次方向键
     对正——朝向读不到，宁可按一下也不空放
  4) 有目标但所有技能都罩不到 → 朝它走（只按左右，这个场景没有纵向移动）
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
MOVE_FACE_PX = 10         # 朝向观测阈值：身体中心往一边累计挪过这么多像素 = 朝那边
                         # （模板匹配的抖动就 ±几像素，凑不够这个数，不会误翻）
JUMP_CAST_MS = 100        # 跳放连招：按下跳键后隔这么久再按技能键（起跳到能出手的手感值）

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
    "jump_key": "AltLeft",      # 跳跃键（跳放连招用）
    "pick_key": "KeyZ",         # 拾取键
    # 技能列表：顺序 = 释放优先级（面板上 ⠿ 拖动排序）。
    #   key    快捷键（KeyboardEvent.code 名，空 = 还没录，决策时跳过）
    #   type   aoe 群攻 / single 单体：范围内怪 ≥2 优先群攻，只有 1 只优先单体，
    #          同类型里排前面的先用
    #   dir    方向，只有两种：center 以人物为中心（左右对称，人物两侧都打，
    #          出手前不用转身）/ front 正前方（从人物身体中心往前伸，只打面前
    #          那半边，出手前先转身对正）。两种都跟着人物朝向翻。
    #   ground 地面攻击范围 [左, 右, 上, 下]：相对人物身体中心的像素偏移。
    #          center 恒有 左=右（面板上只填「宽」，宽/2 就是每一边）；front 恒有
    #          左=0（面板上填的「宽」就是从人物中心往前多少）。这里存的是规范
    #          形态，面板只是换了个更不容易填反的说法。
    #   jump   可跳放（空中施放）；jrect 跳放范围：怪在 jrect 内、ground 外
    #          就按「跳 → 100ms → 技能键」的连招打
    "skills": [
        {"key": "ControlLeft", "type": "aoe", "dir": "center",
         "ground": [100, 100, 60, 60],
         "jump": True, "jrect": [100, 100, 200, 60]},
    ],
    "atk_ms": 420,       # 两次放技能的最小间隔（毫秒）
    "pick": True,        # 全程按 Z 拾取
    "pick_ms": 130,      # 拾取按键间隔（毫秒）
    "patrol": True,      # 视野里没怪时左右来回走
    "patrol_sec": 2.5,   # 巡逻换向间隔（秒）
    "move": True,        # 怪在技能范围外时走过去
    "dy_tol": 150,       # 纵向容差：目标候选带在技能范围并集之下再扩这么多（像素）
    "base_w": 0,
    # ↑ 技能范围是按「画面宽度 = base_w」量的基准（0 = 未知，1:1 用）。
    #   技能够得着的距离在游戏世界里是固定的，像素尺寸却 ∝ 渲染分辨率：换窗口
    #   大小、换系统缩放（游戏是 DPI-unaware，会被 Windows 位图拉伸）、RDP 重连
    #   都会改这个宽度。运行时长按 base_w 等比换算，打怪范围才不会缩水或超伸。
    "input": "auto",     # 按键出口：auto 优先硬件 / hid 只认硬件 / soft 只用软件注入
    "hid_port": "",      # 硬件串口，留空自动扫描（CH9329 通常挂在 CH343 那个口上）
}

# 字符串型配置的合法取值，非法就退回默认，免得写进一个谁都不认识的模式
# 药水键兼容旧的 pot8/pot9/pot0（VK 名），新的都用 KeyboardEvent.code 名
_KEY_NAMES = tuple(CODE_VK) + POT_KEYS
ENUMS = {"pot_key": _KEY_NAMES, "mp_key": _KEY_NAMES,
         "jump_key": tuple(CODE_VK),
         "pick_key": tuple(CODE_VK),
         "input": ("auto", "hid", "soft")}

CLAMP = {
    "hp_pct": (10.0, 95.0),
    "mp_pct": (10.0, 95.0),
    "pot_sec": (0.5, 30.0),
    "atk_ms": (120, 3000),
    "pick_ms": (60, 2000),
    "patrol_sec": (0.5, 20.0),
    "dy_tol": (40, 400),
    "base_w": (0, 8192),
}


def _clamp_rect4(v, dflt):
    """[左,右,上,下] 四个偏移：坏值退回默认，范围钳到 0~600px。"""
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        return list(dflt)
    out = []
    for x, d in zip(v, dflt):
        try:
            x = float(x)
        except (TypeError, ValueError):
            x = float(d)
        out.append(round(min(600.0, max(0.0, x)), 1))
    return out


SK_DIRS = ("center", "front")


def norm_skill_rect(rect, d):
    """把 [左,右,上,下] 收成这个方向的唯一形态（都是「人物朝右」时量的）。

    center（以人物为中心）：左右必须相等，取大的一边对称展开。面板上只让人填
    「宽」，宽 / 2 就是每一边。
    front（正前方）：左恒为 0，从人物身体中心往前量，身后那块不吃这个框。

    以前左、右是两个独立数字，面板上填 左15 右191 说是「身前」，画出来却是
    ±191（身前又按 max 对称展开了），看着就像填反了。
    """
    l, r, u, dn = (float(x) for x in rect)
    if d == "front":
        return [0.0, max(l, r), u, dn]
    h = max(l, r)
    return [h, h, u, dn]


def _skill_dir(raw_dir, rect):
    """方向取值：只认 center / front，旧配置按语义猜一个最接近的。

    旧版有两个值：both（左右）和 front（身前）。旧 both 里左右相等的其实是
    「以人物为中心」（全方位），左右不等的（比如左0 右140）其实是只打身前那
    半边 = 正前方。旧 front 一律按正前方。
    """
    d = raw_dir
    if d in ("left", "right"):
        return "front"          # 更早的版本还有固定单边：语义最接近正前方
    if d == "both":
        return "front" if abs(float(rect[0]) - float(rect[1])) > 1.0 else "center"
    if d in SK_DIRS:
        return d
    return "center"


def _clamp_skills(v, dflt):
    """技能列表：逐项校验，最多 8 个（再多人也按不过来，画面也画不下）。"""
    base = dflt[0]
    out = []
    if isinstance(v, list):
        for s in v[:8]:
            if not isinstance(s, dict):
                continue
            key = s.get("key")
            g = _clamp_rect4(s.get("ground"), base["ground"])
            d = _skill_dir(s.get("dir"), g)
            out.append({
                "key": key if key in CODE_VK else "",
                "type": s.get("type") if s.get("type") in ("aoe", "single") else "aoe",
                "dir": d,
                "ground": norm_skill_rect(g, d),
                "jump": bool(s.get("jump")),
                "jrect": norm_skill_rect(_clamp_rect4(s.get("jrect"), base["jrect"]), d),
            })
    return out


def clamp_cfg(raw):
    """整理一份配置：非法值退回默认，数值钳到合理区间。"""
    raw = dict(raw or {})
    # 旧配置（单一攻击键 + 距离/高度/跳跃高度）自动换成第一个技能，行为不变
    if "skills" not in raw:
        rp = float(raw.get("range_px", 100.0) or 100.0)
        rh = float(raw.get("range_h", 120.0) or 120.0)
        jr = float(raw.get("jump_reach", 140.0) or 140.0)
        raw["skills"] = [{
            "key": raw.get("atk_key") or "ControlLeft",
            "type": "aoe",
            "dir": raw.get("range_side") or "center",   # 旧值由 _clamp_skills 归类
            "ground": [rp, rp, rh / 2.0, rh / 2.0],
            "jump": True,
            "jrect": [rp, rp, rh / 2.0 + jr, rh / 2.0],
        }]
    out = dict(DEFAULTS)
    for k, dv in DEFAULTS.items():
        if k not in raw:
            continue
        v = raw[k]
        if k == "skills":
            out[k] = _clamp_skills(v, dv)
            continue
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
    for k in ("atk_ms", "pick_ms", "dy_tol", "base_w"):   # 毫秒/像素：整数更好读
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


def center_char_box(w, h):
    """画面正中心对应的假名字牌框：人物识别不到时的兜底锚点。

    只保证身体中心正好落在画面中心——bot/画图对 char 框真正用到的只有
    身体中心（skill_rects/_cx 全从中心换算），牌子实际高矮无所谓，就按
    16px 的 1:1 尺度造一个。决策和画框（server.overlay_char）必须用这
    同一个函数，所见即所打。
    """
    return [w / 2.0 - 8.0, h / 2.0 + 38.0, w / 2.0 + 8.0, h / 2.0 + 54.0]


def _intersects(a, b):
    """两个 [x1,y1,x2,y2] 框是否有交集（ touching 不算，得真叠上）。"""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def skill_rects(skill, char_box, face=0, k=1.0):
    """一个技能的两个判定框（全画面坐标 [x1,y1,x2,y2]）。

    ground/jrect 存的是相对人物身体中心的偏移 [左, 右, 上, 下]，
    是「人物朝右时、画面宽度 = cfg["base_w"] 时」量出来的形态。k = 当前画面
    宽度 / base_w：换分辨率、换系统缩放（游戏是 DPI-unaware，窗口会被 Windows
    位图拉伸）后等比放大/缩小，技能在世界里够得着的距离不变，画面上的框和实际
    判定也始终是同一个东西。

    方向只有两种，这里再按方向收一遍形态（存的时候 clamp 已经收过，这里是
    兜底——配置可能来自旧版本或被手改）：
      center 以人物为中心：左 = 右 = 宽/2，人物两侧都打。
      front  正前方：左 = 0，从人物身体中心往前伸 宽，身后那块打不到。

    **框永远跟着朝向翻**（朝右存 [左,右]，朝左整体镜像成 [右,左]）。center
    对称，翻不翻看着一样；front 翻过去就是「人物面朝哪边打哪边」——挂机关着、
    手动按方向键走路时也一样（server.observe_face 一直在观测位移）。
    face=0（朝向还没读到）就按朝右画，别偷偷对称展开：以前 front 在 face=0 时
    按 max(左,右) 两边展开当「感知范围」，结果面板填 左15 右191、画面上却是
    ±191 的大框，看着就像填反了。

    跳放没开就没有第二个框。server 的 draw_skill_ranges 画的就是它。
    """
    b = char_body_box(char_box)
    cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
    front = (skill.get("dir") or "center") == "front"

    def mk(l, r, u, d):
        l, r = (0.0, max(l, r)) if front else (max(l, r),) * 2
        if face < 0:
            l, r = r, l               # 朝左：整体镜像（对称的框等于没变）
        return [cx - l, cy - u, cx + r, cy + d]

    l, r, u, d = (float(v) * k for v in skill["ground"])
    ground = mk(l, r, u, d)
    jump = None
    if skill.get("jump"):
        jl, jr, ju, jd = (float(v) * k for v in skill["jrect"])
        jump = mk(jl, jr, ju, jd)
    return ground, jump


def skill_scale(cfg, size):
    """当前画面相对技能基准宽度的比例（base_w 未知就 1:1）。

    钳在 0.3~3.0：再离谱的值只可能是尺寸读错（窗口最小化时 client_size 是 0），
    与其把范围放大成整屏，不如按 1:1 打。
    """
    bw = float((cfg or {}).get("base_w") or 0)
    if bw <= 0 or not size or not size[0]:
        return 1.0
    return min(3.0, max(0.3, float(size[0]) / bw))


def _nearest_box(monsters, cx):
    """列表里水平离 cx 最近的一只怪的框（monsters 元素带 box）。"""
    best, best_d = None, None
    for m in monsters:
        d = abs(_cx(m["box"]) - cx)
        if best_d is None or d < best_d:
            best, best_d = m["box"], d
    return best


def _band_target(char_box, monsters, band, dy_tol):
    """技能范围罩不到时的走路目标：候选带（所有技能判定框的并集，往下再扩
    dy_tol 容下一层平台）里最近的一只。带外的怪不追——隔着两层平台追过去
    只会对着空气挥刀。"""
    if band is None or not monsters:
        return None
    cx = _cx(char_box)
    top = band[1]
    bot = band[3] + float(dy_tol)
    best, best_d = None, None
    for m in monsters:
        box = m.get("box")
        if not box or len(box) != 4:
            continue
        if not (box[1] < bot and box[3] > top):
            continue
        d = abs(_cx(box) - cx)
        if best_d is None or d < best_d:
            best, best_d = m, d
    return best


class Bot:
    """一个线程 + 一份配置 + 一份状态。

    get_state() 由 server.py 提供，返回：
        {"fg": bool,            # 游戏窗口是否在前台
         "hp": float | None,    # 血量百分比 0~100，读不到就是 None
         "char": [x1,y1,x2,y2] | None,      # 人物名字牌框（全分辨率像素）
         "size": (w, h) | None,             # 画面尺寸：char 丢了拿它造中心锚点
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
        self._face_anchor = None  # 朝向观测锚点：人物身体中心的最近参考位置
        self._synth = False       # 这一轮是不是在用「画面中心」假锚点撑着
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
            self._reset_face()        # 刚开：不知道角色现在朝哪，第一下必须转身
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
    def _reset_face(self):
        """朝向记忆作废（死亡/失焦回来/丢定位）：锚点一起清，从当前位移重新攒。"""
        self._face = 0
        self._face_anchor = None

    def _watch_face(self, char):
        """朝向从观察到的位移来：人物在画面上往哪边挪，就朝哪边。

        _face 原来只记「自己按过什么键」——你手动走两步、被击退、复活，
        记忆就失准了，身前技能的框和判定全跟着错。这里直接看身体中心
        相对锚点的累计位移：往一边挪过 MOVE_FACE_PX 就算朝那边并把锚点
        挪过来。匹配抖动（±几像素）永远凑不够阈值，不会误翻；bot 自己
        走路/巡逻产生的位移和按键记忆是同向的，互相印证不打架。
        """
        cx = _cx(char)
        if self._face_anchor is None:
            self._face_anchor = cx
            return
        d = cx - self._face_anchor
        if d >= MOVE_FACE_PX:
            self._face, self._face_anchor = 1, cx
        elif d <= -MOVE_FACE_PX:
            self._face, self._face_anchor = -1, cx

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
                # 挂机关着也继续观测朝向：画面上的身前技能框得跟着你手动转向翻
                st = self.get_state() or {}
                if st.get("char"):
                    self._watch_face(st["char"])
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
            self._reset_face()

        # 失焦：绝对不能发按键（会打进别的窗口），同时把按住的键松开
        if not st.get("fg"):
            self.release_all()
            self._was_fg = False
            self._set_status(act="游戏不在前台", target="", err="")
            return
        if not self._was_fg:
            # 重新拿回焦点：这期间你可能手动走过、转过，朝向记忆作废，下一轮先转身
            self._was_fg = True
            self._reset_face()

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

        size = st.get("size")
        char = st.get("char")
        if not char:
            # 识别不到人物：默认人就在画面正中心（相机锁人，角色基本贴着
            # 画面中间），拿假锚点继续打，别整段停摆。连画面尺寸都没有
            # （窗口都没了）才像以前那样松手认栽。
            if not size:
                self._set_dir(0)
                self._reset_face()
                self._synth = False
                self._set_status(act="找不到人物", target="")
                return
            if not self._synth:
                # 刚从「找得到人」掉进「找不到」：朝向记忆作废一次（假锚点
                # 观测不到位移，顺便清掉观测锚点，免得找回真人时中心→真人
                # 的跳变被当成位移翻面）。之后保持记忆——每帧都作废的话
                # face 永远是 0，会一直转身永远打不出手。
                self._reset_face()
                self._synth = True
            char = center_char_box(float(size[0]), float(size[1]))
        else:
            self._synth = False
            self._watch_face(char)    # 朝向跟实际位移走：手动走两步也能翻框

        # 3) 技能选择：逐个技能算判定框（skill_rects），数框里的怪。
        #    列表顺序 = 优先级；某个技能框里怪 ≥2 → 优先群攻，只有 1 只 → 优先
        #    单体；同类型里排前面的先用。地面框罩到 = 直接放；只在跳放框里 =
        #    跳 → 100ms → 技能键。全部技能都罩不到 → 走路 / 巡逻。
        monsters = st.get("monsters") or []
        k = skill_scale(cfg, size)   # 换分辨率/换 DPI 后技能范围等比换算
        cands = []          # (skill, 地面框, 跳放框, 地面怪, 跳放怪)
        band = None         # 所有判定框的并集：走路目标的候选带
        for sk in cfg.get("skills") or []:
            if not sk.get("key"):
                continue                     # 还没录按键的技能不参与
            # 判定框永远按当前朝向算（所见即所打）：正前方技能框翻到哪边就
            # 打哪边，怪在背后就是够不着——先走过去，走一步朝向翻过来就打到了。
            st_face = self._face
            g, j = skill_rects(sk, char, st_face, k)
            gm = [m for m in monsters if m.get("box") and _intersects(g, m["box"])]
            jm = [m for m in monsters if j and m.get("box") and _intersects(j, m["box"])]
            if gm or jm:
                cands.append((sk, g, j, gm, jm))
            for rect in ([g] if j is None else [g, j]):
                band = rect if band is None else [
                    min(band[0], rect[0]), min(band[1], rect[1]),
                    max(band[2], rect[2]), max(band[3], rect[3])]

        if not cands:
            # 没有技能罩得住的怪：像以前一样朝最近的目标走 / 巡逻
            self._set_status(target="")
            tgt = _band_target(char, monsters, band, cfg["dy_tol"] * k)
            if tgt:
                tb = tgt["box"]
                dx = _cx(tb) - _cx(char)
                side = 1 if dx >= 0 else -1
                self._set_status(target=f"{tgt.get('name', '?')} {int(abs(dx))}px")
                if band[0] < tb[2] and tb[0] < band[2]:
                    # 水平上已经挨着技能带了，只是高度差太多（下一层/头顶太高）
                    # → 站住：再走过去也打不着，别白走
                    self._set_dir(0)
                    self._set_status(act="高度够不着")
                elif cfg["move"]:
                    self._set_dir(side)
                    self._face = side
                    self._set_status(act="走向目标")
                else:
                    self._set_dir(0)
                    self._set_status(act="目标太远")
            elif cfg["patrol"]:
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

        # 站放优先：只要还有任何技能的「地面框」罩到怪，就绝不跳。跳一次要
        # 跳→100ms→技能键，能站着打就别浪费，何况两个框重叠时站放本来也打得到。
        # 所有地面框都空了，才轮到跳放框。
        def _n(gm, jm):
            seen = {id(m) for m in gm}
            return len(gm) + sum(1 for m in jm if id(m) not in seen)
        pool0 = ([c for c in cands if c[3]] or    # c[3] = 地面框里的怪
                 [c for c in cands if c[4]] or    # c[4] = 只在跳放框里的怪
                 cands)
        # 怪多优先群攻、单只优先单体（按技能自己的框数怪，跳放框里的也算）；
        # 想要的类型里没有能用的，就退回这一池里任意可用技能
        want = "aoe" if any(_n(gm, jm) >= 2 for _, _, _, gm, jm in pool0) else "single"
        pool = [c for c in pool0 if c[0]["type"] == want] or pool0
        sk, g, j, gm, jm = pool[0]

        mb = _nearest_box(gm or jm, _cx(char))
        dx = _cx(mb) - _cx(char)
        side = 1 if dx >= 0 else -1
        self._set_status(target=f"{_key_label(sk['key'])} 技能 · {int(abs(dx))}px")

        atk_ready = (now - self._t_atk) * 1000.0 >= cfg["atk_ms"]
        settling = (now - self._t_turn) * 1000.0 < TURN_SETTLE_MS
        # 上一次转身之后还没出过手 = 朝向只是「记得对」，不是「刚确认过」。
        # 被击退、复活、手动走两步都会让记忆失准；范围只盖一边的技能失准就是
        # 空放，所以单侧技能每刀前都重新点一次方向键对正（转身 50ms + 等 130ms
        # 塞得进攻击冷却里，几乎不损攻速）。
        calibrated = self._t_turn > self._t_atk
        one_sided = (sk.get("dir") or "center") == "front"  # 正前方：出手前先对正

        # 进范围了：先站住，再转向目标，然后出手
        self._set_dir(0)
        if gm:
            # 地面框里有怪：站原地按技能键（转身 → 等朝向生效 → 出手，老流程原样）
            if self._face != side or (one_sided and atk_ready and not calibrated):
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
                self._tap(sk["key"])
                self._t_atk = now
                self._count("atk")
                self._set_status(act=f"放技能 {_key_label(sk['key'])}", err="")
            else:
                self._set_status(act="等冷却")
        else:
            # 怪只在跳放框里（头顶 / 更远）：起跳 → 空中按技能键。
            # 以人物为中心的框两边都够，直接跳；正前方先对正再起跳，
            # 别跳起来打反方向。
            jump_ready = (now - self._t_jump) * 1000.0 >= JUMP_COOLDOWN_MS
            if one_sided and jump_ready and (self._face != side or not calibrated):
                self._tap("right" if side > 0 else "left", TURN_TAP_MS)
                self._face = side
                self._t_turn = now
                self._set_status(act="转身朝目标", err="")
                return
            if not jump_ready or not atk_ready:
                self._set_status(act="等跳跃冷却" if not jump_ready else "等冷却")
                return
            self._tap(cfg["jump_key"])
            time.sleep(JUMP_CAST_MS / 1000.0)
            self._tap(sk["key"])
            self._t_jump = now
            self._t_atk = now
            self._count("jmp")
            self._count("atk")
            self._set_status(act=f"跳放 {_key_label(sk['key'])}", err="")
