# -*- coding: utf-8 -*-
"""YOLO 检测驱动的匀速追踪器 (按用户总结的目标行为规则).

规则: 开局正中出现白团; 匀速慢移不跳跃; 边旋转边飘; 同一时刻只有一个目标.
设计: 追踪控制器(速度限幅+转向限速) + 运动门控(门外检测=误检) +
     滑行惯性(隐身弱检测期) + 新局重捕(中心高置信白团).
"""
import math


class YoloTracker:
    def __init__(self, fps=10.0, vmax=200.0):
        self.fps = fps
        self.vmax_f = vmax / fps          # px/帧
        self.chase_need = 0.5             # 追赶: 期望速度 = need * 落后距离
        self.chase_cap = 1.8              # 追赶上限倍数 (滑行期 2.2)
        self.hijack_conf = 0.78           # 全局劫持置信度阈值
        self.state = "INIT"
        self.pos = None
        self.vel = (0.0, 0.0)
        self.cruise = None                # 巡航速度 EMA (检测位移实测)
        self.last_det = None              # 上一帧被接受的检测中心
        self.coast = 0                    # 连续无检测帧数
        self.lost_t = 0
        self.box = None
        self.conf = 0.0
        self.cls = 0
        self.rejects = []
        self.n_det = 0
        self.n_rej = 0
        self.prev_dets = []               # 上一帧全部主类检测中心 (持续性过滤用)

    # ---------- 工具 ----------
    @staticmethod
    def _dist(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    def _gate(self):
        sp = math.hypot(*self.vel)
        return 26.0 + 4.0 * sp

    # ---------- 主更新: dets = [(cx, cy, w, h, conf, cls)] ----------
    def update(self, dets):
        st = self._update_impl(dets)
        self.prev_dets = [(d[0], d[1]) for d in dets if d[5] in (0, 1)]
        return st

    def _update_impl(self, dets):
        self.rejects = []
        main = [d for d in dets if d[5] in (0, 1)]

        # 新局重捕: 高置信白团(vortex 也算)出现在中心 30% 区域
        cx0, cy0, R = 375.0, 250.0, 130.0
        cands = [d for d in main if d[4] >= 0.55
                 and self._dist((d[0], d[1]), (cx0, cy0)) <= R]
        # 新局规则: 每局图形在正中生成 -> 中心 80px 内持久 2 帧的强检测直接接管
        center_new = [d for d in main if d[4] >= 0.70
                      and self._dist((d[0], d[1]), (cx0, cy0)) <= 80.0
                      and any(self._dist((d[0], d[1]), p) <= 25.0 for p in self.prev_dets)]
        if self.state in ("INIT", "LOST") or center_new or (self.pos and
                self._dist(self.pos, (cx0, cy0)) > R * 1.2 and cands):
            if cands or center_new:
                pool = cands + center_new
                best = max(pool, key=lambda d: d[4])
                self._acquire(best)
                self.rejects = [d for d in main if d is not best]
                self.n_rej += len(self.rejects)
                return "acquire"
        if not main:
            return self._coast()

        if self.pos is None:
            return self._coast()

        # ---- 门控选择: 评分 = 置信度 - 距离惩罚 - 运动不一致惩罚 ----
        # 真目标速度连续; 背景假检测随背景滚动/闪现, 隐含速度常与轨迹速度不符
        pred = (self.pos[0] + self.vel[0], self.pos[1] + self.vel[1])
        scored = []
        for d in main:
            dist = self._dist((d[0], d[1]), pred)
            s = d[4] - 1.2 * dist / max(self._gate(), 1.0)
            if self.last_det is not None and self.cruise:
                vix, viy = d[0] - self.last_det[0], d[1] - self.last_det[1]
                dv = math.hypot(vix - self.vel[0], viy - self.vel[1])
                if dv > 10.0:                     # 隐含速度偏离 >10px/帧: 疑似假检测
                    s -= 0.8 * min((dv - 10.0) / 12.0, 1.2)
            scored.append((s, dist, d))
        scored.sort(key=lambda t: -t[0])
        s, dist, best = scored[0]

        gate = self._gate()

        def _persistent(d):
            """远距候选需上一帧同位置也有检测 (FP 65% 为单帧闪现, 真目标持续在场)."""
            return any(self._dist((d[0], d[1]), p) <= 25.0 for p in self.prev_dets)

        # 高置信旁路: 分三级
        #  A) 门内 conf>=0.7: 直接信任 (近目标强检测)
        hc = [d for d in main if d[4] >= 0.70
              and self._dist((d[0], d[1]), pred) <= 1.2 * gate]
        if hc:
            best = max(hc, key=lambda d: d[4])
            self._pursue(best)
            self.rejects = [d for d in main if d is not best]
            self.n_rej += len(self.rejects)
            return "track"
        #  B) 远距 conf>=0.65 且持续 2 帧: 劫持恢复 (追踪器已跑飞时的重捕)
        hc = [d for d in main if d[4] >= self.hijack_conf and _persistent(d)]
        if hc:
            best = max(hc, key=lambda d: d[4])
            self._pursue(best)
            self.rejects = [d for d in main if d is not best]
            self.n_rej += len(self.rejects)
            return "track"
        accept = dist <= gate or (dist <= 1.5 * gate and best[4] >= 0.5)
        if not accept:
            # 滑行期重锁: 半径随滑行时长扩张 (误差可能已累积)
            if self.coast >= 2:
                rad = min(60.0 + 12.0 * self.coast, 260.0)
                hc = [d for d in main if d[4] >= 0.5
                      and self._dist((d[0], d[1]), self.pos) <= rad]
                if hc:
                    best = max(hc, key=lambda d: d[4])
                    accept = True
        if accept:
            self._pursue(best)
            self.rejects = [d for d in main if d is not best]
            self.n_rej += len(self.rejects)
            return "track"
        self.n_rej += len(main)
        return self._coast()

    # ---------- 状态转移 ----------
    def _acquire(self, d):
        self.pos = (d[0], d[1])
        self.vel = (0.0, 0.0)
        self.cruise = None
        self.last_det = (d[0], d[1])
        self.box = (d[2], d[3])
        self.conf = d[4]
        self.cls = d[5]
        self.coast = 0
        self.lost_t = 0
        self.state = "TRACK"
        self.n_det += 1

    def _pursue(self, d):
        cx, cy = d[0], d[1]
        # 巡航速度 = 接受检测的帧间位移 (匀速性依据)
        if self.last_det is not None:
            dsp = self._dist((cx, cy), self.last_det)
            dsp = min(dsp, 18.0)
            self.cruise = dsp if self.cruise is None else \
                0.35 * dsp + 0.65 * self.cruise
        # 超前瞄准: 目标点 = 检测位置 + 当前速度 (补偿一步滞后, 消除匀速追逐的系统性落后)
        lx, ly = cx + self.vel[0], cy + self.vel[1]
        dx, dy = lx - self.pos[0], ly - self.pos[1]
        dist = math.hypot(dx, dy)
        raw_dist = math.hypot(cx - self.pos[0], cy - self.pos[1])
        spd = (self.cruise or 3.0)
        if raw_dist > 30.0:                 # 远距落后: 冲刺恢复 (上限 = vmax)
            chase, need = 3.0, 0.8
        elif self.coast >= 2:
            chase, need = 2.2, self.chase_need
        else:
            chase, need = self.chase_cap, self.chase_need
        if need * dist > spd:
            spd = min(chase * spd, need * dist)
        spd = min(spd, self.vmax_f * 1.2)
        if dist > 1e-6:
            tvx, tvy = dx / dist * spd, dy / dist * spd
        else:
            tvx, tvy = 0.0, 0.0
        # 转向限速: 加速度随当前速度比例放大 (cruise 越大越要能快速转向), 落后越远额度越高
        vlen = math.hypot(*self.vel)
        if dist > 45.0:
            amax = max(8.0, 0.4 * vlen)       # 远距落后: 快速调头冲刺恢复
        else:
            amax = max(2.5, 0.22 * vlen) + 0.06 * dist
        dvx, dvy = tvx - self.vel[0], tvy - self.vel[1]
        dvm = math.hypot(dvx, dvy)
        if dvm > amax:
            k = amax / dvm
            dvx, dvy = dvx * k, dvy * k
        self.vel = (self.vel[0] + dvx, self.vel[1] + dvy)
        self.pos = (self.pos[0] + self.vel[0], self.pos[1] + self.vel[1])
        # 防过冲: 已越过检测点则贴齐
        ndx, ndy = cx - self.pos[0], cy - self.pos[1]
        if math.hypot(ndx, ndy) > dist and dist < spd:
            self.pos = (cx, cy)
            self.vel = (tvx, tvy)
        self.last_det = (cx, cy)
        self.box = (d[2], d[3])
        self.conf = d[4]
        self.cls = d[5]
        self.coast = 0
        self.state = "TRACK"
        self.n_det += 1

    def _coast(self):
        if self.pos is None:
            self.state = "INIT"
            return "init"
        self.pos = (self.pos[0] + self.vel[0], self.pos[1] + self.vel[1])
        # 惯性滑行: 前 5 帧纯匀速外推 (实测缺口外推误差 median 12px), 之后缓衰减
        if self.coast >= 5:
            k = 0.95
            self.vel = (self.vel[0] * k, self.vel[1] * k)
        self.coast += 1
        if self.coast > 3:
            self.state = "COAST"
        if self.coast > self.fps * 3.0:     # >3s 无检测 = 丢失
            self.state = "LOST"
            self.lost_t += 1
            return "lost"
        return "coast"
