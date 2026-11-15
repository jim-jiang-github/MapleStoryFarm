# -*- coding: utf-8 -*-
"""esp_firmware_test.py — 直接验证固件: 按住left后RELEASE应回显ALL RELEASED"""
import serial, time
s = serial.Serial("COM12", 115200, timeout=1)
time.sleep(0.5)
s.reset_input_buffer()
s.write(b"PRESS left\n")
time.sleep(0.3)
r1 = s.read(200).decode("utf-8", "ignore").strip()
s.write(b"RELEASE\n")
time.sleep(0.3)
r2 = s.read(200).decode("utf-8", "ignore").strip()
print("PRESS left  =>", r1)
print("RELEASE     =>", r2)
print("=== RELEASE生效 =", "ALL RELEASED" in r2)
s.close()
