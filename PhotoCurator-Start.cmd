@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"

if "%PHOTOCURATOR_BATCH_PARSE_ONLY%"=="1" exit /b 0
if not exist "desktop_app.py" goto incomplete
if not exist "photo_curator.py" goto incomplete
if not exist ".venv\Scripts\pythonw.exe" goto install

if exist "startup-error.log" del /q "startup-error.log" >nul 2>nul

start "" ".venv\Scripts\pythonw.exe" "desktop_app.py"
timeout /t 4 /nobreak >nul

if exist "startup-error.log" goto browser
exit /b 0

:install
call "%~dp0PhotoCurator-Install.cmd"
exit /b %errorlevel%

:browser
call "%~dp0PhotoCurator-Browser.cmd"
exit /b %errorlevel%
:incomplete
echo.
echo PhotoCurator project files are incomplete.
echo Extract the entire ZIP/repository folder before running the launcher.
echo Do not run a launcher directly from inside a compressed archive.
echo.
pause
exit /b 2
