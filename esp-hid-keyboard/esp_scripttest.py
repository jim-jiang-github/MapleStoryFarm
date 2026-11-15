# -*- coding: utf-8 -*-
"""esp_scripttest.py — 验证停止脚本后是否自动松开全部"""
import json, time, urllib.request

BASE = "http://127.0.0.1:8080"

def get(p):
    return json.loads(urllib.request.urlopen(BASE + p, timeout=5).read())

def post(p, body=None):
    data = json.dumps(body).encode() if body else b""
    req = urllib.request.Request(BASE + p, data=data,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())

print("[1] 连接 COM12:", get("/api/connect?port=COM12"))
steps = [{"action": "press", "key": "left", "sleep": {"mode": "fixed", "value": 30}}]
print("[2] 运行 按住left 无限循环:", post("/api/script/run", {"steps": steps, "loops": 0}))
time.sleep(1.0)
echo1 = get("/api/status")
print("[3] 运行中回显:", echo1.get("echo"))
print("[4] 发送停止:", post("/api/script/stop"))
time.sleep(0.8)
echo2 = get("/api/status")
print("[5] 停止后回显:", echo2.get("echo"))
rel = "ALL RELEASED" in str(echo2.get("echo"))
print("=== 验证: 停止后已松开全部 =", rel)
