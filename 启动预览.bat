@echo off

rem NOTE: This file MUST stay GBK(ANSI)-encoded with CRLF line endings.
rem       cmd parses .bat in the system ANSI codepage - saving it as UTF-8
rem       garbles the Chinese filename below and the call fails.
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Capture Preview

set "PICKED="
for %%C in ("python" "C:\Users\JimJiang\.workbuddy\binaries\python\versions\3.13.12\python.exe") do (
    if not defined PICKED (
        %%~C -c "import mss,cv2,fastapi,uvicorn,win32gui,numpy" >nul 2>nul
        if not errorlevel 1 set "PICKED=%%~C"
    )
)

if not defined PICKED (
    echo [ERROR] No usable Python found ^(need 3.10+ with mss, opencv-python, fastapi, uvicorn, pywin32^).
    echo.
    pause
    exit /b 1
)

echo Python : %PICKED%
echo Service: http://127.0.0.1:8765
echo Close this window to stop the service.
echo.

"%PICKED%" "webui\server.py" --open

rem ---- 死亡取证：精确退出时刻 + 当时全量进程表 ----
rem service.log 里 python 自己的记录止于「服务启动」，死亡方式看不见；
>> "webui\service.log" echo [%date% %time%] python 退出 exit=%errorlevel%
>> "webui\service.log" tasklist

echo.
echo Service stopped. (exit code %errorlevel% ^| 退出原因见 webui\service.log)
pause
