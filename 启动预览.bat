@echo off

rem NOTE: This file MUST stay GBK(ANSI)-encoded with CRLF line endings.
rem       cmd parses .bat in the system ANSI codepage - saving it as UTF-8
rem       garbles the Chinese filename below and the call fails.
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Capture Preview

set "PICKED="
for %%C in ("C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.12.9-windows-x86_64-none\python.exe" "python" "C:\Users\JimJiang\.workbuddy\binaries\python\versions\3.13.12\python.exe") do (
    if not defined PICKED (
        %%~C -c "import mss,cv2,fastapi,uvicorn,win32gui,numpy,ultralytics,openvino" >nul 2>nul
        if not errorlevel 1 set "PICKED=%%~C"
    )
)

if not defined PICKED (
    echo [ERROR] No usable Python found ^(need 3.10+ with mss, opencv-python, fastapi, uvicorn, pywin32, ultralytics, openvino^).
    echo.
    pause
    exit /b 1
)

echo Python : %PICKED%
echo Service: http://127.0.0.1:8765
echo Close this window to stop the service.
echo.

"%PICKED%" "webui\server.py" --open

rem ---- ����ȡ֤����ȷ�˳�ʱ�� + ��ʱȫ�����̱� ----
rem service.log �� python �Լ��ļ�¼ֹ�ڡ�������������������ʽ��������
>> "webui\service.log" echo [%date% %time%] python �˳� exit=%errorlevel%
>> "webui\service.log" tasklist

echo.
echo Service stopped. (exit code %errorlevel% ^| �˳�ԭ��� webui\service.log)
pause
