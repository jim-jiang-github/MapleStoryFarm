# -*- coding: utf-8 -*-
"""esp_fulltest.py — 验证完整键盘协议回显"""
import serial, time, sys

port = sys.argv[1] if len(sys.argv) > 1 else "COM12"
s = serial.Serial(port, 115200, timeout=1)
time.sleep(0.5)
s.reset_input_buffer()
cmds = ["HELP", "KEY a", "PRESS left", "PRESS ctrl+shift+z",
        "RELEASE", "KEY enter", "RELEASE ctrl"]
for c in cmds:
    s.write((c + "\n").encode())
    time.sleep(0.3)
    echo = s.read(300).decode("utf-8", "ignore").strip()
    print(f"{c:24s} => {echo or '(无回显)'}")
s.close()
