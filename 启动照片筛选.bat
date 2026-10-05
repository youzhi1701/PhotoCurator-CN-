@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo 尚未完成首次安装，正在转到安装程序...
    call "一键安装并启动.bat"
    exit /b
)

if exist "启动错误.log" del /q "启动错误.log" >nul 2>nul
start "" ".venv\Scripts\pythonw.exe" "desktop_app.py"
exit /b 0
