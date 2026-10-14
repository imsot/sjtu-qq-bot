@echo off
rem ASCII only - cmd parses .bat as GBK, non-ASCII breaks command lines.
rem Double-click this file: it self-elevates via UAC, then runs fix_sleep.ps1.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_sleep.ps1"
echo.
pause
