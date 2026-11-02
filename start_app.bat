@echo off
chcp 65001 >nul
cd /d "%~dp0"
set NO_PROXY=127.0.0.1,localhost,::1
call .venv\Scripts\activate
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 5; Start-Process 'http://127.0.0.1:7861'"
python app.py
pause
