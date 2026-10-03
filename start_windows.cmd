@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup_windows.ps1 first. Python 3.12 is required.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -c "import sys; assert sys.version_info[:2] == (3,12), 'Rebuild .venv with Python 3.12'"
if errorlevel 1 (
    pause
    exit /b 1
)
".venv\Scripts\python.exe" main.py
set "ERA_EXIT_CODE=%ERRORLEVEL%"
if not "%ERA_EXIT_CODE%"=="0" (
    echo Era exited with code %ERA_EXIT_CODE%. Review logs\era.log and run tools\era_doctor.py.
    pause
)
exit /b %ERA_EXIT_CODE%
