@echo off
setlocal
cd /d "%~dp0"

echo =======================================================
echo   Sabrina Operator Console - Debug Mode
echo =======================================================

set PYTHONDONTWRITEBYTECODE=1

set PYTHON_EXE=
if exist "..\freqtrade_pivot\.venv\Scripts\python.exe" (
    set PYTHON_EXE="..\freqtrade_pivot\.venv\Scripts\python.exe"
) else if exist ".venv\Scripts\python.exe" (
    set PYTHON_EXE=".venv\Scripts\python.exe"
) else if exist "..\freqtrade_pivot\.venv\Scripts\pythonw.exe" (
    set PYTHON_EXE="..\freqtrade_pivot\.venv\Scripts\pythonw.exe"
) else (
    set PYTHON_EXE=python.exe
)

%PYTHON_EXE% "launch_console.py"
pause
