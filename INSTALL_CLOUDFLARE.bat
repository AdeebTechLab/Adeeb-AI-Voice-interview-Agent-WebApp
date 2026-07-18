@echo off
cd /d "%~dp0"
title Adeeb AI Meeting Agent - Install Cloudflare
color 0B
call "%~dp0scripts\windows_core.bat" install-cloudflare
set "CODE=%ERRORLEVEL%"
echo.
pause
exit /b %CODE%
