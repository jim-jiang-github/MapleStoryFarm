@echo off

rem NOTE: This file MUST stay GBK(ANSI)-encoded with CRLF line endings.
rem       cmd parses .bat in the system ANSI codepage - saving it as UTF-8
rem       garbles the Chinese filename below and the call fails.
rem ---------------------------------------------------------------
rem Start the service as Administrator.
rem
rem Why: the software fallback (SendInput) has to inject keys into the
rem game window. The game runs elevated, and Windows UIPI silently drops
rem input sent from a lower-integrity process. Use this .bat when no
rem CH9329 board is plugged in, or when the panel says
rem "software injection blocked by system".
rem
rem Just double-click it. If we are not elevated we relaunch ourselves
rem with -Verb RunAs, so the only thing you have to do is answer the UAC
rem prompt. (Right-click -> Run as administrator still works too - then
rem this branch is skipped entirely.)
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if not errorlevel 1 goto :elevated

rem --- 不是管理员：自我提权重启，UAC 会问一次 ---
echo [!] 需要管理员权限，正在请求提权...
echo     请在 UAC 弹窗里点「是」。
echo.
set "SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process -FilePath $env:SELF -WorkingDirectory (Split-Path -Parent $env:SELF) -Verb RunAs } catch { exit 1 }"
if errorlevel 1 (
    echo [!] 提权被取消，没有启动任何东西。
    echo.
    pause
    exit /b 1
)
exit /b 0

:elevated
echo Running as Administrator - software key injection is available.
echo.
call "%~dp0启动预览.bat"
