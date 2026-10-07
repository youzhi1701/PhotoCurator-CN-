@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"

if "%PHOTOCURATOR_BATCH_PARSE_ONLY%"=="1" exit /b 0
if not exist "desktop_app.py" goto incomplete
if not exist "photo_curator.py" goto incomplete
if not exist ".venv\Scripts\python.exe" goto no_env

".venv\Scripts\python.exe" -m py_compile desktop_app.py photo_curator.py photo_dedup_batch.py photo_file_organizer.py photo_ranking_engine.py photo_ranking_v3.py raw_loader.py release_gate_test.py windows_smoke_test.py
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -c "import flask,cv2,numpy,PIL,webview; import raw_loader; print('Runtime OK')"
if errorlevel 1 goto fail
".venv\Scripts\python.exe" "release_gate_test.py"\nif errorlevel 1 goto fail\n".venv\Scripts\python.exe" "windows_smoke_test.py"\nif errorlevel 1 goto fail

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
:incomplete
echo.
echo PhotoCurator project files are incomplete.
echo Extract the entire ZIP/repository folder before running the launcher.
echo Do not run a launcher directly from inside a compressed archive.
echo.
pause
exit /b 2
