# -*- coding: utf-8 -*-
"""本机在局域网里的地址（给「手机同 WiFi 看画面」用）。

为什么需要这个模块：`socket.gethostbyname_ex(hostname)` 会把**所有**网卡的
IPv4 都吐出来，包括 Hyper-V 的 vEthernet（WSL / Default Switch，典型 172.x
段）、VMware/VirtualBox 的 host-only、各类 VPN 的 tun/tap、蓝牙 PAN。这些
地址对手机来说完全不通——照着它打开只会一直转圈，还得多猜「两个地址哪个
才是对的」。

策略：
  ① 默认出口网卡（连 223.5.5.5 走的那张）排最前，这张一定是真的能上网的；
  ② 其余候选按网卡名把虚拟的滤掉（要 psutil，没装就只做 ① + 排除 APIPA）；
  ③ 顺手排除 169.254.*（没拿到 DHCP 时的自立地址，谁都连不上）。
"""
import socket

# 一眼能认出是虚拟网卡的接口名前缀/片段（psutil 给的是系统里的接口名）。
# 这些接口名字里全都带着它们各自的标记，用子串匹配就够，不会误伤物理网卡
# （"Ethernet" / "Wi-Fi" / "以太网" / "WLAN" 都不含下面任何一个）。
_VIRTUAL_HINTS = (
    "vethernet", "hyper-v", "wsl", "docker",
    "vmware", "virtualbox", "vbox", "host-only",
    "tap-", "tun", "tailscale", "zerotier", "wireguard", "openvpn",
    "bluetooth", "loopback",
)


def _virtual_if_ips():
    """虚拟网卡上挂着的 IPv4 集合；拿不到就返回空集（宁可多显示，别滤掉真网卡）。"""
    try:
        import psutil
    except Exception:  # noqa: BLE001  没装 psutil：只走后面那层保守过滤
        return set()
    out = set()
    try:
        for name, addrs in psutil.net_if_addrs().items():
            low = (name or "").lower()
            if not any(h in low for h in _VIRTUAL_HINTS):
                continue
            for a in addrs:
                if a.family == socket.AF_INET and a.address:
                    out.add(a.address)
    except Exception:  # noqa: BLE001  psutil 抽风不该挡住启动横幅
        return set()
    return out


def _usable(ip, virtual):
    return (ip
            and not ip.startswith("127.")
            and not ip.startswith("169.254.")     # 没拿到 DHCP 的自立地址
            and ip not in virtual)


def lan_ipv4():
    """局域网地址列表，第一个永远是默认出口那张网卡（手机优先用它对）。"""
    ips = []
    try:                                  # ① 默认出口网卡：一定真实可用
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("223.5.5.5", 80))
        ip = probe.getsockname()[0]
        probe.close()
        if _usable(ip, set()) and ip not in ips:
            ips.append(ip)
    except OSError:
        pass

    # ② 其余网卡：滤掉虚拟的、滤掉自立地址
    virtual = _virtual_if_ips()
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if _usable(ip, virtual) and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips
