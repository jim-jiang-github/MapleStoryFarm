# -*- coding: utf-8 -*-
"""实时画面：ffmpeg 编 H.264，切成 fMP4 分片，浏览器用 MSE / 原生 HLS 播放。

注意：这个模块只管"发给浏览器的画面"。识别（YOLO 检测、血条 HSV 读数）走的是
server.py 里 grabber 的原始帧，跟这里的编码/压缩完全无关——压缩只影响观看，不影响识别。

为什么不再用 MJPEG / 逐帧 JPEG：
- MJPEG 每帧都是独立 JPEG，同画质带宽是 H.264 的 5~10 倍（30 帧能到 10~20 Mbps）。
  内网穿透扛不住，而且 multipart 长连接会被隧道中间层缓冲，甚至一条连接堵死整条隧道。
- 逐帧轮询一帧一张 JPEG，穿透下单张要 0.5~2 秒，看着像卡死的幻灯片。
- H.264 只编"变化的部分"（P 帧），1000px@20fps 大约 1.5~2.5 Mbps。

为什么是"分片短请求"而不是长连接直播：
免费穿透（tunnelmole/cloudflared quick tunnel）对 chunked 长连接很敏感——攒够缓冲区
才往下发，延迟能到十几秒。改成每 0.5 秒一个分片、客户端用短请求去取，任何 HTTP 隧道
都能过，实测延迟 ~1 秒。分片是完整的 moof+mdat，丢一片只花屏一下，下一片立刻恢复。
"""
import collections
import math
import os
import shutil
import subprocess
import threading
import time

import cv2

VIDEO_W = int(os.environ.get("VIDEO_WIDTH", "1000") or 1000)   # 1000px 宽，够看清字
VIDEO_FPS = int(os.environ.get("VIDEO_FPS", "20") or 20)
VIDEO_BITRATE = os.environ.get("VIDEO_BITRATE", "900k")       # 实测家用上行 + 免费隧道 ≈ 1Mbps
# 分片 = 一个 GOP = 1 秒。之前是 0.5 秒，等于每 7 帧就插一个 I 帧，
# 900k 码率下一半的比特都花在 I 帧上，P 帧被饿得发虚——实测 PSNR 只有 27.8dB。
# 改成 1 秒后同码率 PSNR 33.0dB（掉帧观感几乎无差别，画面清晰度差一大截）。
VIDEO_FRAG_SEC = float(os.environ.get("VIDEO_FRAG_SEC", "1.0") or 1.0)
VIDEO_BOX_FPS = VIDEO_FPS
# main @ L4.0：baseline 没有 CABAC，同码率白白损失 10~15% 画质
VIDEO_CODEC = "avc1.4D4028"
VIDEO_MIME = 'video/mp4; codecs="%s"' % VIDEO_CODEC
HLS_WINDOW = int(os.environ.get("VIDEO_HLS_WINDOW", "4") or 4)   # HLS 播放列表保留几段
# HLS 每段拼几个 1 秒分片。手机走隧道时一个请求光往返就要 0.5~1 秒，
# 1 秒一段根本来不及（每段要 1.5 秒才拉完），拼成 2 秒一段才能勉强跟上实时；
# 代价是 iOS 上延迟多 2 秒左右——换来的是"一直动"，不是"动一下停一下"。
HLS_GROUP = max(1, int(os.environ.get("VIDEO_HLS_GROUP", "2") or 2))

_FFMPEG = None


def ffmpeg_exe():
    """优先用 imageio-ffmpeg 自带的 ffmpeg（pip 装，不用单独下 50MB 的 exe）。"""
    global _FFMPEG
    if _FFMPEG is None:
        try:
            import imageio_ffmpeg
            _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # noqa: BLE001
            _FFMPEG = shutil.which("ffmpeg") or ""
        if _FFMPEG and not os.path.exists(_FFMPEG):
            _FFMPEG = shutil.which("ffmpeg") or ""
    return _FFMPEG


def _kbps(text, default=900):
    """把 "900k" / "1.2M" / "900000" 统一换算成 kbps 整数。"""
    try:
        t = str(text).strip().lower()
        if t.endswith("k"):
            return max(1, int(float(t[:-1])))
        if t.endswith("m"):
            return max(1, int(float(t[:-1]) * 1000))
        return max(1, int(float(t) / 1000))
    except (TypeError, ValueError):
        return default


def _read_exact(f, n):
    buf = b""
    while len(buf) < n:
        part = f.read(n - len(buf))
        if not part:
            raise EOFError
        buf += part
    return buf


def _iter_boxes(f):
    """按顺序读 ISO-BMFF 盒子：先 length(4) + type(4)，length==1 时后面跟 8 字节扩展长度。"""
    while True:
        hdr = _read_exact(f, 8)
        size = int.from_bytes(hdr[:4], "big")
        typ = hdr[4:8]
        if size == 1:
            ext = _read_exact(f, 8)
            size = int.from_bytes(ext, "big")
            yield typ, hdr + ext + _read_exact(f, size - 16)
        elif size == 0:                     # 一直到 EOF，流式输出不该出现，出现就收摊
            raise EOFError
        else:
            yield typ, hdr + _read_exact(f, size - 8)


class VideoStream:
    """一路编码流：一个 ffmpeg 进程 + 一个喂帧线程 + 一个切片线程。"""

    def __init__(self, snapshot, overlay=None, width=None, fps=None, bars=False,
                 bitrate=None):
        self.snapshot = snapshot
        self.overlay = overlay
        self.width = width or VIDEO_W
        self.fps = fps or VIDEO_FPS
        self.bars = bool(bars)
        self.bitrate = bitrate or VIDEO_BITRATE
        self._kbps = _kbps(self.bitrate)
        self.key = (self.width, self.fps, self.bars)

        self.init = b""
        self.size = (0, 0)
        self.seq = 0                        # 下一个要发出去的分片序号
        self.frags = collections.deque(maxlen=240)
        self.cond = threading.Condition()
        self.error = ""
        self.encoder = ""
        self.started_at = time.time()
        self.last_access = time.time()
        self.stopped = threading.Event()
        self.proc = None
        self.sent_frags = 0
        self.last_ts = 0.0

        frame = self.snapshot()
        h, w = frame.shape[:2]
        tw = min(self.width, w)
        tw -= tw % 2                        # yuv420p 要求偶数
        th = int(round(h * tw / w))
        th -= th % 2
        self.size = (tw, max(2, th))
        threading.Thread(target=self._feed, name="video-feed", daemon=True).start()

    # ---------- 对外 ----------
    def touch(self):
        self.last_access = time.time()

    @property
    def ready(self):
        return bool(self.init)

    def info(self):
        return {
            "codec": VIDEO_MIME,
            "width": self.size[0], "height": self.size[1],
            "fps": self.fps, "bars": self.bars, "kbps": self._kbps,
            "encoder": self.encoder, "ready": self.ready,
            "seq": self.seq, "oldest": self.frags[0][0] if self.frags else self.seq,
            "sent": self.sent_frags, "error": self.error,
        }

    def stop(self):
        self.stopped.set()
        with self.cond:
            self.cond.notify_all()
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass

    # ---------- HLS（iOS 原生播放用） ----------
    def snapshot_frag(self, seq):
        """取指定序号的整片（moof+mdat）。太老已被回收就返回 None。"""
        with self.cond:
            for f in self.frags:
                if f[0] == seq:
                    self.last_access = time.time()
                    return f[2]
        return None

    def hls_playlist(self, init_uri="init.mp4", seg_prefix="seg_", seg_ext=".m4s", extra=""):
        """滚动播放列表：最近几段 + EXT-X-MAP。iOS Safari 没有 MSE，但天生会放 HLS。"""
        with self.cond:
            frags = list(self.frags)[-(HLS_WINDOW * HLS_GROUP):]
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
        # 只列"凑满一整段"的组，末组不满就先不给，免得播放器算错时长
        groups = [frags[i:i + HLS_GROUP] for i in range(0, len(frags), HLS_GROUP)]
        groups = [g for g in groups if len(g) == HLS_GROUP]
        if not groups:
            lines += ["#EXT-X-TARGETDURATION:3", "#EXT-X-MEDIA-SEQUENCE:0",
                      '#EXT-X-MAP:URI="%s%s"' % (init_uri, extra)]
            return "\n".join(lines) + "\n"
        durs = []
        flat = [f for g in groups for f in g]
        for i, f in enumerate(flat):
            d = (flat[i + 1][1] - f[1]) if i + 1 < len(flat) else VIDEO_FRAG_SEC
            durs.append(min(max(d, 0.2), 5.0))
        seg_durs = [sum(durs[i * HLS_GROUP:(i + 1) * HLS_GROUP]) for i in range(len(groups))]
        lines.append("#EXT-X-TARGETDURATION:%d" % max(2, int(math.ceil(max(seg_durs)))))
        lines.append("#EXT-X-MEDIA-SEQUENCE:%d" % groups[0][0][0])
        lines.append('#EXT-X-MAP:URI="%s%s"' % (init_uri, extra))
        for g, d in zip(groups, seg_durs):
            lines.append("#EXTINF:%.3f," % d)
            lines.append("%s%d%s%s" % (seg_prefix, g[0][0], seg_ext, extra))
        return "\n".join(lines) + "\n"

    def hls_segment(self, seq):
        """一段 = HLS_GROUP 个分片拼起来（每片自带 moof+mdat，直接接就行）。"""
        out = []
        with self.cond:
            want = set(range(seq, seq + HLS_GROUP))
            got = {f[0]: f[2] for f in self.frags if f[0] in want}
            self.last_access = time.time()
        if len(got) != HLS_GROUP:
            return None
        for i in range(seq, seq + HLS_GROUP):
            out.append(got[i])
        return b"".join(out)

    def get_frags(self, since, timeout=1.2):
        """等新分片。返回 (next_seq, 负载)。落后太多（已被丢弃）时返回 (None, None) 让客户端重连。"""
        deadline = time.time() + timeout
        with self.cond:
            while True:
                if self.stopped.is_set():
                    return None, None
                if self.frags:
                    oldest = self.frags[0][0]
                    if since < oldest:
                        return None, None
                    avail = [f for f in self.frags if f[0] >= since]
                    if len(avail) > 6:          # 落后太多就直接跳到最新，别追存储
                        avail = avail[-6:]
                    if avail:
                        self.sent_frags += len(avail)
                        self.last_access = time.time()
                        self.last_ts = avail[-1][1]
                        return avail[-1][0] + 1, b"".join(f[2] for f in avail)
                if time.time() >= deadline or self.error:
                    return since, b""
                self.cond.wait(max(0.02, min(0.25, deadline - time.time())))

    # ---------- 喂帧 ----------
    def _feed(self):
        exe = ffmpeg_exe()
        if not exe:
            self.error = "没找到 ffmpeg（pip install imageio-ffmpeg）"
            with self.cond:
                self.cond.notify_all()
            return
        tw, th = self.size
        gop = max(2, int(round(self.fps * VIDEO_FRAG_SEC)))
        cmd = [
            exe, "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{tw}x{th}", "-r", str(self.fps), "-i", "pipe:0",
            "-an",
            # veryfast 比 ultrafast 慢不了多少（1000px/20fps 单核 10% 左右），但开了
            # CABAC + 亚像素运动估计；tune animation 对冒险岛这种平涂 2D 画面特别对路。
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "animation",
            "-profile:v", "main", "-level:v", "4.0",
            # 只给 2 个线程：x264 默认按核数开（20 核就是 30 个帧线程），
            # 帧级并行会让输出滞后"线程数"帧，等于自己加了 1 秒延迟。
            # 实测 2 线程已经 230fps（要 20fps），够用得多。
            "-threads", "2",
            "-b:v", self.bitrate, "-maxrate", self.bitrate,
            "-bufsize", str(int(self._kbps * 2)) + "k",
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            # 关 B 帧/前视/切片线程：出片延迟只剩几毫秒，同时不牺牲压缩率
            "-x264-params",
            "scenecut=0:rc-lookahead=0:sync-lookahead=0:bframes=0:sliced-threads=0:ref=2",
            "-pix_fmt", "yuv420p",
            "-f", "mp4",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-flush_packets", "1",
            "pipe:1",
        ]
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0,
            )
        except Exception as e:  # noqa: BLE001
            self.error = f"启动 ffmpeg 失败: {e}"
            with self.cond:
                self.cond.notify_all()
            return
        self.encoder = "libx264 veryfast/animation"
        threading.Thread(target=self._pump, name="video-pump", daemon=True).start()

        # stderr 单独读，避免管道写满把 ffmpeg 卡死
        def _drain():
            try:
                for line in self.proc.stderr:
                    text = line.decode("utf-8", "ignore").strip()
                    if text:
                        self.error = text[:200]
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_drain, daemon=True).start()

        period = 1.0 / self.fps
        nxt = time.perf_counter()
        try:
            while not self.stopped.is_set():
                frame = self.snapshot()
                if self.overlay is not None and self.bars:
                    frame = self.overlay(frame)
                if frame.shape[1] != tw or frame.shape[0] != th:
                    frame = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
                self.proc.stdin.write(frame.tobytes())
                nxt += period
                delay = nxt - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                elif delay < -period * 3:        # 落后太多就别追了，重新对齐时钟
                    nxt = time.perf_counter()
        except Exception as e:  # noqa: BLE001
            if not self.stopped.is_set():
                self.error = f"送帧中断: {type(e).__name__}"
        finally:
            try:
                self.proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            self.stopped.set()
            with self.cond:
                self.cond.notify_all()

    # ---------- 切片 ----------
    def _pump(self):
        proc = self.proc
        init_parts = []
        cur = []
        try:
            for typ, data in _iter_boxes(proc.stdout):
                if typ == b"moof":
                    cur = [data]
                elif typ == b"mdat":
                    cur.append(data)
                    blob = b"".join(cur)
                    cur = []
                    with self.cond:
                        self.frags.append((self.seq, time.time(), blob))
                        self.seq += 1
                        self.cond.notify_all()
                elif not self.init:                 # moof 之前出现的都是初始化段
                    init_parts.append(data)
                    if typ == b"moov":
                        self.init = b"".join(init_parts)
                        with self.cond:
                            self.cond.notify_all()
        except Exception:  # noqa: BLE001
            pass
        finally:
            if not self.init and init_parts:
                self.init = b"".join(init_parts)
            if not self.error:
                self.error = "编码进程已退出"
            self.stopped.set()
            with self.cond:
                self.cond.notify_all()
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


class VideoHub:
    """按 (宽度, 帧率, 是否带标定框) 复用同一路流；没人看就自动收掉，不白烧 CPU。"""

    def __init__(self, snapshot, overlay=None, idle_timeout=25.0):
        self.snapshot = snapshot
        self.overlay = overlay
        self.idle_timeout = idle_timeout
        self.streams = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._janitor, name="video-janitor", daemon=True).start()

    def get(self, width=None, fps=None, bars=False, bitrate=None):
        key = (width or VIDEO_W, fps or VIDEO_FPS, bool(bars), bitrate or VIDEO_BITRATE)
        with self.lock:
            st = self.streams.get(key)
            if st is None or st.stopped.is_set():
                st = VideoStream(self.snapshot, self.overlay, width=key[0],
                                 fps=key[1], bars=key[2], bitrate=key[3])
                self.streams[key] = st
            st.touch()
            return st

    def _janitor(self):
        while True:
            time.sleep(5)
            now = time.time()
            with self.lock:
                for key, st in list(self.streams.items()):
                    if st.stopped.is_set():
                        self.streams.pop(key, None)
                    elif now - st.last_access > self.idle_timeout:
                        st.stop()
                        self.streams.pop(key, None)

    def stop_all(self):
        with self.lock:
            for st in self.streams.values():
                st.stop()
            self.streams.clear()
