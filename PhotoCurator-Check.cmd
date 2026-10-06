@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"

if "%PHOTOCURATOR_BATCH_PARSE_ONLY%"=="1" exit /b 0
if not exist ".venv\Scripts\python.exe" goto no_env

".venv\Scripts\python.exe" -m py_compile desktop_app.py photo_curator.py photo_dedup_batch.py photo_file_organizer.py photo_ranking_engine.py photo_ranking_v3.py raw_loader.py "基础冒烟测试.py" "Codespaces冒烟测试.py"
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -c "import flask,cv2,numpy,PIL,webview; import raw_loader; print('Runtime OK')"
if errorlevel 1 goto fail
".venv\Scripts\python.exe" "基础冒烟测试.py"
if errorlevel 1 goto fail

echo.
echo PhotoCurator environment check passed.
pause
exit /b 0

:no_env
echo Runtime environment is not installed yet.
echo Run PhotoCurator-Install.cmd first.
pause
exit /b 1

:fail
echo.
echo Environment check failed.
echo Run PhotoCurator-Install.cmd to repair.
pause
exit /b 1
