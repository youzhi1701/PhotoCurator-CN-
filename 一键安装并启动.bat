@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 照片筛选 - 首次安装并启动
set PYTHONUTF8=1
set PIP_DISABLE_PIP_VERSION_CHECK=1

echo.
echo ==========================================
echo   照片筛选 · PhotoCurator 中文桌面版
echo   首次安装 / 修复依赖 / 启动
echo ==========================================
echo.

set "PYEXE="
set "PYARG="
where py >nul 2>nul
if %errorlevel%==0 (
    set "PYEXE=py"
    set "PYARG=-3"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PYEXE=python"
    )
)

if not defined PYEXE (
    echo [错误] 未检测到 Python。
    echo 请先安装 Python 3.9 或更高版本，并勾选“Add Python to PATH”。
    echo 安装完成后重新双击本文件。
    echo.
    pause
    exit /b 1
)

echo [1/4] 检查 Python...
%PYEXE% %PYARG% -c "import sys; print('Python', sys.version.split()[0]); assert sys.version_info >= (3,9), '需要 Python 3.9+'"
if errorlevel 1 (
    echo.
    echo [错误] Python 版本过低，需要 Python 3.9 或更高版本。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [2/4] 正在创建独立运行环境 .venv ...
    %PYEXE% %PYARG% -m venv ".venv"
    if errorlevel 1 goto :fail
) else (
    echo.
    echo [2/4] 已检测到现有独立运行环境。
)

echo.
echo [3/4] 正在安装/更新运行依赖...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo [4/4] 安装完成，正在启动独立窗口...
if exist "启动错误.log" del /q "启动错误.log" >nul 2>nul
start "" ".venv\Scripts\pythonw.exe" "desktop_app.py"
timeout /t 2 /nobreak >nul
exit /b 0

:fail
echo.
echo [安装失败] 请保留本窗口中的错误信息。
echo 你也可以稍后重新运行“一键安装并启动.bat”自动修复。
echo.
pause
exit /b 1
