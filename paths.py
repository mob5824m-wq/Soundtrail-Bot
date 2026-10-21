"""
Soundtrail Support - runtime filesystem locations.

Every writable location is resolved here, at call time, so tests (and
packaged builds) can redirect state with environment variables before the
bot is imported.

Data directory, first match wins
    1. ``$SOUNDTRAIL_SUPPORT_DATA``
    2. ``<repo>/data``

Files inside the data directory
    config.json   live configuration (bot token, roles, channels). Created on
                  the first save; never committed (``data/`` is git-ignored).
    bot.db        SQLite: announcement history and scheduled announcements.
    bot.log       rotating-free plain log, appended on every run.

The repository's ``config.json`` is a *template*. It is read only when the
data directory has no ``config.json`` yet, and it is never written to, so a
token can not end up in a tracked file by accident.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "soundtrail-support"
APP_TITLE = "Soundtrail Support"
APP_DIR = Path(__file__).resolve().parent
TEMPLATE_CONFIG_PATH = APP_DIR / "config.json"
VERSION_PATH = APP_DIR / "VERSION"


def app_version() -> str:
    """The version string from the ``VERSION`` file (``unknown`` if missing)."""
    try:
        return VERSION_PATH.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def data_dir() -> Path:
    """Writable directory for the database, log and live config."""
    override = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")
    path = Path(override).expanduser() if override else APP_DIR / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    """Where the live config.json is read from and written to."""
    return data_dir() / "config.json"


def db_path() -> Path:
    return data_dir() / "bot.db"


def log_path() -> Path:
    return data_dir() / "bot.log"


def describe() -> str:
    """Human-readable summary of where this run keeps its files."""
    return "\n".join(
        [
            f"{APP_TITLE} {app_version()}",
            f"  data directory : {data_dir()}",
            f"  config (live)  : {config_path()}",
            f"  config (template, read-only fallback): {TEMPLATE_CONFIG_PATH}",
            f"  database       : {db_path()}",
            f"  log            : {log_path()}",
        ]
    )
