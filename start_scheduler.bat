@echo off
chcp 65001 >nul
cd /d "%~dp0"
set NO_PROXY=127.0.0.1,localhost,::1
call .venv\Scripts\activate
python -m astock.jobs
pause
