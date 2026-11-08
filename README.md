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
抓屏帧 → ffmpeg 编 H.264（veryfast + tune animation + main profile）
       → 每 1 秒切一个 fMP4 分片（一个分片 = 一个 GOP = 一个关键帧开头）
       → 浏览器 MediaSource 拼起来播 / iPhone 走原生 HLS
```

> **识别和串流是两条独立的线**：YOLO 检测和血条读数永远在本地吃抓屏的**原始帧**
> （原始 BGR 像素，不经过任何编码），压缩只作用于"发给浏览器的画面"。
> 所以网再慢、画质档位再低，HP/MP/EXP 的百分比精度都不受影响。

### 编码参数为什么这么选（同一段真实画面，900k 实测）

| 参数 | 实际码率 | PSNR |
|---|---|---|
| 旧：ultrafast + baseline + 0.5s GOP | 1177 kbps | 27.8 dB |
| 新：veryfast + animation + main + 1s GOP | 897 kbps | 33.0 dB |

同码率下 PSNR +5.2 dB（误差能量降到约 1/3），码率反而更低。两个关键点：

- **0.5 秒一个 I 帧等于每 7 帧重画一次**，900k 码率下一半比特都喂给 I 帧，P 帧被饿得发虚——
  这是「清晰度不够」的主因。改成 1 秒一个 GOP 就够用（想更省/更清晰可以调 `VIDEO_FRAG_SEC`）。
- **baseline 没有 CABAC**，白丢 10~15% 画质；main profile 谁都放得动。
- `tune animation` 对冒险岛这种平涂 2D 特别对路（参考帧更多、去块更强）。

### 三条通道，前端自动选

| 通道 | 给谁 | 说明 |
|---|---|---|
| `/video/live` | 支持 MSE 的浏览器（Chrome / Edge / 安卓） | 单条长连接连续推流，延迟最低 |
| `/video/seg` | 会缓冲长连接的隧道（tunnelmole 等） | 短请求逐片取，逐片兜底 |
| `/video/hls/index.m3u8` | **iPhone / iPad** | Safari 没有 MSE，但原生会放 HLS |
| 逐帧 JPEG | 以上全失败 | 一帧一张 JPEG，最差的兜底 |

页面右侧「画面通道」那一行会写清楚当前走的哪条；左上角徽标 `H.264 实时` / `HLS 实时`。

### 手机上为什么以前特别卡

iPhone 的 Safari 没有 MSE（iOS 17 之前完全没有），旧版本检测不到 MSE 就退回**逐帧 JPEG**：
穿透链路上单张要 0.5~1 秒往返，于是变成 **1 帧/秒的幻灯片**，而且只有 1000px/质量 68。
现在这些设备走原生 HLS，码流、帧率、清晰度和电脑上完全一样，只是延迟多 1~2 秒。

### 在家用手机看，别走穿透

启动脚本会打印一行：

```
手机(同 WiFi) -> http://192.168.x.x:8765/?k=口令   ← 在家就用这个，最流畅
```

局域网往返几毫秒，2 Mbps 的高清档都毫无压力，跟在本机看没区别。
手机在外面时才需要隧道地址。

### 免费隧道的天花板（实测）

单条 cloudflared quick tunnel：**往返 0.5~1.0 秒，吞吐 ~1.2 Mbps**。
这个往返时延决定了"逐片请求"类的传输都很吃力（一个请求就要 0.5 秒起步），
所以画面优先走长连接；也决定了再怎么压码率，手机上都会有 1 秒以上的延迟。
真要"像本地一样顺"，只能换中继方式：

- 路由器把 8765 端口映射出去（家宽分到公网 IP 时可用，延迟 20~40ms）；
- 自己的 VPS 跑 frp（同省 30~60ms），比免费隧道稳得多。

### 画质档位

页面上的「画质」按钮：流畅 800p/15fps/480k、标准 1000p/20fps/900k、高清 1280p/25fps/1600k。
**带宽是硬瓶颈**：家用上行 + 免费隧道实测只有 1 Mbps 上下，所以默认「标准」。
卡顿会自动降一档（起播头 15 秒不算，避免误判），选择存 localStorage，下次打开还在。

也可以用环境变量改服务端默认值：`VIDEO_WIDTH` / `VIDEO_FPS` / `VIDEO_BITRATE` / `VIDEO_FRAG_SEC`。

### H.265 呢？

不能用于浏览器：Chrome/Edge 的 MSE 基本不支持 HEVC（要系统装 HEVC 扩展 + 硬解，各家行为还不一致），
H.264 main 是唯一"哪里都能放"的选择。带宽受限时，H.265 省下的那点码率也换不来质变。

## 依赖

```bash
pip install mss opencv-python fastapi uvicorn pywin32 numpy
pip install imageio-ffmpeg     # 外网视频用，自带 ffmpeg 可执行文件（不用单独装 50MB 的 ffmpeg）
```

YOLO 模型文件（`best.pt`、`*_openvino_model/`）不入库，换机器要自己拷。
