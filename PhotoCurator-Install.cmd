@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PY_CMD="

if "%PHOTOCURATOR_BATCH_PARSE_ONLY%"=="1" exit /b 0
if not exist "requirements.txt" goto incomplete
if not exist "desktop_app.py" goto incomplete
if not exist "photo_curator.py" goto incomplete

echo.
echo ==========================================
echo PhotoCurator - Install / Repair / Start
echo ==========================================
echo.

call :find_python
if not defined PY_CMD goto no_python

echo [1/5] Python runtime
%PY_CMD% -c "import sys,struct; print('Python',sys.version.split()[0],str(struct.calcsize('P')*8)+'bit',sys.executable)"
if errorlevel 1 goto fail

if not exist ".venv\Scripts\python.exe" goto create_venv
".venv\Scripts\python.exe" -c "import sys,struct; raise SystemExit(0 if (3,9)<=sys.version_info[:2]<=(3,12) and struct.calcsize('P')*8==64 else 1)" >nul 2>nul
if not errorlevel 1 goto venv_ready
echo [2/5] Rebuilding incompatible virtual environment...
rmdir /s /q ".venv"

:create_venv
echo [2/5] Creating virtual environment...
%PY_CMD% -m venv ".venv"
if errorlevel 1 goto fail

:venv_ready
echo [3/5] Installing core dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail

if not exist "requirements-optional.txt" goto verify
echo [4/5] Installing RAW / HEIC support...
".venv\Scripts\python.exe" -m pip install -r requirements-optional.txt
if errorlevel 1 echo Optional RAW / HEIC dependencies failed; core JPG/PNG/WebP support remains available.

:verify
echo [5/5] Verifying runtime...
".venv\Scripts\python.exe" -c "import flask,cv2,numpy,PIL,webview; import raw_loader; print('Core runtime OK'); print('RAW:',raw_loader.HAS_RAWPY); print('HEIC:',raw_loader.HAS_HEIF)"
if errorlevel 1 goto fail

echo.
echo Installation finished. Starting PhotoCurator...
call "%~dp0PhotoCurator-Start.cmd"
exit /b %errorlevel%

:find_python
where py >nul 2>nul
if errorlevel 1 goto try_python
py -3.11 -c "import struct; raise SystemExit(0 if struct.calcsize('P')*8==64 else 1)" >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3.11"
if defined PY_CMD exit /b 0
py -3.12 -c "import struct; raise SystemExit(0 if struct.calcsize('P')*8==64 else 1)" >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3.12"
if defined PY_CMD exit /b 0
py -3.10 -c "import struct; raise SystemExit(0 if struct.calcsize('P')*8==64 else 1)" >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3.10"
if defined PY_CMD exit /b 0
py -3.9 -c "import struct; raise SystemExit(0 if struct.calcsize('P')*8==64 else 1)" >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3.9"
if defined PY_CMD exit /b 0

:try_python
where python >nul 2>nul
if errorlevel 1 exit /b 0
python -c "import sys,struct; raise SystemExit(0 if (3,9)<=sys.version_info[:2]<=(3,12) and struct.calcsize('P')*8==64 else 1)" >nul 2>nul
if not errorlevel 1 set "PY_CMD=python"
exit /b 0

:no_python
echo.
echo No compatible 64-bit Python was found.
echo Install Python 3.11 x64, then run this file again.
echo Supported versions: Python 3.10 - 3.12.
echo.
pause
exit /b 1

:fail
echo.
echo Installation or repair failed.
echo Run PhotoCurator-Debug.cmd or inspect startup-error.log.
echo.
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
