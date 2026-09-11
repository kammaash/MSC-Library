@echo off
setlocal enabledelayedexpansion
title Montessori Digital Library
cd /d "%~dp0"

echo ============================================================
echo   Montessori Senior Secondary School - Digital Library
echo ============================================================
echo.
echo If OTHER computers or phones on the school network can't open
echo this system, run SETUP_FIREWALL.bat once (right-click it and
echo choose "Run as administrator") - that fixes the most common
echo cause on Windows. This only needs to be done once, ever.
echo.

REM ---- 1. Make sure Python is available -------------------------------
where python >nul 2>nul
if errorlevel 1 goto :no_python

for /f "delims=" %%V in ('python --version 2^>^&1') do set "PYVER_OUTPUT=%%V"
echo Using: !PYVER_OUTPUT!
echo !PYVER_OUTPUT!| findstr /C:"Python 3" >nul
if errorlevel 1 goto :bad_python
goto :venv_setup

:no_python
echo [ERROR] Python was not found on this computer.
echo Please install Python 3.10+ from https://www.python.org/downloads/
echo and make sure "Add Python to PATH" is checked during install.
pause
exit /b 1

:bad_python
echo.
echo [ERROR] The "python" command on this computer does not run real Python.
echo It printed: !PYVER_OUTPUT!
echo.
echo This usually means one of two things:
echo   1. Python was never actually installed, and Windows has a
echo      placeholder that opens the Microsoft Store instead - if a
echo      Store page opened when this script ran, that confirms it.
echo      Install Python from https://www.python.org/downloads/
echo      ^(not through the Microsoft Store^), then try again.
echo   2. Something else on this PC - PyCharm, Anaconda, or another
echo      Python install - is overriding the "python" command with
echo      its own broken/incomplete copy. Open a plain Command Prompt
echo      ^(Start menu, search "cmd" - not a tool inside PyCharm or any
echo      other program^) and type: where python
echo      That lists every copy Windows finds, in the order it tries
echo      them. The one at the TOP is the one being used.
echo.
echo NOTE: if this project folder was opened inside PyCharm at any
echo point, close PyCharm before running this file, and don't reopen
echo this folder in PyCharm - it isn't needed to run the application,
echo and it can create its own separate, conflicting Python setup
echo ^(a ".venv" folder, different from this script's "venv" folder^)
echo that causes exactly this kind of confusion.
pause
exit /b 1

:venv_setup
REM ---- 2. Create/verify the virtual environment ------------------------
REM A venv folder copied from a different computer (or a different
REM folder path on this same computer) will NOT work correctly - it has
REM machine-specific paths baked into it. Instead of just checking
REM whether the folder exists, actually try running its python.exe; if
REM that fails, delete it and build a fresh one automatically.
set "NEED_FRESH_VENV=0"
if not exist "venv\Scripts\python.exe" set "NEED_FRESH_VENV=1"
if "!NEED_FRESH_VENV!"=="0" (
    venv\Scripts\python.exe --version >nul 2>nul
    if errorlevel 1 set "NEED_FRESH_VENV=1"
)
if "!NEED_FRESH_VENV!"=="0" goto :activate_venv

if exist "venv" (
    echo Found a virtual environment that doesn't work on this computer
    echo ^(this happens if the "venv" folder was copied from another PC
    echo  or a different folder - it can't just be moved^). Rebuilding
    echo it fresh - this is normal and only takes a minute.
    rmdir /s /q venv
) else (
    echo Setting up the application for the first time, please wait...
)
python -m venv venv
if errorlevel 1 goto :venv_failed
goto :activate_venv

:venv_failed
echo.
echo [ERROR] Could not create the virtual environment.
echo The message from Python above (if any) explains why - most likely:
echo   1. This folder is somewhere Windows restricts write access, like
echo      Program Files, or inside a synced OneDrive/Google Drive folder.
echo      Move the whole project folder to somewhere simple like
echo      C:\MontessoriDigitalLibrary\ and run this file again from there.
echo   2. Antivirus software blocked it - check for a notification from
echo      your antivirus around the time this ran.
echo   3. The Python install itself is incomplete/corrupted - reinstalling
echo      Python from python.org usually fixes this.
pause
exit /b 1

:activate_venv
call venv\Scripts\activate.bat

REM ---- 3. Install/upgrade requirements (fast no-op after first run) ----
echo Checking required packages ^(first run can take a few minutes^)...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install required packages.
    echo Check your internet connection, then run this file again.
    pause
    exit /b 1
)

REM ---- 4. Wait for the server to actually be ready, THEN open browser --
REM    (fixes the "This site can't be reached" error from opening the
REM    browser too early, before Flask had finished starting up)
start "" powershell -NoProfile -WindowStyle Hidden -Command ^
    "$ok=$false; for($i=0; $i -lt 60; $i++){ try { (New-Object Net.Sockets.TcpClient('127.0.0.1',5000)).Close(); $ok=$true; break } catch { Start-Sleep -Seconds 1 } }; if ($ok) { Start-Process 'http://127.0.0.1:5000' }"

REM ---- 5. Start the application (this window shows the live server log) -
REM    If the server ever crashes mid-session (power blip, an unexpected
REM    error, etc.), automatically restart it instead of leaving the whole
REM    library down until someone notices this window closed. Capped at 5
REM    quick restarts so a genuinely broken install doesn't loop forever
REM    silently — after that it stops and waits for a person to look at it.
echo.
echo Starting the Library Management System...
echo Your browser will open automatically once the server is ready
echo ^(this can take up to a minute the first time^).
echo.
echo Leave this window open while you use the application.
echo Close this window ^(or press Ctrl+C^) to stop the server.
echo.

set "RESTART_COUNT=0"
:run_server
python app.py
set "EXIT_CODE=%errorlevel%"
if "%EXIT_CODE%"=="0" goto :server_stopped_cleanly

set /a RESTART_COUNT+=1
if %RESTART_COUNT% GEQ 5 goto :too_many_crashes
echo.
echo ============================================================
echo The server stopped unexpectedly ^(this was restart attempt
echo %RESTART_COUNT% of 5^). Restarting automatically in 3 seconds...
echo If this keeps happening, check logs\app.log for the actual
echo error and share it so the root cause can be fixed properly.
echo ============================================================
timeout /t 3 /nobreak >nul
goto :run_server

:too_many_crashes
echo.
echo ============================================================
echo The server has crashed %RESTART_COUNT% times in a row and has
echo stopped auto-restarting. Check logs\app.log for the error
echo ^(look for the most recent "FATAL" entry^) before trying again.
echo ============================================================
pause
exit /b 1

:server_stopped_cleanly
echo.
echo ============================================================
echo The server has stopped.
echo ============================================================
pause
