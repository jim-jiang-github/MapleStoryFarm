# -*- coding: utf-8 -*-
"""bot.py 的决策用例：不发真按键，用记录器代替 sender，逐帧喂状态看按下什么。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))
import bot  # noqa: E402

CHAR = [600, 300, 660, 320]          # 人物名字牌：中心 (630, 310)
# 技能/跳/拾取的键现在在面板上可配，断言一律从 DEFAULTS 取，别写死
ATK = bot.DEFAULTS["skills"][0]["key"]   # ControlLeft
JUMP = bot.DEFAULTS["jump_key"]          # AltLeft
PICK = bot.DEFAULTS["pick_key"]          # KeyZ
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
          "size": kw.get("size"), "roi": kw.get("roi"),
          "monsters": kw.get("monsters", list(kw.get("m", []))),
          "dot": kw.get("dot")}
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
sk0 = bot.DEFAULTS["skills"][0]
check("默认技能 1 个、键 ControlLeft", sk0["key"] == "ControlLeft", sk0)
check("默认地面范围 左右100 上下60", sk0["ground"] == [100, 100, 60, 60], sk0["ground"])
check("默认可跳放、跳放上扩 200", sk0["jump"] and sk0["jrect"][2] == 200, sk0["jrect"])
check("喝药间隔默认 3.0s", bot.DEFAULTS["pot_sec"] == 3.0, bot.DEFAULTS["pot_sec"])
check("放技能间隔已移除（节奏由各技能 CD 管）", "atk_ms" not in bot.DEFAULTS,
      bot.DEFAULTS.get("atk_ms"))
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

print("[1c] 过了 130ms 才按技能键")
settle(b)
c = step(b, m=mon)
check("这一帧按了技能键", down_keys(c) == [ATK], c)
check("状态是放技能", b.snapshot()[1]["act"] == f"放技能 {bot._key_label(ATK)}",
      b.snapshot()[1])

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

print("[6b] 头顶差得不多的怪 → 跳放：跳 → 100ms → 技能键（两侧技能不转身，直接跳）")
b = make()
top_mon = [{"name": "Snail", "box": [690, 130, 750, 190], "conf": 0.8}]   # 怪底 190，离攻击区顶 2.5px
c = step(b, m=top_mon)
check("跳+技能键连招", down_keys(c) == [JUMP, ATK], c)
check("没按方向（两侧技能跳放不转身，老行为）",
      not any(k in ("left", "right") for k in down_keys(c)), c)
check("状态是跳放", b.snapshot()[1]["act"] == f"跳放 {bot._key_label(ATK)}",
      b.snapshot()[1])
c = step(b, m=top_mon)
check("跳跃冷却内不再跳", (JUMP, True) not in c, c)

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
cfg = bot.clamp_cfg({"hp_pct": 300, "pot_sec": 0,
                     "pot_key": "zzz", "pick": 0})
check("hp_pct 钳到 95", cfg["hp_pct"] == 95.0, cfg)
check("pot_sec 钳到 0.5", cfg["pot_sec"] == 0.5, cfg)
check("非法药键退回默认", cfg["pot_key"] == "pot8", cfg)
check("pick 转布尔", cfg["pick"] is False, cfg)

sc = bot.clamp_cfg({"skills": [{"key": "Nope", "type": "zzz",
                                "ground": [9999, -5, "x", 60],
                                "jump": 1, "jrect": [1, 2]}]})["skills"][0]
check("非法技能键清空", sc["key"] == "", sc)
check("非法类型退回 aoe", sc["type"] == "aoe", sc)
check("ground 逐项钳位（坏值退默认，center 再收成左右相等）",
      sc["ground"] == [600.0, 600.0, 60.0, 60.0], sc["ground"])
check("jrect 不是 4 个数退回默认",
      sc["jrect"] == bot.DEFAULTS["skills"][0]["jrect"], sc["jrect"])
check("技能超过 8 个截断",
      len(bot.clamp_cfg({"skills": [{"key": "KeyA"}] * 20})["skills"]) == 8)
check("旧配置（atk_key+距离/高度）自动换成第一个技能",
      bot.clamp_cfg({"atk_key": "KeyX", "range_px": 80, "range_h": 100,
                     "jump_reach": 120})["skills"] ==
      [{"key": "KeyX", "type": "aoe", "dir": "center", "ground": [80.0, 80.0, 50.0, 50.0],
        "jump": True, "jrect": [80.0, 80.0, 170.0, 50.0], "cd_ms": 0}])
check("旧配置 range_side=front 带进 dir",
      bot.clamp_cfg({"atk_key": "KeyX", "range_side": "front"})["skills"][0]["dir"] == "front")
check("乱写的 dir 退回 center",
      bot.clamp_cfg({"skills": [{"key": "KeyA", "dir": "up"}]})["skills"][0]["dir"] == "center")
check("旧值 left/right 迁移成 front（没有固定单边的技能）",
      bot.clamp_cfg({"skills": [{"key": "KeyA", "dir": "left"},
                                {"key": "KeyB", "dir": "right"}]})["skills"]
      == [{"key": "KeyA", "type": "aoe", "dir": "front",
           "ground": [0.0, 100.0, 60.0, 60.0], "jump": False,
           "jrect": [0.0, 100.0, 200.0, 60.0], "cd_ms": 0},
          {"key": "KeyB", "type": "aoe", "dir": "front",
           "ground": [0.0, 100.0, 60.0, 60.0], "jump": False,
           "jrect": [0.0, 100.0, 200.0, 60.0], "cd_ms": 0}])


def _dir_of(ground, d):
    return bot.clamp_cfg({"skills": [{"key": "KeyA", "dir": d, "ground": ground}]})["skills"][0]


check("旧 both + 左右相等 → 归类成 center（就是「以人物为中心」）",
      _dir_of([150, 150, 60, 60], "both")["dir"] == "center")
check("旧 both + 左右不等（左0 右140）→ 归类成 front（只打身前那半边）",
      _dir_of([0, 140, 55, 31], "both") ==
      {"key": "KeyA", "type": "aoe", "dir": "front", "ground": [0.0, 140.0, 55.0, 31.0],
       "jump": False, "jrect": [0.0, 100.0, 200.0, 60.0], "cd_ms": 0})
check("center 存成规范形态：左右相等（取大的一边）",
      _dir_of([20, 200, 55, 31], "center")["ground"] == [200.0, 200.0, 55.0, 31.0])
check("front 存成规范形态：左恒为 0",
      _dir_of([30, 200, 55, 31], "front")["ground"] == [0.0, 200.0, 55.0, 31.0])
check("技能列表允许清空（= 不攻击，只喝药拾取）",
      bot.clamp_cfg({"skills": []})["skills"] == [])

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

print("[17] skill_rects：偏移矩形以人物身体中心为原点（默认技能 = 老攻击区域）")
g, j = bot.skill_rects(bot.DEFAULTS["skills"][0], CHAR)
check("地面框：中心 ± 左右100 / 上下60（和老 range_px=100 × range_h=120 一致）",
      g == [530.0, 192.5, 730.0, 312.5], g)
check("跳放框：上边扩到 200（= 老上下60 + jump_reach 140）",
      j == [530.0, 52.5, 730.0, 312.5], j)
g2, j2 = bot.skill_rects({"key": "KeyA", "type": "single", "dir": "center",
                          "ground": [180, 180, 60, 60],
                          "jump": False, "jrect": [0, 0, 0, 0]}, CHAR)
check("以人物为中心：框两侧对称（宽 = 左+右 = 360）",
      g2 == [450.0, 192.5, 810.0, 312.5], g2)
check("跳放关了就没有第二个框", j2 is None, j2)
g3, _ = bot.skill_rects({"key": "KeyA", "type": "single", "dir": "front",
                         "ground": [180, 180, 60, 60],
                         "jump": False, "jrect": [0, 0, 0, 0]}, CHAR)
check("正前方：只从人物身体中心往前伸 180，身后那块不算",
      g3 == [630.0, 192.5, 810.0, 312.5], g3)

print("[17b] 正前方：框随朝向整体镜像（左恒为 0，不许塌成窄条）")
ASYM = {"key": "KeyA", "type": "single", "dir": "front",
        "ground": [30, 170, 60, 60], "jump": False, "jrect": [0, 0, 0, 0]}
ga, _ = bot.skill_rects(ASYM, CHAR, 1)
check("朝右：从人物中心往右 170（存的左30 被收成左0）",
      ga == [630.0, 192.5, 800.0, 312.5], ga)
gb, _ = bot.skill_rects(ASYM, CHAR, -1)
check("朝左：整体镜像到左边 170（不是砍成 30px 窄条）",
      gb == [460.0, 192.5, 630.0, 312.5], gb)
gc, _ = bot.skill_rects(ASYM, CHAR, 0)
check("朝向还没读到 → 按朝右画，不再偷偷对称展开",
      gc == [630.0, 192.5, 800.0, 312.5], gc)
_, gj = bot.skill_rects({**ASYM, "jump": True, "jrect": [20, 260, 100, 60]}, CHAR, -1)
check("跳放框同样镜像", gj == [370.0, 152.5, 630.0, 312.5], gj)

print("[17c] 方向=正前方也不许僵住：框一样跟着朝向翻（用户实测「朝左框不动」）")
BOTH_ASYM = {"key": "KeyX", "type": "single", "dir": "front",
             "ground": [0, 140, 55, 31], "jump": True, "jrect": [0, 153, 103, 42]}
br_, _ = bot.skill_rects(BOTH_ASYM, CHAR, 1)
check("朝右：只往右边伸 140（左0 右140，原样）",
      br_ == [630.0, 197.5, 770.0, 283.5], br_)
bl_, _ = bot.skill_rects(BOTH_ASYM, CHAR, -1)
check("朝左：整体镜像，只往左边伸 140（以前这里是死的，框永远在右边）",
      bl_ == [490.0, 197.5, 630.0, 283.5], bl_)
bu_, bju = bot.skill_rects(BOTH_ASYM, CHAR, 0)
check("朝向未知：照原样（按朝右画，不当成两边都够）",
      bu_ == [630.0, 197.5, 770.0, 283.5], bu_)
check("跳放框同理（这一帧朝向未知，也按朝右）",
      bju == [630.0, 149.5, 783.0, 294.5], bju)
bs_, _ = bot.skill_rects({**BOTH_ASYM, "dir": "center", "ground": [120, 120, 55, 31]}, CHAR, -1)
check("左右对称的框镜像前后完全一样（翻不翻看不出来，也没副作用）",
      bs_ == [510.0, 197.5, 750.0, 283.5], bs_)

print("[17d] 方向=左右 + 单边框：判定框跟着朝向走（画到哪就打到哪）")
MONR = [{"name": "Snail", "box": [700, 240, 760, 280], "conf": 0.8}]   # 右边
MONL = [{"name": "Snail", "box": [500, 240, 560, 280], "conf": 0.8}]   # 左边
def bot_facing(face):
    """造一个「已经站稳、朝向已知、刚对正过」的 bot。

    第一轮会重置朝向，先暖一轮；再把 t_turn 摆到 t_atk 之后（calibrated），
    免掉正前方技能「每刀前重新对正」那一下转身——那个行为 [19] 单独测。
    """
    b = make()
    b.cfg["skills"] = [BOTH_ASYM]
    step(b, now=4000.0, m=[])          # 暖一轮：吃掉「刚拿回焦点」的朝向复位
    b._face = face
    b._t_turn, b._t_atk = 4000.0, 3999.0
    return b


c = step(bot_facing(1), now=5000.0, m=MONR)
check("朝右：右边那只在框里 → 直接出手（正面技能框在右，不用再转身）",
      down_keys(c) == ["KeyX"], c)
c = step(bot_facing(1), now=5000.0, m=MONL)
check("朝右：左边那只在框外 → 不打它，先朝它走", "KeyX" not in down_keys(c), c)
c = step(bot_facing(-1), now=5000.0, m=MONL)
check("朝左：框镜到左边 → 左边那只出手", "KeyX" in down_keys(c), c)
c = step(bot_facing(-1), now=5000.0, m=MONR)
check("朝左：右边那只反而够不着了（框在哪边就打哪边）",
      "KeyX" not in down_keys(c), c)

FWD_SK = {"key": "KeyA", "type": "single", "dir": "front",
          "ground": [0, 180, 60, 60], "jump": False, "jrect": [0, 180, 200, 60]}
right_mon = [{"name": "Snail", "box": [690, 300, 750, 360], "conf": 0.8}]
print("[18] 正前方：框跟着朝向走，面朝哪边打哪边")
b = bot_facing(-1)                       # 已经站稳、面朝左
b.cfg["skills"] = [FWD_SK]
c = step(b, now=5000.0, m=left_mon)
check("面朝左 + 怪在左边 → 直接出 KeyA", down_keys(c) == ["KeyA"], c)
c = step(b, now=6000.0, m=right_mon)
check("面朝左 + 怪在背后 → 框不覆盖，先朝它走（不是原地空放）",
      "KeyA" not in down_keys(c) and "right" in down_keys(c), c)
check("状态是走向目标", b.snapshot()[1]["act"] == "走向目标", b.snapshot()[1])

print("[19] 正前方每刀前重新对正（记忆说朝对了也要再点一次）")
b = bot_facing(-1)
b.cfg["skills"] = [FWD_SK]
t = 5000.0
c = step(b, now=t, m=left_mon)
check("先出技能", down_keys(c) == ["KeyA"], c)
c = step(b, now=t + 0.1, m=left_mon)          # 150ms 防抖底线内 = 冷却中
check("冷却中不重复转身", down_keys(c) == [], c)
c = step(b, now=t + 0.4, m=left_mon)          # 底线过了
check("冷却好了先重新对正", down_keys(c) == ["left"], c)
c = step(b, now=t + 0.55, m=left_mon)
check("对正之后才出技能", down_keys(c) == ["KeyA"], c)

print("[20] 正前方 + 头顶的怪：没对正先转身，对正了才跳放")
b = bot_facing(-1)
b.cfg["skills"] = [{**FWD_SK, "jump": True}]
top_left = [{"name": "Snail", "box": [540, 130, 600, 190], "conf": 0.8}]
b._t_atk = 5000.0                    # 刚出过手 = 记忆里的朝向没被确认过
c = step(b, now=5000.0, m=top_left)
check("先转身对正（别跳起来打反方向）", down_keys(c) == ["left"], c)
settle(b)
b._t_atk = 0.0                       # 已经对正过
c = step(b, now=5000.3, m=top_left)
check("对正后跳放：跳+技能键", down_keys(c) == [JUMP, "KeyA"], c)

print("[21] 怪多优先群攻、单只优先单体、同类型排前面的先用")
b = make()
b.cfg["skills"] = [
    {"key": "KeyS", "type": "single", "ground": [200, 200, 60, 60],
     "jump": False, "jrect": [200, 200, 120, 60]},
    {"key": "KeyD", "type": "aoe", "ground": [200, 200, 60, 60],
     "jump": False, "jrect": [200, 200, 120, 60]},
]
two = [{"name": "A", "box": [690, 300, 750, 360], "conf": 0.9},
       {"name": "B", "box": [520, 300, 580, 360], "conf": 0.9}]
c = step(b, m=two)
settle(b)
c = step(b, m=two)
check("两只怪 → 群攻 KeyD", down_keys(c) == ["KeyD"], c)
one = [two[0]]
c = step(b, m=one)
check("目标换到另一边 → 先转身（face=-1 → 右）", down_keys(c) == ["right"], c)
settle(b)
b._t_atk -= 1.0                     # 拨开冷却
c = step(b, m=one)
check("单只怪 → 单体 KeyS", down_keys(c) == ["KeyS"], c)

print("[21b] 同类型按列表顺序：第一个群攻先用")
b = make()
b.cfg["skills"] = [
    {"key": "KeyQ", "type": "aoe", "ground": [200, 200, 60, 60],
     "jump": False, "jrect": [200, 200, 120, 60]},
    {"key": "KeyD", "type": "aoe", "ground": [200, 200, 60, 60],
     "jump": False, "jrect": [200, 200, 120, 60]},
]
c = step(b, m=one)
settle(b)
c = step(b, m=one)
check("排在前面的 KeyQ 先用", down_keys(c) == ["KeyQ"], c)

print("[21c] 前面的技能罩不到、后面的罩得到 → 用后面的（顺序 = 优先级，不阻塞）")
b = make()
b.cfg["skills"] = [
    {"key": "KeyQ", "type": "aoe", "ground": [0, 40, 60, 60],        # 只盖右边 40px
     "jump": False, "jrect": [0, 40, 120, 60]},
    {"key": "KeyD", "type": "aoe", "ground": [200, 200, 60, 60],
     "jump": False, "jrect": [200, 200, 120, 60]},
]
far_left = [{"name": "C", "box": [460, 300, 520, 360], "conf": 0.9}]   # 中心 490，dx=-140
c = step(b, m=far_left)
settle(b)
c = step(b, m=far_left)
check("第一个技能够不着 → 用第二个 KeyD", down_keys(c) == ["KeyD"], c)

print("[21d] 站放优先：跳放框里有更多怪，也先站着打（两个框重叠时同理）")
b = make()
b.cfg["skills"] = [
    # 能站放：地面框盖住身边这只；它的跳放框也盖着头顶那两只（两框重叠）
    {"key": "KeyS", "type": "single", "ground": [200, 200, 60, 60],
     "jump": True, "jrect": [400, 400, 320, 60]},
    # 只能跳放：地面框罩不到任何东西
    {"key": "KeyD", "type": "aoe", "ground": [0, 0, 40, 40],
     "jump": True, "jrect": [400, 400, 320, 60]},
]
near = [{"name": "N", "box": [690, 300, 750, 360], "conf": 0.9}]
top = [{"name": "T1", "box": [560, 120, 620, 180], "conf": 0.9},
       {"name": "T2", "box": [660, 120, 720, 180], "conf": 0.9}]
step(b, m=near + top)
settle(b)
c = step(b, m=near + top)
check("有站放机会 → 站着放 KeyS，不起跳", down_keys(c) == ["KeyS"], c)
check("状态写的是放技能，不是跳放",
      b.snapshot()[1]["act"] == f"放技能 {bot._key_label('KeyS')}", b.snapshot()[1])

print("[21e] 一个技能都站放不了 → 这才轮到跳放")
b = make()
b.cfg["skills"] = [
    {"key": "KeyS", "type": "single", "ground": [0, 0, 40, 40],
     "jump": False, "jrect": [0, 0, 0, 0]},
    {"key": "KeyD", "type": "aoe", "ground": [0, 0, 40, 40],
     "jump": True, "jrect": [400, 400, 320, 60]},
]
c = step(b, m=top)
check("所有地面框都空 → 跳放 KeyD", down_keys(c) == [JUMP, "KeyD"], c)

print("[21d] 技能没录按键 → 跳过，不算候选")
b = make()
b.cfg["skills"] = [
    {"key": "", "type": "aoe", "ground": [500, 500, 200, 200],
     "jump": False, "jrect": [500, 500, 300, 200]},
    {"key": "KeyD", "type": "aoe", "ground": [200, 200, 60, 60],
     "jump": False, "jrect": [200, 200, 120, 60]},
]
c = step(b, m=one)
settle(b)
c = step(b, m=one)
check("没键的技能被跳过，用 KeyD", down_keys(c) == ["KeyD"], c)

print("[21f] 技能自己的 CD：单体没转好 → 群攻补位；全在 CD → 等CD 不空转")
b = make()
b.cfg["skills"] = [
    {"key": "KeyS", "type": "single", "ground": [200, 200, 60, 60],
     "jump": False, "jrect": [200, 200, 120, 60], "cd_ms": 2000},
    {"key": "KeyD", "type": "aoe", "ground": [200, 200, 60, 60],
     "jump": False, "jrect": [200, 200, 120, 60], "cd_ms": 1000},
]
step(b, now=100.0, m=one)                 # 第一帧转身
c = step(b, now=100.2, m=one)             # 转完 → 单体先出手
check("单体转好 → 先用 KeyS", down_keys(c) == ["KeyS"], c)
c = step(b, now=100.8, m=one)             # KeyS 在 CD（到 102.2）→ KeyD 补位
check("单体在 CD → 群攻 KeyD 补位", down_keys(c) == ["KeyD"], c)
check("状态是放技能 KeyD",
      b.snapshot()[1]["act"] == f"放技能 {bot._key_label('KeyD')}", b.snapshot()[1])
c = step(b, now=101.5, m=one)             # KeyS 到 102.2 / KeyD 到 101.8，全在 CD
check("全在 CD → 不出技能", down_keys(c) == [], c)
check("状态报等CD", b.snapshot()[1]["act"].startswith("等CD"), b.snapshot()[1]["act"])
c = step(b, now=102.3, m=one)             # KeyS 转好 → 回到单体
check("CD 转好 → 回到 KeyS", down_keys(c) == ["KeyS"], c)

print("[21g] 跳放也吃技能 CD：CD 里不重复起跳")
b = make()
b.cfg["skills"] = [
    {"key": "KeyS", "type": "single", "ground": [0, 0, 40, 40],
     "jump": False, "jrect": [0, 0, 0, 0], "cd_ms": 0},
    {"key": "KeyD", "type": "aoe", "ground": [0, 0, 40, 40],
     "jump": True, "jrect": [400, 400, 320, 60], "cd_ms": 5000},
]
c = step(b, now=100.0, m=top)
check("第一次正常跳放", down_keys(c) == [JUMP, "KeyD"], c)
c = step(b, now=101.2, m=top)             # 跳跃冷却过了，但 KeyD 的 CD 没到
check("技能 CD 里不起跳", down_keys(c) == [], c)
check("状态报等CD", b.snapshot()[1]["act"].startswith("等CD"), b.snapshot()[1]["act"])

print("[21h] cd_ms 落库钳位：非法退 0、越界压 30000")
cdcfg = bot.clamp_cfg({"skills": [
    {"key": "KeyA", "cd_ms": "abc"},
    {"key": "KeyB", "cd_ms": 99999},
]})
check("非法字符串 → 0", cdcfg["skills"][0]["cd_ms"] == 0, cdcfg["skills"][0])
check("超过 30000 压到 30000", cdcfg["skills"][1]["cd_ms"] == 30000,
      cdcfg["skills"][1])
oldcfg = bot.clamp_cfg({"skills": [{"key": "KeyA"}]})
check("老配置没写 cd_ms → 补 0（行为不变）", oldcfg["skills"][0]["cd_ms"] == 0,
      oldcfg["skills"][0])

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

print("[26] 朝向跟观察到的位移走（手动走两步，框也要跟着翻）")
b = make()
b.cfg["patrol"] = False              # 无怪+巡逻关：bot 自己一个键都不发
step(b, now=100.0, m=[])
check("静止开局 face=0（不知道朝哪）", b.snapshot()[1]["face"] == 0, b.snapshot()[1])
moved_r = [CHAR[0] + 30, CHAR[1], CHAR[2] + 30, CHAR[3]]
step(b, now=100.1, m=[], char=moved_r)
check("手动右移 30px → face=1", b.snapshot()[1]["face"] == 1, b.snapshot()[1])
step(b, now=100.2, m=[], char=[moved_r[0] + 3, CHAR[1], moved_r[2] + 3, CHAR[3]])
check("±3px 匹配抖动不翻面", b.snapshot()[1]["face"] == 1, b.snapshot()[1])
moved_l = [CHAR[0] - 60, CHAR[1], CHAR[2] - 60, CHAR[3]]
step(b, now=100.3, m=[], char=moved_l)
check("手动左移 → face=-1", b.snapshot()[1]["face"] == -1, b.snapshot()[1])
step(b, now=100.4, m=[], char=moved_l)   # 原地不动：face 保持 -1
check("原地不动 face 不变", b.snapshot()[1]["face"] == -1, b.snapshot()[1])
b._reset_face()
step(b, now=100.5, m=[], char=moved_l)
check("复位后从当前位移重新攒（face=0）", b.snapshot()[1]["face"] == 0, b.snapshot()[1])
step(b, now=100.6, m=[], char=[moved_l[0] - 15, CHAR[1], moved_l[2] - 15, CHAR[3]])
check("复位后继续左移 → 又是 face=-1", b.snapshot()[1]["face"] == -1, b.snapshot()[1])

print("[27] 人物识别不到 → 不再拿画面中心当假锚点打，往走丢的反方向走回来")
b = make()
c = step(b, m=[{"name": "Stump", "box": [1150, 300, 1210, 360], "conf": 0.9}],
         char=CHAR, size=(1280, 720))     # 怪在右边远处 → 按住 right 追过去
check("丢失前正在往右走", b._held == 1, b._held)
c = step(b, now=20.0, m=[{"name": "Stump", "box": [1150, 300, 1210, 360], "conf": 0.9}],
         char=None, size=(1280, 720))     # 人往右走丢了
check("反方向（left）走回来", down_keys(c) == ["left"], c)
check("不拿假锚点出手", ATK not in down_keys(c), c)
check("状态写明在找回", "丢定位" in b.snapshot()[1]["act"], b.snapshot()[1])
c = step(b, now=20.5, m=[], char=None, size=(1280, 720))
check("还没找回就继续往左走", b._held == -1, b._held)
c = step(b, now=22.2, m=[], char=None, size=(1280, 720))
check("两秒没找回来 → 掉头往另一边找", b._held == 1, b._held)
c = step(b, now=22.3, m=[], char=CHAR, size=(1280, 720))
check("认回人后清掉丢失状态", b._lost_since is None and b._last_cx == 630,
      (b._lost_since, b._last_cx))

# 站着打时丢的（被击退/手动跑出去）：没按方向键，按最后认到的位置猜方向
b3 = make()
step(b3, m=[{"name": "Stump", "box": [700, 300, 760, 360], "conf": 0.9}],
     char=CHAR, size=(1280, 720))        # 站着打：_held 应为 0
check("站着打时没按方向键", b3._held == 0, b3._held)
step(b3, m=[], char=None, size=(1280, 720))
check("站着丢 → 最后位置在中线左边，往右（中间）走", b3._held == 1, b3._held)

b2 = make()
b2.cfg["patrol"] = False
step(b2, now=10.0, m=[], char=CHAR)                    # 真人：观测锚点立在 630
step(b2, now=10.1, m=[], char=None, size=(1280, 720))  # 丢定位：锚点应作废
step(b2, now=10.2, m=[], char=[CHAR[0] - 500, CHAR[1], CHAR[2] - 500, CHAR[3]])
check("找回真人后不把中心→真人的跳变当位移翻面（锚点已清）",
      b2.snapshot()[1]["face"] == 0, b2.snapshot()[1])
step(b2, now=10.3, m=[], char=[CHAR[0] - 515, CHAR[1], CHAR[2] - 515, CHAR[3]])
check("之后真人继续左移才翻面", b2.snapshot()[1]["face"] == -1, b2.snapshot()[1])
print("[27b] 画面尺寸也没有（窗口没了）→ 还是老的停手行为")
b = make()
c = step(b, m=[{"name": "Stump", "box": [700, 300, 760, 360], "conf": 0.9}],
         char=None, size=None)
check("不按方向/攻击", not any(k in ("left", "right", ATK) for k in down_keys(c)), c)
check("状态是找不到人物", b.snapshot()[1]["act"] == "找不到人物", b.snapshot()[1])
print("[27c] 移动关着时丢定位 → 不盲走，站着等识别恢复")
b4 = make()
b4.cfg["move"] = False
step(b4, m=[], char=CHAR, size=(1280, 720))
step(b4, m=[], char=None, size=(1280, 720))
check("不按方向键", b4._held == 0, b4._held)
check("状态写明站着等", "站着等" in b4.snapshot()[1]["act"], b4.snapshot()[1])

print("\n[28] 换分辨率 / 换系统缩放：技能范围按基准宽度等比换算")
SK_B = {"key": ATK, "type": "aoe", "dir": "both",
        "ground": [100, 100, 60, 60], "jump": False}
b = make()
b.cfg = bot.clamp_cfg({**bot.DEFAULTS, "base_w": 1366, "skills": [SK_B]})
check("clamp_cfg 保留 base_w", b.cfg["base_w"] == 1366, b.cfg.get("base_w"))
check("base_w 非法值归 0（当未知处理）",
      bot.clamp_cfg({"base_w": -5})["base_w"] == 0)
g1, _ = bot.skill_rects(b.cfg["skills"][0], CHAR)
check("画面宽度 = base_w 时范围原样（±100 → 200 宽）",
      round(g1[2] - g1[0]) == 200, g1)
g2, _ = bot.skill_rects(b.cfg["skills"][0], CHAR, k=1.5)
check("画面宽了 1.5 倍后范围等比放大（±150 → 300 宽）",
      round(g2[2] - g2[0]) == 300 and abs(bot._cx(g2) - bot._cx(g1)) < 1e-9, g2)
check("skill_scale 按当前画面宽度算系数",
      abs(bot.skill_scale(b.cfg, (2049, 1152)) - 1.5) < 1e-6,
      bot.skill_scale(b.cfg, (2049, 1152)))
check("没有画面尺寸（窗口没了）时退回 1:1",
      bot.skill_scale(b.cfg, None) == 1.0 and bot.skill_scale(b.cfg, (0, 0)) == 1.0)
check("base_w 未知（老配置）时也是 1:1，行为不变",
      bot.skill_scale({"base_w": 0}, (2049, 1152)) == 1.0)

# 同一只怪：1366 基准下框只到 ±100（够不着），2049 画面下 ±150（够得着）
M_FAR = [{"name": "Stump", "box": [740, 290, 780, 320], "conf": 0.9}]
c = step(b, m=M_FAR, size=(1366, 768))
check("1366 画面下 250px 外的怪在框外 → 不出手", ATK not in down_keys(c), down_keys(c))
c = step(b, m=M_FAR, size=(2049, 1152))
check("2049 画面下同一只怪进框 → 出手（范围跟着放大）",
      ATK in down_keys(c), down_keys(c))

print("\n[29] 走位以人物识别区为墙，但识别区外面有怪要追出去打")
ROI_N = (0.30, 0.08, 0.60, 0.90)          # 1366 宽下：x 410~820，左右各留 3% 余量
lim = bot.walk_x_limits((1366, 768), ROI_N)
check("活动范围 = 识别区左右各收 3% 画面宽",
      lim is not None and abs(lim[0] - (1366 * 0.30 + 1366 * 0.03)) < 0.01
      and abs(lim[1] - (1366 * 0.60 - 1366 * 0.03)) < 0.01, lim)
check("没给识别区 → 不限制（老行为）", bot.walk_x_limits((1366, 768), None) is None)
_narrow = bot.walk_x_limits((1366, 768), (0.5, 0.1, 0.51, 0.9))
check("识别区窄到没余地 → 围着中心给一条缝，不会两边反着判",
      _narrow is not None and _narrow[1] - _narrow[0] == 16.0
      and abs(sum(_narrow) / 2 - 1366 * 0.505) < 0.5, _narrow)
_chase = bot.walk_x_limits((1366, 768), ROI_N, bot.WALK_CHASE_MARGIN)
check("追击余量：墙往外放，左右各多出 15% 画面宽",
      _chase is not None and abs(_chase[1] - (lim[1] + 1366 * 0.15)) < 0.01
      and abs(_chase[0] - (lim[0] - 1366 * 0.15)) < 0.01, (lim, _chase))
_wide = bot.walk_x_limits((1366, 768), (0.12, 0.08, 0.88, 0.90), 0.15)
check("追击也不进左右 HUD 条（小地图/任务栏里印着同名文字）",
      _wide is not None and abs(_wide[0] - 1366 * bot.WALK_HUD_SIDE) < 0.01
      and abs(_wide[1] - 1366 * (1 - bot.WALK_HUD_SIDE)) < 0.01, _wide)

# 站在识别区右边界上，怪就在框外一点点（走两步够得着）→ 追出去打，别杵着
EDGE_R = [760, 300, 820, 320]             # 名字牌中心 790 > hi - 1
b = make()
c = step(b, m=[{"name": "Stump", "box": [900, 300, 960, 360], "conf": 0.9}],
         char=EDGE_R, size=(1366, 768), roi=ROI_N)
check("识别区外的怪够得着 → 按住 right 追出去", "right" in down_keys(c), c)
check("状态写明是追出识别区", "追出识别区" in b.snapshot()[1]["act"], b.snapshot()[1])

# 怪在屏幕那一头，走到底（含追击余量）也够不着 → 不追，转成巡逻，不会傻站着
b = make()
b._patrol_dir = -1
b._t_patrol = time.time()                 # 刚换过向，别让计时器插一脚
c = step(b, m=[{"name": "Stump", "box": [1150, 300, 1210, 360], "conf": 0.9}],
         char=EDGE_R, size=(1366, 768), roi=ROI_N)
check("够不着的怪 → 不往右追", "right" not in down_keys(c), c)
check("够不着的那只不当目标", b.snapshot()[1]["target"] == "", b.snapshot()[1])
check("改成巡逻往回走（不是干站着）", "left" in down_keys(c), c)
# 关掉巡逻也不许「到边界顶墙」：状态要说明够不着
b = make()
b.cfg["patrol"] = False
c = step(b, m=[{"name": "Stump", "box": [1150, 300, 1210, 360], "conf": 0.9}],
         char=EDGE_R, size=(1366, 768), roi=ROI_N)
check("巡逻关着 → 站住但状态说清楚（够不着）",
      "right" not in down_keys(c) and "够不着" in b.snapshot()[1]["act"],
      (c, b.snapshot()[1]))
# 同一位置，怪在左边（往里走）→ 照走，它得能自己走回来
b = make()
c = step(b, m=[{"name": "Stump", "box": [500, 300, 560, 360], "conf": 0.9}],
         char=EDGE_R, size=(1366, 768), roi=ROI_N)
check("目标在左边（往识别区里走）→ 允许走过来", "left" in down_keys(c), c)

# 巡逻撞到左边界 → 立刻掉头
EDGE_L = [420, 300, 480, 320]             # 名字牌中心 450 < lo+1，已经在墙外
b = make()
b.cfg["patrol"] = True
b._patrol_dir = -1
b._t_patrol = time.time()                 # 刚换过向，别让计时器插一脚
c = step(b, m=[], char=EDGE_L, size=(1366, 768), roi=ROI_N)
check("巡逻往左撞墙 → 这一帧不按 left", "left" not in down_keys(c), c)
check("巡逻方向翻过来了", b._patrol_dir == 1, b._patrol_dir)
step(b, m=[], char=EDGE_L, size=(1366, 768), roi=ROI_N)
check("下一帧就按住 right 往回巡逻", b._held == 1, b._held)

print("\n[30] 巡逻途中丢定位：人往巡逻方向出界了，反方向走回来")
b = make()
b.cfg["patrol"] = True
b._patrol_dir = -1
b._t_patrol = time.time()                 # 刚换过向，别让计时器插一脚
c = step(b, m=[], char=CHAR, size=(1366, 768), roi=ROI_N)
check("丢失前正往左巡逻", b._held == -1, b._held)
c = step(b, m=[], char=None, size=(1366, 768), roi=ROI_N)
check("往右走回来（巡逻的反方向）", b._held == 1 and "left" not in down_keys(c), c)
check("往回走不受识别区边界拦（人已经在墙外，得让它走回来）",
      "right" in down_keys(c), c)

print("\n[31] 小地图限位：黄点贴边 = 出界方向封死（世界坐标的绝对墙）")
Z = [0.2, 0.0, 0.8, 1.0]                 # 允许活动 20%~80%
check("_mm_block：中间不拦", bot._mm_block(Z, [0.5, 0.5]) is None,
      bot._mm_block(Z, [0.5, 0.5]))
check("_mm_block：贴左墙 → 封左", bot._mm_block(Z, [0.15, 0.5]) == -1,
      bot._mm_block(Z, [0.15, 0.5]))
check("_mm_block：贴右墙 → 封右", bot._mm_block(Z, [0.85, 0.5]) == 1,
      bot._mm_block(Z, [0.85, 0.5]))
check("_mm_block：没圈范围不拦", bot._mm_block([], [0.99, 0.5]) is None)
check("_mm_block：黄点丢了不拦", bot._mm_block(Z, None) is None)

# 怪在右边（识别区外但够得着）：黄点已在右墙 → 不追，转巡逻
b = make()
b.cfg["patrol"] = True
b.cfg["mm_zone"] = Z
b._patrol_dir = 1                        # 巡逻本来要往右
far_r = [{"name": "Stump", "box": [1100, 300, 1160, 360], "conf": 0.9}]
c = step(b, m=far_r, size=(1366, 768), dot=[0.79, 0.5])
check("黄点贴右墙：不往右追", "right" not in down_keys(c), c)
check("目标被当不存在", b.snapshot()[1].get("target") == "", b.snapshot()[1])
# 黄点丢了（None）：墙失效，照常追出去（fail-open，别钉死）
b2 = make()
b2.cfg["mm_zone"] = Z
c = step(b2, m=far_r, size=(1366, 768), dot=None)
check("黄点丢了：照常追", "right" in down_keys(c), c)

# 巡逻撞小地图墙：立刻掉头（识别区那道墙还远着）
b = make()
b.cfg["patrol"] = True
b.cfg["mm_zone"] = Z
b._patrol_dir = -1                       # 往左巡逻，黄点已贴左墙
b._t_patrol = time.time()
c = step(b, m=[], size=(1366, 768), roi=None, dot=[0.15, 0.5])
check("巡逻撞小地图左墙 → 这一帧不按 left", "left" not in down_keys(c), c)
check("巡逻方向翻过来了", b._patrol_dir == 1, b._patrol_dir)
c = step(b, m=[], size=(1366, 768), roi=None, dot=[0.15, 0.5])
check("下一帧往右巡逻", b._held == 1, b._held)

# 没圈范围的配置：一切如旧
b = make()
b.cfg["patrol"] = True
c = step(b, m=far_r, size=(1366, 768), dot=[0.99, 0.5])
check("没圈范围：照常往右追", "right" in down_keys(c), c)

# clamp：坏值/顺序自动整理
check("mm_zone 乱序自动整理", bot.clamp_cfg({"mm_zone": [0.8, 1, 0.2, 0]})["mm_zone"]
      == [0.2, 0.0, 0.8, 1.0], bot.clamp_cfg({"mm_zone": [0.8, 1, 0.2, 0]})["mm_zone"])
check("mm_zone 非法 → 空（关）", bot.clamp_cfg({"mm_zone": "x"})["mm_zone"] == [])
check("mm_zone 太窄 → 空（关）", bot.clamp_cfg({"mm_zone": [0.4, 0, 0.41, 1]})["mm_zone"] == [])

# 巡逻按范围扫：黄点在跟时过了 patrol_sec 也不换向（扫到边才掉头）
b = make()
b.cfg["patrol"] = True
b.cfg["mm_zone"] = Z
b._patrol_dir = 1                        # 往右扫，黄点在范围中间
b._t_patrol = time.time() - 10           # 早就过了 2.5s 换向间隔
c = step(b, m=[], size=(1366, 768), roi=None, dot=[0.5, 0.5], now=time.time())
check("范围开着：过时不掉头，继续往右扫", b._patrol_dir == 1, b._patrol_dir)
check("范围开着：还是按着 right", "right" in down_keys(c), c)
# 黄点丢了（None）：退回定时换向
b2 = make()
b2.cfg["patrol"] = True
b2.cfg["mm_zone"] = Z
b2._patrol_dir = 1
b2._t_patrol = time.time() - 10
step(b2, m=[], size=(1366, 768), roi=None, dot=None, now=time.time())
check("黄点丢了：定时换向照常", b2._patrol_dir == -1, b2._patrol_dir)
# 没圈范围：定时换向照常
b3 = make()
b3.cfg["patrol"] = True
b3._patrol_dir = 1
b3._t_patrol = time.time() - 10
step(b3, m=[], size=(1366, 768), roi=None, dot=[0.5, 0.5], now=time.time())
check("没圈范围：定时换向照常", b3._patrol_dir == -1, b3._patrol_dir)

print(f"\n通过 {ok} / {ok + fail}")
sys.exit(1 if fail else 0)
