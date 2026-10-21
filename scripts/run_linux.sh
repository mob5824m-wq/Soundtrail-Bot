#!/usr/bin/env bash
# Launcher for Linux and macOS: creates the virtualenv, installs requirements
# and starts the bot. The bot token comes from DISCORD_TOKEN or from the
# 'bot_token' field of data/config.json (see README).
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR/.."

PYTHON_BIN="${PYTHON:-python3}"
VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment in $VENV_DIR ..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "Installing dependencies ..."
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt

mkdir -p data

if [ -z "${DISCORD_TOKEN:-}" ]; then
  if ! python -c "import json,sys;cfg=json.load(open('data/config.json'));sys.exit(0 if cfg.get('bot_token') else 1)" 2>/dev/null; then
    echo
    echo "No bot token found. Set DISCORD_TOKEN, or add \"bot_token\" to data/config.json."
    echo "See README.md section 2."
    exit 1
  fi
fi

echo "Starting Soundtrail Support ..."
exec python bot.py
