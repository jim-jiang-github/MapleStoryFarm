# -*- coding: utf-8 -*-
"""bot.py 的决策用例：不发真按键，用记录器代替 sender，逐帧喂状态看按下什么。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
import bot  # noqa: E402

CHAR = [600, 300, 660, 320]          # 人物名字牌：中心 (630, 310)
# 攻击/跳/拾取的键现在在面板上可配，断言一律从 DEFAULTS 取，别写死
ATK = bot.DEFAULTS["atk_key"]        # ControlLeft
JUMP = bot.DEFAULTS["jump_key"]      # AltLeft
PICK = bot.DEFAULTS["pick_key"]      # KeyZ
calls = []


def recorder(name, down):
    calls.append((name, down))


def make(monsters=(), hp=100.0, mp=None, fg=True, char=CHAR):
    b = bot.Bot(get_state=lambda: {"fg": fg, "hp": hp, "mp": mp, "char": char,
                                   "monsters": list(monsters)}, sender=recorder)
    b.cfg = dict(bot.DEFAULTS)
    return b


def step(b, now=None, **kw):
    st = {"fg": kw.get("fg", True), "hp": kw.get("hp", 100.0),
          "mp": kw.get("mp"), "char": kw.get("char", CHAR),
          "monsters": kw.get("monsters", list(kw.get("m", [])))}
    b.get_state = lambda: st
    calls.clear()
    b._step(b.cfg, now=now)
    return list(calls)


def down_keys(c):
    # 拾取键是每帧都可能按的，跟其它动作不冲突；除专门测拾取的用例，这里都剔掉
    return [n for n, d in c if d and n != PICK]


def up_keys(c):
    return [n for n, d in c if not d and n != PICK]


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


print("[0] 出厂默认值（和面板/README 必须一致）")
check("血线默认 40%", bot.DEFAULTS["hp_pct"] == 40.0, bot.DEFAULTS["hp_pct"])
check("攻击距离默认 100px", bot.DEFAULTS["range_px"] == 100.0, bot.DEFAULTS["range_px"])
check("攻击高度默认 120px", bot.DEFAULTS["range_h"] == 120.0, bot.DEFAULTS["range_h"])
check("跳跃高度默认 140px", bot.DEFAULTS["jump_reach"] == 140.0, bot.DEFAULTS["jump_reach"])
check("喝药间隔默认 3.0s", bot.DEFAULTS["pot_sec"] == 3.0, bot.DEFAULTS["pot_sec"])
check("攻击间隔默认 420ms", bot.DEFAULTS["atk_ms"] == 420, bot.DEFAULTS["atk_ms"])
check("喝药键默认 8", bot.DEFAULTS["pot_key"] == "pot8", bot.DEFAULTS["pot_key"])

print("[1] 怪在攻击距离内 → 先转身，同一帧不出手")
b = make()
mon = [{"name": "Stump", "box": [690, 300, 750, 360], "conf": 0.9}]   # 中心 720，dx=90
c = step(b, m=mon)
# 第一帧还不知道朝向：只点一下右方向转身，Ctrl 留到下一帧
check("第一帧只转身", down_keys(c) == ["right"], c)
check("转身键随即抬起（不会一直按住走过去）", up_keys(c) == ["right"], c)
check("状态是转身朝目标", b.snapshot()[1]["act"] == "转身朝目标", b.snapshot()[1])

print("[1b] 刚转完身的那一帧仍然不打（等游戏把朝向渲染出来）")
c = step(b, m=mon)
check("这一帧不出手", ATK not in down_keys(c), c)
check("状态是等朝向生效", b.snapshot()[1]["act"] == "等朝向生效", b.snapshot()[1])

print("[1c] 过了 130ms 才按 Ctrl")
settle(b)
c = step(b, m=mon)
check("这一帧按了 Ctrl", down_keys(c) == [ATK], c)
check("状态是攻击", b.snapshot()[1]["act"] == "攻击", b.snapshot()[1])

print("[2] 攻击冷却：紧接着再来一帧不应重复攻击")
c = step(b, m=mon)
check("这一帧没攻击", ATK not in down_keys(c), c)
check("状态是等冷却", b.snapshot()[1]["act"] == "等冷却", b.snapshot()[1])

print("[3] 怪在左边、距离内 → 先左转，等朝向生效再打")
b = make()
left_mon = [{"name": "Snail", "box": [510, 300, 570, 360], "conf": 0.8}]  # 中心 540, dx=-90
c = step(b, m=left_mon)
check("第一帧点左方向", down_keys(c) == ["left"], c)
settle(b)
c = step(b, m=left_mon)
check("第二帧才攻击", down_keys(c) == [ATK], c)

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
c = step(b, m=[{"name": "Stump", "box": [690, 300, 750, 360], "conf": 0.9}])
check("右方向先松开", up_keys(c)[0] == "right", c)
check("然后攻击", ATK in down_keys(c), c)

print("[6] 纵向差距过大（上下层）→ 不把它当目标")
b = make()
c = step(b, m=[{"name": "Stump", "box": [740, 600, 800, 660], "conf": 0.9}])   # dy≈325
check("不攻击上层怪", ATK not in down_keys(c), c)
check("去巡逻/守株待兔", b.snapshot()[1]["act"] in ("巡逻找怪", "守株待兔"),
      b.snapshot()[1])

print("[6b] 头顶差得不多的怪 → 跳起来打（不走路、不出手）")
b = make()
top_mon = [{"name": "Snail", "box": [690, 130, 750, 190], "conf": 0.8}]   # 怪底 190，离攻击区顶 2.5px
c = step(b, m=top_mon)
check("按了跳 (Alt 键)", (JUMP, True) in c, c)
check("没按方向/攻击", not any(k in (ATK, "left", "right") for k in down_keys(c)), c)
check("状态是跳起攻击", b.snapshot()[1]["act"] == "跳起攻击", b.snapshot()[1])
c = step(b, m=top_mon)
check("跳跃冷却内不再按跳", (JUMP, True) not in c, c)

print("[6c] 头顶差太多的怪（超出跳跃判定框）→ 根本不进决策，去巡逻")
b = make()
high_mon = [{"name": "Snail", "box": [690, 0, 750, 40], "conf": 0.8}]    # 怪底 40 < 候选带顶 52.5
c = step(b, m=high_mon)
check("不跳不出手", (JUMP, True) not in c and ATK not in down_keys(c), c)
check("状态去巡逻了", b.snapshot()[1]["act"] in ("巡逻找怪", "守株待兔"), b.snapshot()[1])

print("[6d] 正下方下一层的怪 → 站住提示高度够不着，不走不跳不打")
b = make()
low_mon = [{"name": "Snail", "box": [690, 320, 750, 380], "conf": 0.8}]  # 怪顶 320 > 攻击区底 312.5
c = step(b, m=low_mon)
check("不出手不跳", ATK not in down_keys(c) and (JUMP, True) not in c, c)
check("不按方向", not any(k in ("left", "right") for k in down_keys(c)), c)
check("状态是高度够不着", b.snapshot()[1]["act"] == "高度够不着", b.snapshot()[1])

print("[7] 血低于 40% → 喝 8")
b = make()
c = step(b, hp=30.0)
check("按 8 且不干别的", down_keys(c)[0] == "pot8"
      and not any(k in down_keys(c) for k in ("ControlLeft", "right")), c)

print("[8] 喝药冷却：距上次不到 3 秒不再按")
c = step(b, hp=35.0)
check("没再按 8", "pot8" not in down_keys(c), c)
b._t_pot -= 3.0          # 手动把冷却拨回去
c = step(b, hp=35.0)
check("冷却到了又按 8", "pot8" in down_keys(c), c)

print("[9] 血量读不到（None）→ 不瞎按")
b = make()
c = step(b, hp=None)
check("没按药", not any(k.startswith("pot") for k in down_keys(c)), c)

print("[10] 拾取默认全程按 Z")
b = make()
c = step(b)
check("按了拾取键", PICK in [n for n, d in c if d], c)
b.cfg["pick"] = False
c = step(b)
check("关掉后不按拾取键", PICK not in [n for n, d in c if d], c)

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
check("不按方向/攻击", not any(k in ("left", "right", ATK) for k in down_keys(c)), c)
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

print("[17] 攻击方向：正前方模式的矩形只朝一侧展开")
BOTH_Z = [530.0, 192.5, 730.0, 312.5]      # CHAR → 身体中心 (630, 252.5)，宽 100 高 120
check("默认两侧", bot.DEFAULTS["range_side"] == "both")
check("正前方朝右：从身体中心往右",
      bot.attack_zone(CHAR, 100, 120, 1, "front") == [630.0, 192.5, 730.0, 312.5],
      bot.attack_zone(CHAR, 100, 120, 1, "front"))
check("正前方朝左：从身体中心往左",
      bot.attack_zone(CHAR, 100, 120, -1, "front") == [530.0, 192.5, 630.0, 312.5],
      bot.attack_zone(CHAR, 100, 120, -1, "front"))
check("朝向未知（0）时按两侧算，别假装知道",
      bot.attack_zone(CHAR, 100, 120, 0, "front") == BOTH_Z)
check("两侧模式与朝向无关",
      bot.attack_zone(CHAR, 100, 120, -1, "both") == BOTH_Z)

print("[18] 正前方模式：怪在身后但够得着 → 转身，不是走过去")
b = make()
b.cfg["range_side"] = "front"
t = 5000.0
c = step(b, now=t, m=left_mon)
check("第一帧点左方向", down_keys(c) == ["left"], c)
check("状态是转身朝目标（没判成太远）",
      b.snapshot()[1]["act"] == "转身朝目标", b.snapshot()[1])
check("状态里带真实朝向", b.snapshot()[1]["face"] == -1, b.snapshot()[1])
c = step(b, now=t + 0.2, m=left_mon)
check("对正后出刀", down_keys(c) == [ATK], c)

print("[19] 正前方模式每刀前重新对正（记忆说朝对了也要再点一次）")
c = step(b, now=t + 0.3, m=left_mon)          # 冷却中
check("冷却中不重复转身", down_keys(c) == [], c)
c = step(b, now=t + 0.7, m=left_mon)          # 420ms 冷却到
check("冷却好了先重新对正", down_keys(c) == ["left"], c)
c = step(b, now=t + 0.85, m=left_mon)
check("对正之后才出刀", down_keys(c) == [ATK], c)

print("[20] 正前方模式 + 头顶的怪：先对正再起跳，别跳起来砍反方向")
b = make()
b.cfg["range_side"] = "front"
c = step(b, m=top_mon)
check("先转身对正", down_keys(c) == ["right"], c)
c = step(b, m=top_mon)
check("对正后按跳", (JUMP, True) in c, c)

print("[21] 攻击方向的取值校验")
check("front 合法", bot.clamp_cfg({"range_side": "front"})["range_side"] == "front")
check("乱写的值退回两侧", bot.clamp_cfg({"range_side": "up"})["range_side"] == "both")

print("[22] 喝药链：主键按了血量没涨 → 按备用 1→4 换着喝")
pot_presses = lambda c: [n for n, d in c if d and (n.startswith("pot") or n in ("KeyA", "KeyS"))]
b = make(hp=30.0)
b.cfg["pot_chain"] = ["KeyA", "KeyS"]
t = 100.0
c = step(b, now=t, hp=30.0)
check("血量低于线 → 按主键 pot8", pot_presses(c) == ["pot8"], pot_presses(c))
c = step(b, now=t + 0.5, hp=30.0)
check("验证期内（<0.8s）不连按", pot_presses(c) == [], pot_presses(c))
c = step(b, now=t + 0.9, hp=30.0)
check("主键没反应 → 立刻换备用1 KeyA", pot_presses(c) == ["KeyA"], pot_presses(c))
c = step(b, now=t + 1.2, hp=55.0)
check("血量涨回来 → 认定喝到，停手", pot_presses(c) == [], pot_presses(c))
c = step(b, now=t + 10.0, hp=20.0)
check("下次低血直接用生效的 KeyA", pot_presses(c) == ["KeyA"], pot_presses(c))
c = step(b, now=t + 10.9, hp=20.0)
check("KeyA 也空 → 换 KeyS", pot_presses(c) == ["KeyS"], pot_presses(c))
c = step(b, now=t + 11.8, hp=20.0)
check("链全空绕完一圈 → 停手等间隔", pot_presses(c) == [], pot_presses(c))
c = step(b, now=t + 11.8 + 3.1, hp=20.0)
check("歇完间隔从头（pot8）再试", pot_presses(c) == ["pot8"], pot_presses(c))

print("[23] 边掉血边喝：没回到线以上但比按下时高，也算喝到")
b2 = make(hp=30.0)
t2 = 200.0
step(b2, now=t2, hp=30.0)
c = step(b2, now=t2 + 0.9, hp=32.0)
check("涨了 2% 就算生效，不换键", pot_presses(c) == [], pot_presses(c))

print("[24] 备用药水键的配置清洗")
check("只收认识的键", bot.clamp_cfg({"pot_chain": ["KeyA", "BadKey", "F5"]})["pot_chain"]
      == ["KeyA", "", "F5", ""],
      bot.clamp_cfg({"pot_chain": ["KeyA", "BadKey", "F5"]})["pot_chain"])
check("超过 4 格截断", len(bot.clamp_cfg({"pot_chain": ["KeyA"] * 9})["pot_chain"]) == 4)
check("默认全空", bot.DEFAULTS["pot_chain"] == ["", "", "", ""])
check("蓝药链同样清洗", bot.clamp_cfg({"mp_chain": ["KeyZ", "BadKey"]})["mp_chain"]
      == ["KeyZ", "", "", ""],
      bot.clamp_cfg({"mp_chain": ["KeyZ", "BadKey"]})["mp_chain"])

print("[25] 喝蓝药：和红药同一套状态机，量的是蓝、按的是 mp_key")
mp_presses = lambda c: [n for n, d in c if d and (n.startswith("pot") or n in ("KeyM", "KeyN"))]
b3 = make(hp=50.0, mp=25.0)
b3.cfg["mp_pct"] = 30.0
b3.cfg["mp_key"] = "pot9"
b3.cfg["mp_chain"] = ["KeyM", "KeyN"]
t3 = 300.0
c = step(b3, now=t3, hp=50.0, mp=25.0)
check("蓝量低于线 → 按主键 pot9", mp_presses(c) == ["pot9"], mp_presses(c))
c = step(b3, now=t3 + 0.5, hp=50.0, mp=25.0)
check("验证期内不连按", mp_presses(c) == [], mp_presses(c))
c = step(b3, now=t3 + 0.9, hp=50.0, mp=25.0)
check("主键没反应 → 立刻换备用1 KeyM", mp_presses(c) == ["KeyM"], mp_presses(c))
c = step(b3, now=t3 + 1.2, hp=50.0, mp=60.0)
check("蓝量涨回来 → 认定喝到，停手", mp_presses(c) == [], mp_presses(c))
c = step(b3, now=t3 + 10.0, hp=50.0, mp=20.0)
check("下次低蓝直接用生效的 KeyM", mp_presses(c) == ["KeyM"], mp_presses(c))
c = step(b3, now=t3 + 10.9, hp=50.0, mp=20.0)
check("KeyM 也空 → 换 KeyN", mp_presses(c) == ["KeyN"], mp_presses(c))
c = step(b3, now=t3 + 11.8, hp=50.0, mp=20.0)
check("链全空绕完一圈 → 停手等间隔", mp_presses(c) == [], mp_presses(c))
c = step(b3, now=t3 + 11.8 + 3.1, hp=50.0, mp=20.0)
check("歇完间隔从头（pot9）再试", mp_presses(c) == ["pot9"], mp_presses(c))
check("蓝够时不按蓝药键", mp_presses(step(b3, now=t3 + 20.0, hp=50.0, mp=80.0)) == [])

print(f"\n通过 {ok} / {ok + fail}")
sys.exit(1 if fail else 0)
