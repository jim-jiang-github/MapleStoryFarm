@echo off

rem NOTE: This file MUST stay GBK(ANSI)-encoded with CRLF line endings.
rem       cmd parses .bat in the system ANSI codepage - saving it as UTF-8
rem       garbles the Chinese filename below and the call fails.
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Capture Preview

rem No import probe here: importing ultralytics+openvino costs 3-4s on every start.
rem Use the hardcoded uv python (the one with all deps); fall back to python on PATH.
rem If a package is really missing, server.py prints the ImportError in this window.
set "PICKED=C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.12.9-windows-x86_64-none\python.exe"
if not exist "%PICKED%" set "PICKED="
if not defined PICKED (
    where python >nul 2>nul
    if not errorlevel 1 set "PICKED=python"
)

if not defined PICKED (
    echo [ERROR] No usable Python found ^(put python on PATH, or edit the hardcoded path in this .bat^).
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
