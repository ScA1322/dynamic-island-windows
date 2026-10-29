@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Run setup.bat first.
    exit /b 1
)
rem pythonw = no console window. For a console with logs use:  .venv\Scripts\python.exe main.py --debug
start "" ".venv\Scripts\pythonw.exe" main.py %*
