@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"
title 照片筛选 - 环境自检

if not exist ".venv\Scripts\python.exe" (
    echo [失败] 尚未安装运行环境。
    echo 请先运行“一键安装并启动.bat”。
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   照片筛选 · 环境自检
echo ==========================================
echo.

".venv\Scripts\python.exe" -c "import sys; print('[Python]',sys.version); assert (3,9)<=sys.version_info[:2]<=(3,12)"
if errorlevel 1 goto :fail

".venv\Scripts\python.exe" -m py_compile desktop_app.py photo_curator.py photo_dedup_batch.py photo_file_organizer.py photo_ranking_engine.py photo_ranking_v3.py raw_loader.py
if errorlevel 1 goto :fail
echo [代码] Python 语法检查通过

".venv\Scripts\python.exe" -c "import flask,cv2,numpy,PIL,webview; import raw_loader; print('[依赖] 核心依赖正常'); print('[RAW]',raw_loader.HAS_RAWPY); print('[HEIC]',raw_loader.HAS_HEIF)"
if errorlevel 1 goto :fail

".venv\Scripts\python.exe" -c "import tempfile,pathlib; p=pathlib.Path(tempfile.gettempdir())/'photocurator_selfcheck.tmp'; p.write_text('ok',encoding='utf-8'); p.unlink(); print('[缓存] 系统临时目录可写')"
if errorlevel 1 goto :fail

".venv\Scripts\python.exe" "基础冒烟测试.py"
if errorlevel 1 goto :fail
echo [路径] 中文目录、特殊字符文件名和图像读取检查通过

echo.
echo [通过] 当前基础运行环境正常。
echo 如独立窗口仍有问题，请运行“调试运行.bat”。
echo.
pause
exit /b 0

:fail
echo.
echo [失败] 自检未通过，请查看上方错误。
echo 建议重新运行“一键安装并启动.bat”进行修复。
echo.
pause
exit /b 1
