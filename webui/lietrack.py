# -*- coding: utf-8 -*-
"""测谎仪跟随：OCR 关键词触发 → YOLO 追踪 → 真实鼠标平滑跟随。

移植自 ScreenTrackerApp（screen_tracker.py / track_core.py，2026-11-15 验收版本），
改动只在于「什么时候开始跟」：那边是人工框选一块区域一直跟；这里是等 OCR 在画面上
认出关键词才开始跟，关键词消失一会儿就松手。

分工：
  * `YoloTracker`  —— 原样搬来的匀速追踪器（门控 + 滑行 + 超前瞄准），只在
                     「检测结果」和「鼠标目标点」之间做滤波；
  * `SmoothMouse`  —— 150Hz 后台线程 + 临界阻尼弹簧，把追踪器的目标点变成
                     连续平滑的光标轨迹（分辨率无关，画布坐标系）；
  * `OcrReader`    —— 关键词识别。优先 RapidOCR（中文识别靠谱），没装就退化成
                     「关键词小图 + 模板匹配」；两条路都失败也不会崩，只是不触发；
  * `LieTrack`     —— 把上面三件事串起来的线程：OCR 判定关键词在场 → 开追，
                     关键词离场超过 hold_sec → 松手。

坐标约定：模型是在**整个客户区**按 750×500 标准画布训练的（collect_dataset.py 里
`CANVAS_W, CANVAS_H` 就是拿客户区整体归一化），所以这里同样把客户区缩放到
750×500 送进模型，输出再用 `region` 的物理宽高缩放回屏幕坐标。

依赖：cv2 / numpy / ultralytics / rapidocr-onnxruntime（可选）。
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

import cv2
import numpy as np

# 模型训练分辨率（客户区 -> 这个尺寸，检测完再按同样比例缩回去）
CANVAS_W, CANVAS_H = 750, 500

DEFAULT_KEYWORDS = ["测谎仪", "游戏开始前", "请前往安全地点"]

DEFAULT_CFG = {
    "enabled": True,          # 条件触发总开关：认到关键词才动鼠标
    "trigger": "template",    # template=认那张弹窗图（默认，最快最稳）/ ocr=中文关键词
    "template": "lietrack_tpl.png",   # 弹窗标题「谎言探测仪」那一小块图
    "tpl_thresh": 0.70,       # 模板相似度门限（TM_CCOEFF_NORMED，1.0 = 完全一致）
    "keywords": list(DEFAULT_KEYWORDS),   # 只有 trigger=ocr 时用
    "ocr_fps": 2,             # 触发检查的频率（模板匹配一帧十几毫秒，2Hz 足够）
    "ocr_max_w": 960,         # OCR 模式下先把画面缩到这个宽度（模板模式不用）
    "ocr_engine": "auto",     # auto / rapidocr / template（仅 trigger=ocr 时有意义）
    "track_fps": 12,          # 追踪频率（YOLO 单帧 ~40ms，12fps 不会把 CPU 吃满）
    "det_conf": 0.10,         # 检测置信度下限（与验收脚本一致）
    "hold_sec": 1.5,          # 只用于面板上「关键词在场」的显示判据（不影响跟多久）
    "run_sec": 15,            # **一次触发跟多久**：命中只负责起跑，跑满这么多秒才自动松手
    "lost_sec": 2.0,          # 追踪器连续丢失这么久 → 本轮结束
    "hit_frames": 2,          # 连续几轮 OCR 都命中才算「关键词在场」（去抖）
    "move_mouse": True,       # 是否真的移动光标（关掉只做识别，便于观察）
    # （「必须前台」不再是配置项：永远是必须前台。见 _run_loop 里的闸门与
    #   「跟随测试」的 forced 例外。以前留过开关，实测关掉后光标会在别的窗口里乱跑。）
    "weights": "lietrack_ft3.pt",
}

# ---------------- 配置 ----------------
_cfg_lock = threading.Lock()


def _clamp_num(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return min(hi, max(lo, v))


def normalize_keywords(raw):
    """把各种写法的关键词列表归一成去空白的字符串列表。

    前端传「测谎仪, 游戏开始前」这种一行文本，或直接传数组，都收；空项丢掉。
    匹配时也会去空白，所以「游戏 开始前」和「游戏开始前」等价。
    """
    if isinstance(raw, str):
        raw = raw.replace("，", ",").replace("、", ",").replace("\n", ",")
        raw = raw.split(",")
    out = []
    for k in raw or []:
        if not isinstance(k, str):
            continue
        k = k.strip()
        if k:
            out.append(k)
    return out


def clean_cfg(raw, base=None):
    """把外部传进来的配置洗成合法值。未知键丢弃，避免把状态写进配置。"""
    cfg = dict(base or DEFAULT_CFG)
    if not isinstance(raw, dict):
        return cfg
    if "enabled" in raw:
        cfg["enabled"] = bool(raw["enabled"])
    if "move_mouse" in raw:
        cfg["move_mouse"] = bool(raw["move_mouse"])
    if "only_foreground" in raw:
        # 老配置里还有这个键：接受但忽略——前台是硬要求，不给关。
        pass
    if "keywords" in raw:
        cfg["keywords"] = normalize_keywords(raw["keywords"]) or list(DEFAULT_KEYWORDS)
    if raw.get("trigger") in ("template", "ocr"):
        cfg["trigger"] = raw["trigger"]
    if isinstance(raw.get("template"), str) and raw["template"].strip():
        cfg["template"] = raw["template"].strip()
    if "tpl_thresh" in raw:
        cfg["tpl_thresh"] = _clamp_num(raw["tpl_thresh"], 0.40, 0.98, cfg["tpl_thresh"])
    if raw.get("ocr_engine") in ("auto", "rapidocr", "template"):
        cfg["ocr_engine"] = raw["ocr_engine"]
    if "ocr_fps" in raw:
        cfg["ocr_fps"] = _clamp_num(raw["ocr_fps"], 0.5, 10, cfg["ocr_fps"])
    if "ocr_max_w" in raw:
        cfg["ocr_max_w"] = int(_clamp_num(raw["ocr_max_w"], 480, 1920, cfg["ocr_max_w"]))
    if "track_fps" in raw:
        cfg["track_fps"] = _clamp_num(raw["track_fps"], 2, 30, cfg["track_fps"])
    if "det_conf" in raw:
        cfg["det_conf"] = _clamp_num(raw["det_conf"], 0.01, 0.9, cfg["det_conf"])
    if "hold_sec" in raw:
        cfg["hold_sec"] = _clamp_num(raw["hold_sec"], 0.0, 30, cfg["hold_sec"])
    if "run_sec" in raw:
        cfg["run_sec"] = _clamp_num(raw["run_sec"], 1, 120, cfg["run_sec"])
    if "lost_sec" in raw:
        cfg["lost_sec"] = _clamp_num(raw["lost_sec"], 0.5, 30, cfg["lost_sec"])
    if "hit_frames" in raw:
        cfg["hit_frames"] = int(_clamp_num(raw["hit_frames"], 1, 10, cfg["hit_frames"]))
    if isinstance(raw.get("weights"), str) and raw["weights"].strip():
        cfg["weights"] = raw["weights"].strip()
    return cfg


def load_cfg(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return clean_cfg(raw)


def save_cfg(path, cfg):
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ---------------- 关键词匹配 ----------------
_PUNCT = " \t\r\n　，。！？、：；·…—-_/\\|()（）[]【】<>《》\"'`~!?.,:;"


def _flat(s):
    """去掉空白和标点：OCR 出来的「游戏 开始前 。」要能对上「游戏开始前」。"""
    return "".join(ch for ch in str(s) if ch not in _PUNCT).lower()


def match_keywords(texts, keywords):
    """texts: OCR 出来的文本列表（或 [(text, ...)] 之类），keywords: 关键词列表。

    返回 (命中的关键词, 命中片段原文) —— 没命中返回 ("", "")。
    逐个关键词找子串：关键词之间是「或」的关系，命中短的优先返回（配置里靠前的优先）。
    """
    flat_texts = []
    for t in texts or []:
        if isinstance(t, str):
            flat_texts.append((_flat(t), t))
        elif isinstance(t, (list, tuple)) and t:
            flat_texts.append((_flat(t[1] if len(t) > 1 else t[0]),
                               t[1] if len(t) > 1 else str(t[0])))
    for kw in keywords or []:
        fk = _flat(kw)
        if not fk:
            continue
        for flat, orig in flat_texts:
            if fk in flat:
                return kw, orig
    return "", ""


# ---------------- OCR ----------------
class OcrReader:
    """关键词识别。构造时不导入重依赖，第一次 read 才初始化引擎（失败就退化）。"""

    def __init__(self, engine="auto"):
        self.engine_pref = engine if engine in ("auto", "rapidocr", "template") else "auto"
        self.name = ""
        self.error = ""
        self._rapid = None
        self._rapid_failed = False
        self._tpl_cache = {}
        self._lock = threading.Lock()

    # -- RapidOCR --
    def _ensure_rapid(self):
        if self._rapid is not None or self._rapid_failed:
            return self._rapid
        try:                                    # 1.4.x
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            try:                                 # 2.x 改了包名
                from rapidocr import RapidOCR
            except ImportError as e:
                self._rapid_failed = True
                self.error = f"没装 RapidOCR（{e}），退化用模板匹配"
                return None
        try:
            self._rapid = RapidOCR()
            self.error = ""
        except Exception as e:  # noqa: BLE001  模型文件缺失之类
            self._rapid_failed = True
            self.error = f"RapidOCR 初始化失败: {type(e).__name__}: {e}"
            self._rapid = None
        return self._rapid

    def _read_rapid(self, img):
        r = self._ensure_rapid()
        if r is None:
            return None
        try:
            res = r(img)
        except Exception as e:  # noqa: BLE001  单帧失败不该让线程死
            self.error = f"OCR 失败: {type(e).__name__}: {e}"
            return None
        # 1.4.x: (list[[box, text, score]], elapse)；2.x: 带 .txts/.scores 的对象
        if isinstance(res, tuple):
            items = res[0] or []
        else:
            items = getattr(res, "txts", None)
            if items is None:
                items = []
            else:
                items = [[None, t, s] for t, s in
                         zip(items, getattr(res, "scores", [1.0] * len(items)))]
        out = []
        for it in items:
            if not isinstance(it, (list, tuple)) or len(it) < 2:
                continue
            text = it[1]
            if isinstance(text, str) and text.strip():
                out.append(text.strip())
        return out

    # -- 模板匹配兜底 --
    def _templates_for(self, kw):
        """关键词渲染成灰度小图（多种字号），配不到中文字体就返回空。"""
        if kw in self._tpl_cache:
            return self._tpl_cache[kw]
        tpls = []
        font_paths = [
            r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\msyhbd.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\simsun.ttc",
        ]
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            self._tpl_cache[kw] = tpls
            return tpls
        for fp in font_paths:
            if not Path(fp).exists():
                continue
            for size in (14, 16, 18, 20, 24, 28):
                try:
                    font = ImageFont.truetype(fp, size)
                except OSError:
                    continue
                box = font.getbbox(kw)
                w, h = max(8, box[2] - box[0]), max(8, box[3] - box[1])
                im = Image.new("L", (w + 8, h + 8), 0)
                ImageDraw.Draw(im).text((4 - box[0], 4 - box[1]), kw, fill=255, font=font)
                arr = np.array(im)
                if arr.max() >= 8:
                    tpls.append(arr)
            if tpls:                 # 一种字体够用，别把 4 种全渲染
                break
        self._tpl_cache[kw] = tpls
        return tpls

    def _read_template(self, img, keywords):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        best_kw, best_score = "", 0.0
        for kw in keywords or []:
            for tpl in self._templates_for(kw):
                th, tw = tpl.shape[:2]
                if gray.shape[0] < th or gray.shape[1] < tw:
                    continue
                res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
                score = float(res.max()) if res.size else 0.0
                if score > best_score:
                    best_kw, best_score = kw, score
        return best_kw, best_score

    def read(self, img, keywords=None):
        """返回 (texts, template_hit)。template_hit = (关键词, 分数)。"""
        with self._lock:
            if self.engine_pref in ("auto", "rapidocr"):
                texts = self._read_rapid(img)
                if texts is not None:
                    self.name = "rapidocr"
                    return texts, ("", 0.0)
                if self.engine_pref == "rapidocr":
                    return [], ("", 0.0)
            self.name = "template"
            kw, score = self._read_template(img, keywords or [])
            return [], (kw, score)


# ---------------- 模板触发（认「谎言探测仪」这个弹窗本身） ----------------
class TemplateTrigger:
    """模板匹配：在画面里找弹窗标题那一小块图（`webui/lietrack_tpl.png`）。

    为什么不用 OCR：中文 OCR 一次要 0.5~2.7 秒（还随 CPU 争用波动），而「这个弹窗
    在不在」本质是一张固定 UI 图在不在——`cv2.matchTemplate` 十几毫秒就能答完，
    还不用背 OCR 引擎那 9 秒的冷启动。模板截自用户给的弹窗截图左上角标题「谎言探测仪」，
    86x18 像素，够独特（别处不会出现这七个字），又小到能容忍鼠标/光标盖住对话框其它部分。
    """

    name = "template"

    def __init__(self, path=None, thresh=0.7, scales=(0.90, 0.95, 1.0, 1.05, 1.10)):
        self.path = path
        self.thresh = float(thresh)
        self.scales = tuple(scales)
        self.tpl = None
        self.loaded = False
        self.error = ""
        self.ms = 0.0
        self.score = 0.0
        self.box = None
        if path:
            self.load(path)

    def load(self, path=None):
        if path:
            self.path = path
        if not self.path:
            self.error = "没配模板文件"
            return False
        try:
            # 中文路径用 imdecode 读（cv2.imread 对非 ASCII 路径会直接返回 None）
            data = np.fromfile(str(self.path), dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
            if img is None:
                raise ValueError("模板图读不出来（不是图片？路径不对？）")
            self.tpl = img
            self.loaded = True
            self.error = ""
            return True
        except Exception as e:  # noqa: BLE001
            self.loaded = False
            self.error = f"{type(e).__name__}: {e}"
            return False

    def detect(self, frame):
        """在整帧里找模板。返回 (score, box)，box = (x, y, w, h) 左上角 + 尺寸。"""
        t0 = time.perf_counter()
        if not self.loaded or frame is None or frame.size == 0:
            return 0.0, None
        gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        best_s = -1.0
        best_box = None
        for sc in self.scales:
            if abs(sc - 1.0) < 1e-6:
                tpl = self.tpl
            else:
                w = max(8, int(round(self.tpl.shape[1] * sc)))
                h = max(6, int(round(self.tpl.shape[0] * sc)))
                if w > gray.shape[1] or h > gray.shape[0]:
                    continue
                tpl = cv2.resize(self.tpl, (w, h), interpolation=cv2.INTER_AREA)
            if tpl.shape[0] > gray.shape[0] or tpl.shape[1] > gray.shape[1]:
                continue
            r = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
            _mn, mx, _ml, mxl = cv2.minMaxLoc(r)
            if mx > best_s:
                best_s = float(mx)
                best_box = (int(mxl[0]), int(mxl[1]), int(tpl.shape[1]), int(tpl.shape[0]))
        self.ms = (time.perf_counter() - t0) * 1000
        self.score = max(0.0, best_s)
        self.box = best_box
        return self.score, best_box


# ---------------- 平滑鼠标 ----------------
def _get_cursor_pos():
    import ctypes
    from ctypes import wintypes
    p = wintypes.POINT(0, 0)
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _set_cursor_pos(x, y):
    import ctypes
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


class SmoothMouse:
    """150Hz 后台线程 + 临界阻尼弹簧：光标连续平滑地跟随目标，像人手。

    坐标系是**画布坐标**（750×500），换算到屏幕用 ox/oy + 缩放；region 变了
    （窗口被拖动、换了系统缩放）只要 set_geometry 一下就继续，不用重建线程。

    active=False 时线程完全不碰光标：不跟随的时候鼠标必须还是你自己的。
    """

    def __init__(self, ox=0.0, oy=0.0, sx=1.0, sy=1.0, hz=150.0, tau=0.07,
                 get_pos=None, set_pos=None):
        self.dt = 1.0 / hz
        c = 2.0 / tau
        self.k = (c / 2.0) ** 2
        self.c = c
        self.lock = threading.Lock()
        self.ox, self.oy, self.sx, self.sy = float(ox), float(oy), float(sx), float(sy)
        self.get_pos = get_pos or _get_cursor_pos
        self.set_pos = set_pos or _set_cursor_pos
        gx, gy = self.get_pos()
        self.x = (gx - self.ox) / max(self.sx, 1e-6)
        self.y = (gy - self.oy) / max(self.sy, 1e-6)
        self.vx = self.vy = 0.0
        self.tx, self.ty = self.x, self.y
        self.active = False
        self.running = True
        self.writes = 0          # 写光标的次数（测试/排查「鼠标到底动没动」）
        threading.Thread(target=self._loop, daemon=True, name="lietrack-mouse").start()

    def set_geometry(self, ox, oy, sx, sy):
        with self.lock:
            self.ox, self.oy = float(ox), float(oy)
            self.sx, self.sy = float(sx), float(sy)

    def set_target(self, x, y):
        with self.lock:
            self.tx, self.ty = float(x), float(y)

    def start_tracking(self):
        """开跟：先把内部状态同步到光标现在的位置，避免开追瞬间猛拽一下。"""
        with self.lock:
            gx, gy = self.get_pos()
            self.x = (gx - self.ox) / max(self.sx, 1e-6)
            self.y = (gy - self.oy) / max(self.sy, 1e-6)
            self.vx = self.vy = 0.0
            self.tx, self.ty = self.x, self.y
            self.active = True

    def stop_tracking(self):
        """松手：光标停在原地，之后不再写 SetCursorPos。"""
        with self.lock:
            self.active = False

    def _loop(self):
        while self.running:
            with self.lock:
                if not self.active:
                    # 不跟随时跟着真实光标走，下次开追不会从旧位置起跳
                    gx, gy = self.get_pos()
                    self.x = (gx - self.ox) / max(self.sx, 1e-6)
                    self.y = (gy - self.oy) / max(self.sy, 1e-6)
                    self.vx = self.vy = 0.0
                    time.sleep(max(self.dt, 0.02))
                    continue
                ax = self.k * (self.tx - self.x) - self.c * self.vx
                ay = self.k * (self.ty - self.y) - self.c * self.vy
                self.vx += ax * self.dt
                self.vy += ay * self.dt
                self.x += self.vx * self.dt
                self.y += self.vy * self.dt
                sxp = int(round(self.ox + self.x * self.sx))
                syp = int(round(self.oy + self.y * self.sy))
                self.writes += 1
            self.set_pos(sxp, syp)
            time.sleep(self.dt)

    def stop(self):
        self.running = False


# ---------------- 追踪器（ScreenTrackerApp/track_core.py 原样搬运） ----------------
class YoloTracker:
    """YOLO 检测驱动的匀速追踪器（按目标行为规则调出来的）。

    规则：开局正中出现白团；匀速慢移不跳跃；边旋转边飘；同一时刻只有一个目标。
    设计：追踪控制器(速度限幅+转向限速) + 运动门控(门外检测=误检) +
         滑行惯性(隐身弱检测期) + 新局重捕(中心高置信白团)。
    """

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

    def reset(self):
        """新的一局：位置/速度/状态全清，回到「等正中间出现白团」。"""
        self.state = "INIT"
        self.pos = None
        self.vel = (0.0, 0.0)
        self.cruise = None
        self.last_det = None
        self.coast = 0
        self.lost_t = 0
        self.prev_dets = []

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


# ---------------- 检测器 ----------------
class Detector:
    """YOLO11n（3 类：白团 / 隐身漩涡 / 背景图形）。模型懒加载，第一次触发才吃内存。"""

    def __init__(self, weights, conf=0.10, device="cpu"):
        self.weights = str(weights)
        self.conf = float(conf)
        self.device = device
        self._model = None
        self.error = ""

    def load(self):
        if self._model is not None:
            return self._model
        from ultralytics import YOLO
        self._model = YOLO(self.weights)
        return self._model

    @property
    def loaded(self):
        return self._model is not None

    def detect(self, canvas_frame):
        """输入 750×500 画布帧，输出 [(cx, cy, w, h, conf, cls)]。"""
        model = self.load()
        r = model.predict(canvas_frame, imgsz=640, conf=self.conf,
                          verbose=False, device=self.device)[0]
        dets = []
        for b in r.boxes:
            cls = int(b.cls[0])
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            dets.append(((x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1,
                         float(b.conf[0]), cls))
        return dets


# ---------------- 触发服务 ----------------
class LieTrack(threading.Thread):
    """条件触发的追踪线程。

    `frame_provider()` 返回 (BGR 客户区帧, region)；region 是屏幕物理像素
    {"left","top","width","height"}，用于把 750×500 画布坐标换回屏幕坐标。
    返回 (None, None) 表示现在没有画面（窗口没了/最小化），本轮流空。

    可注入：`detector_factory` / `mouse_factory` / `ocr_factory`，测试时全换假的，
    一个按键都不发。
    """

    def __init__(self, frame_provider=None, cfg_file=None, foreground=None,
                 detector_factory=None, mouse_factory=None, ocr_factory=None,
                 tpl_factory=None):
        super().__init__(daemon=True, name="lietrack")
        self.frame_provider = frame_provider or (lambda: (None, None))
        self.foreground = foreground or (lambda: True)
        self.cfg_file = Path(cfg_file) if cfg_file else None
        self.cfg = load_cfg(self.cfg_file) if self.cfg_file else dict(DEFAULT_CFG)
        self.lock = threading.Lock()
        self.running = True

        self.tracker = YoloTracker(fps=self.cfg["track_fps"])
        self.ocr = ocr_factory(self.cfg["ocr_engine"]) if ocr_factory else OcrReader(self.cfg["ocr_engine"])
        # 触发源：模板匹配（默认）。懒加载——第一次用到才读图，出错只记 error 不挡服务。
        self.tpl = None
        self._tpl_factory = tpl_factory or (lambda path, thresh: TemplateTrigger(path, thresh))
        self._tpl_path_used = None
        self._det = None
        self._det_factory = detector_factory or self._default_detector
        self._mouse = None
        self._mouse_factory = mouse_factory or (lambda ox, oy, sx, sy: SmoothMouse(ox, oy, sx, sy))

        self.st = {
            "engine": "", "armed": False, "present": False, "keyword": "",
            "text": "", "ocr_ms": 0.0, "ocr_fps": 0.0, "tracking": False,
            "state": "INIT", "canvas": None, "target": None, "det_fps": 0.0,
            "hits": 0, "triggers": 0, "error": "", "last_trigger": 0.0,
            "frames": 0, "note": "", "run_left": 0.0,
            "score": 0.0,          # 模板相似度（trigger=template 时的当前值）
            "tpl_ok": None,        # 模板是否加载成功
        }
        # 一次触发跟到什么时候（time.time() 时间戳）。0 = 没在跑。
        # 关键词只在「起跑瞬间」有用：跑起来之后关键词消失也不收手，跑满 run_sec 才松。
        self._until = 0.0
        # True = 上一轮刚跑满、关键词还挂在画面上：等它先消失一次才允许再触发
        self._wait_clear = False
        # >0 = 强制起跑截止时间（「跟随测试」用：模拟窗口里没有可认的标题，
        # 直接绕过触发判定，跟 sec 秒）。跟真实触发共用 _until 的那套语义。
        self._force_until = 0.0
        self._force_run = 0.0

    # -- 配置 --
    def get_cfg(self):
        with self.lock:
            return dict(self.cfg)

    def set_cfg(self, patch):
        with self.lock:
            self.cfg = clean_cfg(patch, self.cfg)
            cfg = dict(self.cfg)
        if self.cfg_file:
            try:
                save_cfg(self.cfg_file, cfg)
            except OSError as e:
                self.st["error"] = f"配置保存失败: {e}"
        self.tracker.fps = cfg["track_fps"]
        if cfg["enabled"]:
            self.st["armed"] = True
        else:
            self._release("已关闭")
        return cfg

    def snapshot(self):
        with self.lock:
            st = dict(self.st)
            st["engine"] = st["engine"] or (
                "template" if self.cfg["trigger"] == "template"
                else (getattr(self.ocr, "name", "") or ""))
            st["cfg"] = dict(self.cfg)
            if self._det is not None and self._det.loaded:
                st["model"] = self._det.weights
            if isinstance(self.ocr, OcrReader):
                st["ocr_error"] = self.ocr.error
            if self.tpl is not None:
                st["tpl_error"] = self.tpl.error
            return st

    def _default_detector(self, weights):
        return Detector(weights, conf=self.cfg["det_conf"])

    def _resolve_asset(self, name):
        """资源文件（权重 / 模板图）的绝对路径。

        打包后模块在 PYZ 里没有 __file__，退回 paths.RES_DIR（exe 内部资源根）。
        """
        p = Path(name)
        if p.is_absolute():
            return p
        cands = []
        try:
            cands.append(Path(__file__).resolve().parent / p)
        except NameError:               # 打包后的 .pyz 里 __file__ 可能不存在
            pass
        try:
            import paths as _ap        # 同目录的 paths.py（源码运行与 exe 都靠它）
            cands.append(Path(_ap.RES_DIR) / p)
        except Exception:              # noqa: BLE001
            pass
        for c in cands:
            if c.exists():
                return c
        return cands[0] if cands else p

    def _resolve_weights(self):
        return self._resolve_asset(self.cfg["weights"])

    def _ensure_detector(self):
        """按当前配置拿到检测器（配置里换了权重文件时重建）。"""
        if self._det is None:
            weights = self._resolve_weights()
            if not weights.exists():
                raise FileNotFoundError(f"模型不存在: {weights}")
            self._det = self._det_factory(str(weights))
        return self._det

    def _ensure_tpl(self):
        """按当前配置拿到模板触发器（改过模板路径/门限就重建）。"""
        path = self._resolve_asset(self.cfg["template"])
        key = (str(path), float(self.cfg["tpl_thresh"]))
        if self.tpl is None or self._tpl_path_used != key:
            trg = self._tpl_factory(str(path), float(self.cfg["tpl_thresh"]))
            if not getattr(trg, "loaded", False):
                raise FileNotFoundError(trg.error or f"模板不存在: {path}")
            self.tpl = trg
            self._tpl_path_used = key
        return self.tpl

    def _ensure_mouse(self, region):
        sx = region["width"] / CANVAS_W
        sy = region["height"] / CANVAS_H
        if self._mouse is None:
            self._mouse = self._mouse_factory(region["left"], region["top"], sx, sy)
        else:
            self._mouse.set_geometry(region["left"], region["top"], sx, sy)
        return self._mouse

    # -- 主循环 --
    def run(self):
        """线程入口。

        单轮异常只记进 status 然后重开一轮，**不让线程静默退出**——否则服务看着
        好好的、面板一直「待命」，鼠标永远不动，现场很难查（踩过）。
        """
        while self.running:
            try:
                self._run_loop()
                break                       # _run_loop 返回 = running 已变 False
            except Exception as e:          # noqa: BLE001
                with self.lock:
                    self.st["error"] = f"线程异常: {type(e).__name__}: {e}"
                self._release("异常")
                time.sleep(0.5)

    def _run_loop(self):
        t_ocr = 0.0
        t_track = 0.0
        t_ocr_rate = time.perf_counter()
        ocr_n = 0
        last_present = 0.0
        hits = 0
        while self.running:
            cfg = self.get_cfg()
            now = time.time()
            forced = self._force_until > 0.0 and now < self._force_until
            if not cfg["enabled"] and not forced:
                self._release("未启用")
                time.sleep(0.2)
                continue
            self.st["armed"] = True

            # ★ 只在「该 OCR」或「正在跟」时才去抓帧。
            # 空闲时本来什么都不做，可 frame_provider 每调一次就是 find_window +
            # client_region + 整帧 copy；以前循环 sleep 0.005，等于 200Hz 白烤 CPU
            # （实测把机器压到 100% 满载，OCR 从 0.5 秒涨到 2~3 秒）。
            ocr_period = 1.0 / cfg["ocr_fps"]
            in_run = self._until > 0.0 and now < self._until
            want_ocr = now - t_ocr >= ocr_period
            if not (want_ocr or in_run or forced):
                time.sleep(min(0.06, max(0.006, t_ocr + ocr_period - now)))
                continue

            frame, region = self.frame_provider()
            if frame is None or region is None:
                self._release("没有画面")
                time.sleep(0.3)
                continue
            if not forced and not self.foreground():
                self._release("游戏不在前台")
                time.sleep(0.2)
                continue

            now = time.time()
            # ---- 1) 触发判定：默认认模板（弹窗图），可选退回中文 OCR ----
            # 「跟随测试」强制起跑期间整段跳过：模拟窗口里没有可认的标题，
            # 让它自己判定反而会把 present 判回 False，把测试搅乱。
            if now - t_ocr >= ocr_period and not (now < self._force_until):
                t_ocr = now
                t0 = time.perf_counter()
                tpl_score = 0.0
                try:
                    if cfg["trigger"] == "template":
                        trg = self._ensure_tpl()
                        tpl_score, _box = trg.detect(frame)
                        hit_kw = cfg["template"] if tpl_score >= cfg["tpl_thresh"] else ""
                        hit_text = f"相似度 {tpl_score:.2f}"
                        texts = []
                    else:
                        # 全帧 OCR 是 700ms 级（1366 宽），降到 960 宽约省一半，中文照样认得出。
                        # 关键词是固定 UI 文字，缩这么点不影响命中。
                        ocr_img = frame
                        if frame.ndim == 3 and frame.shape[1] > cfg.get("ocr_max_w", 960):
                            k = cfg.get("ocr_max_w", 960) / frame.shape[1]
                            ocr_img = cv2.resize(frame, (int(frame.shape[1] * k),
                                                         int(frame.shape[0] * k)),
                                                 interpolation=cv2.INTER_AREA)
                        texts, tpl_hit = self.ocr.read(ocr_img, cfg["keywords"])
                        hit_kw, hit_text = match_keywords(texts, cfg["keywords"])
                        if not hit_kw and tpl_hit and tpl_hit[0]:
                            # 模板匹配的分数门限比 OCR 置信度保守（同字体同字号时能到 0.9+）
                            tpl_score = float(tpl_hit[1])
                            if tpl_score >= 0.55:
                                hit_kw = tpl_hit[0]
                    ocr_ms = (time.perf_counter() - t0) * 1000
                except Exception as e:  # noqa: BLE001  单轮失败不停服务
                    hit_kw, hit_text, ocr_ms, tpl_score = "", "", 0.0, 0.0
                    self.st["error"] = f"触发判定失败: {type(e).__name__}: {e}"
                ocr_n += 1
                if now - t_ocr_rate >= 1.0:
                    self.st["ocr_fps"] = ocr_n / (now - t_ocr_rate)
                    ocr_n, t_ocr_rate = 0, now
                with self.lock:
                    self.st["engine"] = ("template" if cfg["trigger"] == "template"
                                         else getattr(self.ocr, "name", "")) or self.st["engine"]
                    self.st["ocr_ms"] = round(ocr_ms, 1)
                    self.st["score"] = round(float(tpl_score), 3)
                    self.st["text"] = (hit_text or (texts[0] if texts else ""))[:40]
                    self.st["tpl_ok"] = True if cfg["trigger"] == "template" else self.st["tpl_ok"]
                if hit_kw:
                    hits += 1
                    last_present = now
                    if hits >= cfg["hit_frames"]:
                        with self.lock:
                            if not self.st["present"]:
                                self.st["triggers"] += 1
                                self.st["last_trigger"] = now
                            self.st["present"] = True
                            self.st["keyword"] = hit_kw
                            self.st["hits"] = hits
                else:
                    hits = 0
                    self._wait_clear = False      # 关键词退场了，下一次命中可以重新触发
                    with self.lock:
                        self.st["hits"] = 0
                        if self.st["present"] and now - last_present > cfg["hold_sec"]:
                            self.st["present"] = False

            # ---- 2) 跟随窗口：关键词只负责「起跑」，跑满 run_sec 才自动收手 ----
            # 要的就是这个手感：认到关键词 → 开始跟 → 15 秒后自己松手。
            # 中途关键词消失、目标暂时丢掉都不打断（丢目标就重置回「等中心重捕」）。
            present = self.st["present"]
            if present and self._until <= 0.0 and not self._wait_clear:
                dur = float(cfg["run_sec"])
                if now < self._force_until:
                    dur = max(1.0, self._force_until - now)   # 「跟随测试」按测试时长跑
                self._until = now + dur
                self.tracker.reset()
                with self.lock:
                    self.st["run_left"] = self._until - now
            in_run = self._until > 0.0 and now < self._until
            if in_run:
                try:
                    det = self._ensure_detector()
                    mouse = self._ensure_mouse(region)
                except Exception as e:  # noqa: BLE001
                    self.st["error"] = f"{type(e).__name__}: {e}"
                    self._release("追踪启动失败")
                    time.sleep(0.5)
                    continue
                if not self.st["tracking"]:
                    with self.lock:
                        self.st["tracking"] = True
                    self.tracker.reset()
                    if cfg["move_mouse"]:
                        mouse.start_tracking()

                if now - t_track >= 1.0 / cfg["track_fps"]:
                    # 追踪器速度量纲是 px/帧，帧率按两次检测的实测间隔自适应
                    if t_track and now - t_track < 2.0:
                        self.tracker.fps = 1.0 / max(1e-3, now - t_track)
                    t_track = now
                    try:
                        canvas = cv2.resize(frame, (CANVAS_W, CANVAS_H),
                                            interpolation=cv2.INTER_AREA)
                        dets = det.detect(canvas)
                        st = self.tracker.update(dets)
                    except Exception as e:  # noqa: BLE001  推理炸了别把线程带走
                        self.st["error"] = f"推理失败: {type(e).__name__}: {e}"
                        time.sleep(0.2)
                        continue
                    pos = self.tracker.pos
                    tgt = None
                    if pos is not None:
                        sx = region["width"] / CANVAS_W
                        sy = region["height"] / CANVAS_H
                        tgt = (int(round(region["left"] + pos[0] * sx)),
                               int(round(region["top"] + pos[1] * sy)))
                        if cfg["move_mouse"]:
                            mouse.set_target(pos[0], pos[1])
                    with self.lock:
                        self.st["state"] = st
                        self.st["canvas"] = [round(pos[0], 1), round(pos[1], 1)] if pos else None
                        self.st["target"] = list(tgt) if tgt else None
                        self.st["frames"] += 1
                        self.st["error"] = ""
                    if self.tracker.state == "LOST":
                        # 目标暂时没了：不收摊，清回 INIT 等中心重捕，跟到时间到为止
                        self.tracker.reset()
                with self.lock:
                    self.st["run_left"] = max(0.0, self._until - time.time())
            elif self._until > 0.0:
                # 跑满了：松手，等下一次关键词。
                # 关键词要是还赖在画面上，也不许立刻重新起跑（会变成一直跟下去），
                # 必须等它先消失一次（_wait_clear 解除）才有下一次触发。
                was_test = self._force_until > 0.0
                self._release("跟随测试结束" if was_test else f"跑满 {cfg['run_sec']:.0f} 秒")
                self._until = 0.0
                with self.lock:
                    self.st["run_left"] = 0.0
                    self.st["present"] = False
                    if was_test:
                        # 测试窗口里没有「弹窗消失」这件事，直接解锁，允许再测一次
                        self._force_until = 0.0
                        self._wait_clear = False
                    else:
                        self._wait_clear = True
            # 跟着的时候按 track_fps 睡到下一个检测点；没跟时也别空转（一次抓帧就是
            # 整帧内存拷贝，5ms 一圈等于 200Hz）
            if self.st["tracking"]:
                nxt = t_track + 1.0 / cfg["track_fps"]
                time.sleep(max(0.004, min(0.05, nxt - time.time())))
            else:
                time.sleep(0.02)
        self._release("线程退出")

    def probe(self):
        """对当前帧跑一次触发判定，不追踪（面板上的「测触发」用）。"""
        frame, _ = self.frame_provider()
        cfg = self.get_cfg()
        mode = cfg["trigger"]
        if frame is None:
            return {"ok": False, "msg": "现在没有画面（窗口没找到/最小化）",
                    "trigger": mode, "engine": mode, "texts": [], "hit": "", "score": 0.0}
        t0 = time.perf_counter()
        if mode == "template":
            try:
                trg = self._ensure_tpl()
                score, box = trg.detect(frame)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "msg": f"模板加载失败: {type(e).__name__}: {e}",
                        "trigger": mode, "engine": "template", "texts": [], "hit": "",
                        "score": 0.0}
            hit_kw = cfg["template"] if score >= cfg["tpl_thresh"] else ""
            return {
                "ok": True, "trigger": mode, "engine": "template",
                "texts": [], "hit": hit_kw,
                "text": f"相似度 {score:.3f}", "score": round(float(score), 4),
                "thresh": float(cfg["tpl_thresh"]), "box": list(box) if box else None,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": getattr(trg, "error", ""),
            }
        try:
            img = frame
            max_w = cfg.get("ocr_max_w", 960)
            if frame.ndim == 3 and frame.shape[1] > max_w:
                k = max_w / frame.shape[1]
                img = cv2.resize(frame, (int(frame.shape[1] * k), int(frame.shape[0] * k)),
                                 interpolation=cv2.INTER_AREA)
            texts, tpl = self.ocr.read(img, cfg["keywords"])
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": f"OCR 失败: {type(e).__name__}: {e}",
                    "trigger": mode, "engine": getattr(self.ocr, "name", ""),
                    "texts": [], "hit": "", "score": 0.0}
        hit_kw, hit_text = match_keywords(texts, cfg["keywords"])
        tpl_kw, tpl_score = tpl if tpl else ("", 0.0)
        if not hit_kw and tpl_kw and tpl_score >= 0.55:
            hit_kw, hit_text = tpl_kw, tpl_kw
        return {
            "ok": True, "trigger": mode, "engine": getattr(self.ocr, "name", "") or "",
            "texts": list(texts or [])[:10], "hit": hit_kw, "text": hit_text,
            "tpl": [tpl_kw, round(float(tpl_score), 3)],
            "score": round(float(tpl_score), 4),
            "ms": round((time.perf_counter() - t0) * 1000, 1),
            "error": getattr(self.ocr, "error", ""),
        }

    def warmup(self):
        """启动后后台预热：读模板图、装好 lietrack 自己的 YOLO（可选还跑一次 OCR）。

        不做这件事，第一次命中要等：ultralytics 导入 ~2 s + 模型首帧编译 ~3 s
        （OCR 模式还要加 9 s 引擎冷启动）。用户体感就是「识别到关键词用了很久」。
        """
        out = {"template": "", "detector": "", "ocr": "", "ms": 0.0}
        t0 = time.perf_counter()
        try:
            self._ensure_tpl()
            out["template"] = "ok"
        except Exception as e:  # noqa: BLE001
            out["template"] = f"{type(e).__name__}: {e}"
        cfg = self.get_cfg()
        if cfg["trigger"] == "ocr":
            try:
                self.ocr.read(np.zeros((360, 640, 3), np.uint8), cfg["keywords"])
                out["ocr"] = "ok"
            except Exception as e:  # noqa: BLE001
                out["ocr"] = f"{type(e).__name__}: {e}"
        try:
            det = self._ensure_detector()
            det.detect(np.zeros((CANVAS_H, CANVAS_W, 3), np.uint8))
            out["detector"] = "ok"
        except Exception as e:  # noqa: BLE001
            out["detector"] = f"{type(e).__name__}: {e}"
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        with self.lock:
            self.st["warmup"] = dict(out)
        return out

    def _release(self, why=""):
        with self.lock:
            was = self.st["tracking"]
            self.st["tracking"] = False
            self.st["state"] = "INIT"
            self.st["canvas"] = None
            self.st["target"] = None
            self.st["run_left"] = 0.0
            if why and was:
                self.st["note"] = why
        # 松手就结束这一轮，下一次关键词重新计时（否则停用再启用会接着走旧窗口）
        self._until = 0.0
        if self._mouse is not None:
            try:
                self._mouse.stop_tracking()
            except Exception:  # noqa: BLE001
                pass

    def release(self, why="手动松开"):
        """外部（接口/面板）要求立刻松开鼠标，但不影响「是否启用」。"""
        self._release(why)

    def force(self, sec=10.0):
        """绕过触发判定，直接起跑 sec 秒（「跟随测试」用）。

        模拟用的那个测谎窗口里没有「谎言探测仪」这行标题，模板永远认不出来，
        所以测试时直接给它一个「已经在场」的时间窗，走的是和真实触发完全相同
        的追踪/鼠标那条路。sec 到点后按 `_wait_clear` 的规矩正常收手。
        """
        sec = max(1.0, min(float(sec), 120.0))
        now = time.time()
        self.tracker.reset()
        self._release("")             # 清掉上一轮的 _until / 鼠标，从干净状态起跑
        with self.lock:
            self._force_until = now + sec
            self._force_run = sec
            self._wait_clear = False
            self.st["present"] = True
            self.st["keyword"] = "跟随测试"
        return sec

    def stop(self):
        self.running = False
        self._release("停止")
