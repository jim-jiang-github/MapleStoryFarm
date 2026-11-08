# -*- coding: utf-8 -*-
"""在同一批真实帧上对比各模型版本的检出质量（以 best.pt @960 为参考基准）。

指标:
  dets   : 每帧平均检出数（越高不一定越好，含误检）
  conf   : 平均置信度
  recall : 与参考模型框匹配上的比例（IoU>0.5 且类名相同）—— 漏检的代理指标
  extra  : 参考模型没有、自己多出来的框数/帧 —— 误检的代理指标
"""
import glob
import sys
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, "webui")

import cv2  # noqa: E402

from ultralytics import YOLO  # noqa: E402


def iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    iw = max(0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


def detect(model, frame, imgsz, conf):
    r = model.predict(frame, imgsz=imgsz, conf=conf, verbose=False, device="cpu")[0]
    out = []
    for xyxy, c, cl in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(),
                           r.boxes.cls.tolist()):
        out.append((xyxy, r.names[int(cl)].split("_", 1)[-1], float(c)))
    return out


def main():
    files = sorted(glob.glob(".image/calib/*.jpg"))
    files = files[::2][:30]
    frames = [cv2.imread(f) for f in files]
    frames = [f for f in frames if f is not None]
    print(f"frames: {len(frames)}  size: {frames[0].shape[1]}x{frames[0].shape[0]}")

    # 参考基准: 原始训练权重 @ 960（用户说"正确率很高"的那个版本）
    ref_model = YOLO("best.pt")
    refs = []
    t0 = time.perf_counter()
    for f in frames:
        refs.append(detect(ref_model, f, 960, 0.25))
    t_ref = (time.perf_counter() - t0) / len(frames) * 1000
    print(f"reference best.pt@960: {sum(len(r) for r in refs)/len(refs):.1f} dets/frame, "
          f"{t_ref:.0f} ms/frame\n")

    variants = [
        ("FP32 544x960", "model_544x960_openvino_model", (544, 960), 0.25),
        ("FP32 384x672", "model_384x672_openvino_model", (384, 672), 0.25),
        ("INT8 544x960", "model_int8_openvino_model", (544, 960), 0.25),
        ("INT8 384x672", "model_int8_fast_openvino_model", (384, 672), 0.25),
        ("INT8 384x672 conf.40", "model_int8_fast_openvino_model", (384, 672), 0.40),
        ("INT8 384x672 conf.50", "model_int8_fast_openvino_model", (384, 672), 0.50),
    ]

    print(f"{'variant':22s} {'dets':>5s} {'conf':>5s} {'recall':>7s} {'extra':>6s} {'ms':>6s}")
    for tag, path, imgsz, conf in variants:
        m = YOLO(path, task="detect")
        t0 = time.perf_counter()
        outs = [detect(m, f, imgsz, conf) for f in frames]
        ms = (time.perf_counter() - t0) / len(frames) * 1000

        n_det = sum(len(o) for o in outs) / len(outs)
        confs = [d[2] for o in outs for d in o]
        mean_conf = sum(confs) / len(confs) if confs else 0.0

        matched = total_ref = extra = 0
        for ref, out in zip(refs, outs):
            used = set()
            for rb, rn, _ in ref:
                hit = None
                for i, (ob, on, oc) in enumerate(out):
                    if i in used or on != rn:
                        continue
                    v = iou(rb, ob)
                    if v > 0.5 and (hit is None or v > hit[0]):
                        hit = (v, i)
                if hit:
                    used.add(hit[1])
                    matched += 1
                total_ref += 1
            extra += len(out) - len(used)

        recall = matched / total_ref * 100 if total_ref else 0
        print(f"{tag:22s} {n_det:5.1f} {mean_conf:5.2f} {recall:6.1f}% "
              f"{extra/len(frames):6.1f} {ms:6.0f}")


if __name__ == "__main__":
    main()
