@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"

if "%PHOTOCURATOR_BATCH_PARSE_ONLY%"=="1" exit /b 0
if not exist ".venv\Scripts\python.exe" goto install

".venv\Scripts\python.exe" "desktop_app.py"
echo.
echo PhotoCurator exited. Keep the messages above if an error occurred.
pause
exit /b %errorlevel%

:install
call "%~dp0PhotoCurator-Install.cmd"
exit /b %errorlevel%
