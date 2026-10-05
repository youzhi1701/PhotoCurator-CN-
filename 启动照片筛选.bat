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
timeout /t 3 /nobreak >nul

if exist "启动错误.log" (
    echo.
    echo [提示] 独立桌面窗口启动失败，正在自动切换到浏览器兼容模式...
    echo 如需查看原因，可运行“打开日志文件夹.bat”或查看“启动错误.log”。
    echo.
    call "浏览器兼容模式.bat"
)
exit /b 0
