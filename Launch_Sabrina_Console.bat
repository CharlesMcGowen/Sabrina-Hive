@echo off
setlocal
cd /d "%~dp0"

echo =======================================================
echo   Sabrina Operator Console - Starting Fresh Instance
echo =======================================================

:: Always load latest .py source code without stale pyc caches
set PYTHONDONTWRITEBYTECODE=1

:: Synchronize UI assets to guarantee latest HTML/CSS/JS
if exist "sabrina_console_desktop\sabrina_desktop_ui.html" (
    copy /y "sabrina_console_desktop\sabrina_desktop_ui.html" "sabrina_desktop_ui.html" >nul 2>&1
)

:: Locate virtual environment pythonw or python
set "PYTHON_EXE="
if exist "%~dp0freqtrade_pivot\.venv\Scripts\pythonw.exe" (
    set "PYTHON_EXE=%~dp0freqtrade_pivot\.venv\Scripts\pythonw.exe"
) else if exist "%~dp0sabrina_console_desktop\.venv\Scripts\pythonw.exe" (
    set "PYTHON_EXE=%~dp0sabrina_console_desktop\.venv\Scripts\pythonw.exe"
) else if exist "%~dp0freqtrade_pivot\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0freqtrade_pivot\.venv\Scripts\python.exe"
) else (
    set "PYTHON_EXE=pythonw.exe"
)

:: Launch the Console with its normal Ego connection
start "" "%PYTHON_EXE%" "%~dp0sabrina_console_desktop\launch_console.py" %*
exit /b 0
