"""多目标跟踪：卡尔曼预测 + 匈牙利匹配 + ByteTrack 两阶段关联 + OC-SORT 方向一致。

为什么换掉原来的「IoU 贪心 + 最近距离兜底」：
- 贪心匹配逐对挑最高 IoU，一个错误配对会连累后面的轨迹，快速移动时成片换 ID；
- 「最近距离兜底」没有方向约束，旁边只要站着一只同类怪就会被抢走；
- 框不做运动外推，漏检那几帧框停在原地，等检测回来已经对不上。

这里用的是公开方案里的成熟组合（均为论文 + 主流开源实现的默认做法）：
- ByteTrack (Zhang et al., ECCV 2022)：先用高分框关联，再用低分框补一次，
  检测器偶尔掉分不至于直接丢目标；track_buffer 内保留丢失轨迹。
- OC-SORT (Cao et al., CVPR 2023)：关联代价里加一项「速度方向一致性」，
  惩罚方向突变，避免两只怪交叉时互换 ID。
- SORT / DeepSORT：8 维状态 [cx, cy, a, h, vcx, vcy, va, vh] 的匀速卡尔曼，
  过程/观测噪声按目标高度缩放（同一套比例，画面缩放不用重新调）。

依赖只有 numpy / OpenCV / scipy，不引入 lap、filterpy 之类的新包。
"""

from __future__ import annotations

import numpy as np
import cv2
from scipy.optimize import linear_sum_assignment

# ---- 卡尔曼噪声比例（SORT 的标准取值，乘以目标高度 h 使用）----
_STD_POS = 1.0 / 20      # 观测：中心位置
_STD_AREA = 1.0 / 160    # 观测：宽高比
_STD_VEL = 1.0 / 160     # 过程：速度


def iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


def _box_to_z(box):
    """[x1,y1,x2,y2] -> [cx, cy, a=w/h, h]"""
    x1, y1, x2, y2 = box
    w = max(1.0, x2 - x1)
    h = max(1.0, y2 - y1)
    return np.array([x1 + w / 2, y1 + h / 2, w / h, h], dtype=np.float32)


def _z_to_box(x):
    v = np.asarray(x, dtype=np.float32).reshape(-1)   # cv2 返回 (8,1)，先展平
    cx, cy, a, h = float(v[0]), float(v[1]), float(v[2]), float(v[3])
    h = max(1.0, h)
    w = max(1.0, a * h)
    return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]


def _new_kf(z):
    """8 维匀速模型：位置/宽高比 4 维 + 对应速度 4 维。"""
    kf = cv2.KalmanFilter(8, 4)
    F = np.eye(8, dtype=np.float32)
    for i in range(4):
        F[i, i + 4] = 1.0
    H = np.zeros((4, 8), dtype=np.float32)
    for i in range(4):
        H[i, i] = 1.0
    kf.transitionMatrix = F
    kf.measurementMatrix = H
    kf.statePost = np.array([z[0], z[1], z[2], z[3], 0, 0, 0, 0], dtype=np.float32).reshape(8, 1)
    kf.statePre = kf.statePost.copy()
    kf.errorCovPost = np.eye(8, dtype=np.float32) * 10.0
    _set_noise(kf, float(z[3]))
    return kf


def _set_noise(kf, h):
    """按目标高度缩放噪声：远处的小目标位置抖动本来就小，用同一套像素噪声会过冲。"""
    h = max(8.0, float(h))
    p = _STD_POS * h
    q_pos = np.square([p, p, _STD_AREA, p]).astype(np.float32)
    q_vel = np.square([_STD_VEL * h, _STD_VEL * h, _STD_AREA * 0.5, _STD_VEL * h]).astype(np.float32)
    kf.processNoiseCov = np.diag(np.concatenate([q_pos, q_vel])).astype(np.float32)
    kf.measurementNoiseCov = np.diag(np.square([p, p, _STD_AREA, p])).astype(np.float32)


class Tracker:
    """tracking-by-detection。

    update(dets, shift) 的参数与返回：
      dets  : [(box, name, conf)]，box 为全分辨率像素 [x1,y1,x2,y2]
      shift : 整幅画面平移 (dx, dy)，由相位相关估计，用于相机滚动补偿
      返回  : 已确认的轨迹列表，每项是 dict：tid / box / name / conf / hits / misses
    """

    def __init__(self, high_thresh=0.30, low_thresh=0.10, new_thresh=0.30,
                 match_thresh=0.90, max_age=15, min_hits=2,
                 inertia=0.20, class_margin=6, resurrect_frames=25):
        self.high_thresh = high_thresh
        self.low_thresh = low_thresh
        self.new_thresh = new_thresh
        # 代价 1-IoU 的上限：0.9 => IoU>=0.1 就能配。密集/快速场景实测比 0.8 稳得多，
        # 因为真正决定配不配的是全局最优 + 方向一致性，而不是单个 IoU 是否够大
        self.match_thresh = match_thresh
        self.max_age = max_age             # 丢失多少帧后删除轨迹（ByteTrack 的 track_buffer）
        self.min_hits = min_hits           # 连续命中多少次才确认（滤掉瞬现误检）
        self.inertia = inertia             # 方向一致性代价权重（OC-SORT，0.2 是官方默认）
        self.class_margin = class_margin
        self.resurrect_frames = resurrect_frames

        self.tracks = {}                   # tid -> 轨迹 dict（含 kf）
        self.lost = {}                     # tid -> 复活缓存
        self.next_id = 1
        self.pending = 0

    # ---------------- 主流程 ----------------

    def update(self, dets, shift=(0.0, 0.0)):
        dx, dy = shift

        # 1) 相机滚动补偿：轨迹整体平移。卡尔曼状态也要跟着移，否则下一帧预测会把它拉回原处
        if abs(dx) + abs(dy) > 0.01:
            for tr in self.tracks.values():
                b = tr["box"]
                tr["box"] = [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]
                kf = tr["kf"]
                kf.statePost[0] += dx
                kf.statePost[1] += dy
                kf.statePre[0] += dx
                kf.statePre[1] += dy

        # 2) 所有轨迹先做一次预测（这是「预测」的核心：框按速度往前推，不是停在原地等）
        hi, lo = [], []
        for box, name, conf in dets:
            (hi if conf >= self.high_thresh else lo).append((box, name, conf))
        lo = [d for d in lo if d[2] >= self.low_thresh]

        tids = list(self.tracks.keys())
        preds = {}
        for t in tids:
            tr = self.tracks[t]
            kf = tr["kf"]
            _set_noise(kf, tr["box"][3] - tr["box"][1])
            pred = kf.predict()
            # cv2 的 predict() 只写 statePre，不推进 statePost。不手动同步的话，
            # 下一帧还是从上次「校正过」的位置再外推一步，等于永远只走一格：
            # 一旦漏检，框就停在原地不动，也就谈不上预测了。
            kf.statePost = pred.copy()
            kf.errorCovPost = kf.errorCovPre.copy()
            preds[t] = _z_to_box(pred)      # 预测框：给关联用，也直接当漏检时的显示框

        # 3) 第一轮关联：高分框 <-> 全部轨迹
        m1, un_t, un_d = self._assign(tids, hi, preds)
        for t, di in m1:
            self._correct(t, hi[di])

        # 4) 第二轮关联（ByteTrack）：低分框 <-> 第一轮落空的轨迹
        if lo and un_t:
            m2, _un_t, _un_d = self._assign(un_t, lo, preds)
            for t, di in m2:
                self._correct(t, lo[di])
                self.tracks[t]["low"] += 1
        else:
            m2 = []

        # 5) 仍未匹配的轨迹：短暂外推几帧，之后停在最后观测位置
        #    —— 一直按速度推会在目标变向/被遮挡时飘走，等它重新出现就对不上了
        used = {t for t, _ in m1} | {t for t, _ in m2}
        dead = []
        for t in tids:
            tr = self.tracks[t]
            if t in used:
                tr["misses"] = 0
                continue
            tr["misses"] += 1
            tr["streak"] = 0                 # 连续命中被打断，已确认状态不撤销
            if tr["misses"] <= 3:
                tr["box"] = list(preds[t])
            else:
                tr["box"] = list(tr["last_box"])
                tr["kf"].statePost[4:] = 0.0  # 停止外推，只保留位置
            if tr["misses"] > self.max_age:
                dead.append(t)
        for t in dead:
            tr = self.tracks.pop(t)
            self.lost[t] = {"name": tr["name"], "box": tr["box"], "age": 0}
        for t in list(self.lost):
            self.lost[t]["age"] += 1
            if self.lost[t]["age"] > self.resurrect_frames:
                del self.lost[t]

        # 6) 新建轨迹：第一轮没被匹配、且分数够高的检测
        matched_hi = {di for _, di in m1}
        for di, (box, name, conf) in enumerate(hi):
            if di in matched_hi:
                continue
            if conf < self.new_thresh:
                continue
            tid = self._take_lost(box, name) or self.next_id
            if tid == self.next_id:
                self.next_id += 1
            self.tracks[tid] = {
                "tid": tid, "box": list(box), "last_box": list(box),
                "kf": _new_kf(_box_to_z(box)),
                "name": name, "conf": conf, "hits": 1, "streak": 1, "misses": 0,
                "low": 0, "confirmed": False, "votes": {name: 1},
                "hist": [((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)],
            }

        self.pending = sum(1 for tr in self.tracks.values() if not tr["confirmed"])
        return [dict(tr, kf=None) for tr in self.tracks.values() if tr["confirmed"]]

    # ---------------- 关联 ----------------

    def _assign(self, tids, dets, preds):
        """匈牙利最优关联。代价 = 1 - IoU(预测框, 检测框) + 方向一致性惩罚。"""
        if not tids or not dets:
            return [], list(tids), list(range(len(dets)))

        cost = np.zeros((len(tids), len(dets)), dtype=np.float32)
        for i, t in enumerate(tids):
            tr = self.tracks[t]
            dirv = self._dir(tr)
            last = tr["box"]
            for j, (box, name, conf) in enumerate(dets):
                # 预测框和原始框取较大的 IoU：预测准的时候它救回快速目标，
                # 预测偏的时候（瞬移、转向）退回原始框，不添乱
                c = 1.0 - max(iou(preds[t], box), iou(last, box))
                # 类别不同也允许关联（模型类 ID 别名多），但要加罚
                if name != tr["name"]:
                    c += 0.35
                # OC-SORT 方向一致性：观测位移方向与轨迹速度方向夹角越大越罚
                if dirv is not None:
                    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                    lx, ly = tr["hist"][-1]
                    ox, oy = cx - lx, cy - ly
                    n1 = (dirv[0] ** 2 + dirv[1] ** 2) ** 0.5
                    n2 = (ox * ox + oy * oy) ** 0.5
                    if n1 > 1e-3 and n2 > 1e-3:
                        cos = (dirv[0] * ox + dirv[1] * oy) / (n1 * n2)
                        c += self.inertia * (1.0 - cos)
                cost[i, j] = c

        ri, ci = linear_sum_assignment(cost)
        matched, ut, ud = [], set(range(len(tids))), set(range(len(dets)))
        for i, j in zip(ri, ci):
            if cost[i, j] > self.match_thresh:
                continue        # 代价超阈值视为没配上，宁可让它丢一帧也不乱配
            matched.append((tids[i], int(j)))
            ut.discard(i)
            ud.discard(j)
        return matched, [tids[i] for i in sorted(ut)], sorted(ud)

    def _dir(self, tr):
        """轨迹速度方向（用卡尔曼状态里的速度，比两帧差分稳）。"""
        sp = np.asarray(tr["kf"].statePost, dtype=np.float32).reshape(-1)
        vx, vy = float(sp[4]), float(sp[5])
        if vx * vx + vy * vy < 4.0:     # 速度太小方向没意义
            return None
        return (vx, vy)

    def _correct(self, t, det):
        box, name, conf = det
        tr = self.tracks[t]
        kf = tr["kf"]
        _set_noise(kf, max(8.0, box[3] - box[1]))
        kf.correct(_box_to_z(box).reshape(4, 1))
        tr["box"] = _z_to_box(kf.statePost)
        tr["last_box"] = list(box)          # 最后观测到的框（漏检时退回这里）
        tr["conf"] = 0.6 * conf + 0.4 * tr["conf"]
        tr["hits"] += 1
        tr["streak"] += 1
        tr["misses"] = 0
        tr["hist"].append(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
        if len(tr["hist"]) > 8:
            tr["hist"].pop(0)
        self._vote(tr, name)
        if tr["streak"] >= self.min_hits:
            tr["confirmed"] = True

    def _take_lost(self, box, name):
        """目标短暂消失后又出现：复用原 tid，画面上的框和颜色都不会跳。"""
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        bw, bh = box[2] - box[0], box[3] - box[1]
        best, best_d = None, None
        for tid, tr in self.lost.items():
            if tr["name"] != name:
                continue
            lx = (tr["box"][0] + tr["box"][2]) / 2
            ly = (tr["box"][1] + tr["box"][3]) / 2
            d = ((cx - lx) ** 2 + (cy - ly) ** 2) ** 0.5
            if d <= max(120.0, 1.0 * (bw + bh)) and (best_d is None or d < best_d):
                best, best_d = tid, d
        if best is not None:
            del self.lost[best]
        return best

    def _vote(self, tr, name):
        """类别投票：确认后要明显领先才改判，防止树/蜗牛之间来回横跳。"""
        votes = tr["votes"]
        votes[name] = votes.get(name, 0) + 1
        if sum(votes.values()) > 60:
            for k in votes:
                votes[k] = max(1, votes[k] // 2)
        best = max(votes, key=votes.get)
        if best == tr["name"]:
            return
        if not tr["confirmed"] or votes[best] >= votes.get(tr["name"], 0) + self.class_margin:
            tr["name"] = best

    # ---------------- 辅助 ----------------

    def reset(self):
        self.tracks.clear()
        self.lost.clear()
        self.pending = 0
