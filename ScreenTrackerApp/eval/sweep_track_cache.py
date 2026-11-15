# -*- coding: utf-8 -*-
"""缓存级追踪器参数扫描: dets_cache.jsonl + 遥测 GT + YoloTracker 回放.

比 sim_track_rot_val (逐帧推理) 快两个数量级, 秒级试参数组合.
注意: 追踪器 fps 需与缓存的采样率一致 (stride3@29.8fps ≈ 10fps → fps=10).
用法: python tools/sweep_track_cache.py --cache dets_cache_ft3.jsonl \
        --params chase_need=0.5,hijack_conf=0.78
"""
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from track_core import YoloTracker

NEAR = 25.0


def run(cache, trk, reset_rounds=True):
    stats = {p: dict(n=0, hit=0, errs=[]) for p in ("count", "fade", "hidden")}
    prev_ph = None
    for r in cache:
        # 每局重置: 采样流上 phase 从 done/idle/result 进入 count = 新局
        # (round 字段在边界帧未采样时会漏计, phase 转变更可靠)
        if reset_rounds and r.get("phase") == "count" and prev_ph not in ("count", None):
            trk.__init__(fps=trk.fps)
            for k, v in getattr(trk, "_param_overrides", {}).items():
                setattr(trk, k, v)
        prev_ph = r.get("phase")
        st = trk.update(r["dets"])
        if r["gx"] is None or r["phase"] not in stats:
            continue
        if trk.pos is not None:
            err = float(np.hypot(trk.pos[0] - r["gx"], trk.pos[1] - r["gy"]))
            s = stats[r["phase"]]
            s["n"] += 1
            if err <= NEAR:
                s["hit"] += 1
            s["errs"].append(err)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.join(ROOT, "kaggle_out", "fps_exp", "dets_cache_s3.jsonl"))
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--params", default="", help="k=v,k=v 覆盖 YoloTracker 属性")
    ap.add_argument("--rounds", default="", help="逗号分隔的参数网格, 如 chase_need=0.4,0.5,0.6")
    args = ap.parse_args()

    cache = [json.loads(l) for l in open(args.cache, encoding="utf-8")]

    grid = []
    if args.rounds:
        # 笛卡尔积: 每个 key 一组值
        import itertools
        combos = []
        for item in args.rounds.split(";"):
            k, vals = item.split("=")
            combos.append((k, [float(v) for v in vals.split(",")]))
        keys = [k for k, _ in combos]
        for values in itertools.product(*[v for _, v in combos]):
            grid.append(dict(zip(keys, values)))
    else:
        grid.append({})
    base_over = {}
    for kv in filter(None, args.params.split(",")):
        k, v = kv.split("=")
        try:
            v = float(v)
        except ValueError:
            pass
        base_over[k] = v

    for g in grid:
        trk = YoloTracker(fps=args.fps)
        over = {**base_over, **g}
        for k, v in over.items():
            setattr(trk, k, v)
        trk._param_overrides = over      # 每局重置后重新应用
        stats = run(cache, trk)
        tag = ",".join(f"{k}={v}" for k, v in {**base_over, **g}.items()) or "default"
        line = f"[{tag}] "
        for ph, s in stats.items():
            if s["n"]:
                e = np.array(s["errs"])
                line += f"{ph}:{s['hit']/s['n']*100:5.1f}% "
        hid = stats["hidden"]
        if hid["n"]:
            e = np.array(hid["errs"])
            print(f"{line} | hidden n={hid['n']} med={np.median(e):.1f}px p90={np.percentile(e,90):.1f}px")
        else:
            print(line)


if __name__ == "__main__":
    main()
