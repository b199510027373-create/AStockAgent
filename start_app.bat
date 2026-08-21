@echo off
chcp 65001 >nul
cd /d %~dp0
call .venv\Scripts\activate
start "" http://127.0.0.1:7861
python app.py
pause