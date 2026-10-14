@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 (pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (pause & exit /b 1)
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name "PastelAIStudio" --collect-submodules keyring.backends --collect-submodules pygments.lexers --hidden-import=win32cred --hidden-import=pywintypes app.py
if errorlevel 1 (pause & exit /b 1)
echo EXE generated: dist\PastelAIStudio.exe
pause
