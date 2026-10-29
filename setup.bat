@echo off
setlocal
cd /d "%~dp0"
echo === Dynamic Island: installing dependencies ===

set "PY="
where py >nul 2>nul && py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3.12+ was not found. Install it from https://www.python.org/downloads/ and re-run.
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    %PY% -m venv .venv || exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip || exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt || exit /b 1
if not exist "assets\icon.ico" ".venv\Scripts\python.exe" tools\make_icon.py
echo.
echo Done. Run start.bat to launch, build.bat to build the .exe.
endlocal
