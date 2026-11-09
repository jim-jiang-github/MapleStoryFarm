# -*- coding: utf-8 -*-
"""char_body_box + 重叠过滤的判定逻辑测试。

server.py 是个会开线程抓屏的大模块，没法直接 import 进测试进程。
这里用 ast 把 char_body_box / overlap_min 两个纯函数的源码抠出来 exec，
测的就是 server.py 里真正跑的那份代码文本——改了函数这里会跟着变。
跑法：python tools/test_charbox.py
"""
import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "webui" / "server.py"
WANT = {"char_body_box", "overlap_min"}

tree = ast.parse(SRC.read_text(encoding="utf-8"))
picked = {}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT:
        picked[node.name] = ast.get_source_segment(SRC.read_text(encoding="utf-8"), node)
missing = WANT - set(picked)
assert not missing, f"server.py 里缺函数: {missing}"

ns = {}
exec(compile("\n\n".join(picked.values()), str(SRC), "exec"), ns)
char_body_box, overlap_min = ns["char_body_box"], ns["overlap_min"]

HIDE = 0.8   # 和 server.py 里的 CHAR_OVERLAP_HIDE 保持一致

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)
    print(("PASS " if cond else "FAIL ") + name)


# ---- 身体框推导：牌框 [684,412,738,427]（54x15 名字牌）----
b = char_body_box([684, 412, 738, 427])
check("身体框在牌子上方", b[3] <= 412 and b[1] < b[3])
check("身高约 60*(15/16)=56", 50 <= b[3] - b[1] <= 62)
check("牌底到身底留 ~8px 空隙", 4 <= 412 - b[3] <= 12)
check("身体水平居中于牌子", abs((b[0] + b[2]) / 2 - 711) < 0.6)
check("身宽约身高的 0.9（bw 是半宽）", abs((b[2] - b[0]) - 0.9 * (b[3] - b[1])) < 1)

# 换分辨率（牌高翻倍）等比放大
b2 = char_body_box([0, 400, 108, 430])
check("牌高翻倍身体等比放大", abs((b2[3] - b2[1]) / (b[3] - b[1]) - 2) < 0.05)

# 异常输入：扁到 8px 的框也别炸
b3 = char_body_box([10, 100, 30, 100])
check("高度 0 的牌框也能算", b3[3] > b3[1] and b3[2] > b3[0])

# ---- 重叠过滤判定 ----
CB = char_body_box([684, 412, 738, 427])          # 人物身体框


def hidden(mbox):
    return overlap_min(mbox, CB) >= HIDE


# 怪物框完全套在身体框里（站在人身上的怪）→ 隐藏
check("完全在身体框内 → 隐藏", hidden([CB[0] + 4, CB[1] + 4, CB[2] - 4, CB[3] - 4]))
# 身体框完全在怪物框里（巨怪罩住人）→ 隐藏
big = [CB[0] - 80, CB[1] - 80, CB[2] + 80, CB[3] + 80]
check("怪物框罩住整个人 → 隐藏", hidden(big))
# 贴身但只重叠一半 → 显示（还要正常打）
half = [CB[2] - 15, CB[1] + 5, CB[2] + 45, CB[3] - 5]
check("半重叠 → 显示", not hidden(half))
# 挨着站、零交集 → 显示
side = [CB[2] + 2, CB[1], CB[2] + 60, CB[3]]
check("零交集 → 显示", not hidden(side))
# 略微搭边 10% → 显示
edge = [CB[2] - 6, CB[1], CB[2] + 50, CB[3]]
check("搭边 10% → 显示", not hidden(edge))
# 上下平台的怪（dy 远）→ 永远显示
check("下层平台的怪 → 显示", not hidden([CB[0], CB[3] + 120, CB[2], CB[3] + 180]))

# 过滤时机语义：pick_visible 已经把 shown 记上了，隐藏期间不清 shown，
# 怪从人身上走开后应立即重新出现（不重新攒 show_hits）——这里是文档性断言。
check("HIDE 阈值在 (0.5, 1) 区间", 0.5 < HIDE <= 1)

print()
if fails:
    print(f"{len(fails)} 项失败: {fails}")
    sys.exit(1)
print("全部通过")
