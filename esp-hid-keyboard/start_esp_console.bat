@echo off
cd /d "C:\Users\Administrator\esp-hid-keyboard"
netstat -ano | findstr /C ":8080" | findstr /C "LISTENING" >nul 2>&1
if %errorlevel%==0 goto openpage
start "" "C:\Users\Administrator\AppData\Local\Programs\Python\Python310\pythonw.exe" "esp_server.py"
timeout /t 4 /nobreak >nul
:openpage
start "" "http://127.0.0.1:8080"
exit