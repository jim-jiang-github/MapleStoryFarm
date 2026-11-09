@echo off
rem ---------------------------------------------------------------
rem Start the service as Administrator.
rem
rem Why: the software fallback (SendInput) has to inject keys into the
rem game window. The game runs elevated, and Windows UIPI silently drops
rem input sent from a lower-integrity process. Use this .bat when no
rem CH9329 board is plugged in, or when the panel says
rem "software injection blocked by system".
rem
rem Right-click this file -> "Run as administrator".
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo [!] Need Administrator rights.
    echo     Right-click this file and pick "Run as administrator".
    echo.
    pause
    exit /b 1
)

echo Running as Administrator - software key injection is available.
echo.
call "%~dp0启动预览.bat"
