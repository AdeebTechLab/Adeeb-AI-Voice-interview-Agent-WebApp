@echo off
cd /d "%~dp0"
title Adeeb AI Meeting Agent - First-Time Setup
color 0A
call "%~dp0scripts\windows_core.bat" setup
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" echo Setup finished. You may now run START_ADEEB.bat.
pause
exit /b %CODE%
