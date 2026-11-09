# -*- coding: utf-8 -*-
"""文件位置：源码运行 与 PyInstaller 打包后 统一在这里解析。

打包成 exe 之后有几个坑，这个模块专门处理：

1. **可写文件不能放 `_MEIPASS`。** onefile 模式下那是每次启动新建的临时目录，
   往里写标定/配置 = 关掉程序就没了；onedir 下也在 `_internal` 里，升级解压
   覆盖就丢。所以可写的一律放 exe 旁边的 `data/`。
2. **内置资源在 `_MEIPASS`。** html、默认配置、默认人物模板都打进去了，只读。
3. **模型目录不能靠当前工作目录。** 快捷方式启动时 CWD 可能是 system32，
   `YOLO("model_int8_fast_openvino_model")` 会找不到 —— 必须基于资源根拼绝对路径。

源码运行时 `RES_DIR == DATA_DIR == webui/`，行为与以前完全一致。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    # --add-data "webui;webui" → _MEIPASS/webui
    RES_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "webui"
else:
    RES_DIR = Path(__file__).resolve().parent


def _pick_data_dir():
    """exe 旁边放 data/；那里不可写（比如装在 Program Files）就退到用户目录。

    MAPLE_DATA_DIR 环境变量优先级最高：无头自测（tools/test_headless.py）用它把
    配置/标定/日志全部指到临时目录，跑多少遍都不会碰仓库里真实的那几份 json。
    """
    env = os.environ.get("MAPLE_DATA_DIR", "").strip()
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if not FROZEN:
        return RES_DIR
    cand = Path(sys.executable).resolve().parent / "data"
    try:
        cand.mkdir(parents=True, exist_ok=True)
        probe = cand / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return cand
    except OSError:
        fallback = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MapleStoryFarm"
        fallback.mkdir(parents=True, exist_ok=True)
        print(f"[paths] {cand} 不可写，配置改放 {fallback}")
        return fallback


DATA_DIR = _pick_data_dir()


def data_file(name):
    """可写文件（配置/标定/模板）。

    第一次运行从内置资源复制一份默认值过去，之后都读 data/ 里的那份——
    升级 exe 不会把用户标定好的血条、屏蔽区冲掉。
    """
    dst = DATA_DIR / name
    if not dst.exists():
        src = RES_DIR / name
        if src.exists() and src.resolve() != dst.resolve():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
    return dst


def res_file(name):
    """只读资源（html）。data/ 里有同名文件就以它为准，方便临时替换。"""
    p = DATA_DIR / name
    return p if p.exists() else (RES_DIR / name)


def model_dir(name):
    """模型目录的绝对路径。源码=项目根/xxx，打包=_MEIPASS/xxx。"""
    return str((RES_DIR.parent / name).resolve())
