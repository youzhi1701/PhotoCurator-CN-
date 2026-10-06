@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"

if "%PHOTOCURATOR_BATCH_PARSE_ONLY%"=="1" exit /b 0
if not exist ".venv\Scripts\pythonw.exe" goto install

if exist "startup-error.log" del /q "startup-error.log" >nul 2>nul
if exist "启动错误.log" del /q "启动错误.log" >nul 2>nul

start "" ".venv\Scripts\pythonw.exe" "desktop_app.py"
timeout /t 4 /nobreak >nul

if exist "startup-error.log" goto browser
if exist "启动错误.log" goto browser
exit /b 0

:install
call "%~dp0PhotoCurator-Install.cmd"
exit /b %errorlevel%

:browser
call "%~dp0PhotoCurator-Browser.cmd"
exit /b %errorlevel%
