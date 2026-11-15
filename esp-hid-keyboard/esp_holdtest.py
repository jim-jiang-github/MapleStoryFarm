# -*- coding: utf-8 -*-
"""esp_holdtest.py — 验证 定时器按压时长(hold) + 运行脚本不打断已在执行的定时器"""
import json, time, urllib.request

BASE = "http://127.0.0.1:8080"
def get(p): return json.loads(urllib.request.urlopen(BASE+p, timeout=5).read())
def post(p, body=None):
    data = json.dumps(body).encode() if body else b""
    req = urllib.request.Request(BASE+p, data=data, headers={"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())

def tlist(): return [(t['id'], t['interval'], t['key'], t.get('hold'), t['running']) for t in get("/api/timer/list")["timers"]]

# 1) 先手动启动一个定时器 A（模拟已在执行的按键事件）
print("[1] 手动add+start 定时器A(每3s按w, 按住1s):", post("/api/timer/add", {"interval":3,"key":"w","hold":1}))
print("[2] 启动A:", post("/api/timer/start", {"id":1}))
time.sleep(0.3)
print("[3] A 状态:", tlist())

# 2) 运行脚本(连带启动定时器B，带hold)——A 不应被打断
steps=[{"action":"tap","key":"left","sleep":{"mode":"fixed","value":0.2}}]
print("[4] 运行脚本+定时器B(每2s按a, 按住1.5s):", post("/api/script/run", {"steps":steps,"loops":2,"timers":[{"interval":2,"key":"a","hold":1.5}]}))
time.sleep(0.3)
print("[5] 运行后 所有定时器(应A、B都running):", tlist())
print("[6] 脚本状态:", get("/api/script/status"))

# 3) 停止 → 全停
print("[7] 停止:", post("/api/script/stop"))
time.sleep(0.3)
print("[8] 停止后 定时器(应全false):", tlist())
print("[9] 脚本状态:", get("/api/script/status"))
