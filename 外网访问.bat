@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Capture Preview - WAN Access

set "PICKED="
for %%C in ("python" "C:\Users\JimJiang\.workbuddy\binaries\python\versions\3.13.12\python.exe") do (
    if not defined PICKED (
        %%~C -c "import mss,cv2,fastapi,uvicorn,win32gui,numpy" >nul 2>nul
        if not errorlevel 1 set "PICKED=%%~C"
    )
)

if not defined PICKED (
    echo [ERROR] No usable Python found ^(need mss, opencv-python, fastapi, uvicorn, pywin32^).
    echo.
    pause
    exit /b 1
)

REM ===== Access token: visitors must open  https://xxxx/?k=THIS_TOKEN =====
REM Change it here if you want a different one.
if "%PREVIEW_TOKEN%"=="" set "PREVIEW_TOKEN=msfarm2026"

REM Close the old preview window first, otherwise the token will NOT be applied
REM (the old server without token keeps occupying port 8765).
netstat -ano | findstr /C:":8765" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo [WARN] Port 8765 is already in use. Close the old preview window first,
    echo        then run this script again - otherwise the token gate is skipped.
    echo.
)

echo [1/2] Starting preview server on port 8765  (token: %PREVIEW_TOKEN%) ...
start "capture-preview" /min "%PICKED%" "webui\server.py" --port 8765
timeout /t 4 >nul

echo [2/2] Starting tunnel (no signup, random public URL) ...
echo       It will print the FINAL share link (already carrying ?k=token) and
echo       copy it to your clipboard. Just paste that link.
echo.
"%PICKED%" "tools\tunnel.py" 8765

echo.
echo Tunnel stopped. The minimized "capture-preview" server window is still running;
echo close it if you are done.
pause
