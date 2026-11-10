# -*- coding: utf-8 -*-
"""起一条免注册隧道，并把「带口令的完整网址」直接打出来 + 复制到剪贴板。

用法：python tools/tunnel.py 8765
环境变量：
  PREVIEW_TOKEN  访问口令（外网访问.bat 里设的）
  TUNNEL_TOOL    cloudflared / tunnelmole，强制指定；默认有 cloudflared 就用它

为什么优先 cloudflared：
  tunnelmole 的免费转发会把长连接整段缓冲（实测 MJPEG 10 秒 0 字节），
  而且单个请求的往返延迟能到 1~3 秒，H.264 直播/分片都吃力。
  cloudflared 走 Cloudflare 边缘，HTTP/2 流式转发，延迟通常 200~500ms，
  画面能真的"动起来"。
"""
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

PORT = sys.argv[1] if len(sys.argv) > 1 else "8765"
TOKEN = (os.environ.get("PREVIEW_TOKEN") or "").strip()
ROOT = Path(__file__).resolve().parent.parent

CF_URL_RE = re.compile(r"https://[A-Za-z0-9-]+\.trycloudflare\.com")
TM_URL_RE = re.compile(r"https://[A-Za-z0-9._-]+\.tunnelmole\.(?:net|io)")


def find_cloudflared():
    env = (os.environ.get("CLOUDFLARED") or "").strip()
    if env and Path(env).exists():
        return env
    for p in (ROOT / "tools" / "bin" / "cloudflared.exe",
              ROOT / ".image" / "cloudflared.exe"):
        if p.exists() and p.stat().st_size > 10 * 1024 * 1024:   # 残缺文件不算
            return str(p)
    return shutil.which("cloudflared")


def copy_to_clipboard(text):
    try:
        import win32clipboard
        import win32con
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:  # noqa: BLE001
        return False


def lan_ips():
    """本机局域网地址：手机连着同一个 WiFi 时走它，延迟从 1 秒降到几毫秒。

    过滤逻辑放在 webui/netinfo.py 里（服务启动横幅也用它），这里只是把项目
    根加入 sys.path 后转调——两处各写一份的话，迟早会有一处漏掉虚拟网卡。
    """
    try:
        sys.path.insert(0, str(ROOT / "webui"))
        from netinfo import lan_ipv4
        return lan_ipv4()
    except Exception:  # noqa: BLE001  极端情况（打包/路径异常）退回朴素做法
        ips = []
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not ip.startswith("127.") and not ip.startswith("169.254.") and ip not in ips:
                    ips.append(ip)
        except OSError:
            pass
        return ips


def banner(url, tool):
    line = "=" * 68
    tail = f"?k={TOKEN}" if TOKEN else ""
    m = f"{url}/m{tail}"
    print()
    print(line)
    print(f"  手机在外面看（这个就够了，建议加到主屏幕）  [{tool}]")
    print()
    print(f"  {m}")
    print()
    if TOKEN:
        print(f"  口令: {TOKEN}    （不带 ?k= 打不开，7 天内免重复输入）")
    else:
        print("  注意: 没设 PREVIEW_TOKEN，任何人都能打开这个地址")
    print()
    print("  在家 / 局域网（电脑上改屏蔽区、标定血条用这个，画面是实时流）:")
    for ip in lan_ips():
        print(f"  http://{ip}:{PORT}/{tail}")
    if copy_to_clipboard(m):
        print()
        print("  已复制到剪贴板 ✓")
    print(line)
    print()


def run(cmd, pattern, tool):
    shell = isinstance(cmd, str)
    proc = subprocess.Popen(cmd, shell=shell, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            encoding="utf-8", errors="replace")
    printed = False
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if not printed:
                m = pattern.search(line)
                if m:
                    printed = True
                    banner(m.group(0), tool)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        print("\n[tunnel] 已停止。最小化的 capture-preview 服务窗口还在跑，不用了就关掉它。")
    return printed


def main():
    want = (os.environ.get("TUNNEL_TOOL") or "").strip().lower()
    cf = find_cloudflared() if want in ("", "cloudflared", "cf") else None

    if cf:
        print(f"[tunnel] cloudflared -> http://localhost:{PORT}")
        if run([cf, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
               CF_URL_RE, "cloudflared"):
            return
        print("[tunnel] cloudflared 没拿到网址，退回 tunnelmole …\n")

    print(f"[tunnel] tunnelmole -> {PORT}")
    run(f"npx -y tunnelmole {PORT}", TM_URL_RE, "tunnelmole")
    print("\n[tunnel] 提示：tunnelmole 会缓冲长连接、单次往返 1~3 秒，"
          "画面会有点跟不上。\n         想更流畅就装上 cloudflared（见 README）。")


if __name__ == "__main__":
    main()
