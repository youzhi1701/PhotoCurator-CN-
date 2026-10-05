@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 照片筛选 - 浏览器兼容模式

if not exist ".venv\Scripts\python.exe" (
    echo 尚未完成首次安装，请先运行“一键安装并启动.bat”。
    pause
    exit /b 1
)

echo.
echo 正在启动兼容模式...
echo 浏览器地址：http://127.0.0.1:5014
echo 关闭本窗口即可停止服务。
echo.
start "" "http://127.0.0.1:5014"
".venv\Scripts\python.exe" "photo_curator.py"
pause
