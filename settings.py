"""Per-server configuration, the dashboard settings and the staff permission rule.

Config shape (``data/config.json``)::

    {
      "bot_token": "...",
      "default_timezone": "America/Toronto",
      "dashboard_enabled": true,
      "dashboard_host": "127.0.0.1",
      "dashboard_port": 8765,
      "dashboard_token": "...",            # generated on first run
      "dashboard_secure_cookie": false,
      "dashboard_allowed_hosts": [],       # extra Host names (reverse proxy)
      "dashboard_trusted_proxies": [],     # addresses whose X-Forwarded-For counts
      "guilds": {
        "<guild id>": {
          "staff_role_id": 123,
          "announcement_channel_id": 456,
          "staff_channel_id": 789,
          "timezone": "America/Toronto",
          "welcome_channel_id": 321,
          "welcome_message": "Welcome {mention}!"
        }
      }
    }

Each server is configured on its own. Nothing here touches Discord or the
database, which keeps it easy to test.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import discord

import paths

logger = logging.getLogger("soundtrail_support.settings")

DEFAULT_TIMEZONE = "America/Toronto"

DEFAULT_CONFIG: dict[str, Any] = {
    "bot_token": "",
    "default_timezone": DEFAULT_TIMEZONE,
    "dashboard_enabled": True,
    "dashboard_host": "127.0.0.1",
    "dashboard_port": 8765,
    "dashboard_token": "",
    "dashboard_secure_cookie": False,
    "dashboard_allowed_hosts": [],
    "dashboard_trusted_proxies": [],
    "guilds": {},
}

DEFAULT_GUILD: dict[str, Any] = {
    "staff_role_id": None,
    "announcement_channel_id": None,
    "staff_channel_id": None,
    "timezone": None,  # None -> the config-wide default_timezone
    "welcome_channel_id": None,
    "welcome_message": None,  # None -> the built-in welcome text
    "require_approval": False,  # True -> announcements wait for another staff member
    "quiet_start": None,  # "HH:MM" in the server's timezone; None with quiet_end -> off
    "quiet_end": None,
    "quiet_hold_manual": False,  # True -> announcements made by hand wait for quiet hours to end too
    "ticket_category_id": None,  # Discord category that new ticket channels go in; None -> top level
    "ticket_auto_close_days": 0,  # close a ticket after this many idle days; 0 -> never
    "ticket_transcript_retention_days": 0,  # remove local transcript copies after this many days; 0 -> never
    "support_hours_start": None,  # local 24-hour opening time; both empty disables the schedule
    "support_hours_end": None,  # local 24-hour closing time
    "after_hours_message": None,  # shown in new/reopened tickets outside support hours
}

# Keys a dashboard or command may change, and the type each one takes.
SETTABLE_KEYS = tuple(DEFAULT_GUILD)


# --------------------------------------------------------------------------- #
# Loading and saving
# --------------------------------------------------------------------------- #
def load_config() -> dict[str, Any]:
    """Read the live config, falling back to the repository template.

    A missing or corrupt file is logged and treated as "not configured yet",
    so the bot starts and says what is missing instead of crashing on import.
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    source = paths.config_path()
    if not source.is_file():
        source = paths.TEMPLATE_CONFIG_PATH
    if source.is_file():
        try:
            loaded = json.loads(source.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except (OSError, ValueError) as exc:
            logger.error("Ignoring unreadable config %s (%s).", source, exc)
    if not isinstance(cfg.get("guilds"), dict):
        cfg["guilds"] = {}
    for key in ("dashboard_allowed_hosts", "dashboard_trusted_proxies"):
        if not isinstance(cfg.get(key), list):
            cfg[key] = []
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    """Write the live config to the data directory (never the repository)."""
    target: Path = paths.config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # the file holds the bot and dashboard secrets
    except OSError:
        pass
    os.replace(tmp, target)


def resolve_token(cfg: dict[str, Any]) -> Optional[str]:
    """``DISCORD_TOKEN`` wins over ``bot_token`` in the config file."""
    return os.environ.get("DISCORD_TOKEN") or cfg.get("bot_token") or None


# --------------------------------------------------------------------------- #
# Per-server lookups
# --------------------------------------------------------------------------- #
def get_guild_config(cfg: dict[str, Any], guild_id: int) -> dict[str, Any]:
    """Settings for one server, with defaults filled in. Never ``None``."""
    stored = (cfg.get("guilds") or {}).get(str(int(guild_id)))
    result = dict(DEFAULT_GUILD)
    if isinstance(stored, dict):
        result.update({k: v for k, v in stored.items() if k in DEFAULT_GUILD})
    if not result.get("timezone"):
        result["timezone"] = cfg.get("default_timezone") or DEFAULT_TIMEZONE
    return result


def _as_id(value: Any) -> Optional[int]:
    return int(value) if value else None


def get_staff_role_id(cfg: dict[str, Any], guild_id: int) -> Optional[int]:
    return _as_id(get_guild_config(cfg, guild_id)["staff_role_id"])


def get_announcement_channel_id(cfg: dict[str, Any], guild_id: int) -> Optional[int]:
    return _as_id(get_guild_config(cfg, guild_id)["announcement_channel_id"])


def get_staff_channel_id(cfg: dict[str, Any], guild_id: int) -> Optional[int]:
    return _as_id(get_guild_config(cfg, guild_id)["staff_channel_id"])


def get_welcome_channel_id(cfg: dict[str, Any], guild_id: int) -> Optional[int]:
    return _as_id(get_guild_config(cfg, guild_id)["welcome_channel_id"])


def get_require_approval(cfg: dict[str, Any], guild_id: int) -> bool:
    return bool(get_guild_config(cfg, guild_id)["require_approval"])


def get_quiet_hours(cfg: dict[str, Any], guild_id: int) -> tuple[Optional[str], Optional[str]]:
    entry = get_guild_config(cfg, guild_id)
    return entry["quiet_start"] or None, entry["quiet_end"] or None


def get_quiet_hold_manual(cfg: dict[str, Any], guild_id: int) -> bool:
    return bool(get_guild_config(cfg, guild_id)["quiet_hold_manual"])


def get_ticket_category_id(cfg: dict[str, Any], guild_id: int) -> Optional[int]:
    return _as_id(get_guild_config(cfg, guild_id)["ticket_category_id"])


def get_ticket_auto_close_days(cfg: dict[str, Any], guild_id: int) -> int:
    return int(get_guild_config(cfg, guild_id)["ticket_auto_close_days"] or 0)


def get_ticket_transcript_retention_days(cfg: dict[str, Any], guild_id: int) -> int:
    return int(get_guild_config(cfg, guild_id)["ticket_transcript_retention_days"] or 0)


def get_support_hours(cfg: dict[str, Any], guild_id: int) -> tuple[Optional[str], Optional[str]]:
    entry = get_guild_config(cfg, guild_id)
    return entry["support_hours_start"] or None, entry["support_hours_end"] or None


def get_after_hours_message(cfg: dict[str, Any], guild_id: int) -> Optional[str]:
    return get_guild_config(cfg, guild_id)["after_hours_message"] or None


def get_timezone_name(cfg: dict[str, Any], guild_id: int) -> str:
    return str(get_guild_config(cfg, guild_id)["timezone"])


def set_guild_value(cfg: dict[str, Any], guild_id: int, key: str, value: Any) -> None:
    """Set one key for a server in memory. Call :func:`save_config` after."""
    if key not in DEFAULT_GUILD:
        raise KeyError(key)
    guilds = cfg.setdefault("guilds", {})
    entry = guilds.setdefault(str(int(guild_id)), dict(DEFAULT_GUILD))
    entry[key] = value


# --------------------------------------------------------------------------- #
# Who may use the staff commands
# --------------------------------------------------------------------------- #
def staff_denial(
    member: Any,
    cfg: dict[str, Any],
    guild_id: int,
    *,
    guild: Optional[discord.Guild] = None,
) -> Optional[str]:
    """Why ``member`` may not run staff commands, or ``None`` if they may.

    * Administrators always pass.
    * Once a staff role is configured, *only* holders of that role pass.
      Discord's *Manage Server* permission is deliberately not enough on its
      own: a server can hand that permission to anyone.
    * A server with no staff role yet falls back to *Manage Server*, so a
      fresh install keeps working until ``/manage setup`` is run.
    """
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and getattr(perms, "administrator", False):
        return None

    staff_role_id = get_staff_role_id(cfg, guild_id)
    if staff_role_id is None:
        if perms is not None and getattr(perms, "manage_guild", False):
            return None
        return (
            "Only administrators can use this until a staff role is set. "
            "An administrator can run `/manage setup staff_role:@Staff`."
        )

    roles = getattr(member, "roles", None) or []
    if any(getattr(role, "id", None) == staff_role_id for role in roles):
        return None
    mention = f"<@&{staff_role_id}>"
    if guild is not None and hasattr(guild, "get_role"):
        role = guild.get_role(staff_role_id)
        if role is not None:
            mention = role.mention
    return f"Only members with the {mention} role can use this command."
