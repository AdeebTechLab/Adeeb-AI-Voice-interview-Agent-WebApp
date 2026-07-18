@echo off
cd /d "%~dp0"
title Adeeb AI Meeting Agent - System Check
color 0E
call "%~dp0scripts\windows_core.bat" check
set "CODE=%ERRORLEVEL%"
echo.
pause
exit /b %CODE%
