@echo off

rem NOTE: This file MUST stay GBK(ANSI)-encoded with CRLF line endings.
rem       cmd parses .bat in the system ANSI codepage - saving it as UTF-8
rem       garbles the Chinese text below and the output breaks.
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Build MapleStoryFarm exe

rem ---- 挑一个带 PyInstaller 的 Python：先找系统 python，再找本机固定安装的 ----
set "PICKED="
for %%C in ("python" "C:\Users\JimJiang\.workbuddy\binaries\python\versions\3.13.12\python.exe") do (
    if not defined PICKED (
        %%~C -c "import PyInstaller" >nul 2>nul
        if not errorlevel 1 set "PICKED=%%~C"
    )
)

if not defined PICKED (
    echo [ERROR] 没找到带 PyInstaller 的 Python。先执行： pip install pyinstaller ultralytics openvino
    echo.
    pause
    exit /b 1
)

echo Python : %PICKED%
echo 开始打包：要几分钟到十几分钟（torch 很大）。窗口别关。
echo.

"%PICKED%" build_exe.py %*
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (
    echo 打包完成。整个 dist\MapleStoryFarm 文件夹一起拷到目标机器，双击里面的 MapleStoryFarm.exe 即可运行。
) else (
    echo 打包失败（退出码 %RC%）。把窗口里的报错发出来看看。
)
echo.
pause
