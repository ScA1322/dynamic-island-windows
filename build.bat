@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.bat first.
    exit /b 1
)
echo === Running tests ===
".venv\Scripts\python.exe" -m pytest || (echo Tests failed, build aborted. & exit /b 1)

if not exist "assets\icon.ico" ".venv\Scripts\python.exe" tools\make_icon.py

echo === Building with PyInstaller ===
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean DynamicIsland.spec || exit /b 1

echo.
echo Built: dist\DynamicIsland\DynamicIsland.exe
echo Copy the whole dist\DynamicIsland folder anywhere you like; Python is not required.
endlocal
