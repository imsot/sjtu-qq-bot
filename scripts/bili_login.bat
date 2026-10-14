@echo off
rem ASCII-only on purpose: cmd parses batch files in ANSI/GBK codepage,
rem UTF-8 Chinese comments get shredded into broken commands.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set BOT_PYTHON=python
if exist "%~dp0config.bat" call "%~dp0config.bat"
echo === Bilibili QR Login ===
echo [1/3] checking qrcode lib...
"%BOT_PYTHON%" -c "import qrcode" 2>nul
if %errorlevel%==0 goto :run
echo [2/3] installing qrcode (10-30s, DO NOT close this window)...
"%BOT_PYTHON%" -m pip install qrcode -i https://pypi.tuna.tsinghua.edu.cn/simple
"%BOT_PYTHON%" -c "import qrcode" 2>nul
if %errorlevel%==0 goto :run
echo     tsinghua mirror failed, retrying with default index...
"%BOT_PYTHON%" -m pip install qrcode
"%BOT_PYTHON%" -c "import qrcode" 2>nul
if %errorlevel%==0 goto :run
echo [FAIL] install failed. Check your network / pip source and try again.
pause
exit /b 1
:run
echo [3/3] starting login script, QR will pop up shortly...
"%BOT_PYTHON%" bili_login.py
echo.
echo === done, you can close this window ===
pause
