@echo off
setlocal
title Chub Chat Exporter Installer
color 0A
cd /d "%~dp0"

echo.
echo  ================================================
echo    Chub Chat Exporter Installer
echo  ================================================
echo.

where py >nul 2>&1
if errorlevel 1 (
    echo   Python launcher was not found.
    echo   Install Python 3.10 or newer from:
    echo   https://www.python.org/downloads/windows/
    echo.
    echo   During install, check "Add python.exe to PATH".
    pause
    exit /b 1
)

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo   Python 3.10 or newer is required.
    py -3 --version
    pause
    exit /b 1
)

echo   Updating pip...
py -3 -m ensurepip --upgrade >nul 2>&1
py -3 -m pip install --upgrade pip
if errorlevel 1 (
    echo.
    echo   Pip update failed.
    pause
    exit /b 1
)

echo.
echo   Installing Python requirements...
py -3 -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo   Requirement install failed.
    pause
    exit /b 1
)

echo.
echo  ================================================
echo   Install complete.
echo   Run run_exporter.bat to start.
echo  ================================================
echo.
pause
