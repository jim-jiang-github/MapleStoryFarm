# -*- coding: utf-8 -*-
"""
esp_server.py — ESP32-S3 完整键盘控制台（浏览器界面）
本机启动本地 HTTP 服务，用浏览器打开 http://127.0.0.1:8080 控制：
  选择串口 -> 连接 -> 点击任意键/按住/组合键 -> 停止松开

协议(固件): 每行一条命令
  KEY <combo>     点按  PRESS <combo>  按住  RELEASE  松开全部  RELEASE <key> 松开指定
  combo 可用 + 组合，如 ctrl+shift+z

用法: python esp_server.py [--port 8080]
依赖: pyserial (已随 esptool 安装)
"""
import argparse, json, os, random, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import serial, serial.tools.list_ports

BAUD = 115200
SER = None
SER_LOCK = threading.Lock()
ECHO = []
ECHO_MAX = 300


def find_esp_ports():
    found = []
    try:
        for p in serial.tools.list_ports.comports():
            if p.vid == 0x303A:
                found.append(p.device)
        if not found:
            for p in serial.tools.list_ports.comports():
                if "USB" in p.description and "串" in p.description:
                    found.append(p.device)
    except Exception:
        pass
    return list(dict.fromkeys(found))


LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "esp_console.log")


def _log_file(line):
    """把日志同时落盘，便于事后核对时序（不会因浏览器轮询清空而丢失）"""
    try:
        if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > 2 * 1024 * 1024:
            os.remove(LOG_FILE)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def echo_push(text):
    global ECHO
    ts = time.strftime("%H:%M:%S") + (".%03d" % int((time.time() % 1) * 1000))
    line = ts + "  " + text
    with SER_LOCK:
        ECHO.append(line)
        if len(ECHO) > ECHO_MAX:
            ECHO = ECHO[-ECHO_MAX:]
    _log_file(line)


def reader_loop():
    while True:
        time.sleep(0.08)
        with SER_LOCK:
            s = SER
        if s and s.is_open:
            try:
                n = s.in_waiting
                if n:
                    data = s.read(n).decode("utf-8", "ignore").strip()
                    if data:
                        echo_push(data)
            except Exception:
                pass


def connect(port):
    global SER
    with SER_LOCK:
        if SER and SER.is_open:
            SER.close()
        SER = serial.Serial(port, BAUD, timeout=0.2)
        time.sleep(1)
        SER.reset_input_buffer()
        return True


SER_WLOCK = threading.Lock()   # 串口写入锁：一次写完一行，避免多线程字节交错


def send_line(cmd):
    """发一行协议命令（持写锁完成整行写入）"""
    with SER_LOCK:
        s = SER
    if not s or not s.is_open:
        return False
    with SER_WLOCK:
        try:
            s.write((cmd + "\n").encode())
            s.flush()
            return True
        except Exception:
            return False


# ---------- 按键归属管理：主脚本与定时器互不夺键 ----------
HELD = {}                 # key -> owner（谁按住了这个键）
HELD_LOCK = threading.Lock()

OWNER_SCRIPT = "脚本"


def tap_key(key, owner):
    """点按（固件自动松开）。若该键正被其他 owner 按住则跳过本轮。"""
    key = (key or "").strip()
    if not key:
        return False
    with HELD_LOCK:
        holder = HELD.get(key)
        if holder and holder != owner and holder != "手动":
            echo_push("[跳过] %s 正被 %s 按住，%s 本次点按跳过" % (key, holder, owner))
            return False
    return send_line("KEY " + key)


def press_key(key, owner):
    """按住。若该键正被其他 owner 按住则跳过。"""
    key = (key or "").strip()
    if not key:
        return False
    with HELD_LOCK:
        holder = HELD.get(key)
        if holder and holder != owner:
            echo_push("[跳过] %s 正被 %s 按住，%s 本次按住跳过" % (key, holder, owner))
            return False
        HELD[key] = owner
    return send_line("PRESS " + key)


def release_key(key, owner=None):
    """松开。owner 为空表示不限归属；指定 owner 时只松自己按住的键。"""
    key = (key or "").strip()
    if not key:
        return False
    with HELD_LOCK:
        holder = HELD.get(key)
        if holder is None:
            return False
        if owner and holder != owner:
            return False
        HELD.pop(key, None)
    return send_line("RELEASE " + key)


def release_own(owner):
    """只松开某个 owner 名下的所有键（不会影响别人按住的键）"""
    with HELD_LOCK:
        keys = [k for k, o in HELD.items() if o == owner]
        for k in keys:
            HELD.pop(k, None)
    for k in keys:
        send_line("RELEASE " + k)
    return keys


def release_all_manual():
    """手动“全部松开”：清空归属并让固件释放所有键"""
    with HELD_LOCK:
        HELD.clear()
    return send_line("RELEASE")


# ---------- 定时器优先：定时器动作期间，脚本按键让路 ----------
TIMER_BUSY = threading.Event()
TIMER_BUSY_LOCK = threading.Lock()
_timer_busy_n = 0


def _timer_enter():
    global _timer_busy_n
    with TIMER_BUSY_LOCK:
        _timer_busy_n += 1
        TIMER_BUSY.set()


def _timer_leave():
    global _timer_busy_n
    with TIMER_BUSY_LOCK:
        _timer_busy_n = max(0, _timer_busy_n - 1)
        if _timer_busy_n == 0:
            TIMER_BUSY.clear()


def wait_timer_idle(stop=None, timeout=0):
    """脚本侧调用：等定时器这次动作做完再继续。stop 置位则立刻放弃。"""
    waited = False
    t0 = time.time()
    while TIMER_BUSY.is_set():
        if stop is not None and stop.is_set():
            return False
        if timeout and time.time() - t0 > timeout:
            return False
        if not waited:
            echo_push("[脚本] 定时器动作中，本步暂停等待")
            waited = True
        time.sleep(0.01)
    if waited:
        echo_push("[脚本] 定时器动作结束，继续")
    return True


# ---------- 脚本播放引擎 ----------
SCRIPT = {"thread": None, "stop": threading.Event(), "timers": []}


def _sleep_sec(step):
    sl = step.get("sleep") or {}
    mode = sl.get("mode", "fixed")
    if mode == "random":
        lo = float(sl.get("min", 0))
        hi = float(sl.get("max", 0))
        if hi > lo:
            return random.uniform(lo, hi)
        return lo
    return max(0.0, float(sl.get("value", 0)))


def _exec_step(step, stop=None):
    a = step.get("action", "tap")
    key = (step.get("key") or "").strip()
    # 定时器优先：定时器正在动作时，脚本这一步等它做完
    if not wait_timer_idle(stop):
        return
    if a == "tap":
        ok = tap_key(key, OWNER_SCRIPT)
        echo_push("[脚本] KEY " + key + ("" if ok else " (跳过)"))
    elif a == "press":
        ok = press_key(key, OWNER_SCRIPT)
        echo_push("[脚本] PRESS " + key + ("" if ok else " (跳过)"))
    elif a == "release":
        if key:
            release_key(key, OWNER_SCRIPT)
            echo_push("[脚本] RELEASE " + key)
        else:
            ks = release_own(OWNER_SCRIPT)
            echo_push("[脚本] RELEASE 脚本按住的: " + (", ".join(ks) if ks else "无"))
    else:
        ks = release_own(OWNER_SCRIPT)
        echo_push("[脚本] RELEASE 脚本按住的: " + (", ".join(ks) if ks else "无"))


def _norm_groups(groups=None):
    """规整分组结构: [{repeat, steps}]，repeat=0 表示无限"""
    out = []
    for g in (groups or []):
        if not isinstance(g, dict):
            continue
        steps = [s for s in (g.get("steps") or []) if isinstance(s, dict)]
        if not steps:
            continue
        try:
            rep = int(g.get("repeat", 1) or 0)
        except Exception:
            rep = 1
        out.append({"repeat": max(0, rep), "steps": steps})
    return out


def _legacy_groups(steps, loops):
    """旧结构(扁平 steps + loops 次) 折成单个分组，语义等价"""
    steps = [s for s in (steps or []) if isinstance(s, dict)]
    if not steps:
        return []
    try:
        rep = int(loops or 0)
    except Exception:
        rep = 0
    return [{"repeat": max(0, rep), "steps": steps}]


def _play(groups, rounds, stop):
    """按分组播放：组内顺序执行并重复 repeat 次，组间依次衔接；
    rounds 为整体轮数(0/None=无限)。"""
    def _halt():
        release_own(OWNER_SCRIPT)
        echo_push("[脚本] 已停止(已松开脚本按键)")
    try:
        total = 0
        r = 0
        while True:
            if rounds is not None and rounds > 0 and r >= rounds:
                break
            for gi, g in enumerate(groups, 1):
                rep = g["repeat"]
                echo_push("[脚本] 第%d组开始: 循环 %s, %d 步" %
                          (gi, "无限" if rep == 0 else ("%d 次" % rep), len(g["steps"])))
                k = 0
                while True:
                    if rep > 0 and k >= rep:
                        break
                    for st in g["steps"]:
                        if stop.is_set():
                            _halt()
                            return
                        _exec_step(st, stop)
                        if stop.is_set():
                            _halt()
                            return
                        _wait(stop, _sleep_sec(st))
                        total += 1
                    k += 1
                    if rep > 0 and k >= rep:
                        echo_push("[脚本] 第%d组完成 %d 轮" % (gi, k))
                    if stop.is_set():
                        _halt()
                        return
            r += 1
        release_own(OWNER_SCRIPT)
        echo_push("[脚本完成] 共 %d 步" % total)
    except Exception as e:
        try:
            release_own(OWNER_SCRIPT)
        except Exception:
            pass
        echo_push("[脚本错误] " + str(e))


def _wait(stop, sec):
    stop.wait(timeout=sec)  # 可被 stop 中断


def _describe_steps(steps):
    out = []
    for st in steps:
        a = st.get("action", "tap")
        k = st.get("key", "") or ("(全部)" if a in ("release", "releaseall") else "")
        sl = st.get("sleep") or {}
        if sl.get("mode") == "random":
            out.append("%s %s 随机%.2f~%.2fs" % (a, k, float(sl.get("min", 0) or 0), float(sl.get("max", 0) or 0)))
        else:
            out.append("%s %s 睡%.2fs" % (a, k, float(sl.get("value", 0) or 0)))
    return " | ".join(out)


def _describe_groups(groups):
    parts = []
    for i, g in enumerate(groups, 1):
        rep = "无限" if g["repeat"] == 0 else ("%d次" % g["repeat"])
        parts.append("组%d×%s[%s]" % (i, rep, _describe_steps(g["steps"])))
    return "  ‖  ".join(parts)


def script_run(groups, rounds, timers=None):
    if SCRIPT["thread"] and SCRIPT["thread"].is_alive():
        script_stop()
        SCRIPT["thread"].join(timeout=1)
    total_steps = sum(len(g["steps"]) for g in groups)
    echo_push("[脚本] 实际参数: 整体循环=%s 组数=%d 步数=%d" %
              (rounds if rounds else "无限", len(groups), total_steps))
    echo_push("[脚本] 分组: " + _describe_groups(groups))
    SCRIPT["stop"] = threading.Event()
    t = threading.Thread(target=_play, args=(groups, rounds, SCRIPT["stop"]))
    t.daemon = True
    SCRIPT["thread"] = t
    t.start()
    started = []
    if timers:   # 一并启动并行定时器；已在跑但参数变了的，按新参数重启
        for tm in timers:
            if not (tm and tm.get("key")):
                continue
            iv = max(0.05, float(tm.get("interval", 5) or 5))
            hold = max(0.0, float(tm.get("hold", 0) or 0))
            old = tm.get("tid")
            if old and old in TIMERS:
                cur = TIMERS[old]
                if (abs(cur["interval"] - iv) < 1e-6 and cur["key"] == tm["key"]
                        and abs(cur.get("hold", 0) - hold) < 1e-6
                        and cur["thread"] and cur["thread"].is_alive()):
                    started.append(old)      # 参数没变，沿用原来的
                    echo_push("[定时#%d] 参数未变，沿用中" % old)
                    continue
                timer_stop(old)
                timer_remove(old)
                echo_push("[定时#%d] 参数已变化，按新参数重启" % old)
            tid = timer_add(iv, tm["key"], hold)
            timer_start(tid)
            started.append(tid)
    SCRIPT["timers"] = started   # 记住这次随脚本启动的定时器，停止时一并停
    return started


def script_stop():
    SCRIPT["stop"].set()
    # 随脚本一起启动的定时器，停止脚本时一并停掉（单独添加的定时器不受影响）
    for tid in SCRIPT.get("timers") or []:
        try:
            timer_stop(tid)
            timer_remove(tid)
        except Exception:
            pass
    SCRIPT["timers"] = []
    try:
        release_own(OWNER_SCRIPT)   # 只松开脚本自己按住的键，不影响定时器
    except Exception:
        pass


def script_running():
    return bool(SCRIPT["thread"] and SCRIPT["thread"].is_alive())


# ---------- 脚本持久化 ----------
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")


def _script_path(name):
    return os.path.join(SCRIPTS_DIR, name + ".json")


def script_save(name, groups, loops, timer=None):
    if not os.path.isdir(SCRIPTS_DIR):
        os.makedirs(SCRIPTS_DIR)
    flat = [st for g in groups for st in g["steps"]]
    with open(_script_path(name), "w", encoding="utf-8") as f:
        json.dump({"name": name, "groups": groups, "loops": loops,
                   "steps": flat, "timer": timer},
                  f, ensure_ascii=False, indent=2)
    return True


def script_list():
    if not os.path.isdir(SCRIPTS_DIR):
        return []
    return sorted(fn[:-5] for fn in os.listdir(SCRIPTS_DIR) if fn.endswith(".json"))


def script_load(name):
    p = _script_path(name)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def script_delete(name):
    p = _script_path(name)
    if os.path.isfile(p):
        os.remove(p)
        return True
    return False


# ---------- 倒计时按键(多个并行定时器) ----------
TIMERS = {}     # tid -> {interval,key,thread,stop}
_timer_seq = 0


def _timer_loop(interval, key, hold, stop, tid):
    owner = "定时#%d" % tid
    last_fire = 0.0
    try:
        while True:
            if stop.wait(interval):   # 等待 interval 秒，可被停止中断
                break
            now = time.time()
            gap = (now - last_fire) if last_fire else 0.0
            last_fire = now
            echo_push("[%s] 触发%s" % (owner, ("，距上次 %.2fs(设定 %.2fs)" % (gap, interval)) if gap else "（首次）"))
            _timer_enter()            # 定时器优先：通知脚本让路
            try:
                time.sleep(0.06)      # 给脚本一点换拍窗口，避免半路打断
                if hold and hold > 0:
                    # 按住 hold 秒再松开；被别人按住时本轮跳过，不夺键
                    if not press_key(key, owner):
                        continue
                    echo_push("[%s] PRESS %s" % (owner, key))
                    if stop.wait(hold):
                        release_key(key, owner)
                        break
                    release_key(key, owner)
                    echo_push("[%s] RELEASE %s" % (owner, key))
                else:
                    if tap_key(key, owner):
                        echo_push("[%s] KEY %s" % (owner, key))
                time.sleep(0.10)      # 等固件把 60ms 的点按松开做完
            finally:
                _timer_leave()        # 通知脚本可以继续了
        release_own(owner)   # 只松开自己按住的键
        echo_push("[%s] 已停止" % owner)
    except Exception as e:
        release_own(owner)
        echo_push("[%s错误] %s" % (owner, str(e)))


def timer_add(interval, key, hold=0):
    global _timer_seq
    _timer_seq += 1
    tid = _timer_seq
    TIMERS[tid] = {"interval": max(0.05, float(interval)),
                   "key": key, "hold": max(0.0, float(hold)),
                   "thread": None, "stop": threading.Event()}
    return tid


def timer_start(tid):
    t = TIMERS.get(tid)
    if not t:
        return False
    if t["thread"] and t["thread"].is_alive():
        return True
    t["stop"] = threading.Event()
    echo_push("[定时#%d] 启动: 每 %.2fs 按 %s (保持 %.2fs)" % (tid, t["interval"], t["key"], t.get("hold", 0) or 0))
    th = threading.Thread(target=_timer_loop,
                          args=(t["interval"], t["key"], t["hold"], t["stop"], tid))
    th.daemon = True
    t["thread"] = th
    th.start()
    return True


def timer_stop(tid):
    t = TIMERS.get(tid)
    if t:
        t["stop"].set()


def timer_remove(tid):
    t = TIMERS.pop(tid, None)
    if t:
        t["stop"].set()   # 停止其线程(线程会 RELEASE 并退出)
    return bool(t)


def timer_list():
    return [{"id": tid, "interval": t["interval"], "key": t["key"],
             "hold": t.get("hold", 0),
             "running": bool(t["thread"] and t["thread"].is_alive())}
            for tid, t in TIMERS.items()]


HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ESP32-S3 完整键盘控制台</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--line:#334155;--txt:#e2e8f0;--dim:#94a3b8;--ok:#16a34a;--err:#dc2626;--mod:#7c3aed}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--txt);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;display:flex;justify-content:center;padding:20px}
  .wrap{width:100%;max-width:860px}
  h1{font-size:22px;margin-bottom:2px}
  .sub{color:var(--dim);font-size:13px;margin-bottom:14px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
  .card h2{font-size:13px;color:var(--dim);margin-bottom:8px;font-weight:600}
  .row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
  select,input{background:#0f172a;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:7px;font-size:14px}
  select{flex:1;min-width:120px}
  button{background:var(--line);color:var(--txt);border:none;border-radius:8px;padding:8px 12px;font-size:14px;cursor:pointer;transition:.15s}
  button:hover{filter:brightness(1.25)}
  button:active{transform:scale(.95)}
  .btn-ok{background:var(--ok)} .btn-warn{background:#d97706} .btn-err{background:var(--err)}
  .status{font-size:13px;padding:4px 12px;border-radius:20px}
  .st-off{background:#334155;color:var(--dim)} .st-on{background:#14532d;color:#4ade80}
  .kb{display:flex;flex-direction:column;gap:6px}
  .kb .line{display:flex;gap:6px;justify-content:center}
  .k{min-width:40px;height:42px;padding:0 8px;border-radius:6px;background:#0f172a;border:1px solid var(--line);color:var(--txt);font-size:14px;font-weight:600;display:flex;align-items:center;justify-content:center;cursor:pointer;user-select:none}
  .k:hover{border-color:#60a5fa;background:#1e3a8a}
  .k:active{transform:scale(.93)}
  .k.wide{flex:1;min-width:80px}
  .k.space{flex:6;min-width:200px}
  .k.mod{background:#312e81;border-color:#7c3aed;color:#c4b5fd}
  .k.mod.on{background:var(--mod);color:#fff;border-color:#a78bfa;box-shadow:0 0 8px rgba(124,58,237,.6)}
  .k.stop{background:var(--err);flex:1;min-width:120px;height:44px;font-size:15px}
  #log{background:#0f172a;border:1px solid var(--line);border-radius:8px;height:120px;overflow-y:auto;padding:8px;font-family:Consolas,monospace;font-size:12px;color:#a5f3fc;white-space:pre-wrap}
  .combo{flex:1;min-width:200px}
  .hint{color:var(--dim);font-size:12px;margin-top:4px}
  .hidden{display:none!important}
  .srow{align-items:center}
  .srow input[type=text],.srow input:not([type]){background:#0f172a;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:6px;font-size:13px}
  .srow select{background:#0f172a;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:6px;font-size:13px}
  .sdel{background:#7f1d1d;color:#fecaca;font-weight:700;padding:4px 10px}
  .mode{display:flex;gap:6px}
  .mode button.on{background:var(--ok);color:#fff}
  .grp{border:1px dashed #475569;border-radius:8px;padding:8px;margin-top:8px;background:rgba(15,23,42,.35)}
  .ghead{align-items:center;gap:6px;margin-bottom:0}
  .gtitle{font-size:12px;font-weight:700;color:#c4b5fd;min-width:52px}
  .grep{background:#0f172a;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:5px;font-size:13px}
  .ghead label,.ghead span{font-size:12px;color:var(--dim)}
  .gdel{background:#7f1d1d;color:#fecaca;font-weight:700;padding:4px 10px}
</style>
</head>
<body>
<div class="wrap">
  <h1>⌨️ ESP32-S3 完整键盘控制台</h1>
  <div class="sub">USB 复合设备 · 模拟任意按键 · 支持点按/按住/组合键</div>

  <div class="card">
    <h2>串口连接</h2>
    <div class="row">
      <select id="portSel"></select>
      <button onclick="loadPorts()">刷新</button>
      <button id="btnConn" class="btn-ok" onclick="toggleConn()">连接</button>
      <span id="status" class="status st-off">未连接</span>
    </div>
  </div>

  <div class="card">
    <h2>修饰键（点击切换，组合时自动并入）</h2>
    <div class="row">
      <button class="k mod" data-mod="ctrl" onclick="toggleMod(this,'ctrl')">Ctrl</button>
      <button class="k mod" data-mod="shift" onclick="toggleMod(this,'shift')">Shift</button>
      <button class="k mod" data-mod="alt" onclick="toggleMod(this,'alt')">Alt</button>
      <button class="k mod" data-mod="win" onclick="toggleMod(this,'win')">Win</button>
      <span class="hint" id="modHint">（组合示例：切出 Ctrl 后点 A = Ctrl+A）</span>
    </div>
  </div>

  <div class="card">
    <h2>功能键</h2>
    <div class="row" id="frow"></div>
    <h2>数字行</h2>
    <div class="row" id="nrow"></div>
  </div>

  <div class="card">
    <h2>字母键盘</h2>
    <div class="kb" id="kb"></div>
  </div>

  <div class="card">
    <h2>方向 / 编辑键</h2>
    <div class="row" id="edrow"></div>
  </div>

  <div class="card">
    <h2>操作</h2>
    <div class="row">
      <button class="k stop" onclick="release()">松开全部</button>
      <button class="k stop" onclick="releaseMods()">松开修饰键</button>
      <div class="mode" style="flex:1">
        <button id="modeTap" class="on" onclick="setMode('tap')">点按</button>
        <button id="modeHold" onclick="setMode('hold')">按住</button>
      </div>
    </div>
    <div class="row" style="margin-top:8px">
      <input class="combo" id="comboInput" placeholder="组合键，如 ctrl+shift+z / enter / f5">
      <button onclick="comboSend()">发送</button>
      <button class="btn-warn" onclick="press()">按住发送</button>
    </div>
  </div>

  <div class="card">
    <h2>脚本编辑器（按顺序播放键盘事件，支持循环与随机延迟）</h2>
    <div class="row" style="margin-bottom:8px">
      <input type="text" id="scriptName" placeholder="脚本名" style="width:110px">
      <button class="btn-warn" onclick="saveScript()">💾 保存</button>
      <select id="scriptSel" style="flex:1;min-width:140px"></select>
      <button onclick="loadScript()">加载</button>
      <button class="btn-err" onclick="delScript()">删除</button>
    </div>
    <div class="row">
      <label style="font-size:13px;color:var(--dim)">整体循环:</label>
      <input type="number" id="loopNum" value="1" min="0" style="width:70px">
      <span style="font-size:12px;color:var(--dim)">轮 (0=无限；每组另有自己的循环次数)</span>
      <button class="btn-ok" onclick="runScript()">▶ 运行脚本</button>
      <button class="btn-err" onclick="stopScript()">■ 停止</button>
      <span id="scrState" style="font-size:13px;color:var(--dim)"></span>
    </div>
    <div class="row" style="margin-top:6px">
      <button class="btn-warn" onclick="addGroup()">＋ 添加循环组</button>
      <span class="hint">每组的「＋ 事件」只往该组加一行；组内从上到下播完并按本组次数重复，再进下一组 · 点按=按下立即松开 · 按住=保持到停止 · 睡眠=每步后等待</span>
    </div>
    <div id="scriptRows"></div>
  </div>

  <div class="card">
    <h2>倒计时按键（多个并行定时器，与主脚本同时运行）</h2>
    <div class="row" style="margin-bottom:6px">
      <button class="btn-warn" onclick="addTimerRow()">+ 添加定时器</button>
      <span class="hint">每行一个定时器：每隔固定秒数自动点按一次指定键；各自启停，可与主脚本并行</span>
    </div>
    <div id="timerRows"></div>
  </div>

  <div class="card">
    <h2>日志</h2>
    <div id="log">等待连接...</div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
let port=null, connected=false, mode='tap';
let mods = {};  // {ctrl:true,...}
let logLines=[];
const TS_RE=/^\\d{2}:\\d{2}:\\d{2}\\.\\d{3}/;
function nowStamp(){ const d=new Date(); const p=(n,w)=>String(n).padStart(w,'0');
  return p(d.getHours(),2)+':'+p(d.getMinutes(),2)+':'+p(d.getSeconds(),2)+'.'+p(d.getMilliseconds(),3); }
function log(t){ let s=String(t); if(!TS_RE.test(s)) s=nowStamp()+'  '+s;
  const el=$('log'); logLines.push(s); if(logLines.length>200)logLines.shift();
  el.textContent=logLines.join('\\n'); el.scrollTop=el.scrollHeight; }
async function api(p){ try{ const r=await fetch('/api/'+p); return await r.json(); }catch(e){ return {ok:false,err:String(e)}; } }

async function loadPorts(){ const r=await api('ports'); const sel=$('portSel'); sel.innerHTML='';
  if(r.ports&&r.ports.length){ r.ports.forEach(p=>{const o=document.createElement('option');o.value=p;o.textContent=p;sel.appendChild(o);}); log('串口: '+r.ports.join(', ')); }
  else { sel.innerHTML='<option value="">(无串口)</option>'; log('未检测到 ESP32'); } }
async function toggleConn(){
  if(connected){ await api('disconnect'); setConn(false); log('已断开'); return; }
  const p=$('portSel').value; if(!p){log('请选择串口');return;}
  const r=await api('connect?port='+encodeURIComponent(p));
  if(r.ok){setConn(true,p);log('已连接 '+p);} else log('连接失败: '+(r.err||r.msg)); }
function setConn(on,p){ connected=on; $('btnConn').textContent=on?'断开':'连接';
  $('btnConn').className=on?'btn-warn':'btn-ok';
  const st=$('status'); st.textContent=on?('已连接 '+p):'未连接'; st.className='status '+(on?'st-on':'st-off'); }

// 键表: [标签, 协议键名]
const FKEY = []; for(let i=1;i<=12;i++) FKEY.push(['F'+i, 'f'+i]);
const NUM = ['0','1','2','3','4','5','6','7','8','9'];
const KBROWS = [
  ['q','w','e','r','t','y','u','i','o','p'],
  ['a','s','d','f','g','h','j','k','l'],
  ['z','x','c','v','b','n','m'],
];
const EDKEY = [['Enter','enter'],['Esc','esc'],['Tab','tab'],['Space','space'],['←','left'],['→','right'],['↑','up'],['↓','down'],
               ['Backspace','backspace'],['Delete','delete'],['Home','home'],['End','end'],['PgUp','pgup'],['PgDn','pgdn'],['Caps','caps']];

function makeBtn(label, name, cls){
  const b=document.createElement('button'); b.className='k'+(cls?' '+cls:''); b.textContent=label;
  b.onclick=()=>tapKey(name); b.title=name; return b; }
function buildKB(){
  const fr=$('frow'); FKEY.forEach(([l,n])=>fr.appendChild(makeBtn(l,n,'wide')));
  const nr=$('nrow'); NUM.forEach(n=>nr.appendChild(makeBtn(n,n)));
  const kb=$('kb');
  KBROWS.forEach(row=>{ const line=document.createElement('div'); line.className='line';
    row.forEach(n=>line.appendChild(makeBtn(n.toUpperCase(),n))); kb.appendChild(line); });
  const ed=$('edrow'); EDKEY.forEach(([l,n])=>{ ed.appendChild(makeBtn(l,n,'wide')); });
}
function currentCombo(extra){
  const parts=[]; for(const m of ['ctrl','shift','alt','win']) if(mods[m]) parts.push(m);
  if(extra) parts.push(extra); return parts.join('+'); }
function tapKey(name){ if(!connected){log('未连接');return;}
  const combo=currentCombo(name); if(!combo){log('空按键');return;}
  api(mode==='tap'?('tap?combo='+encodeURIComponent(combo)):('press?combo='+encodeURIComponent(combo))).then(r=>{
    log((r.ok?((mode==='tap'?'[点按] ':'[按住] ')+combo):'[失败] '+r.err)); }); }
function toggleMod(btn,m){ mods[m]=!mods[m]; btn.classList.toggle('on',mods[m]);
  const on=Object.keys(mods).filter(x=>mods[x]).join('+'); $('modHint').textContent=on?('当前修饰: '+on+'  → 点键即组合'):'（组合示例：切出 Ctrl 后点 A = Ctrl+A）'; }
function releaseMods(){ Object.keys(mods).forEach(m=>mods[m]=false);
  document.querySelectorAll('.k.mod').forEach(b=>b.classList.remove('on')); $('modHint').textContent='修饰已全部松开';
  if(connected) api('releaseone?key='+Object.keys(mods).filter(x=>mods[x]).join('+')); }
function release(){ if(!connected){log('未连接');return;} api('release').then(r=>{ log(r.ok?'[松开全部]':'[失败] '+r.err); }); }
function setMode(m){ mode=m; $('modeTap').classList.toggle('on',m==='tap'); $('modeHold').classList.toggle('on',m==='hold'); }
async function comboSend(){ const v=$('comboInput').value.trim(); if(!v){return;}
  const r=await api('tap?combo='+encodeURIComponent(v)); log((r.ok?'[点按] '+v:'[失败] '+r.err)); $('comboInput').value=''; }
async function press(){ const v=$('comboInput').value.trim(); if(!v){return;}
  const r=await api('press?combo='+encodeURIComponent(v)); log((r.ok?'[按住] '+v:'[失败] '+r.err)); }

async function poll(){ const r=await api('status');
  if(r.echo&&r.echo.length){ for(const e of r.echo){ log('[回显] '+String(e).replace(TS_RE,'').trim()); } }
  setTimeout(poll,800); }

// ---------- 脚本编辑器 ----------
function scrRowHTML(){
  return '<div class="row srow" style="margin-top:6px">' +
    '<select class="saction" style="width:84px">' +
      '<option value="tap">点按</option><option value="press">按住</option>' +
      '<option value="release">松开</option><option value="releaseall">松开全部</option></select>' +
    '<input type="text" class="skey" placeholder="键/组合,如 a / ctrl+c / f5 / enter" style="flex:1.2;min-width:130px">' +
    '<select class="ssleepmode" style="width:66px"><option value="fixed">固定</option><option value="random">随机</option></select>' +
    '<input type="text" class="ssleep" placeholder="秒" style="width:52px" value="0.3">' +
    '<span class="srnd hidden" style="color:var(--dim)">~</span>' +
    '<input type="text" class="ssleep2 hidden" placeholder="秒" style="width:52px" value="1">' +
    '<button class="sdel" onclick="delScriptRow(this)">×</button></div>';
}
// ---- 循环组 ----
function allGroups(){ return [...document.querySelectorAll('#scriptRows .grp')]; }
function lastGroup(){ const gs=allGroups(); return gs.length?gs[gs.length-1]:null; }
function renumber(){ allGroups().forEach((g,i)=>{ const t=g.querySelector('.gtitle'); if(t) t.textContent='第 '+(i+1)+' 组'; }); }
function groupHTML(rep){
  const v=(rep===undefined||rep===null)?10:rep;
  return '<div class="grp"><div class="row ghead">' +
    '<span class="gtitle">第 1 组</span>' +
    '<label>本组循环</label>' +
    '<input type="number" class="grep" min="0" value="'+v+'" style="width:62px">' +
    '<label>次 (0=无限)</label>' +
    '<button class="btn-warn gadd" onclick="addScriptRow(this)">＋ 事件</button>' +
    '<button class="gdel" onclick="delGroup(this)">删组</button></div>' +
    '<div class="grows"></div></div>';
}
function addGroup(rep, withRow){
  const box=$('scriptRows'); box.insertAdjacentHTML('beforeend', groupHTML(rep));
  const g=box.lastElementChild;
  if(withRow!==false) addScriptRow(g.querySelector('.grows'));
  renumber(); return g;
}
function delGroup(btn){
  const g=btn.closest('.grp');
  if(!confirm('删除这一组（含组内所有事件）？')) return;
  g.remove(); renumber();
  if(!allGroups().length) addGroup();
}
function addScriptRow(src){
  let box=null;
  if(src && src.classList && src.classList.contains('grows')) box=src;
  else if(src && src.closest && src.closest('.grp')) box=src.closest('.grp').querySelector('.grows');
  if(!box){
    const g=lastGroup()||addGroup(undefined,false);
    box=g.querySelector('.grows');
  }
  box.insertAdjacentHTML('beforeend', scrRowHTML());
  const row=box.lastElementChild;
  row.querySelector('.ssleepmode').onchange=()=>updateSleepUI(row);
  updateSleepUI(row); return row;
}
function delScriptRow(btn){ btn.closest('.srow').remove(); }
function updateSleepUI(row){ const m=row.querySelector('.ssleepmode'); const rnd=m.value==='random';
  row.querySelector('.ssleep').classList.toggle('hidden',rnd);
  row.querySelector('.srnd').classList.toggle('hidden',!rnd);
  row.querySelector('.ssleep2').classList.toggle('hidden',!rnd); }
function stepOfRow(r){
  const action=r.querySelector('.saction').value;
  const key=r.querySelector('.skey').value.trim();
  const mode=r.querySelector('.ssleepmode').value;
  let sleep={mode};
  if(mode==='random'){ sleep.min=parseFloat(r.querySelector('.ssleep').value)||0; sleep.max=parseFloat(r.querySelector('.ssleep2').value)||0; }
  else { sleep.value=parseFloat(r.querySelector('.ssleep').value)||0; }
  if(action==='releaseall') return {action:'releaseall',key:'',sleep};
  if(!key) return null;
  return {action,key,sleep};
}
function collectGroups(){
  const out=[];
  allGroups().forEach(g=>{
    let rep=parseInt(g.querySelector('.grep').value);
    if(isNaN(rep)) rep=1;
    rep=Math.max(0,rep);
    const steps=[];
    [...g.querySelectorAll('.srow')].forEach(r=>{ const s=stepOfRow(r); if(s) steps.push(s); });
    if(steps.length) out.push({repeat:rep, steps});
  });
  return out;
}
async function runScript(){
  if(!connected){ log('脚本: 未连接串口'); return; }
  const groups=collectGroups();
  if(!groups.length){ log('脚本: 没有有效事件（需填按键）'); return; }
  const loops=parseInt($('loopNum').value)||0;
  const pairs=[];
  document.querySelectorAll('#timerRows .timerrow').forEach(row=>{
    const key=row.querySelector('.tkey').value.trim();
    if(!key) return;
    pairs.push({row:row, t:{tid:row.dataset.tid?parseInt(row.dataset.tid):null,
      interval:parseFloat(row.querySelector('.tint').value)||5,
      key:key,
      hold:parseFloat((row.querySelector('.thold')||{}).value)||0}});
  });
  const timers=pairs.map(p=>p.t);
  const r=await fetch('/api/script/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({groups,loops,timers})}).then(x=>x.json());
  if(r.ok && Array.isArray(r.timers)){ pairs.forEach((p,i)=>{ if(r.timers[i]) p.row.dataset.tid=r.timers[i]; }); }
  const nstep=groups.reduce((a,g)=>a+g.steps.length,0);
  log(r.ok?('[脚本] 开始运行 '+groups.length+' 组 / '+nstep+' 步, 整体循环 '+(loops||'无限')+', 定时器 '+((r.timers||[]).length)+' 个'):('[脚本] 启动失败 '+(r.err||'')));
  pollScr(); pollTimers();
}
async function stopScript(){
  await fetch('/api/script/stop',{method:'POST'}).then(x=>x.json());
  document.querySelectorAll('#timerRows .timerrow').forEach(r=>r.dataset.tid='');
  log('[脚本] 已停止(定时器一并停止)');
}
async function pollScr(){ const r=await api('script/status');
  const el=$('scrState'); el.textContent=r.running?'● 运行中':'';
  el.style.color=r.running?'#4ade80':'var(--dim)';
  setTimeout(pollScr,900); }

// ---------- 脚本保存/加载 ----------
function fillRow(row, st){
  row.querySelector('.saction').value=(st.action==='releaseall'?'releaseall':(st.action||'tap'));
  row.querySelector('.skey').value=st.key||'';
  const mode=(st.sleep&&st.sleep.mode)||'fixed';
  row.querySelector('.ssleepmode').value=mode;
  if(mode==='random'){ row.querySelector('.ssleep').value=st.sleep.min!==undefined?st.sleep.min:''; row.querySelector('.ssleep2').value=st.sleep.max!==undefined?st.sleep.max:''; }
  else { row.querySelector('.ssleep').value=(st.sleep&&st.sleep.value!==undefined)?st.sleep.value:''; }
  updateSleepUI(row);
}
function renderGroups(groups){
  const box=$('scriptRows'); box.innerHTML='';
  if(!groups || !groups.length){ addGroup(); return; }
  groups.forEach(g=>{
    const ge=addGroup(g.repeat, false);
    const grows=ge.querySelector('.grows');
    (g.steps||[]).forEach(st=>{ fillRow(addScriptRow(grows), st); });
    if(!(g.steps||[]).length) addScriptRow(grows);
  });
  renumber();
}
async function saveScript(){
  const name=$('scriptName').value.trim();
  if(!name){ log('保存: 请先填脚本名'); return; }
  const groups=collectGroups();
  if(!groups.length){ log('保存: 脚本里没有有效事件'); return; }
  const loops=parseInt($('loopNum').value)||0;
  const timers=[...document.querySelectorAll('#timerRows .timerrow')].map(r=>({
    interval:parseFloat(r.querySelector('.tint').value)||5,
    key:r.querySelector('.tkey').value.trim(),
    hold:parseFloat(r.querySelector('.thold').value)||0}));
  const r=await fetch('/api/script/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,groups,loops,timer:timers})}).then(x=>x.json());
  const nstep=groups.reduce((a,g)=>a+g.steps.length,0);
  log(r.ok?('[保存] '+name+' ('+groups.length+'组/'+nstep+'步, 整体循环'+loops+', 定时'+timers.length+'个)'):('[保存失败] '+(r.err||'')));
  refreshScriptList();
}
async function refreshScriptList(){
  const r=await api('script/list'); const sel=$('scriptSel'); sel.innerHTML='';
  (r.names||[]).forEach(n=>{const o=document.createElement('option');o.value=n;o.textContent=n;sel.appendChild(o);});
}
async function loadScript(){
  const name=$('scriptSel').value; if(!name){log('加载: 没有已保存脚本');return;}
  const r=await api('script/load?name='+encodeURIComponent(name));
  if(r.ok){ const s=r.script||{};
    if(Array.isArray(s.groups) && s.groups.length){
      renderGroups(s.groups);
      $('loopNum').value=(s.loops===0?0:(s.loops||1));
    } else {
      const rep=(s.loops===0?0:(s.loops||1));
      renderGroups([{repeat:rep, steps:s.steps||[]}]);
      $('loopNum').value=1;
      log('[加载] 旧格式脚本，已转成单组（本组循环 '+rep+' 次，整体循环 1 轮）');
    }
    $('scriptName').value=name;
    if(s.timer && Array.isArray(s.timer)){
      $('timerRows').innerHTML=''; s.timer.forEach(t=>addTimerRow(t));
      log('[定时配置] 已恢复 '+s.timer.length+' 个');
    }
    log('[加载] '+name); }
  else log('[加载失败] '+(r.err||''));
}
async function delScript(){
  const name=$('scriptSel').value; if(!name)return;
  if(!confirm('删除脚本 "'+name+'" ？'))return;
  const r=await api('script/delete?name='+encodeURIComponent(name));
  log(r.ok?('[删除] '+name):('[删除失败] '+r.err));
  refreshScriptList();
}

// ---------- 倒计时按键(多个并行定时器) ----------
function timerRowHTML(){
  return '<div class="row srow timerrow" data-tid="" style="margin-top:6px">' +
    '<label style="font-size:13px;color:var(--dim)">每</label>' +
    '<input type="number" class="tint" value="5" min="0.1" step="0.5" style="width:60px">' +
    '<label style="font-size:13px;color:var(--dim)">秒按</label>' +
    '<input type="text" class="tkey" placeholder="键/组合,如 a / enter / f5" style="flex:1;min-width:110px">' +
    '<label style="font-size:13px;color:var(--dim)">按住</label>' +
    '<input type="number" class="thold" value="0" min="0" step="0.1" style="width:52px" title="按住时长(秒), 0=点按(立即松开)">' +
    '<label style="font-size:13px;color:var(--dim)">秒(0=点按)</label>' +
    '<button class="btn-ok tstart" onclick="tStart(this)">▶</button>' +
    '<button class="btn-err tstop" onclick="tStop(this)">■</button>' +
    '<button class="sdel tdel" onclick="tDel(this)">×</button>' +
    '<span class="tstate" style="font-size:12px;color:var(--dim);min-width:20px"></span></div>';
}
function addTimerRow(list){
  const box=$('timerRows'); box.insertAdjacentHTML('beforeend', timerRowHTML());
  const row=box.lastElementChild;
  if(list){ row.querySelector('.tint').value=list.interval; row.querySelector('.tkey').value=list.key||''; row.querySelector('.thold').value=list.hold||0; }
  return row;
}
async function tStart(btn){
  const row=btn.closest('.timerrow');
  if(!connected){log('定时: 未连接串口');return;}
  const interval=parseFloat(row.querySelector('.tint').value)||5;
  const key=row.querySelector('.tkey').value.trim();
  const hold=parseFloat(row.querySelector('.thold').value)||0;
  if(!key){log('定时: 请填要按的键');return;}
  const add=await fetch('/api/timer/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interval,key,hold})}).then(x=>x.json());
  if(!add.ok){ log('[定时] 添加失败 '+(add.err||'')); return; }
  row.dataset.tid=add.id;
  await fetch('/api/timer/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:add.id})}).then(x=>x.json());
  log('[定时#'+add.id+'] 每 '+interval+' 秒按 '+key+(hold>0?(' 按住'+hold+'秒'):'(点按)'));
  pollTimers();
}
async function tStop(btn){
  const row=btn.closest('.timerrow'); const tid=row.dataset.tid;
  if(!tid)return;
  await fetch('/api/timer/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:parseInt(tid)})}).then(x=>x.json());
  log('[定时#'+tid+'] 已停止');
}
async function tDel(btn){
  const row=btn.closest('.timerrow'); const tid=row.dataset.tid;
  if(tid) await fetch('/api/timer/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:parseInt(tid)})}).then(x=>x.json());
  row.remove();
}
async function pollTimers(){
  const r=await api('timer/list');
  (r.timers||[]).forEach(t=>{
    const row=document.querySelector(`.timerrow[data-tid="${t.id}"]`);
    if(row){ row.dataset.tid=t.id; const st=row.querySelector('.tstate');
      st.textContent=t.running?'●':''; st.style.color=t.running?'#4ade80':'var(--dim)'; }
  });
  setTimeout(pollTimers,900);
}

buildKB(); loadPorts(); poll(); addGroup(); pollScr(); refreshScriptList(); addTimerRow(); pollTimers();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/ports":
            self._send({"ok": True, "ports": find_esp_ports()})
            return

        if path == "/api/connect":
            port = (qs.get("port") or [""])[0]
            try:
                ok = connect(port)
                self._send({"ok": ok, "port": port})
            except Exception as e:
                self._send({"ok": False, "err": str(e)})
            return

        if path == "/api/disconnect":
            with SER_LOCK:
                global SER
                if SER:
                    try:
                        SER.write(b"RELEASE\n")
                    except Exception:
                        pass
                    try:
                        SER.close()
                    except Exception:
                        pass
                SER = None
            self._send({"ok": True})
            return

        if path == "/api/tap":
            combo = (qs.get("combo") or ["a"])[0]
            ok = send_line("KEY " + combo)
            self._send({"ok": ok})
            return

        if path == "/api/press":
            combo = (qs.get("combo") or ["a"])[0]
            with HELD_LOCK:
                HELD[combo] = "手动"
            ok = send_line("PRESS " + combo)
            self._send({"ok": ok})
            return

        if path == "/api/release":
            ok = release_all_manual()
            self._send({"ok": ok})
            return

        if path == "/api/releaseone":
            key = (qs.get("key") or ["ctrl"])[0]
            if key:
                ok = send_line("RELEASE " + key)
                self._send({"ok": ok})
            else:
                self._send({"ok": True})
            return

        if path == "/api/status":
            with SER_LOCK:
                conn = bool(SER and SER.is_open)
                echo = list(ECHO)
                ECHO.clear()
            self._send({"ok": True, "connected": conn, "echo": echo})
            return

        if path == "/api/script/status":
            self._send({"ok": True, "running": script_running()})
            return

        if path == "/api/script/list":
            self._send({"ok": True, "names": script_list()})
            return

        if path == "/api/script/load":
            name = (qs.get("name") or [""])[0]
            data = script_load(name)
            if data:
                self._send({"ok": True, "script": data})
            else:
                self._send({"ok": False, "err": "脚本不存在"})
            return

        if path == "/api/script/delete":
            name = (qs.get("name") or [""])[0]
            ok = script_delete(name)
            self._send({"ok": ok, "names": script_list()})
            return

        if path == "/api/timer/list":
            self._send({"ok": True, "timers": timer_list()})
            return

        self._send({"ok": False, "err": "unknown"}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""

        if path == "/api/script/run":
            try:
                data = json.loads(body) if body else {}
                steps = data.get("steps", [])
                loops = data.get("loops", 1)
                timers = data.get("timers") or []
                groups_in = data.get("groups")
                if groups_in:
                    groups = _norm_groups(groups_in)
                    rounds = int(loops or 0)
                else:   # 兼容老请求: 扁平 steps + loops
                    groups = _legacy_groups(steps, loops)
                    rounds = 1
                if not groups:
                    self._send({"ok": False, "err": "脚本为空"})
                    return
                started = script_run(groups, rounds, timers)
                self._send({"ok": True, "running": True, "timers": started})
            except Exception as e:
                self._send({"ok": False, "err": str(e)})
            return

        if path == "/api/script/stop":
            script_stop()
            for tid in list(TIMERS.keys()):   # 停止脚本时一并停止所有定时器
                timer_stop(tid)
            self._send({"ok": True})
            return

        if path == "/api/script/save":
            try:
                data = json.loads(body) if body else {}
                name = (data.get("name") or "").strip()
                steps = data.get("steps", [])
                loops = data.get("loops", 1)
                if not name:
                    self._send({"ok": False, "err": "请填写脚本名"})
                    return
                groups_in = data.get("groups")
                if groups_in:
                    groups = _norm_groups(groups_in)
                else:
                    groups = _legacy_groups(data.get("steps", []), 1)
                if not groups:
                    self._send({"ok": False, "err": "脚本为空"})
                    return
                timer = data.get("timer")
                script_save(name, groups, loops, timer)
                self._send({"ok": True, "names": script_list()})
            except Exception as e:
                self._send({"ok": False, "err": str(e)})
            return

        if path == "/api/timer/add":
            try:
                data = json.loads(body) if body else {}
                interval = float(data.get("interval", 5))
                key = (data.get("key") or "").strip()
                hold = float(data.get("hold", 0) or 0)
                if not key:
                    self._send({"ok": False, "err": "请填写按键"})
                    return
                tid = timer_add(interval, key, hold)
                self._send({"ok": True, "id": tid})
            except Exception as e:
                self._send({"ok": False, "err": str(e)})
            return

        if path == "/api/timer/start":
            try:
                data = json.loads(body) if body else {}
                tid = int(data.get("id", 0))
                ok = timer_start(tid)
                self._send({"ok": ok, "id": tid})
            except Exception as e:
                self._send({"ok": False, "err": str(e)})
            return

        if path == "/api/timer/stop":
            try:
                data = json.loads(body) if body else {}
                tid = int(data.get("id", 0))
                timer_stop(tid)
                self._send({"ok": True, "id": tid})
            except Exception as e:
                self._send({"ok": False, "err": str(e)})
            return

        if path == "/api/timer/remove":
            try:
                data = json.loads(body) if body else {}
                tid = int(data.get("id", 0))
                ok = timer_remove(tid)
                self._send({"ok": ok, "id": tid})
            except Exception as e:
                self._send({"ok": False, "err": str(e)})
            return

        self._send({"ok": False, "err": "unknown"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    threading.Thread(target=reader_loop, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[ESP完整键盘控制台] 浏览器打开 http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
