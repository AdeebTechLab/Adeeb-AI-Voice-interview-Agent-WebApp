@echo off
cd /d "%~dp0"
title Adeeb AI Meeting Agent - Repair
color 0E
call "%~dp0scripts\windows_core.bat" repair
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" echo Repair completed successfully.
pause
exit /b %CODE%
