# -*- coding: utf-8 -*-
"""esp_runtest.py — 验证 运行脚本时一并启动定时器 + 停止时一并停"""
import json, time, urllib.request

BASE = "http://127.0.0.1:8080"
def get(p): return json.loads(urllib.request.urlopen(BASE+p, timeout=5).read())
def post(p, body=None):
    data = json.dumps(body).encode() if body else b""
    req = urllib.request.Request(BASE+p, data=data, headers={"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())

steps=[{"action":"tap","key":"left","sleep":{"mode":"fixed","value":0.2}}]
timers=[{"interval":2,"key":"a"},{"interval":5,"key":"space"}]
print("[1] 运行脚本+2定时器:", post("/api/script/run", {"steps":steps,"loops":2,"timers":timers}))
time.sleep(0.3)
print("[2] 定时器状态(应2个运行):", [(t['id'],t['running']) for t in get("/api/timer/list")["timers"]])
print("[3] 脚本状态:", get("/api/script/status"))
print("[4] 停止(应脚本+定时器全停):", post("/api/script/stop"))
time.sleep(0.3)
print("[5] 停止后定时器状态(应全false):", [(t['id'],t['running']) for t in get("/api/timer/list")["timers"]])
print("[6] 脚本状态:", get("/api/script/status"))
