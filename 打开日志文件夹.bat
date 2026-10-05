@echo off
chcp 65001 >nul
setlocal
set "LOGDIR=%USERPROFILE%\.photo_curator\logs"
if not exist "%LOGDIR%" (
    echo 当前还没有运行日志。
    echo 日志会在程序实际运行后自动生成。
    echo 预期位置：%LOGDIR%
    pause
    exit /b 0
)
start "" explorer "%LOGDIR%"
exit /b 0
