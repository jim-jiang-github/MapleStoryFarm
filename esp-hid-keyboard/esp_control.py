# -*- coding: utf-8 -*-
"""
esp_control.py — 电脑端控制脚本：通过串口向 ESP32-S3 发指令，让它模拟方向键

用法:
  python esp_control.py L       # 按住 ← (左方向键)
  python esp_control.py R       # 按住 → (右方向键)
  python esp_control.py S       # 松开/抬起全部键
  python esp_control.py U       # 同 S，松开

可选:
  --port COM12   指定串口(默认自动找 ESP32 的 USB 串口)
  --hold 1.5     发指令后保持 N 秒再自动松开(方便脚本控制按住时长)

例:
  python esp_control.py L --hold 2     # 按住左 2 秒后松开
"""
import argparse, sys, time, serial, serial.tools.list_ports

def find_esp_port():
    for p in serial.tools.list_ports.comports():
        if p.vid == 0x303A:   # Espressif / ESP32
            return p.device
    # 兜底：名字含"USB 串行设备"
    for p in serial.tools.list_ports.comports():
        if "USB" in p.description and "串" in p.description:
            return p.device
    return None

def main():
    ap = argparse.ArgumentParser(description="控制 ESP32-S3 模拟方向键")
    ap.add_argument("cmd", choices=["L", "R", "S", "U", "l", "r", "s", "u"],
                    help="L=按住左, R=按住右, S/U=松开全部")
    ap.add_argument("--port", default=None, help="串口(默认自动找 ESP32)")
    ap.add_argument("--hold", type=float, default=0, help="按住保持秒数, >0 则到时自动松开")
    args = ap.parse_args()

    port = args.port or find_esp_port()
    if not port:
        sys.exit("[错误] 未找到 ESP32 串口，请用 --port 指定")
    print(f"使用串口: {port}")

    ser = serial.Serial(port, 115200, timeout=1)
    time.sleep(1)
    ser.reset_input_buffer()
    ser.write(args.cmd.upper().encode())
    print(f"已发送: {args.cmd.upper()}")

    if args.hold and args.cmd.upper() in ("L", "R"):
        print(f"保持 {args.hold}s...")
        time.sleep(args.hold)
        ser.write(b"S")
        print("已自动松开 (S)")

    ser.close()

if __name__ == "__main__":
    main()
