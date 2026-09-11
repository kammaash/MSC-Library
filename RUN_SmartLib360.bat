@echo off
setlocal enabledelayedexpansion
title Smart-Lib 360
cd /d "%~dp0"

if not exist "MontessoriDigitalLibrary.exe" goto :no_exe

echo ============================================================
echo   Smart-Lib 360 is starting...
echo ============================================================
echo.
echo Leave this window open while the library is in use.
echo Close this window to stop the server.
echo.

set "RESTART_COUNT=0"
:run_app
MontessoriDigitalLibrary.exe
set "EXIT_CODE=%errorlevel%"
if "%EXIT_CODE%"=="0" goto :stopped_cleanly

set /a RESTART_COUNT+=1
if %RESTART_COUNT% GEQ 5 goto :too_many_crashes
echo.
echo ============================================================
echo The server stopped unexpectedly (restart attempt %RESTART_COUNT%
echo of 5). Restarting automatically in 3 seconds...
echo If this keeps happening, check logs\app.log for the actual
echo error and share it so the root cause can be fixed properly.
echo ============================================================
timeout /t 3 /nobreak >nul
goto :run_app

:too_many_crashes
echo.
echo ============================================================
echo The server has crashed %RESTART_COUNT% times in a row and has
echo stopped auto-restarting. Check logs\app.log for the error
echo (look for the most recent "FATAL" entry) before trying again.
echo ============================================================
pause
exit /b 1

:no_exe
echo [ERROR] MontessoriDigitalLibrary.exe was not found in this folder.
echo Run this file from inside dist\MontessoriDigitalLibrary\ after
echo building with BUILD_EXE.bat.
pause
exit /b 1

:stopped_cleanly
echo.
echo ============================================================
echo The server has stopped.
echo ============================================================
pause
