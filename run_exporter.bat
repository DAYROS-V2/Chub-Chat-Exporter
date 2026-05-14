@echo off
title Chub Chat Exporter
color 0A
cls

chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo  ================================================
echo    Chub Chat Exporter
echo    Mass exports your own Chub chats
echo  ================================================
echo.
echo   [1]  Login / setup Chub profile
echo   [2]  Export all chats
echo   [3]  Export all chats + bot PNGs
echo   [4]  Exit
echo.

set /p CHOICE="   Pick a mode (1-4): "

if "%CHOICE%"=="1" goto LOGIN
if "%CHOICE%"=="2" goto EXPORT
if "%CHOICE%"=="3" goto EXPORT_BOTS
if "%CHOICE%"=="4" goto END

echo   Invalid choice. Try again.
pause
goto END

:LOGIN
py -3 chub_chat_exporter.py login
goto DONE

:EXPORT
set "LIMIT="
set /p LIMIT="   Limit chats? Leave blank for all: "
if "%LIMIT%"=="" (
    py -3 chub_chat_exporter.py export
) else (
    py -3 chub_chat_exporter.py export --limit %LIMIT%
)
goto DONE

:EXPORT_BOTS
set "LIMIT="
set /p LIMIT="   Limit chats? Leave blank for all: "
if "%LIMIT%"=="" (
    py -3 chub_chat_exporter.py export-with-bots
) else (
    py -3 chub_chat_exporter.py export-with-bots --limit %LIMIT%
)
goto DONE

:DONE
echo.
echo  ================================================
echo   Done. Check the chat_exports folder.
echo  ================================================

:END
echo.
echo  Press any key to close...
pause >nul
