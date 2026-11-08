# -*- coding: utf-8 -*-
"""CH9329F 硬件键盘：发真正的 USB HID 事件，不是软件注入。

为什么需要它
------------
`SendInput` 注进去的按键在系统里带 `LLKHF_INJECTED` 标记：
- 走 DirectInput / Raw Input 的游戏可能直接无视；
- 带反外挂的游戏能一眼看出"这不是人手按的"。

CH9329F 是"串口 → USB HID"的小板：串口端插控制机（一个 COM 口），
键鼠端插目标机（系统里就是一个标准 USB 键盘）。目标机收到的是真实的
USB 报文，没有任何注入痕迹。同一条线上还有一块 CH343 负责串口。

协议（CH9329 键盘帧）
--------------------
    57 AB | 地址 00 | 命令 02 | 长度 08 | 数据 8 字节 | 校验和

    数据 = [控制键位图, 00, 键码1..键码6]
    控制键位图：01 左Ctrl 02 左Shift 04 左Alt 08 左Win 10 右Ctrl ...
    校验和 = 前面所有字节求和取低 8 位

**关键：键盘帧是全量状态，不是增量。** 每次发出去的都是"键盘现在的样子"，
不是"刚按了哪个键"。所以：
- 同时按住右方向 + 喝药，必须把两个键塞进同一帧；
- 松开就是发一帧不包含那个键的；
- 少发一次就会卡键（角色一直跑）。

这块板子默认 9600 8N1，一帧 14 字节约 15ms。所以每帧都要"有用"，
不能像 SendInput 那样无脑刷。
"""
from __future__ import annotations

import threading
import time

FRAME_HEAD = b"\x57\xab"
ADDR = 0x00
CMD_KEYBOARD = 0x02
CMD_INFO = 0x01

# 键名与 bot.py 的 VK 表一一对应，这样后端可以直接当 sender 用
MOD = {"attack": 0x01, "jump": 0x04}                    # Ctrl / Alt
CODE = {
    "left": 0x50, "right": 0x4F, "up": 0x52, "down": 0x51,
    "pick": 0x1D,                                          # Z
    "pot8": 0x25, "pot9": 0x26, "pot0": 0x27,              # 8 / 9 / 0
}
NAMES = tuple(MOD) + tuple(CODE)

# 常见串口芯片的 VID:PID。CH343 是这块小板的串口端，优先试它。
CH34X_HINTS = ("1A86", "CH343", "CH340", "CH341", "USB-Enhanced-SERIAL")
# 虚拟串口/蓝牙串口不用试，试了只是浪费时间
SKIP_HINTS = ("ELTIMA", "Virtual", "Bluetooth", "蓝牙")


def _frame(cmd, data=b""):
    body = bytes([ADDR, cmd, len(data)]) + data
    return FRAME_HEAD + body + bytes([sum(FRAME_HEAD + body) & 0xFF])


def keyboard_frame(ctrl, codes):
    """拼一帧键盘状态。codes 里最多 6 个键码（USB HID 的限制）。"""
    data = bytes([ctrl & 0xFF, 0x00]) + bytes(list(codes)[:6]).ljust(6, b"\x00")
    return _frame(CMD_KEYBOARD, data)


class HidUnavailable(RuntimeError):
    """串口打不开 / 芯片没应答。"""


class HidKeyboard:
    """持有串口，维护"当前按住的键"，每次变化发一帧全量状态。

    用法上它就是一个 `sender(name, down)`，可以直接塞给 bot.Bot。
    """

    def __init__(self, port, baud=9600, timeout=0.05):
        self.port = str(port).upper()
        self.baud = int(baud)
        self._down = set()
        self._lock = threading.Lock()
        self._ser = None
        self._open(timeout)

    # ---- 串口 ----
    def _open(self, timeout):
        try:
            import serial  # 延迟导入：没装 pyserial 时软件注入还能照常跑
        except ImportError as e:  # pragma: no cover
            raise HidUnavailable("没装 pyserial") from e
        try:
            self._ser = serial.Serial(self.port, self.baud, timeout=timeout,
                                      write_timeout=0.15)
            self._ser.reset_input_buffer()
        except Exception as e:  # noqa: BLE001
            raise HidUnavailable(f"{self.port} 打不开: {e}") from e

    @property
    def alive(self):
        return self._ser is not None and self._ser.is_open

    def close(self):
        with self._lock:
            try:
                if self.alive:
                    self._write(keyboard_frame(0, ()))   # 先松开一切再关，别让键卡住
                    self._ser.close()
            except Exception:  # noqa: BLE001
                pass
            self._ser = None
            self._down.clear()

    # ---- 发送 ----
    def _write(self, data):
        if not self.alive:
            raise HidUnavailable("串口已关闭")
        # 芯片每帧都会回 ACK。不读的话缓冲区会一直涨，隔一会儿清一次最省事。
        try:
            if self._ser.in_waiting:
                self._ser.reset_input_buffer()
        except Exception:  # noqa: BLE001
            pass
        n = self._ser.write(data)
        if n != len(data):
            raise HidUnavailable(f"只写进去 {n}/{len(data)} 字节")

    def _flush_state(self):
        ctrl, codes = 0, []
        # 按名字排序：set 的遍历顺序不固定，不排的话同样一组键会发出不同字节序的帧，
        # 测试就没法断言了（对芯片来说两者等价，但可复现更重要）。
        for name in sorted(self._down):
            ctrl |= MOD.get(name, 0)
            c = CODE.get(name)
            if c is not None and c not in codes:
                codes.append(c)
        if len(codes) > 6:
            raise HidUnavailable(f"同时按了 {len(codes)} 个键，超过 HID 上限 6")
        self._write(keyboard_frame(ctrl, codes))

    def __call__(self, name, down):
        """sender 接口：name 见 NAMES，down=True 按下。"""
        with self._lock:
            if down:
                if name in self._down:
                    return
                self._down.add(name)
            else:
                if name not in self._down:
                    return          # 没按着的键不用发帧
                self._down.discard(name)
            self._flush_state()

    def release_all(self):
        with self._lock:
            if not self._down:
                return
            self._down.clear()
            self._flush_state()

    def hold(self, name, ms=60):
        self(name, True)
        time.sleep(max(1, int(ms)) / 1000.0)
        self(name, False)

    def info(self):
        """读芯片应答，确认对面真的是 CH9329。"""
        with self._lock:
            if not self.alive:
                raise HidUnavailable("串口已关闭")
            self._ser.reset_input_buffer()
            self._ser.write(_frame(CMD_INFO))
            t0 = time.time()
            buf = b""
            while time.time() - t0 < 0.4:
                buf += self._ser.read(32)
                if len(buf) >= 6:
                    break
        if buf[:2] != FRAME_HEAD:
            raise HidUnavailable("芯片没有应答（不是 CH9329，或者波特率不对）")
        return buf.hex(" ").upper()


def probe(port, baud=9600, timeout=0.25):
    """试一下这个口是不是 CH9329。只读不写键盘，安全。"""
    try:
        import serial
    except ImportError:  # pragma: no cover
        return False
    try:
        with serial.Serial(port, baud, timeout=timeout) as s:
            s.reset_input_buffer()
            s.write(_frame(CMD_INFO))
            t0 = time.time()
            buf = b""
            while time.time() - t0 < timeout:
                buf += s.read(32)
                if len(buf) >= 6:
                    break
            return buf[:2] == FRAME_HEAD
    except Exception:  # noqa: BLE001
        return False


def list_ports():
    """返回 [(port, desc)]，按"像 CH343"的程度排前面。"""
    try:
        from serial.tools import list_ports as lp
    except ImportError:  # pragma: no cover
        return []
    out = []
    for p in lp.comports():
        desc = f"{p.description or ''} {p.hwid or ''}".strip()
        if any(h.lower() in desc.lower() for h in SKIP_HINTS):
            continue
        rank = 0 if any(h.lower() in desc.lower() for h in CH34X_HINTS) else 1
        out.append((rank, p.device, desc))
    out.sort()
    return [(dev, desc) for _, dev, desc in out]


def find_port(timeout=0.25):
    """扫一遍串口，找到能应答 CH9329 的那个。找不到返回 None。"""
    for dev, _desc in list_ports():
        if probe(dev, timeout=timeout):
            return dev
    return None
