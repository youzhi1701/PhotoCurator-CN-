@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 照片筛选 - 调试运行

if not exist ".venv\Scripts\python.exe" (
    echo 尚未完成首次安装，请先运行“一键安装并启动.bat”。
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "desktop_app.py"
echo.
echo 程序已经退出。如上方有报错，请保留此窗口内容。
pause
