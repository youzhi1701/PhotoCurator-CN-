@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title 照片筛选 - 首次安装并启动
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

echo.
echo ==========================================
echo   照片筛选 · PhotoCurator 中文桌面版
echo   首次安装 / 修复依赖 / 启动
echo ==========================================
echo.

set "PY_CMD="

where py >nul 2>nul
if errorlevel 1 goto :try_python

call :try_py 3.11
call :try_py 3.12
call :try_py 3.10
call :try_py 3.9
if defined PY_CMD goto :python_found

:try_python
where python >nul 2>nul
if errorlevel 1 goto :no_python
python -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] <= (3,12) else 1)" >nul 2>nul
if errorlevel 1 goto :bad_python
set "PY_CMD=python"

:python_found
echo [1/5] 使用兼容 Python：
%PY_CMD% -c "import sys; print('      Python', sys.version.split()[0], '-', sys.executable)"
if errorlevel 1 goto :fail

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] <= (3,12) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo [2/5] 现有 .venv Python 版本不兼容，正在重建...
        rmdir /s /q ".venv"
    ) else (
        echo [2/5] 已检测到可用的独立运行环境。
    )
) else (
    echo [2/5] 尚未创建独立运行环境。
)

if not exist ".venv\Scripts\python.exe" (
    echo [3/5] 正在创建 .venv 独立运行环境...
    %PY_CMD% -m venv ".venv"
    if errorlevel 1 goto :fail
) else (
    echo [3/5] 无需重新创建 .venv。
)

echo [4/5] 正在安装/修复运行依赖...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [5/5] 依赖检查...
".venv\Scripts\python.exe" -c "import flask,cv2,numpy,PIL,webview; import raw_loader; print('      核心依赖正常'); print('      RAW支持:', raw_loader.HAS_RAWPY); print('      HEIC支持:', raw_loader.HAS_HEIF)"
if errorlevel 1 goto :fail

echo.
echo 安装完成，正在启动独立窗口...
if exist "启动错误.log" del /q "启动错误.log" >nul 2>nul
start "" ".venv\Scripts\pythonw.exe" "desktop_app.py"
timeout /t 3 /nobreak >nul

if exist "启动错误.log" (
    echo.
    echo [启动失败] 已生成“启动错误.log”：
    type "启动错误.log"
    echo.
    pause
    exit /b 1
)

echo.
echo 已启动。以后直接双击“启动照片筛选.bat”即可。
timeout /t 2 /nobreak >nul
exit /b 0

:try_py
if defined PY_CMD exit /b 0
py -%1 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == tuple(map(int,'%1'.split('.'))) else 1)" >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -%1"
exit /b 0

:no_python
echo [错误] 未检测到兼容的 Python。
echo 推荐安装 Python 3.11 64 位，然后重新运行本文件。
echo 支持版本：Python 3.9 - 3.12。
echo.
pause
exit /b 1

:bad_python
echo [错误] 检测到了 Python，但版本不在当前兼容范围内。
python -c "import sys; print('当前版本：',sys.version)"
echo.
echo 当前项目建议使用 Python 3.11 64 位。
echo 支持版本：Python 3.9 - 3.12。
echo 安装兼容版本后不需要卸载现有 Python。
echo.
pause
exit /b 1

:fail
echo.
echo ==========================================
echo [安装/修复失败]
echo 请保留本窗口中的错误信息。
echo 也可以把“启动错误.log”发给我继续排查。
echo ==========================================
echo.
pause
exit /b 1
