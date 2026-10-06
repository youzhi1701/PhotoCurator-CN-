@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PHOTOCURATOR_OPEN_BROWSER=1"

if "%PHOTOCURATOR_BATCH_PARSE_ONLY%"=="1" exit /b 0
if not exist ".venv\Scripts\python.exe" goto install

echo.
echo Starting PhotoCurator browser compatibility mode...
echo Close this window to stop the local service.
echo.
".venv\Scripts\python.exe" "photo_curator.py"
echo.
echo PhotoCurator service stopped.
pause
exit /b 0

:install
call "%~dp0PhotoCurator-Install.cmd"
exit /b %errorlevel%
