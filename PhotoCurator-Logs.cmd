@echo off
setlocal EnableExtensions
set "LOGDIR=%USERPROFILE%\.photo_curator\logs"

if "%PHOTOCURATOR_BATCH_PARSE_ONLY%"=="1" exit /b 0
if not exist "%LOGDIR%" goto no_logs
start "" explorer "%LOGDIR%"
exit /b 0

:no_logs
echo No runtime log folder exists yet.
pause
exit /b 0
