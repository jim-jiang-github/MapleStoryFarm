# -*- coding: utf-8 -*-
"""bot.py 的决策用例：不发真按键，用记录器代替 sender，逐帧喂状态看按下什么。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
import bot  # noqa: E402

CHAR = [600, 300, 660, 320]          # 人物名字牌：中心 (630, 310)
calls = []


def recorder(name, down):
    calls.append((name, down))


def make(monsters=(), hp=100.0, fg=True, char=CHAR):
    b = bot.Bot(get_state=lambda: {"fg": fg, "hp": hp, "char": char,
                                   "monsters": list(monsters)}, sender=recorder)
    b.cfg = dict(bot.DEFAULTS)
    return b


def step(b, **kw):
    st = {"fg": kw.get("fg", True), "hp": kw.get("hp", 100.0),
          "char": kw.get("char", CHAR), "monsters": kw.get("monsters", list(kw.get("m", [])))}
    b.get_state = lambda: st
    calls.clear()
    b._step(b.cfg)
    return list(calls)


def down_keys(c):
    # 拾取 Z 是每帧都可能按的，跟其它动作不冲突；除专门测拾取的用例，这里都剔掉
    return [n for n, d in c if d and n != "pick"]


def up_keys(c):
    return [n for n, d in c if not d and n != "pick"]


def settle(b, ms=200):
    """把「上一次转身」的时间往前推，模拟已经等够了（不用真 sleep）。"""
    b._t_turn -= ms / 1000.0


ok = 0
fail = 0


def check(title, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {title}")
    else:
        fail += 1
        print(f"  FAIL {title}  {detail}")


print("[1] 怪在攻击距离内 → 先转身，同一帧不出手")
b = make()
mon = [{"name": "Stump", "box": [740, 300, 800, 360], "conf": 0.9}]   # 中心 770，dx=140
c = step(b, m=mon)
# 第一帧还不知道朝向：只点一下右方向转身，Ctrl 留到下一帧
check("第一帧只转身", down_keys(c) == ["right"], c)
check("转身键随即抬起（不会一直按住走过去）", up_keys(c) == ["right"], c)
check("状态是转身朝目标", b.snapshot()[1]["act"] == "转身朝目标", b.snapshot()[1])

print("[1b] 刚转完身的那一帧仍然不打（等游戏把朝向渲染出来）")
c = step(b, m=mon)
check("这一帧不出手", "attack" not in down_keys(c), c)
check("状态是等朝向生效", b.snapshot()[1]["act"] == "等朝向生效", b.snapshot()[1])

print("[1c] 过了 130ms 才按 Ctrl")
settle(b)
c = step(b, m=mon)
check("这一帧按了 Ctrl", down_keys(c) == ["attack"], c)
check("状态是攻击", b.snapshot()[1]["act"] == "攻击", b.snapshot()[1])

print("[2] 攻击冷却：紧接着再来一帧不应重复攻击")
c = step(b, m=mon)
check("这一帧没攻击", "attack" not in down_keys(c), c)
check("状态是等冷却", b.snapshot()[1]["act"] == "等冷却", b.snapshot()[1])

print("[3] 怪在左边、距离内 → 先左转，等朝向生效再打")
b = make()
left_mon = [{"name": "Snail", "box": [460, 300, 520, 360], "conf": 0.8}]  # 中心 490, dx=-140
c = step(b, m=left_mon)
check("第一帧点左方向", down_keys(c) == ["left"], c)
settle(b)
c = step(b, m=left_mon)
check("第二帧才攻击", down_keys(c) == ["attack"], c)

print("[3b] 重新拿回焦点后朝向记忆作废：第一帧必须重新转身")
b = make()
step(b, fg=False)                    # 失焦一轮
c = step(b, m=mon)                   # 回到前台
check("回前台第一帧先转身", down_keys(c) == ["right"], c)

print("[4] 怪在范围外 → 按住右方向走过去，不攻击")
b = make()
c = step(b, m=[{"name": "Stump", "box": [940, 300, 1000, 360], "conf": 0.9}])   # 中心 970, dx=340
check("按住 right", down_keys(c) == ["right"] and up_keys(c) == [], c)
check("状态是走向目标", b.snapshot()[1]["act"] == "走向目标", b.snapshot()[1])

print("[5] 进入范围后松开方向键")
c = step(b, m=[{"name": "Stump", "box": [740, 300, 800, 360], "conf": 0.9}])
check("右方向先松开", up_keys(c)[0] == "right", c)
check("然后攻击", "attack" in down_keys(c), c)

print("[6] 纵向差距过大（上下层）→ 不把它当目标")
b = make()
c = step(b, m=[{"name": "Stump", "box": [740, 600, 800, 660], "conf": 0.9}])   # dy≈325
check("不攻击上层怪", "attack" not in down_keys(c), c)
check("去巡逻/守株待兔", b.snapshot()[1]["act"] in ("巡逻找怪", "守株待兔"),
      b.snapshot()[1])

print("[7] 血低于 60% → 喝 8")
b = make()
c = step(b, hp=55.0)
check("按 8 且不干别的", down_keys(c)[0] == "pot8"
      and not any(k in down_keys(c) for k in ("attack", "right")), c)

print("[8] 喝药冷却：距上次不到 3 秒不再按")
c = step(b, hp=50.0)
check("没再按 8", "pot8" not in down_keys(c), c)
b._t_pot -= 3.0          # 手动把冷却拨回去
c = step(b, hp=50.0)
check("冷却到了又按 8", "pot8" in down_keys(c), c)

print("[9] 血量读不到（None）→ 不瞎按")
b = make()
c = step(b, hp=None)
check("没按药", not any(k.startswith("pot") for k in down_keys(c)), c)

print("[10] 拾取默认全程按 Z")
b = make()
c = step(b)
check("按了 Z", "pick" in [n for n, d in c if d], c)
b.cfg["pick"] = False
c = step(b)
check("关掉后不按 Z", "pick" not in [n for n, d in c if d], c)

print("[11] 失焦 → 立刻松开按住的键，且不发任何按键")
b = make()
step(b, m=[{"name": "Stump", "box": [940, 300, 1000, 360], "conf": 0.9}])   # 先按住 right
c = step(b, fg=False, m=[{"name": "Stump", "box": [940, 300, 1000, 360], "conf": 0.9}])
check("松开了 right", ("right", False) in c, c)
check("没有新的按下", down_keys(c) == [], c)
check("状态提示不在前台", b.snapshot()[1]["act"] == "游戏不在前台", b.snapshot()[1])

print("[12] 没识别到人物 → 松键、不乱按")
b = make()
c = step(b, char=None)
check("不按方向/攻击", not any(k in ("left", "right", "attack") for k in down_keys(c)), c)
check("状态是找不到人物", b.snapshot()[1]["act"] == "找不到人物", b.snapshot()[1])

print("[13] 巡逻：没怪时按住一个方向，到点换向")
b = make()
c = step(b)
first = b._held
check("按住了一个方向", first in (-1, 1) and down_keys(c) in (["left"], ["right"]), c)
b._t_patrol -= 3.0
c = step(b)
check("换向", b._held == -first, (first, b._held))

print("[14] 配置钳位")
cfg = bot.clamp_cfg({"hp_pct": 300, "pot_sec": 0, "atk_ms": 99999,
                     "range_px": 5, "pot_key": "zzz", "pick": 0})
check("hp_pct 钳到 95", cfg["hp_pct"] == 95.0, cfg)
check("pot_sec 钳到 0.5", cfg["pot_sec"] == 0.5, cfg)
check("atk_ms 钳到 3000", cfg["atk_ms"] == 3000, cfg)
check("range_px 钳到 40", cfg["range_px"] == 40.0, cfg)
check("非法药键退回默认", cfg["pot_key"] == "pot8", cfg)
check("pick 转布尔", cfg["pick"] is False, cfg)

print("[15] 失焦只在第一轮松键，之后不再重复发抬起")
b = make()
step(b, m=[{"name": "Stump", "box": [940, 300, 1000, 360], "conf": 0.9}])   # 按住 right
c1 = step(b, fg=False)
c2 = step(b, fg=False)
check("第一轮松开 right", ("right", False) in c1, c1)
check("第二轮一个事件都没有（不会每隔 20ms 空发抬起，按住 Ctrl 打字不会被打断）",
      c2 == [], c2)

print("[16] 本来就没按键时失焦也不发任何抬起")
b = make()
c = step(b, fg=False)
check("没有抬起事件", c == [], c)

print(f"\n通过 {ok} / {ok + fail}")
sys.exit(1 if fail else 0)
