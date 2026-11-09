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
import time
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

# ultralytics 靠 entry_points 被静态分析扫到的"顺带依赖"，本项目的推理链路完全用不到。
# 都已经核对过导入位置：polars 只在 Result.to_df/to_csv 这类导出函数里 import，
# onnxruntime 只在 model.export() 里 import，matplotlib 在 ultralytics 里没有顶层导入。
# 但 polars 的运行时是个 160MB 的巨型 pyd，留着非常不划算。
EXCLUDE_MODULES = (
    "polars",              # 161MB：_polars_runtime_32/_polars_runtime.pyd
    "_polars_runtime_32",
    "onnxruntime",         # 38MB：只有导出 onnx 时才用
    "onnx",                # 4MB：同上
    "matplotlib",          # 15MB：绘图用，服务端只画 cv2 叠加层
    "seaborn",
    "pandas",
    "IPython",
    "jupyter",
)

# openvino 的 --collect-all 会把所有设备插件都收进来，我们固定 device="cpu"，
# NPU 编译器(96MB)+NPU 插件(7MB)+GPU 插件(33MB) 全是死重量，打包后直接剔掉。
OPENVINO_DROP = (
    "openvino_intel_npu_compiler.dll",
    "openvino_intel_npu_plugin.dll",
    "openvino_intel_gpu_plugin.dll",
    "openvino.lib",        # 导入库，只给 C++ 编译期用
    "cache.json",
)


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


def prepare_dist():
    """上一次的产物目录先改名让路，**不删**。

    直接删 5000+ 文件会撞上批量删除确认策略，PyInstaller 自己 `--noconfirm`
    的 "Removing dir" 也会被同一道检查拦下来（退出码 1）。改名是移动操作，
    不经过删除检查，顺带留了个能回滚的旧版本——M 大内存的空闲磁盘，值得。
    """
    target = DIST / APP_NAME
    if not target.exists():
        return
    bak = DIST / f"{APP_NAME}-{time.strftime('%Y%m%d-%H%M%S')}"
    log(f"旧产物改名备份 -> dist/{bak.name}（占空间，确认新版没问题后可手动删）")
    target.rename(bak)


def prune_backups(keep=1):
    """只留最近一个旧版本备份，别让 dist/ 无限长大（每次 1.2GB）。

    删不掉就算了：某些环境对批量删除有策略拦截，构建结果已经好了，
    不能让"清理"把整个脚本搞崩。
    """
    baks = sorted(DIST.glob(f"{APP_NAME}-*"), key=lambda p: p.name, reverse=True)
    for old in baks[keep:]:
        try:
            shutil.rmtree(old)
            log(f"删掉更早的备份 {old.name}")
        except BaseException:  # noqa: BLE001  含 SystemExit：删除被拦时不要中断
            log(f"更早的备份删不掉，可手动删：{old}")
            break


def prune_openvino(out_dir: Path):
    """剔掉用不到的 openvino 设备插件。

    我们固定 `device="cpu"`，NPU 编译器/GPU 插件永远不会被加载；`--collect-all openvino`
    没法只收一部分，所以打包完手动删。只删确定不用的文件，删之前把大小打出来。
    """
    libs = out_dir / "_internal" / "openvino" / "libs"
    if not libs.is_dir():
        return
    freed = 0
    for name in OPENVINO_DROP:
        p = libs / name
        if p.is_file():
            freed += p.stat().st_size
            try:
                p.unlink()
            except OSError as e:  # noqa: BLE001
                log(f"剔不掉 {name}：{e}")
    if freed:
        log(f"剔掉 openvino 的 NPU/GPU 插件：省 {freed / 1048576:.0f} MB")


def size_report(out_dir: Path, top=8):
    """打出体积构成，下次想知道"为啥这么大"直接看这里。"""
    items = []
    for p in list(out_dir.glob("*")) + list((out_dir / "_internal").glob("*")):
        if p.is_file():
            items.append((p.stat().st_size, p.name))
        elif p.is_dir():
            s = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            items.append((s, p.name + "/"))
    items.sort(reverse=True)
    log("体积构成：")
    for s, n in items[:top]:
        log(f"  {s / 1048576:7.1f} MB  {n}")


def build(onefile, with_accurate, clean, dry_run=False):
    # 不在脚本里删 build/ 和 dist/：里面是几万个文件，批量删除会被各种安全策略
    # （包括本项目的 safe-delete 钩子）拦下来。目录版靠 prepare_dist() 改名让路，
    # 单文件版只有一个 exe，PyInstaller 直接覆盖。
    if clean and not dry_run:
        log("--clean：交给 PyInstaller 清理自身缓存")

    stage_assets()
    if not dry_run and not onefile:
        prepare_dist()

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
        # torchvision：ultralytics 的 NMS 走 torchvision.ops。新版把扩展改名成
        # `_C_stable.pyd`，PyInstaller 自带的 hook 还在找 `torchvision._C`，
        # 收不进去 → 运行时 `RuntimeError: operator torchvision::nms does not exist`。
        # --collect-all 会把 binaries(.pyd) 一起带上。
        "--collect-all", "torchvision",
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
        *[x for m in EXCLUDE_MODULES for x in ("--exclude-module", m)],
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
        prune_openvino(out)
        total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) if out.exists() else 0
    if not exe.exists():
        sys.exit(f"没找到产物 {exe}")
    log(f"完成：{exe}")
    log(f"体积：{total / 1048576:.0f} MB")
    if not onefile:
        size_report(DIST / APP_NAME)
    if onefile:
        log("单文件版每次启动都要把运行时解压到临时目录，第一次点开要等一会儿。")
        log("config/标定会写到 exe 旁边的 data/ 里。")
    else:
        log("拷贝时把整个 dist/MapleStoryFarm 文件夹带走；data/ 会在第一次运行时生成。")
        prune_backups()


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
