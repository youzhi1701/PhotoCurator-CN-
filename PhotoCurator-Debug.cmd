@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PHOTOCURATOR_DIAGNOSTICS=1"

if "%PHOTOCURATOR_BATCH_PARSE_ONLY%"=="1" exit /b 0
if not exist "desktop_app.py" goto incomplete
if not exist "photo_curator.py" goto incomplete
if not exist ".venv\Scripts\python.exe" goto install

".venv\Scripts\python.exe" "desktop_app.py"
echo.
echo PhotoCurator exited. Keep the messages above if an error occurred.
pause
exit /b %errorlevel%

:install
call "%~dp0PhotoCurator-Install.cmd"
exit /b %errorlevel%
:incomplete
echo.
echo PhotoCurator project files are incomplete.
echo Extract the entire ZIP/repository folder before running the launcher.
echo Do not run a launcher directly from inside a compressed archive.
echo.
pause
exit /b 2
