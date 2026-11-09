# -*- coding: utf-8 -*-
"""识别冷启动进度（det_progress）的判定逻辑测试。

server.py 会开线程抓屏，不能直接 import 进测试。这里用 ast 把 det_progress 的
源码和 DET_STAGES / DET_EXPECT_S 两个常量抠出来 exec，测的就是线上跑的那份代码。
跑法：python tools/test_detprog.py
"""
import ast
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "webui" / "server.py"
text = SRC.read_text(encoding="utf-8")
tree = ast.parse(text)

fn_src = None
consts = {}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name == "det_progress":
        fn_src = ast.get_source_segment(text, node)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in ("DET_STAGES", "DET_EXPECT_S"):
                consts[t.id] = ast.literal_eval(node.value)

assert fn_src, "server.py 里没有 det_progress()"
assert set(consts) == {"DET_STAGES", "DET_EXPECT_S"}, f"常量缺失: {consts}"

# 假时钟：NOW 可变，det_progress 只看 perf_counter 的差值
NOW = [1000.0]
state = {
    "enabled": False, "error": "", "stage": "idle", "det_t0": 0.0,
}
grabber = types.SimpleNamespace(window_found=True)

ns = {
    "time": types.SimpleNamespace(perf_counter=lambda: NOW[0]),
    "grabber": grabber,
    "det_state": state,
    **consts,
}
exec(compile(fn_src, str(SRC), "exec"), ns)
det_progress = ns["det_progress"]

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {name}")
    else:
        fail += 1
        print(f"  FAIL {name}  {detail}")


def start(stage, elapsed_s, window=True):
    """把状态摆到「某阶段、已用 elapsed_s、窗口在不在」。"""
    state.update({"enabled": True, "error": "", "stage": stage,
                  "det_t0": NOW[0] - elapsed_s})
    grabber.window_found = window
    return det_progress()


print("[1] 没开识别 → 什么都不报")
state.update({"enabled": False, "error": "", "stage": "idle", "det_t0": 0.0})
p = det_progress()
check("stage=idle", p["stage"] == "idle", p)
check("pct=0", p["pct"] == 0, p)
check("没有 waiting 文案", p["waiting"] == "", p)

print("[2] 导入推理库：按总时长线性走")
for secs, want in ((0.0, 0), (3.0, 24), (6.0, 48), (12.0, 96)):
    p = start("import", secs)
    check(f"{secs}s → {want}%", p["pct"] == want, p)
    check(f"{secs}s 的标签是「导入推理库」", p["label"] == "导入推理库", p)

print("[3] 超时不假装 100%（100 只留给首帧跑通）")
p = start("import", 60)
check("封顶 96%", p["pct"] == 96, p)
p = start("warmup", 60)
check("首帧检测也封顶 96%", p["pct"] == 96, p)

print("[4] 各阶段标签如实反映现在卡在哪")
for stage, label in (("import", "导入推理库"), ("load", "加载模型"),
                     ("warmup", "首帧检测")):
    p = start(stage, 1.0)
    check(f"{stage} → {label}", p["label"] == label, p)

print("[5] 就绪")
p = start("ready", 9.0)
check("pct=100", p["pct"] == 100, p)
check("stage=ready", p["stage"] == "ready", p)
check("标签是「就绪」", p["label"] == "就绪", p)

print("[6] 启动失败")
state.update({"enabled": True, "error": "推理库导入失败: X", "stage": "error",
              "det_t0": NOW[0] - 3})
p = det_progress()
check("stage=error", p["stage"] == "error", p)
check("报错原文带出来", p["waiting"] == "推理库导入失败: X", p)

print("[7] 窗口不在时不要假装在算")
p = start("warmup", 5.0, window=False)
check("首帧阶段卡在 40% 以内", p["pct"] <= 40, p)
check("提示找不到窗口", p["waiting"] == "找不到游戏窗口", p)
p = start("import", 5.0, window=False)
check("导入库阶段不受窗口影响", p["waiting"] == "" and p["pct"] == 40, p)
p = start("ready", 9.0, window=False)
check("就绪但窗口不在：仍报 100% + 提示", p["pct"] == 100
      and p["waiting"] == "找不到游戏窗口", p)

print("[8] 进度单调不回退")
prev = -1
good = True
for i in range(0, 121):
    p = start("warmup", i * 0.1)
    if p["pct"] < prev:
        good = False
    prev = p["pct"]
check("0~12 秒采样不回退", good)

print(f"\n通过 {ok} / {ok + fail}")
sys.exit(1 if fail else 0)
