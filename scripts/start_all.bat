@echo off
REM ============================================
REM  QQ Companion Bot - one-click start
REM  Brings up NapCat (if configured) and the bot.
REM  Safe to run repeatedly.
REM
REM  All machine-specific paths (where NapCat lives, which python to use,
REM  the bot's QQ number) live in scripts\watchdog.json - see
REM  watchdog.json.example. Without that file the watchdog only manages
REM  the bot and leaves NapCat alone.
REM ============================================
setlocal
set PYTHONIOENCODING=utf-8
set BOT_PYTHON=python
if exist "%~dp0config.bat" call "%~dp0config.bat"

"%BOT_PYTHON%" "%~dp0watchdog.py"

echo.
echo Done. Logs: bot_out.log / bot_err.log / watchdog_run.log in the repo root.
REM 2>&1 matters: at boot this runs non-interactively, and `timeout` then errors
REM with "Input redirection is not supported". `>nul` alone only hides stdout,
REM so the error still reached the console on every boot.
timeout /t 3 /nobreak >nul 2>&1
endlocal
