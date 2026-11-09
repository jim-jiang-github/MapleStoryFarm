# -*- coding: utf-8 -*-
"""build_exe.py —— 把 WebUI 服务打包成可以拷到别的电脑上直接运行的 exe。

用法（在项目根目录执行）：

    python build_exe.py               # 目录版，推荐：dist/MapleStoryFarm/
    python build_exe.py --onefile     # 单文件版：只有一个 exe，但每次启动要解压
    python build_exe.py --clean       # 先删掉 build/ 和 dist/ 再打
    python build_exe.py --no-accurate # 只打"速度"模型，省 20MB

产物：

    dist/MapleStoryFarm/
        MapleStoryFarm.exe      <- 双击运行，浏览器开 http://127.0.0.1:8765
        _internal/              <- 运行时、torch、openvino、模型都在这
        data/                   <- 第一次运行自动生成：标定/配置/人物模板

**整个 dist/MapleStoryFarm 文件夹一起拷**，目标机器不用装 Python。
data/ 里的东西不会因为换 exe 被覆盖，用户标定好的血条和屏蔽区能留住。

为什么要盯着一堆 --collect-*：ultralytics 的模型/跟踪器是运行时按名字导入的，
PyInstaller 静态分析看不到；openvino 的插件 dll 也不在默认收集范围里。
少收一个就是「exe 能启动，一点识别就报 ModuleNotFoundError」。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEBUI = ROOT / "webui"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
STAGE = BUILD / "stage"
APP_NAME = "MapleStoryFarm"

# 打进 exe 的只读资源。配置 json 也带一份当默认值，第一次运行时由 paths.py
# 复制到 data/，之后用户改的都是 data/ 那份。
RES_GLOBS = ("*.html", "*.json", "*.png")
MODELS = [
    ("model_int8_fast_openvino_model", True),    # 速度模型：必须
    ("model_int8_openvino_model", False),        # 精度模型：可选
]


def log(msg):
    print(f"[build] {msg}", flush=True)


def check_env():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("没装 PyInstaller。先执行： pip install pyinstaller")
    try:
        import ultralytics  # noqa: F401
    except ImportError:
        sys.exit("没装 ultralytics（推理要用）。先执行： pip install ultralytics")
    try:
        import openvino  # noqa: F401
    except ImportError:
        sys.exit("没装 openvino（模型是 OpenVINO IR）。先执行： pip install openvino")
    log(f"python {sys.version.split()[0]}  pyinstaller {PyInstaller.__version__}")


def stage_assets():
    """把要打进包里的静态资源挑出来，别把 .py / __pycache__ 一起塞进去。"""
    if STAGE.exists():
        shutil.rmtree(STAGE)
    (STAGE / "webui").mkdir(parents=True)

    copied = 0
    for pattern in RES_GLOBS:
        for src in WEBUI.glob(pattern):
            if src.is_file():
                shutil.copy2(src, STAGE / "webui" / src.name)
                copied += 1
    if not (STAGE / "webui" / "index.html").exists():
        sys.exit("webui/index.html 没找到，路径不对？")
    log(f"静态资源 {copied} 个 -> build/stage/webui")


def model_flags(with_accurate):
    flags = []
    for name, required in MODELS:
        if not required and not with_accurate:
            log(f"跳过 {name}（--no-accurate）")
            continue
        src = ROOT / name
        if not src.is_dir():
            if required:
                sys.exit(f"模型目录不存在：{src}")
            log(f"跳过 {name}（目录不存在）")
            continue
        size = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
        log(f"模型 {name}  {size / 1048576:.1f} MB")
        flags += ["--add-data", f"{src}{os.pathsep}{name}"]
    return flags


def optional_pyserial():
    """装了 pyserial 就一起打进去：CH9329 硬件按键要用，没有也能跑软件注入。"""
    try:
        import serial  # noqa: F401
    except ImportError:
        log("没装 pyserial -> 包里不带硬件按键支持（自动用 SendInput）")
        return []
    log("带上 pyserial（CH9329 硬件按键）")
    return ["--hidden-import", "serial",
            "--hidden-import", "serial.tools",
            "--hidden-import", "serial.tools.list_ports"]


def build(onefile, with_accurate, clean, dry_run=False):
    if clean:
        for d in (BUILD, DIST):
            if d.exists():
                log(f"清理 {d.name}/")
                shutil.rmtree(d, ignore_errors=True)

    stage_assets()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--onefile" if onefile else "--onedir",
        "--console",                      # 保留控制台：要看端口、局域网地址和报错
        "--paths", str(WEBUI),            # import bot / track / paths 靠它
        "--workpath", str(BUILD / "work"),
        "--specpath", str(BUILD),
        "--distpath", str(DIST),
        # 静态页面 + 默认配置
        "--add-data", f"{STAGE / 'webui'}{os.pathsep}webui",
    ]
    cmd += model_flags(with_accurate)
    cmd += optional_pyserial()

    # ultralytics：模型/跟踪器运行时按名字导入，必须整包收
    cmd += [
        "--collect-all", "ultralytics",
        # openvino：IR 推理 + 插件 dll
        "--collect-all", "openvino",
        # uvicorn 的协议实现是字符串动态加载的，静态分析抓不到
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.loops.asyncio",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.http.h11_impl",
        "--hidden-import", "uvicorn.protocols.http.httptools_impl",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        # 别把没用的大头拖进来
        "--exclude-module", "tkinter",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PySide6",
        str(WEBUI / "server.py"),
    ]

    log("开始打包（第一次要几分钟到十几分钟，torch 很大）...")
    log(" ".join(f'"{c}"' if " " in c else c for c in cmd[2:]))
    if dry_run:
        log("--dry-run：只组装命令，不实际打包")
        return
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        sys.exit(f"PyInstaller 失败，退出码 {r.returncode}")

    if onefile:
        exe = DIST / f"{APP_NAME}.exe"
        total = exe.stat().st_size if exe.exists() else 0
    else:
        out = DIST / APP_NAME
        exe = out / f"{APP_NAME}.exe"
        total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) if out.exists() else 0
    if not exe.exists():
        sys.exit(f"没找到产物 {exe}")
    log(f"完成：{exe}")
    log(f"体积：{total / 1048576:.0f} MB")
    if onefile:
        log("单文件版每次启动都要把运行时解压到临时目录，第一次点开要等一会儿。")
        log("config/标定会写到 exe 旁边的 data/ 里。")
    else:
        log("拷贝时把整个 dist/MapleStoryFarm 文件夹带走；data/ 会在第一次运行时生成。")


def main():
    ap = argparse.ArgumentParser(description="打包 MapleStoryFarm 服务")
    ap.add_argument("--onefile", action="store_true", help="单文件 exe（启动要解压，慢）")
    ap.add_argument("--no-accurate", action="store_true", help="不打包精度模型")
    ap.add_argument("--clean", action="store_true", help="先删 build/ dist/")
    ap.add_argument("--dry-run", action="store_true", help="只组装 PyInstaller 命令，不真的打包")
    a = ap.parse_args()

    check_env()
    build(a.onefile, not a.no_accurate, a.clean, a.dry_run)


if __name__ == "__main__":
    main()
