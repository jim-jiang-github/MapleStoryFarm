# MapleStoryFarm

冒险岛怀旧服挂机 + 预览 WebUI（抓屏 → YOLO 检测 → 网页实时预览 / 血蓝经验条读数）。

## 本地预览

```
启动预览.bat            # 或  python webui/server.py --open
```

默认 http://127.0.0.1:8765 。本地直连走 MJPEG（最省 CPU），画面、检测框、状态条读数都在同一个页面里。

## 外网访问

```
外网访问.bat
```

脚本会做三件事：

1. 带口令（默认 `msfarm2026`）启动预览服务；
2. 起一条免注册隧道，**优先 cloudflared**，没有就退回 tunnelmole；
3. 把 `https://xxxx/?k=口令` 这种完整链接打出来，同时复制到剪贴板 —— 直接粘出去就能用。

口令不带 `?k=` 打不开；打开过一次浏览器记 7 天，不用每次输。

### 为什么优先 cloudflared

| | cloudflared（quick tunnel） | tunnelmole |
|---|---|---|
| 转发方式 | Cloudflare 边缘，HTTP/2 流式 | 中转节点，会**整段缓冲**长连接 |
| 单次往返 | 0.3~0.8s | 0.7~3s（偶发十几秒） |
| H.264 直播 | 可以（单连接连续推流） | 不行，只能退回分片轮询 |
| 装法 | 把 `cloudflared.exe` 丢进 `tools\bin\` 即可（脚本自动发现） | `npx` 自动下载，免安装 |

> cloudflared 首次运行 Windows 会弹防火墙提示，点 **Allow** 就行（它只往外连，不开本地端口）。

## 外网画面是怎么传的

远端访问不再是一帧一张 JPEG（那样 5~10 倍带宽，穿透下基本没法看），而是：

```
抓屏帧 → ffmpeg 编 H.264（ultrafast/zerolatency，只传变化的部分）
       → 每 0.5s 切一个 fMP4 分片
       → 浏览器 MediaSource 拼接解码
```

> **识别和串流是两条独立的线**：YOLO 检测和血条读数永远在本地吃抓屏的**原始帧**
> （原始 BGR 像素，不经过任何编码），压缩只作用于"发给浏览器的画面"。
> 所以网再慢、画质档位再低，HP/MP/EXP 的百分比精度都不受影响。

两条通道，前端自动选：

- `/video/live`：单条长连接连续推流，隧道支持流式时延迟最低（cloudflared / 局域网）；
- `/video/seg`：短请求逐片取，给 tunnelmole 这种会缓冲长连接的隧道兜底。

页面左上角徽标会显示 `H.264 实时`；实在不行才退回 `远程 · 逐帧`（一帧一张 JPEG）。

### 画质档位

页面上的「画质」按钮（流畅 450k / 标准 900k / 高清 1600k）决定外网码率。
**带宽是硬瓶颈**：家用上行 + 免费隧道实测只有 1 Mbps 上下，所以默认「标准」；
画面卡就往下调，细节看不清再往上调。选择存在 localStorage 里，下次打开还在。

也可以用环境变量改服务端默认值：`VIDEO_WIDTH` / `VIDEO_FPS` / `VIDEO_BITRATE` / `VIDEO_FRAG_SEC`。

### H.265 呢？

不能用于浏览器：Chrome/Edge 的 MSE 基本不支持 HEVC（要系统装 HEVC 扩展 + 硬解，各家行为还不一致），
H.264 baseline 是唯一"哪里都能放"的选择。带宽受限时，H.265 省下的那点码率也换不来质变。

## 依赖

```bash
pip install mss opencv-python fastapi uvicorn pywin32 numpy
pip install imageio-ffmpeg     # 外网视频用，自带 ffmpeg 可执行文件（不用单独装 50MB 的 ffmpeg）
```

YOLO 模型文件（`best.pt`、`*_openvino_model/`）不入库，换机器要自己拷。
