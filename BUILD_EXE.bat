@echo off
setlocal enabledelayedexpansion
title Montessori Digital Library - Build Standalone EXE
cd /d "%~dp0"

echo ============================================================
echo   Building a standalone .exe (no Python needed to RUN it)
echo ============================================================
echo.
echo This step itself still needs a WORKING Python on THIS computer
echo (just for this one-time build) - the .exe it produces afterward
echo will NOT need Python installed on whichever computer runs it.
echo.
echo Do this on whichever Windows computer has the least trouble with
echo Python - it does not have to be the same computer that will
echo later run the finished application.
echo.
echo IMPORTANT: if this project folder has ever been opened in
echo PyCharm, close PyCharm first and don't run this from inside it -
echo run this .bat file directly by double-clicking it in File
echo Explorer, or from a plain Command Prompt. PyCharm can create its
echo own separate, conflicting Python setup that causes this build to
echo fail in confusing ways.
echo.

where python >nul 2>nul
if errorlevel 1 goto :no_python

for /f "delims=" %%V in ('python --version 2^>^&1') do set "PYVER_OUTPUT=%%V"
echo Using: !PYVER_OUTPUT!
echo !PYVER_OUTPUT!| findstr /C:"Python 3" >nul
if errorlevel 1 goto :bad_python
goto :venv_setup

:no_python
echo [ERROR] Python was not found on this computer.
echo Install Python 3.10+ from https://www.python.org/downloads/
echo ^(check "Add Python to PATH" during install^), then try again.
pause
exit /b 1

:bad_python
echo.
echo [ERROR] The "python" command on this computer does not run real Python.
echo It printed: !PYVER_OUTPUT!
echo.
echo Try a different computer for this one-time build step, or open a
echo plain Command Prompt (not PyCharm) and run: where python
echo to see what's actually being used, then fix or remove it.
pause
exit /b 1

:venv_setup
set "NEED_FRESH_VENV=0"
if not exist "venv\Scripts\python.exe" set "NEED_FRESH_VENV=1"
if "!NEED_FRESH_VENV!"=="0" (
    venv\Scripts\python.exe --version >nul 2>nul
    if errorlevel 1 set "NEED_FRESH_VENV=1"
)
if "!NEED_FRESH_VENV!"=="0" goto :activate_venv

if exist "venv" rmdir /s /q venv
echo Setting up first, please wait...
python -m venv venv
if errorlevel 1 (
    echo [ERROR] Could not create the virtual environment needed for the build.
    echo See START_LIBRARY.bat's troubleshooting notes - the same causes apply.
    pause
    exit /b 1
)

:activate_venv
call venv\Scripts\activate.bat

echo Installing build requirements ^(first time can take a few minutes^)...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
python -m pip install pyinstaller
if errorlevel 1 (
    echo [ERROR] Failed to install required packages. Check your internet connection.
    pause
    exit /b 1
)

echo.
echo Building... this can take several minutes the first time.
echo.

pyinstaller --noconfirm --clean --name "MontessoriDigitalLibrary" ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    --collect-all pandas ^
    --collect-all reportlab ^
    --collect-all qrcode ^
    --collect-all barcode ^
    --collect-all pdfplumber ^
    --collect-all docx ^
    --collect-all pptx ^
    --collect-all flask_sqlalchemy ^
    --collect-all sqlalchemy ^
    --collect-all PIL ^
    --collect-all waitress ^
    --hidden-import=waitress ^
    --hidden-import=pytesseract ^
    --hidden-import=pdf2image ^
    --hidden-import=barcode.writer ^
    --console ^
    app.py

if errorlevel 1 (
    echo.
    echo [ERROR] The build failed - scroll up to see what PyInstaller reported.
    echo This is often a missing package it couldn't find automatically;
    echo send me the error text and I can adjust the build command.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Done! Your standalone app is in:
echo   dist\MontessoriDigitalLibrary\
echo.
echo   To use it, on THIS or ANY OTHER Windows computer:
echo   1. Copy that whole folder wherever you like (USB drive,
echo      another computer, Desktop, etc.) - it's fully self-contained.
echo   2. If you have an existing database.db, copy it into that
echo      folder too, so your real books/members come with it.
echo   3. Run RUN_SmartLib360.bat inside that folder (NOT the .exe
echo      directly) - it starts the same program, but automatically
echo      restarts it if the server ever crashes, instead of leaving
echo      the whole library down until someone notices.
echo.
echo   IF THE .EXE DOESN'T OPEN (window flashes and closes, or nothing
echo   happens): don't double-click it - instead open a Command Prompt,
echo   type "cd " (with a space), drag the dist\MontessoriDigitalLibrary
echo   folder into that window, press Enter, then type
echo   MontessoriDigitalLibrary.exe and press Enter. The window will
echo   stay open this time and show the actual error text - send me
echo   exactly what it says.
echo ============================================================
echo.
copy /y "RUN_SmartLib360.bat" "dist\MontessoriDigitalLibrary\RUN_SmartLib360.bat" >nul
pause
