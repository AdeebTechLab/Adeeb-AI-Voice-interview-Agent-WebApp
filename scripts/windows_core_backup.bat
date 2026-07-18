@echo off
setlocal EnableExtensions DisableDelayedExpansion
for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
cd /d "%PROJECT_DIR%"
set "MODE=%~1"
set "VENV_DIR=%PROJECT_DIR%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist "logs" mkdir "logs" >nul 2>&1

if /I "%MODE%"=="setup" goto :mode_setup
if /I "%MODE%"=="start" goto :mode_start
if /I "%MODE%"=="repair" goto :mode_repair
if /I "%MODE%"=="check" goto :mode_check
if /I "%MODE%"=="install-cloudflare" goto :mode_install_cloudflare

echo ERROR: Unknown launcher mode "%MODE%".
exit /b 2

:header
echo.
echo ============================================================
echo   Adeeb AI Meeting Agent v15.5.2 - %~1
echo ============================================================
echo Project folder: %PROJECT_DIR%
echo.
exit /b 0

:validate_project
if not exist "%PROJECT_DIR%\app.py" (
  echo ERROR: app.py was not found.
  echo Extract the complete ZIP to a normal folder before running it.
  exit /b 1
)
if not exist "%PROJECT_DIR%\requirements.txt" (
  echo ERROR: requirements.txt was not found.
  exit /b 1
)
if not exist "%PS_EXE%" (
  echo ERROR: Windows PowerShell was not found.
  echo This project requires Windows 10 or Windows 11.
  exit /b 1
)
exit /b 0

:ensure_env
if exist "%PROJECT_DIR%\.env" (
  echo [OK] Existing .env found; it was not overwritten.
  exit /b 0
)
copy /y "%PROJECT_DIR%\.env.example" "%PROJECT_DIR%\.env" >nul
if errorlevel 1 (
  echo ERROR: Could not create .env from .env.example.
  exit /b 1
)
echo [OK] Created .env from .env.example
exit /b 0

:check_venv
if not exist "%VENV_PY%" exit /b 1
"%VENV_PY%" -c "import sys,struct; assert (3,10) <= sys.version_info[:2] < (3,14); assert struct.calcsize('P')*8==64" >nul 2>&1
exit /b %ERRORLEVEL%

:ensure_venv
call :check_venv
if not errorlevel 1 (
  echo [OK] Private Python environment is ready.
  "%VENV_PY%" -c "import sys; print('     Python', sys.version.split()[0], '64-bit')"
  exit /b 0
)

if exist "%VENV_DIR%" (
  echo Existing .venv is incomplete or incompatible. Rebuilding it automatically...
  rmdir /s /q "%VENV_DIR%"
  if exist "%VENV_DIR%" (
    echo ERROR: Could not remove .venv.
    echo Close all Adeeb and Python windows, then run REPAIR_ADEEB.bat.
    exit /b 1
  )
)

call :select_python
if errorlevel 1 exit /b 1

echo Creating private environment with %PY_LABEL%...
if defined PY_SWITCH goto :create_venv_with_switch
"%PY_EXE%" -m venv "%VENV_DIR%"
goto :after_create_venv

:create_venv_with_switch
"%PY_EXE%" %PY_SWITCH% -m venv "%VENV_DIR%"

:after_create_venv
if errorlevel 1 (
  echo ERROR: Python could not create the virtual environment.
  echo Try running REPAIR_ADEEB.bat after closing all Python windows.
  exit /b 1
)
call :check_venv
if errorlevel 1 (
  echo ERROR: The new virtual environment is not usable.
  exit /b 1
)
echo [OK] Private Python environment created.
exit /b 0

:select_python
set "PY_EXE="
set "PY_SWITCH="
set "PY_LABEL="

where py.exe >nul 2>&1
if errorlevel 1 goto :try_python_path
py.exe -3.13 -c "import sys,struct; assert struct.calcsize('P')*8==64" >nul 2>&1
if not errorlevel 1 (
  set "PY_EXE=py.exe"
  set "PY_SWITCH=-3.13"
  set "PY_LABEL=Python 3.13 64-bit"
  goto :python_selected
)
py.exe -3.12 -c "import sys,struct; assert struct.calcsize('P')*8==64" >nul 2>&1
if not errorlevel 1 (
  set "PY_EXE=py.exe"
  set "PY_SWITCH=-3.12"
  set "PY_LABEL=Python 3.12 64-bit"
  goto :python_selected
)
py.exe -3.11 -c "import sys,struct; assert struct.calcsize('P')*8==64" >nul 2>&1
if not errorlevel 1 (
  set "PY_EXE=py.exe"
  set "PY_SWITCH=-3.11"
  set "PY_LABEL=Python 3.11 64-bit"
  goto :python_selected
)
py.exe -3.10 -c "import sys,struct; assert struct.calcsize('P')*8==64" >nul 2>&1
if not errorlevel 1 (
  set "PY_EXE=py.exe"
  set "PY_SWITCH=-3.10"
  set "PY_LABEL=Python 3.10 64-bit"
  goto :python_selected
)

:try_python_path
where python.exe >nul 2>&1
if errorlevel 1 goto :try_python3_path
python.exe -c "import sys,struct; assert (3,10) <= sys.version_info[:2] < (3,14); assert struct.calcsize('P')*8==64" >nul 2>&1
if not errorlevel 1 (
  set "PY_EXE=python.exe"
  set "PY_LABEL=64-bit Python from PATH"
  goto :python_selected
)

:try_python3_path
where python3.exe >nul 2>&1
if errorlevel 1 goto :try_common_python_paths
python3.exe -c "import sys,struct; assert (3,10) <= sys.version_info[:2] < (3,14); assert struct.calcsize('P')*8==64" >nul 2>&1
if not errorlevel 1 (
  set "PY_EXE=python3.exe"
  set "PY_LABEL=64-bit Python from PATH"
  goto :python_selected
)

:try_common_python_paths
call :try_python_file "%LocalAppData%\Programs\Python\Python313\python.exe"
if not errorlevel 1 goto :python_selected
call :try_python_file "%LocalAppData%\Programs\Python\Python312\python.exe"
if not errorlevel 1 goto :python_selected
call :try_python_file "%LocalAppData%\Programs\Python\Python311\python.exe"
if not errorlevel 1 goto :python_selected
call :try_python_file "%LocalAppData%\Programs\Python\Python310\python.exe"
if not errorlevel 1 goto :python_selected
call :try_python_file "%ProgramFiles%\Python313\python.exe"
if not errorlevel 1 goto :python_selected
call :try_python_file "%ProgramFiles%\Python312\python.exe"
if not errorlevel 1 goto :python_selected
call :try_python_file "%ProgramFiles%\Python311\python.exe"
if not errorlevel 1 goto :python_selected
call :try_python_file "%ProgramFiles%\Python310\python.exe"
if not errorlevel 1 goto :python_selected
call :try_python_file "C:\Python313\python.exe"
if not errorlevel 1 goto :python_selected
call :try_python_file "C:\Python312\python.exe"
if not errorlevel 1 goto :python_selected
call :try_python_file "C:\Python311\python.exe"
if not errorlevel 1 goto :python_selected
call :try_python_file "C:\Python310\python.exe"
if not errorlevel 1 goto :python_selected

echo ERROR: Compatible 64-bit Python was not found.
echo Install 64-bit Python 3.11, 3.12, or 3.13 from python.org.
echo During installation tick "Add python.exe to PATH" and "Install launcher for all users".
exit /b 1

:try_python_file
if not exist "%~1" exit /b 1
"%~1" -c "import sys,struct; assert (3,10) <= sys.version_info[:2] < (3,14); assert struct.calcsize('P')*8==64" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY_EXE=%~1"
set "PY_SWITCH="
set "PY_LABEL=64-bit Python at %~1"
exit /b 0

:python_selected
echo [OK] Selected %PY_LABEL%.
if defined PY_SWITCH goto :print_selected_with_switch
"%PY_EXE%" -c "import sys,struct; print('     Detected Python',sys.version.split()[0],str(struct.calcsize('P')*8)+'-bit')"
exit /b %ERRORLEVEL%

:print_selected_with_switch
"%PY_EXE%" %PY_SWITCH% -c "import sys,struct; print('     Detected Python',sys.version.split()[0],str(struct.calcsize('P')*8)+'-bit')"
exit /b %ERRORLEVEL%

:set_lock_file
set "LOCK_FILE="
"%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,13) else 1)" >nul 2>&1
if not errorlevel 1 set "LOCK_FILE=%PROJECT_DIR%\requirements-lock-py313.txt"
if defined LOCK_FILE goto :lock_selected
"%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) else 1)" >nul 2>&1
if not errorlevel 1 set "LOCK_FILE=%PROJECT_DIR%\requirements-lock-py312.txt"
if defined LOCK_FILE goto :lock_selected
"%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)" >nul 2>&1
if not errorlevel 1 set "LOCK_FILE=%PROJECT_DIR%\requirements-lock-py311.txt"
if defined LOCK_FILE goto :lock_selected
"%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "LOCK_FILE=%PROJECT_DIR%\requirements-lock-py310.txt"

:lock_selected
if not defined LOCK_FILE (
  echo ERROR: No dependency lock exists for this Python version.
  exit /b 1
)
if not exist "%LOCK_FILE%" (
  echo ERROR: Dependency lock file was not found: %LOCK_FILE%
  echo Extract the complete ZIP again.
  exit /b 1
)
exit /b 0

:packages_ready
call :set_lock_file
if errorlevel 1 exit /b 1
"%VENV_PY%" "%PROJECT_DIR%\scripts\check_dependencies.py" "%LOCK_FILE%" >nul 2>&1
if errorlevel 1 exit /b 1
"%VENV_PY%" -c "import fastapi,uvicorn,faster_whisper,ctranslate2,cryptography,multipart,httpx,jinja2,pypdf,edge_tts" >nul 2>&1
exit /b %ERRORLEVEL%

:ensure_packages
call :set_lock_file
if errorlevel 1 exit /b 1
call :packages_ready
if not errorlevel 1 (
  echo [OK] Required Python packages are already installed.
  exit /b 0
)

echo Installing required packages. This can take several minutes on first setup...
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel --disable-pip-version-check
if errorlevel 1 (
  echo ERROR: pip could not be prepared.
  exit /b 1
)
"%VENV_PY%" -m pip install --prefer-binary --only-binary=:all: -r "%LOCK_FILE%"
if errorlevel 1 (
  echo Binary-only installation was not available for every package. Trying normal installation...
  "%VENV_PY%" -m pip install --prefer-binary -r "%LOCK_FILE%"
)
if errorlevel 1 (
  echo ERROR: Package installation failed.
  echo Check the internet connection, antivirus, proxy, and available disk space.
  exit /b 1
)
call :packages_ready
if errorlevel 1 (
  echo ERROR: Packages installed but one or more imports still fail.
  exit /b 1
)
echo [OK] All Python packages are ready and fully version-locked.
echo      Lock: %LOCK_FILE%
"%VENV_PY%" -c "import ctranslate2; print('     CTranslate2', ctranslate2.__version__)"
exit /b 0

:generate_secrets
"%VENV_PY%" "%PROJECT_DIR%\scripts\generate_security_secrets.py" --env "%PROJECT_DIR%\.env"
if errorlevel 1 (
  echo ERROR: Security secrets could not be generated.
  exit /b 1
)
echo [OK] Security secrets are present.
exit /b 0

:health_check
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\check_health.ps1" -Seconds 3 >nul 2>&1
exit /b %ERRORLEVEL%

:wait_for_health
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\check_health.ps1" -Seconds 120
exit /b %ERRORLEVEL%

:find_cloudflared
set "CLOUDFLARED="
if exist "%PROJECT_DIR%\tools\cloudflared.exe" set "CLOUDFLARED=%PROJECT_DIR%\tools\cloudflared.exe"
if defined CLOUDFLARED exit /b 0
for /f "delims=" %%I in ('where cloudflared.exe 2^>nul') do if not defined CLOUDFLARED set "CLOUDFLARED=%%I"
if defined CLOUDFLARED exit /b 0
if exist "%LocalAppData%\Microsoft\WinGet\Links\cloudflared.exe" set "CLOUDFLARED=%LocalAppData%\Microsoft\WinGet\Links\cloudflared.exe"
if defined CLOUDFLARED exit /b 0
if exist "%ProgramFiles%\cloudflared\cloudflared.exe" set "CLOUDFLARED=%ProgramFiles%\cloudflared\cloudflared.exe"
if defined CLOUDFLARED exit /b 0
if exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" set "CLOUDFLARED=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
if defined CLOUDFLARED exit /b 0
exit /b 1

:install_cloudflared
call :find_cloudflared
if not errorlevel 1 (
  echo [OK] Cloudflared found: %CLOUDFLARED%
  exit /b 0
)
echo Downloading Cloudflared into this project. No administrator access is required...
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\download_cloudflared.ps1"
if errorlevel 1 exit /b 1
call :find_cloudflared
if errorlevel 1 (
  echo ERROR: Cloudflared download finished but the file was not found.
  exit /b 1
)
echo [OK] Cloudflared ready: %CLOUDFLARED%
exit /b 0

:mode_setup
call :header "First-Time Setup"
call :validate_project
if errorlevel 1 goto :failed
call :ensure_env
if errorlevel 1 goto :failed
call :ensure_venv
if errorlevel 1 goto :failed
call :ensure_packages
if errorlevel 1 goto :failed
call :generate_secrets
if errorlevel 1 goto :failed
call :install_cloudflared
if errorlevel 1 (
  echo.
  echo WARNING: Cloudflared could not be downloaded.
  echo Local Adeeb will still work. Run INSTALL_CLOUDFLARE.bat later for a mobile link.
)
echo.
echo ============================================================
echo SETUP COMPLETED SUCCESSFULLY
echo.
echo Before company use, open .env and set:
echo   ADMIN_PASSWORD=your-strong-password
echo   GROQ_API_KEY=your-key   ^(recommended for the LLM brain^)
echo.
echo Then double-click START_ADEEB.bat.
echo ============================================================
exit /b 0

:mode_repair
call :header "Repair"
call :validate_project
if errorlevel 1 goto :failed
call :health_check
if not errorlevel 1 (
  echo ERROR: Adeeb is currently running.
  echo Close the Adeeb Local Server window before repairing.
  goto :failed
)
if exist "%VENV_DIR%" (
  echo Removing only the private .venv folder...
  rmdir /s /q "%VENV_DIR%"
  if exist "%VENV_DIR%" (
    echo ERROR: .venv is locked. Close all Python and Adeeb windows and try again.
    goto :failed
  )
)
call :ensure_env
if errorlevel 1 goto :failed
call :ensure_venv
if errorlevel 1 goto :failed
call :ensure_packages
if errorlevel 1 goto :failed
call :generate_secrets
if errorlevel 1 goto :failed
echo.
echo [OK] Repair completed. Candidate data and .env were not deleted.
exit /b 0

:mode_start
call :header "Start"
call :validate_project
if errorlevel 1 goto :failed
call :ensure_env
if errorlevel 1 goto :failed
call :ensure_venv
if errorlevel 1 goto :failed
call :ensure_packages
if errorlevel 1 goto :failed
call :generate_secrets
if errorlevel 1 goto :failed

call :health_check
if not errorlevel 1 goto :server_ready

netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo ERROR: Port 8000 is already used by another program, but Adeeb health check failed.
  echo Close the other program or restart Windows, then run START_ADEEB.bat again.
  goto :failed
)

echo Starting the local Adeeb server in a separate window...
start "Adeeb Local Server" /D "%PROJECT_DIR%" "%ComSpec%" /D /C call "scripts\run_server.bat"
call :wait_for_health
if errorlevel 1 (
  echo.
  echo ERROR: Adeeb did not become ready within 120 seconds.
  echo Read the Adeeb Local Server window and logs\server.log.
  goto :failed
)

:server_ready
echo [OK] Adeeb server is healthy at http://127.0.0.1:8000
start "" "http://127.0.0.1:8000"

call :find_cloudflared
if errorlevel 1 (
  echo.
  echo Cloudflared is not installed, so the local admin panel is running but no public mobile link was created.
  echo Run INSTALL_CLOUDFLARE.bat once, then run START_ADEEB.bat again.
  exit /b 0
)

if exist "%PROJECT_DIR%\CURRENT_PUBLIC_LINK.txt" del /q "%PROJECT_DIR%\CURRENT_PUBLIC_LINK.txt" >nul 2>&1
echo Starting the secure Cloudflare mobile tunnel...
start "Adeeb Cloudflare Tunnel" "%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\start_cloudflare_tunnel.ps1" -CloudflaredPath "%CLOUDFLARED%"
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\wait_for_public_link.ps1" -Seconds 60 >nul 2>&1
if errorlevel 1 (
  echo.
  echo WARNING: The local server is running, but the public link was not created within 60 seconds.
  echo Check the Adeeb Cloudflare Tunnel window and your internet connection.
  exit /b 0
)
echo [OK] Public links were saved to CURRENT_PUBLIC_LINK.txt.
echo Keep both Adeeb windows open while interviews are running.
exit /b 0

:mode_install_cloudflare
call :header "Cloudflare Installer"
call :validate_project
if errorlevel 1 goto :failed
call :install_cloudflared
if errorlevel 1 goto :failed
echo.
echo [OK] Cloudflared installation completed.
exit /b 0

:mode_check
call :header "System Check"
call :validate_project
if errorlevel 1 goto :failed
if exist "%PROJECT_DIR%\.env" (echo [OK] .env exists) else (echo [MISSING] .env - run FIRST_TIME_SETUP.bat)
call :select_python
if errorlevel 1 (echo [MISSING] Supported 64-bit Python) else (echo [OK] System Python detected)
call :check_venv
if errorlevel 1 (echo [MISSING OR BROKEN] .venv - run REPAIR_ADEEB.bat) else (echo [OK] .venv is valid)
if exist "%VENV_PY%" (
  call :packages_ready
  if errorlevel 1 (echo [MISSING] Python packages - run REPAIR_ADEEB.bat) else (echo [OK] Python packages import correctly)
  "%VENV_PY%" -m py_compile "%PROJECT_DIR%\app.py" "%PROJECT_DIR%\identity.py" >nul 2>&1
  if errorlevel 1 (echo [PROBLEM] Python source syntax check failed) else (echo [OK] Python source syntax is valid)
)
call :find_cloudflared
if errorlevel 1 (echo [MISSING FOR MOBILE] Run INSTALL_CLOUDFLARE.bat) else (echo [OK] Cloudflared found: %CLOUDFLARED%)
call :health_check
if errorlevel 1 (echo [INFO] Adeeb server is not currently running) else (echo [OK] Adeeb server health check passed)
echo.
echo System check completed.
exit /b 0

:failed
set "FAIL_CODE=%ERRORLEVEL%"
if "%FAIL_CODE%"=="0" set "FAIL_CODE=1"
(
  echo Startup or setup failed at %DATE% %TIME%
  echo Project: %PROJECT_DIR%
  echo Mode: %MODE%
  echo Exit code: %FAIL_CODE%
) > "%PROJECT_DIR%\logs\last_error.txt"
echo.
echo ============================================================
echo OPERATION FAILED. Read the error above.
echo A marker was saved to logs\last_error.txt
echo ============================================================
exit /b %FAIL_CODE%
