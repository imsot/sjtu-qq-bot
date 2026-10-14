@echo off
rem Pure ASCII only - cmd parses bat as GBK, UTF-8 Chinese breaks command lines
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set BOT_PYTHON=python
if exist "%~dp0config.bat" call "%~dp0config.bat"
echo === %date% %time% start === >> "%~dp0..\watchdog_run.log"
"%BOT_PYTHON%" "%~dp0watchdog.py" >> "%~dp0..\watchdog_run.log" 2>&1
