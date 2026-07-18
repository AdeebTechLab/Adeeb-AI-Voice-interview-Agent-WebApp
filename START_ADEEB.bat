@echo off
cd /d "%~dp0"
title Adeeb AI Meeting Agent - Start
color 0A
call "%~dp0scripts\windows_core.bat" start
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" echo Startup command completed.
pause
exit /b %CODE%
