# -*- coding: utf-8 -*-
"""按键出口回退测试。

用户的要求：**检测不到 CH9329 就直接用软件注入（SendInput）**，不能因为没小板
就不按键、也不能卡住。这里用假 hidinput 模块把三种情形都跑一遍：

  1. 扫不到 CH9329        → 走软件注入，面板文案说明原因
  2. 按键路径不阻塞        → find_port 慢 0.3s，但每次 __call__ 必须是毫秒级
  3. 硬件中途掉线          → 回落软件注入并记下原因
  4. SendInput 被系统拒绝  → 返回失败并提示「用管理员身份启动」

真的 send_key 会把按键注进当前窗口，所以全程 monkeypatch 掉。
"""
import os
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "webui"))

import bot as botmod                      # noqa: E402
import hidinput as real_hid               # noqa: E402

_ok = _fail = 0


def check(cond, label):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  ok   {label}")
    else:
        _fail += 1
        print(f"  FAIL {label}")


def install_fake(find_port, keyboard_cls):
    """把假的 hidinput 塞进 sys.modules，Sender 里 `import hidinput` 就会拿到它。"""
    fake = types.ModuleType("hidinput")
    fake.HidUnavailable = real_hid.HidUnavailable
    fake.find_port = find_port
    fake.HidKeyboard = keyboard_cls
    sys.modules["hidinput"] = fake
    return fake


class _SlowNoPort:
    """模拟「板子没插」：find_port 扫描很慢（0.3s）然后返回 None。"""
    calls = 0

    @classmethod
    def find(cls, timeout=0.25):
        cls.calls += 1
        time.sleep(0.3)
        return None


class _NeverOpen:
    def __init__(self, port, *a, **k):
        raise real_hid.HidUnavailable("COM99 打不开: 系统找不到指定的文件")


print("[1] 找不到 CH9329：自动走软件注入")
_SlowNoPort.calls = 0
install_fake(_SlowNoPort.find, _NeverOpen)
t0 = time.perf_counter()
s = botmod.Sender("auto", "")
init_ms = (time.perf_counter() - t0) * 1000
check(s.hid is None, "没有硬件时不持有句柄")
check("未检测到 CH9329" in s.describe(), f"面板文案说明原因：{s.describe()!r}")
check("CH9329" in s.err or "打不开" in s.err, f"err 有具体原因：{s.err!r}")
check(init_ms < 1500, f"构造不卡死（{init_ms:.0f}ms）")

print("[2] 按键走软件注入，且不被串口扫描拖住")
sent = []
botmod.send_key = lambda name, down: sent.append((name, down)) or 1
ts = []
for i in range(8):
    t0 = time.perf_counter()
    good, via = s("right", i % 2 == 0)
    ts.append((time.perf_counter() - t0) * 1000)
    assert good and via == "soft"
worst = max(ts)
check(worst < 50, f"8 次按键最慢 {worst:.1f}ms（扫描 300ms 不能落在按键路径上）")
check(len(sent) == 8, "每次都真的注入了")
check(_SlowNoPort.calls <= 2, f"扫描只在后台做（共 {_SlowNoPort.calls} 次）")

print("[3] 硬件中途掉线：回落软件并记录")
state = {"open": True}


class _FlakyHid:
    """第一次能连上、info 能应答；调一次按键就抛异常，之后再也连不上。"""

    def __init__(self, port, *a, **k):
        if not state["open"]:
            raise real_hid.HidUnavailable("线被拔了")
        self.port = port

    def info(self):
        return "57 AB 00 01"

    def __call__(self, name, down):
        state["open"] = False
        raise OSError("串口消失了")

    def close(self):
        pass

    def release_all(self):
        pass


install_fake(lambda timeout=0.25: "COM5", _FlakyHid)
s2 = botmod.Sender("auto", "")
check(s2.hid is not None, "先连上硬件")
check(s2.describe().startswith("硬件 "), f"面板显示硬件：{s2.describe()!r}")
sent.clear()
good, via = s2("attack", True)
check(good and via == "soft", "掉线这一下就改用软件注入（按键不丢）")
check("回落" in s2.err, f"err 记录掉线：{s2.err!r}")
check(s2.hid is None, "句柄已释放")

print("[4] SendInput 被系统拒绝（游戏以管理员运行）")
botmod.send_key = lambda name, down: 0
s3 = botmod.Sender("soft", "")
good, via = s3("right", True)
check(not good and via == "soft", "返回失败，不假装成功")
check("管理员" in s3.err, f"err 指向真正原因：{s3.err!r}")
check(s3.describe() == "软件注入", f"soft 模式文案：{s3.describe()!r}")

print(f"\n通过 {_ok} / {_ok + _fail}")
sys.exit(1 if _fail else 0)
