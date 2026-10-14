@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -u "%~dp0run_wechat_ai.py"
if errorlevel 1 pause
