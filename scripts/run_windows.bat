@echo off
rem Launcher for Windows: creates the virtualenv, installs requirements and
rem starts the bot. The token comes from DISCORD_TOKEN or from the
rem "bot_token" field of data\config.json (see README).
setlocal
cd /d "%~dp0\.."

if not exist ".venv" (
  echo Creating virtual environment in .venv ...
  py -3 -m venv .venv || python -m venv .venv || goto :error
)
call ".venv\Scripts\activate.bat" || goto :error

echo Installing dependencies ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt || goto :error

if not exist "data" mkdir data

if "%DISCORD_TOKEN%"=="" (
  python -c "import json,sys;cfg=json.load(open('data/config.json'));sys.exit(0 if cfg.get('bot_token') else 1)" 2>nul
  if errorlevel 1 (
    echo.
    echo No bot token found. Set DISCORD_TOKEN, or add "bot_token" to data\config.json.
    echo See README.md section 2.
    exit /b 1
  )
)

echo Starting Soundtrail Support ...
python bot.py
exit /b %errorlevel%

:error
echo Setup failed. See the messages above.
exit /b 1
