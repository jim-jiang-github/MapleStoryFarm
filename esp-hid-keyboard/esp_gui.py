# -*- coding: utf-8 -*-
"""
esp_gui.py — ESP32-S3 模拟键盘控制界面 (tkinter)
功能：串口选择/连接、按左/按右(按住)、停止松开、按住时长、点按、日志回显
用法：python esp_gui.py
"""
import argparse, sys, time
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import serial, serial.tools.list_ports

PORT_BAUD = 115200


def find_esp_ports():
    """找出所有 ESP32 (VID 0x303A) 串口"""
    found = []
    for p in serial.tools.list_ports.comports():
        if p.vid == 0x303A:
            found.append(p.device)
    if not found:
        for p in serial.tools.list_ports.comports():
            if "USB" in p.description and "串" in p.description:
                found.append(p.device)
    # 去重保序
    return list(dict.fromkeys(found))


class EspGui:
    def __init__(self, root):
        self.root = root
        self.root.title("ESP32-S3 模拟键盘控制台")
        self.root.geometry("460x520")
        self.root.minsize(420, 480)

        self.ser = None
        self._hold_job = None
        self._release_timer = None
        self._stop_thread = threading.Event()

        self._build_ui()
        self.refresh_ports()
        self._poll_serial()  # 启动串口回显轮询

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # 串口栏
        conn = ttk.LabelFrame(self.root, text="串口连接")
        conn.pack(fill="x", **pad)
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(conn, textvariable=self.port_var, width=16, state="readonly")
        self.port_cb.grid(row=0, column=0, padx=6, pady=6)
        ttk.Button(conn, text="刷新", command=self.refresh_ports).grid(row=0, column=1, padx=3)
        self.btn_conn = ttk.Button(conn, text="连接", command=self.toggle_connect, width=8)
        self.btn_conn.grid(row=0, column=2, padx=3)
        self.status_var = tk.StringVar(value="未连接")
        ttk.Label(conn, textvariable=self.status_var, foreground="gray").grid(row=0, column=3, padx=6)

        # 方向控制
        ctrl = ttk.LabelFrame(self.root, text="方向键控制")
        ctrl.pack(fill="x", **pad)
        ttk.Button(ctrl, text="←  左 (按住)", command=lambda: self.press("L"),
                   width=12).grid(row=0, column=0, padx=6, pady=8)
        ttk.Button(ctrl, text="停止 / 松开", command=lambda: self.release(),
                   width=12).grid(row=0, column=1, padx=6, pady=8)
        ttk.Button(ctrl, text="→  右 (按住)", command=lambda: self.press("R"),
                   width=12).grid(row=0, column=2, padx=6, pady=8)
        # 点按一行
        ttk.Button(ctrl, text="点按左 ←", command=lambda: self.tap("L"),
                   width=10).grid(row=1, column=0, padx=6, pady=2)
        ttk.Button(ctrl, text="点按右 →", command=lambda: self.tap("R"),
                   width=10).grid(row=1, column=2, padx=6, pady=2)

        # 按住时长
        hold = ttk.LabelFrame(self.root, text="按住时长(秒, 0=一直按住)")
        hold.pack(fill="x", **pad)
        self.hold_var = tk.StringVar(value="0")
        ttk.Entry(hold, textvariable=self.hold_var, width=8).grid(row=0, column=0, padx=6, pady=6)
        self.auto_hold_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(hold, text="按住后到时自动松开", variable=self.auto_hold_var).grid(row=0, column=1, padx=6)

        # 日志
        logf = ttk.LabelFrame(self.root, text="日志")
        logf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logf, height=10, state="disabled", font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    # ---------- 串口 ----------
    def refresh_ports(self):
        ports = find_esp_ports()
        current = self.port_var.get()
        self.port_cb["values"] = ports
        if ports:
            self.port_var.set(current if current in ports else ports[0])
            self._log(f"检测到串口: {', '.join(ports)}")
        else:
            self.port_var.set("")
            self._log("未检测到 ESP32 串口，请检查 USB 连接")

    def toggle_connect(self):
        if self.ser:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("提示", "请先选择串口")
            return
        try:
            self.ser = serial.Serial(port, PORT_BAUD, timeout=0.2)
            time.sleep(1)
            self.ser.reset_input_buffer()
            self.status_var.set("已连接 " + port)
            self.btn_conn.config(text="断开")
            self._log(f"[连接] {port} @ {PORT_BAUD}")
        except Exception as e:
            self._log(f"[连接失败] {e}")
            messagebox.showerror("连接失败", str(e))

    def disconnect(self):
        if self.ser:
            try:
                self.ser.write(b"S")
            except Exception:
                pass
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self._cancel_hold()
        self.status_var.set("未连接")
        self.btn_conn.config(text="连接")
        self._log("[断开] 串口已关闭")

    # ---------- 指令 ----------
    def press(self, key):
        if not self._check():
            return
        self._cancel_hold()
        try:
            self.ser.write(key.encode())
            self._log(f"[指令] {key} -> {'按住←' if key=='L' else '按住→'}")
        except Exception as e:
            self._log(f"[发送失败] {e}")
            return
        # 自动松开
        if self.auto_hold_var.get():
            try:
                sec = float(self.hold_var.get())
            except ValueError:
                sec = 0
            if sec > 0:
                self._release_timer = threading.Timer(sec, self._auto_release)
                self._release_timer.daemon = True
                self._release_timer.start()
                self._log(f"  (将在 {sec}s 后自动松开)")

    def tap(self, key):
        if not self._check():
            return
        self._cancel_hold()
        try:
            self.ser.write(key.encode())
            time.sleep(0.06)
            self.ser.write(b"S")
            self._log(f"[点按] {key} 按下后松开")
        except Exception as e:
            self._log(f"[发送失败] {e}")

    def release(self):
        if not self._check():
            return
        self._cancel_hold()
        try:
            self.ser.write(b"S")
            self._log("[指令] S -> 松开全部")
        except Exception as e:
            self._log(f"[发送失败] {e}")

    def _auto_release(self):
        self.root.after(0, self.release)
        self._release_timer = None

    def _cancel_hold(self):
        if self._release_timer:
            self._release_timer.cancel()
            self._release_timer = None

    def _check(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("未连接", "请先连接串口")
            return False
        return True

    # ---------- 回显轮询 ----------
    def _poll_serial(self):
        if self.ser and self.ser.is_open:
            try:
                n = self.ser.in_waiting
                if n:
                    data = self.ser.read(n).decode("utf-8", "ignore").strip()
                    if data:
                        self._log(f"[回显] {data}")
            except Exception:
                pass
        self.root.after(100, self._poll_serial)

    def _log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")


def main():
    root = tk.Tk()
    app = EspGui(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.disconnect(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
