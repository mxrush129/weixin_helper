@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failure
)
".venv\Scripts\python.exe" -c "import uiautomation, PIL" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto failure
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0app.py"
if errorlevel 1 goto failure
exit /b 0
:failure
echo Could not start WeChat Helper. See the error above.
pause
exit /b 1
