@echo off
setlocal
:: Canonical unified launcher delegation
cd /d "%~dp0\.."
call "Launch_Sabrina_Console.bat" %*
exit /b %ERRORLEVEL%
