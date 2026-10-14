@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 (
        echo Python 3 is required. Download it from https://www.python.org/downloads/windows/
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 (
    echo Program terminated with an error.
    pause
)
