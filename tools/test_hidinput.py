# -*- coding: utf-8 -*-
"""CH9329 帧拼装用例（不碰硬件，不需要插小板）。

期望值全部来自 hidbot.exe --dry-run 的真实输出，改错一个键码或校验和都会红。
跑法：python tools/test_hidinput.py
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
import hidinput as H  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {name}")
    else:
        fail += 1
        print(f"  FAIL {name}  {extra}")


class Fake(H.HidKeyboard):
    """不打开串口，只把要发出去的帧记下来。"""

    def __init__(self):
        self.port = "FAKE"
        self.baud = 9600
        self._down = set()
        self._lock = threading.Lock()
        self._ser = None
        self.frames = []

    @property
    def alive(self):
        return True

    def _write(self, data):
        self.frames.append(data.hex(" ").upper())


print("[1] 单帧字节与 hidbot 一致")
cases = [
    ("按 z", H.keyboard_frame(0, [0x1D]),
     "57 AB 00 02 08 00 00 1D 00 00 00 00 00 29"),
    ("按 Ctrl", H.keyboard_frame(0x01, []),
     "57 AB 00 02 08 01 00 00 00 00 00 00 00 0D"),
    ("全松", H.keyboard_frame(0, []),
     "57 AB 00 02 08 00 00 00 00 00 00 00 00 0C"),
    ("按左", H.keyboard_frame(0, [0x50]),
     "57 AB 00 02 08 00 00 50 00 00 00 00 00 5C"),
    ("按 8", H.keyboard_frame(0, [0x25]),
     "57 AB 00 02 08 00 00 25 00 00 00 00 00 31"),
]
for name, got, want in cases:
    check(name, got.hex(" ").upper() == want, got.hex(" ").upper())

print("[2] 校验和 = 前面所有字节求和取低 8 位")
for name, got, _ in cases:
    check(f"{name} 校验", (sum(got[:-1]) & 0xFF) == got[-1], got.hex(" ").upper())

print("[3] 键名 → 键码表")
check("attack 走修饰位不走键码", "attack" in H.MOD and "attack" not in H.CODE)
check("jump 走修饰位", "jump" in H.MOD)
for k in ("left", "right", "pick", "pot8", "pot9", "pot0"):
    check(f"{k} 有键码", k in H.CODE)

print("[4] 全量状态：按住两个键要发在同一帧里")
k = Fake()
k("right", True)
k("pot8", True)
check("右方向一帧", k.frames[0].startswith("57 AB 00 02 08 00 00 4F"),
      k.frames[0])
check("再按 8 的那帧同时含 4F 和 25",
      "4F 25" in k.frames[1] or "25 4F" in k.frames[1], k.frames[1])
k("right", False)
check("松开右方向后只剩 25", "4F" not in k.frames[-1] and "25" in k.frames[-1],
      k.frames[-1])
k("pot8", False)
check("全松之后是空帧", k.frames[-1] == "57 AB 00 02 08 00 00 00 00 00 00 00 00 0C",
      k.frames[-1])

print("[5] 重复按同一个键不重复发帧")
k = Fake()
k("pick", True)
k("pick", True)
check("只发了一帧", len(k.frames) == 1, len(k.frames))
k("pot0", False)
check("松开没按过的键不发帧（别拿空帧去糊键盘）", len(k.frames) == 1, len(k.frames))

print("[6] Ctrl + 方向能同帧（攻击时同时压着方向也不会丢修饰键）")
k = Fake()
k("attack", True)
k("right", True)
frame = k.frames[-1]
check("控制键位图有 01、键码有 4F", frame.startswith("57 AB 00 02 08 01") and "4F" in frame,
      frame)

print("[7] release_all 一次清干净")
k = Fake()
k("right", True)
k("attack", True)
k.release_all()
check("末帧为空", k.frames[-1] == "57 AB 00 02 08 00 00 00 00 00 00 00 00 0C",
      k.frames[-1])
check("内部状态清空", k._down == set(), k._down)

print("[8] 超过 6 个键要报错（USB HID 上限）")
k = Fake()
try:
    for n in ("left", "right", "up", "down", "pick", "pot8", "pot9"):
        k(n, True)
    check("应该抛异常", False)
except H.HidUnavailable as e:
    check("抛了 HidUnavailable", "6" in str(e), str(e))

print(f"\n通过 {ok} / {ok + fail}")
sys.exit(0 if fail == 0 else 1)
