# -*- coding: utf-8 -*-
"""esp_featuretest.py — 验证 多定时器 + 脚本保存/加载(含多定时器)"""
import json, time, urllib.request

BASE = "http://127.0.0.1:8080"
def get(p):
    return json.loads(urllib.request.urlopen(BASE + p, timeout=5).read())
def post(p, body=None):
    data = json.dumps(body).encode() if body else b""
    req = urllib.request.Request(BASE + p, data=data, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())

# 多定时器
print("[1] 添加定时器1(每2s按a):", post("/api/timer/add", {"interval":2,"key":"a"}))
print("[2] 添加定时器2(每5s按space):", post("/api/timer/add", {"interval":5,"key":"space"}))
print("[3] 定时器列表:", get("/api/timer/list"))
print("[4] 启动#1:", post("/api/timer/start", {"id":1}))
time.sleep(0.2)
lst = get("/api/timer/list")
print("[5] 启动后状态:", [(t['id'],t['running']) for t in lst["timers"]])
print("[6] 启动#2:", post("/api/timer/start", {"id":2}))
time.sleep(0.2)
print("[7] 双启动状态:", [(t['id'],t['running']) for t in get("/api/timer/list")["timers"]])
print("[8] 停止#1:", post("/api/timer/stop", {"id":1}))
time.sleep(0.2)
print("[9] 停止#1后:", [(t['id'],t['running']) for t in get("/api/timer/list")["timers"]])
print("[10] 删除#2:", post("/api/timer/remove", {"id":2}))
print("[11] 删除后:", [t['id'] for t in get("/api/timer/list")["timers"]])

# 保存/加载 含多定时器
steps=[{"action":"tap","key":"left","sleep":{"mode":"fixed","value":0.2}}]
timers=[{"interval":2,"key":"a"},{"interval":5,"key":"space"}]
print("[12] 保存(含2定时器):", post("/api/script/save", {"name":"multi","steps":steps,"loops":3,"timer":timers}))
r = get("/api/script/load?name=multi")
print("[13] 加载 timers:", r['script'].get('timer'))
print("[14] 删除测试脚本:", get("/api/script/delete?name=multi"))
