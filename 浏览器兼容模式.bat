@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 照片筛选 - 浏览器兼容模式
set "PYTHONUTF8=1"
set "PHOTOCURATOR_OPEN_BROWSER=1"

if not exist ".venv\Scripts\python.exe" (
    echo 尚未完成首次安装，请先运行“一键安装并启动.bat”。
    pause
    exit /b 1
)

echo.
echo 正在启动浏览器兼容模式...
echo 本地地址：http://127.0.0.1:5014
echo 浏览器会在服务启动后自动打开。
echo 关闭本窗口即可停止兼容模式服务。
echo.
".venv\Scripts\python.exe" "photo_curator.py"

echo.
echo 服务已退出。
pause
