# -*- coding: utf-8 -*-
"""测谎仪跟随（webui/lietrack.py）的单元测试。

全部用假的检测器 / 假的 OCR / 假的鼠标，**不加载真模型、不碰真实光标**，
所以在没有游戏、没有 CUDA 的机器上也能跑：

    python tools/test_lietrack.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "webui"))

import lietrack as lt  # noqa: E402
import cv2  # noqa: E402


def _tpl_image():
    """读模板图（中文路径要用 imdecode，cv2.imread 会返回 None）。"""
    p = ROOT / "webui" / "lietrack_tpl.png"
    return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)


def _frame_with_template(x=300, y=200, w=1366, h=768, bg=40):
    """造一张假客户区，把「谎言探测仪」标题模板贴在 (x, y) 上。"""
    frame = np.full((h, w, 3), bg, np.uint8)
    tpl = _tpl_image()
    frame[y:y + tpl.shape[0], x:x + tpl.shape[1]] = tpl
    return frame


class FakeDetector:
    """按脚本返回检测框，用来验证「追踪器 + 坐标换算」这条链路。"""

    def __init__(self, seq=None, weights="fake.pt"):
        self.weights = weights
        self.error = ""
        self.loaded = True
        self.seq = seq or [[(375.0, 250.0, 40.0, 40.0, 0.9, 0)]]
        self.i = 0
        self.calls = 0

    def detect(self, canvas):
        self.calls += 1
        dets = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return list(dets)


class FakeOcr:
    name = "fake"

    def __init__(self, texts=None):
        self.error = ""
        self.texts = texts if texts is not None else []
        self.calls = 0

    def read(self, img, keywords=None):
        self.calls += 1
        return list(self.texts), ("", 0.0)


class FakeMouse:
    def __init__(self, *a, **kw):
        self.geometry = None
        self.targets = []
        self.started = False
        self.stopped = 0

    def set_geometry(self, ox, oy, sx, sy):
        self.geometry = (ox, oy, sx, sy)

    def set_target(self, x, y):
        self.targets.append((x, y))

    def start_tracking(self):
        self.started = True

    def stop_tracking(self):
        self.stopped += 1
        self.started = False


class TestKeywords(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(lt.normalize_keywords("测谎仪, 游戏开始前 ，，"), ["测谎仪", "游戏开始前"])
        self.assertEqual(lt.normalize_keywords(["测谎仪", "  ", ""]), ["测谎仪"])
        self.assertEqual(lt.normalize_keywords("甲、乙\n丙"), ["甲", "乙", "丙"])

    def test_match_exact(self):
        hit, text = lt.match_keywords(["游戏开始前，请前往安全地点。"], ["游戏开始前"])
        self.assertEqual(hit, "游戏开始前")
        self.assertIn("游戏开始前", text)

    def test_match_ignores_spaces_and_punct(self):
        hit, _ = lt.match_keywords(["游戏 开始前 。"], ["游戏开始前"])
        self.assertEqual(hit, "游戏开始前")

    def test_match_english_case(self):
        hit, _ = lt.match_keywords(["LIE DETECTOR"], ["lie detector"])
        self.assertEqual(hit, "lie detector")

    def test_no_hit(self):
        self.assertEqual(lt.match_keywords(["血量 100%"], ["测谎仪"]), ("", ""))

    def test_order_prefers_first_keyword(self):
        hit, _ = lt.match_keywords(["请前往安全地点，测谎仪"], ["测谎仪", "请前往安全地点"])
        self.assertEqual(hit, "测谎仪")


class TestConfig(unittest.TestCase):
    def test_clamps(self):
        cfg = lt.clean_cfg({"track_fps": 999, "det_conf": -1, "hit_frames": 0,
                            "ocr_fps": 0.01, "hold_sec": 99})
        self.assertEqual(cfg["track_fps"], 30)
        self.assertGreaterEqual(cfg["det_conf"], 0.01)
        self.assertEqual(cfg["hit_frames"], 1)
        self.assertGreaterEqual(cfg["ocr_fps"], 0.5)
        self.assertEqual(cfg["hold_sec"], 30)

    def test_unknown_keys_dropped(self):
        cfg = lt.clean_cfg({"whatever": 1, "status": "x"})
        self.assertNotIn("whatever", cfg)

    def test_keywords_empty_falls_back(self):
        self.assertEqual(lt.clean_cfg({"keywords": ""})["keywords"], lt.DEFAULT_KEYWORDS)

    def test_roundtrip_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "lietrack.json"
            p.write_text(json.dumps({"keywords": ["测谎仪"], "enabled": False}),
                         encoding="utf-8")
            cfg = lt.load_cfg(p)
            self.assertEqual(cfg["keywords"], ["测谎仪"])
            self.assertFalse(cfg["enabled"])
            lt.save_cfg(p, cfg)
            self.assertEqual(lt.load_cfg(p)["keywords"], ["测谎仪"])


class TestSmoothMouse(unittest.TestCase):
    def test_follows_target_and_stops(self):
        pos = [0, 0]

        def get_pos():
            return pos[0], pos[1]

        def set_pos(x, y):
            pos[0], pos[1] = x, y

        m = lt.SmoothMouse(0, 0, 1, 1, get_pos=get_pos, set_pos=set_pos)
        try:
            m.set_target(80, 40)
            time.sleep(0.08)
            self.assertFalse(m.active)
            self.assertEqual(m.writes, 0, "没开跟就不该写光标")
            m.start_tracking()           # 开跟会先同步到光标当前位置（目标跟着清空）
            m.set_target(80, 40)
            time.sleep(0.3)
            self.assertGreater(m.writes, 0)
            self.assertGreater(pos[0], 20, "光标应该往目标方向走")
            m.stop_tracking()
            w = m.writes
            time.sleep(0.15)
            self.assertEqual(m.writes, w, "松手后不许再碰光标")
        finally:
            m.stop()

    def test_geometry_rescale(self):
        m = lt.SmoothMouse(100, 50, 2.0, 2.0, get_pos=lambda: (300, 150),
                           set_pos=lambda x, y: None)
        try:
            # 光标在屏幕 (300,150) -> 画布 ((300-100)/2, (150-50)/2) = (100, 50)
            self.assertAlmostEqual(m.x, 100.0, places=3)
            self.assertAlmostEqual(m.y, 50.0, places=3)
            m.set_geometry(0, 0, 1.0, 1.0)
            self.assertEqual(m.sx, 1.0)
        finally:
            m.stop()


class TestYoloTracker(unittest.TestCase):
    def test_tracks_constant_velocity(self):
        trk = lt.YoloTracker(fps=10.0)
        trk.reset()
        # 目标从画布中心以 (6, 3) px/帧 匀速移动
        for i in range(25):
            cx, cy = 375 + 6 * i, 250 + 3 * i
            trk.update([(cx, cy, 40, 40, 0.9, 0)])
        self.assertEqual(trk.state, "TRACK")
        self.assertLess(abs(trk.pos[0] - (375 + 6 * 24)), 20)
        self.assertLess(abs(trk.pos[1] - (250 + 3 * 24)), 20)

    def test_lost_after_silence(self):
        trk = lt.YoloTracker(fps=10.0)
        trk.reset()
        trk.update([(375, 250, 40, 40, 0.9, 0)])
        for _ in range(40):
            trk.update([])
        self.assertEqual(trk.state, "LOST")

    def test_reset_clears(self):
        trk = lt.YoloTracker(fps=10.0)
        trk.update([(375, 250, 40, 40, 0.9, 0)])
        self.assertIsNotNone(trk.pos)
        trk.reset()
        self.assertIsNone(trk.pos)
        self.assertEqual(trk.state, "INIT")


class TestLieTrackService(unittest.TestCase):
    """整条链路：关键词出现 → 起跑 → 跑满 run_sec 秒 → 松手。"""

    def _make(self, texts, region=None, cfg_patch=None):
        region = region or {"left": 100, "top": 50, "width": 1500, "height": 1000}
        frame = np.zeros((1000, 1500, 3), np.uint8)
        ocr = FakeOcr(texts)
        mouse = FakeMouse()
        det = FakeDetector()
        cfg = {
            "enabled": True, "trigger": "ocr", "keywords": ["测谎仪"], "ocr_fps": 8, "track_fps": 8,
            "hit_frames": 1, "hold_sec": 0.3, "run_sec": 1.2, "move_mouse": True,
            "only_foreground": False,
        }
        cfg.update(cfg_patch or {})
        t = lt.LieTrack(frame_provider=lambda: (frame, region),
                        detector_factory=lambda w: det,
                        mouse_factory=lambda *a, **k: mouse,
                        ocr_factory=lambda engine: ocr)
        t.set_cfg(cfg)
        return t, ocr, mouse, det, region

    def test_triggers_and_releases(self):
        t, ocr, mouse, det, region = self._make(
            ["游戏开始前，请前往安全地点。"],
            cfg_patch={"keywords": ["游戏开始前"]})
        t.start()
        try:
            time.sleep(0.5)
            st = t.snapshot()
            self.assertTrue(st["present"], "关键词在场应该触发")
            self.assertTrue(st["tracking"], "触发后应该开始追踪")
            self.assertTrue(mouse.started)
            self.assertGreater(mouse.targets.__len__(), 0)
            # 画布 (375,250) → 屏幕 (100+375*2, 50+250*2) = (850, 550)
            self.assertEqual(st["target"], [850, 550])
            self.assertEqual(mouse.geometry, (100, 50, 2.0, 2.0))
            self.assertGreater(st["run_left"], 0.0, "起跑后应该开始倒计时")

            ocr.texts = ["血量 100%"]          # 关键词消失，但这一轮不能被打断
            time.sleep(0.4)
            st = t.snapshot()
            self.assertFalse(st["present"])
            self.assertTrue(st["tracking"], "关键词消失不该立刻松手（要跑满 run_sec）")
            self.assertTrue(mouse.started)

            time.sleep(1.0)                    # 跑满 run_sec=1.2s
            st = t.snapshot()
            self.assertFalse(st["tracking"], "跑满 run_sec 后应该自动松手")
            self.assertFalse(mouse.started)
            self.assertEqual(st["run_left"], 0.0)
        finally:
            t.stop()

    def test_lost_target_keeps_running(self):
        """目标丢失也不收摊：跟到时间到为止，中途等重捕。"""
        t, ocr, mouse, det, region = self._make(
            ["测谎仪"], cfg_patch={"run_sec": 2.0})
        det.seq = [[]]                        # 检测全空 → 追踪器一次都拿不到目标
        t.start()
        try:
            time.sleep(0.8)
            st = t.snapshot()
            self.assertTrue(st["tracking"], "没目标也要继续跟到时间到")
            time.sleep(1.6)
            self.assertFalse(t.snapshot()["tracking"], "时间到才松手")
        finally:
            t.stop()

    def test_disabled_never_tracks(self):
        t, ocr, mouse, det, region = self._make(["测谎仪"], cfg_patch={"enabled": False})
        t.start()
        try:
            time.sleep(0.5)
            st = t.snapshot()
            self.assertFalse(st["tracking"])
            self.assertTrue(mouse.stopped >= 1 or not mouse.started)
        finally:
            t.stop()

    def test_move_mouse_off_still_tracks(self):
        t, ocr, mouse, det, region = self._make(["测谎仪"], cfg_patch={"move_mouse": False})
        t.start()
        try:
            time.sleep(0.4)
            st = t.snapshot()
            self.assertTrue(st["tracking"])
            self.assertFalse(mouse.started, "关掉「动鼠标」就只识别不动手")
            self.assertEqual(mouse.targets, [])
        finally:
            t.stop()

    def test_no_frame_keeps_idle(self):
        t = lt.LieTrack(frame_provider=lambda: (None, None),
                        detector_factory=lambda w: FakeDetector(),
                        mouse_factory=lambda *a, **k: FakeMouse(),
                        ocr_factory=lambda engine: FakeOcr(["测谎仪"]))
        t.set_cfg({"enabled": True, "keywords": ["测谎仪"]})
        t.start()
        try:
            time.sleep(0.4)
            self.assertFalse(t.snapshot()["tracking"])
        finally:
            t.stop()

    def test_foreground_is_mandatory(self):
        """「必须前台」是硬要求：配置里写 only_foreground=False 也不放行（这个开关已删除）。"""
        frame = np.zeros((500, 750, 3), np.uint8)
        region = {"left": 100, "top": 50, "width": 750, "height": 500}
        t = lt.LieTrack(frame_provider=lambda: (frame, region),
                        foreground=lambda: False,
                        detector_factory=lambda w: FakeDetector(),
                        mouse_factory=lambda *a, **k: FakeMouse(),
                        ocr_factory=lambda engine: FakeOcr(["测谎仪"]))
        t.set_cfg({"enabled": True, "trigger": "ocr", "keywords": ["测谎仪"],
                   "ocr_fps": 8, "track_fps": 8, "hit_frames": 1,
                   "run_sec": 2.0, "only_foreground": False, "move_mouse": True})
        t.start()
        try:
            time.sleep(0.6)
            st = t.snapshot()
            self.assertFalse(st["tracking"], "游戏不在前台时绝不该接管鼠标")
            self.assertFalse(st["present"], "不在前台连判定都不该往下走")
        finally:
            t.stop()

    def test_probe_reports_keyword(self):
        t, ocr, mouse, det, region = self._make(["测谎仪 请把鼠标放在图形上"])
        r = t.probe()
        self.assertTrue(r["ok"])
        self.assertEqual(r["hit"], "测谎仪")

    def test_probe_without_frame(self):
        t = lt.LieTrack(frame_provider=lambda: (None, None),
                        ocr_factory=lambda engine: FakeOcr(["测谎仪"]))
        r = t.probe()
        self.assertFalse(r["ok"])
        self.assertIn("没有画面", r["msg"])


class TestTemplateTrigger(unittest.TestCase):
    """模板触发：认「谎言探测仪」弹窗标题那张小图，不再跑 OCR。"""

    def test_default_config_uses_template(self):
        self.assertEqual(lt.DEFAULT_CFG["trigger"], "template")
        self.assertEqual(lt.DEFAULT_CFG["template"], "lietrack_tpl.png")

    def test_finds_pasted_template(self):
        trg = lt.TemplateTrigger(str(ROOT / "webui" / "lietrack_tpl.png"), 0.7)
        self.assertTrue(trg.loaded, trg.error)
        score, box = trg.detect(_frame_with_template(300, 200))
        self.assertGreater(score, 0.95, f"贴回去应该几乎满分，实际 {score:.3f}")
        self.assertEqual((box[0], box[1]), (300, 200), box)

    def test_no_false_positive_on_plain_frame(self):
        trg = lt.TemplateTrigger(str(ROOT / "webui" / "lietrack_tpl.png"), 0.7)
        score, _ = trg.detect(np.full((768, 1366, 3), 40, np.uint8))
        self.assertLess(score, 0.7, f"空画面不该命中，实际 {score:.3f}")

    def test_detects_at_other_scale(self):
        """弹窗被缩放（0.9 倍）也要认得出，门限按实际分数留余量。"""
        trg = lt.TemplateTrigger(str(ROOT / "webui" / "lietrack_tpl.png"), 0.5)
        tpl = _tpl_image()
        small = cv2.resize(tpl, (int(tpl.shape[1] * 0.9), int(tpl.shape[0] * 0.9)),
                           interpolation=cv2.INTER_AREA)
        frame = np.full((768, 1366, 3), 40, np.uint8)
        frame[200:200 + small.shape[0], 300:300 + small.shape[1]] = small
        score, _ = trg.detect(frame)
        self.assertGreater(score, 0.7, f"0.9 倍缩放下应该仍然很稳，实际 {score:.3f}")

    def test_service_triggers_on_template(self):
        """整条链路：模板出现 → 起跑；模板消失也不松手，跑满 run_sec 才松。"""
        frame_with = _frame_with_template(300, 200)
        region = {"left": 100, "top": 50, "width": 1500, "height": 1000}
        state = {"frame": frame_with}
        mouse = FakeMouse()
        t = lt.LieTrack(frame_provider=lambda: (state["frame"], region),
                        detector_factory=lambda w: FakeDetector(),
                        mouse_factory=lambda *a, **k: mouse,
                        ocr_factory=lambda engine: FakeOcr([]))
        t.set_cfg({"enabled": True, "trigger": "template", "ocr_fps": 8, "track_fps": 8,
                   "hit_frames": 1, "hold_sec": 0.2, "run_sec": 1.2, "move_mouse": True,
                   "only_foreground": False})
        t.start()
        try:
            time.sleep(0.5)
            st = t.snapshot()
            self.assertTrue(st["present"], "模板在画面上应该触发")
            self.assertTrue(st["tracking"], "触发后应该开始追踪")
            self.assertGreater(st["score"], 0.9)
            state["frame"] = np.full((768, 1366, 3), 40, np.uint8)   # 弹窗没了
            time.sleep(0.4)
            self.assertFalse(t.snapshot()["present"])
            self.assertTrue(t.snapshot()["tracking"], "弹窗消失也要跑满 run_sec")
            time.sleep(1.0)
            self.assertFalse(t.snapshot()["tracking"])
        finally:
            t.stop()

    def test_probe_template_mode_reports_score(self):
        frame = _frame_with_template(300, 200)
        region = {"left": 0, "top": 0, "width": 1366, "height": 768}
        t = lt.LieTrack(frame_provider=lambda: (frame, region),
                        ocr_factory=lambda engine: FakeOcr([]))
        t.set_cfg({"enabled": True, "trigger": "template"})
        r = t.probe()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["engine"], "template")
        self.assertGreater(r["score"], 0.9)
        self.assertTrue(r["hit"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
