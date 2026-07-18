@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
cd /d "%PROJECT_DIR%"
title Adeeb Local Server
color 0A
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "VENV_PY=%PROJECT_DIR%\.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo ERROR: The private Python environment was not found.
  echo Run FIRST_TIME_SETUP.bat or REPAIR_ADEEB.bat first.
  echo.
  pause
  exit /b 1
)

"%VENV_PY%" "%PROJECT_DIR%\scripts\run_server.py"
set "CODE=%ERRORLEVEL%"
echo.
if "%CODE%"=="0" (
  echo Adeeb server stopped normally.
) else (
  echo Adeeb server stopped with exit code %CODE%.
  echo Read logs\server.log for the complete error.
)
echo.
pause
exit /b %CODE%
