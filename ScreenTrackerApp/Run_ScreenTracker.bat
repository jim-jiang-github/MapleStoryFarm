@echo off
rem Screen Region Tracker: select a screen region, YOLO detects the target,
rem and the real mouse follows it smoothly (150Hz spring interpolation).
rem   Drag a box on the screen snapshot:  ENTER/SPACE = confirm, C = cancel
rem   F12 = quit anytime     F2 = re-select region
cd /d "%~dp0"

if not exist "best_ft3.pt" (
  echo [ERROR] weights not found: best_ft3.pt
  pause
  exit /b 1
)
if not exist "screen_tracker.py" (
  echo [ERROR] screen_tracker.py not found in this folder
  pause
  exit /b 1
)

echo ================================================
echo  Screen Region Tracker  (ft3 YOLO + smooth mouse)
echo  1. Drag a box on the screen (ENTER to confirm, C to cancel)
echo     - best if the box is close to 750x500 / 3:2 ratio
echo  2. Put the target content inside the box; the mouse
echo     will follow the detected shape automatically
echo  3. F12 = quit    F2 = re-select region
echo ================================================
set "PYTHON_EXE=%~dp0..\.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" (
  "%PYTHON_EXE%" screen_tracker.py %*
) else (
  python screen_tracker.py %*
)
pause
