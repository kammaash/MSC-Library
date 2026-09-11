@echo off
setlocal
title Montessori Digital Library - Network Setup

echo ============================================================
echo   Montessori Digital Library - LAN / Firewall Setup
echo ============================================================
echo.
echo This adds ONE firewall rule so other computers and phones on
echo the school network can reach this server on port 5000.
echo It does not open anything to the internet - only devices on
echo the SAME local network can ever use this rule.
echo.

REM ---- Must run as Administrator ------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] This needs to run as Administrator.
    echo.
    echo Right-click SETUP_FIREWALL.bat and choose
    echo "Run as administrator", then try again.
    echo.
    pause
    exit /b 1
)

echo Removing any old rule with the same name first ^(safe if none exists^)...
netsh advfirewall firewall delete rule name="Montessori Digital Library" >nul 2>&1

echo Adding firewall rule for port 5000 ^(TCP, inbound^)...
netsh advfirewall firewall add rule name="Montessori Digital Library" dir=in action=allow protocol=TCP localport=5000 profile=any

if %errorlevel% equ 0 (
    echo.
    echo ============================================================
    echo   Done. Other devices on the same network should now be
    echo   able to reach this server.
    echo.
    echo   Next steps:
    echo   1. Start START_LIBRARY.bat if it isn't already running.
    echo   2. Note the network address it prints
    echo      ^(something like http://192.168.x.x:5000^).
    echo   3. On a phone/laptop connected to the SAME Wi-Fi, open
    echo      that address in a browser.
    echo ============================================================
) else (
    echo.
    echo [ERROR] Could not add the firewall rule. Please check that you
    echo ran this as Administrator ^(see message above^).
)
echo.
pause
