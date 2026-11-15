# -*- coding: utf-8 -*-
"""用指定权重对已采集帧重建 YOLO 检测缓存 (dets_cache.jsonl).

缓存后参数扫描秒级完成, 无需反复推理.
兼容两种遥测格式: 拍平 {idx,phase,gx,gy} 与原始 {idx,tel{game,mainShape}}.
用法: python tools/build_dets_cache.py --weights kaggle_out/ft3/best_ft3.pt \
        --dir kaggle_out/fps_exp --stride 3 --out dets_cache_ft3.jsonl
"""
import argparse
import json
import os
import sys

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def flatten(r):
    """遥测行 → (idx, phase, gx, gy), 兼容拍平/嵌套两种格式."""
    idx = r["idx"]
    if "tel" in r:
        tel = r["tel"]
        ph = tel.get("game", {}).get("phase")
        c = (tel.get("mainShape") or {}).get("center") or {}
        return idx, ph, c.get("x"), c.get("y")
    return idx, r.get("phase"), r.get("gx"), r.get("gy")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=os.path.join(ROOT, "kaggle_out", "n2", "best.pt"))
    ap.add_argument("--dir", default=os.path.join(ROOT, "kaggle_out", "fps_exp"))
    ap.add_argument("--conf", type=float, default=0.10)
    ap.add_argument("--stride", type=int, default=3)
    ap.add_argument("--out", default="dets_cache.jsonl")
    args = ap.parse_args()

    from ultralytics import YOLO
    model = YOLO(args.weights)

    tel_path = os.path.join(args.dir, "telemetry.jsonl")
    raw = [json.loads(l) for l in open(tel_path, encoding="utf-8")]

    out_path = os.path.join(args.dir, args.out)
    n = 0
    prev_ph = None
    rnd = 0
    with open(out_path, "w", encoding="utf-8") as fp:
        for r in raw:
            idx, ph, gx, gy = flatten(r)
            if idx % args.stride:
                prev_ph = ph if ph is not None else prev_ph
                continue
            if ph == "count" and prev_ph not in ("count", None):
                rnd += 1          # 新局: count 段起点 (game.round 不随 replay 递增)
            prev_ph = ph if ph is not None else prev_ph
            img_path = os.path.join(args.dir, f"f{idx:05d}.jpg")
            img = cv2.imread(img_path)
            if img is None:
                continue
            res = model.predict(img, imgsz=640, conf=args.conf, verbose=False)[0]
            dets = []
            for b in res.boxes:
                cls = int(b.cls[0])
                if cls in (0, 1):
                    x1, y1, x2, y2 = b.xyxy[0].tolist()
                    dets.append(((x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1,
                                 float(b.conf[0]), cls))
            fp.write(json.dumps(dict(idx=idx, phase=ph, gx=gx, gy=gy, round=rnd,
                                     dets=dets), ensure_ascii=False) + "\n")
            n += 1
            if n % 200 == 0:
                print(f"  {n} frames done...", flush=True)
    print(f"cache written: {out_path} ({n} entries, {rnd + 1} rounds, stride={args.stride})")


if __name__ == "__main__":
    main()
