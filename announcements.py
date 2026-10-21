"""Announcement parsing, embeds, history, scheduling and templates.

This module owns the rules for announcement text and times, and the database
rows behind them. It does not talk to Discord: posting happens in
:mod:`operations` through a gateway, so the same code serves slash commands,
the dashboard and the demo.

Times
    A send time is typed as a delay (``30m``, ``2h``, ``1d12h``; a bare number
    means minutes) or an absolute local time (``2026-10-20 18:00``) read in the
    server's timezone. Both are stored as UTC.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

import store

MAX_TITLE_LENGTH = 256
MAX_BODY_LENGTH = 4000
MAX_SCHEDULE_AHEAD = timedelta(days=365)
MIN_SCHEDULE_AHEAD = timedelta(minutes=1)
MIN_REPEAT_INTERVAL = timedelta(minutes=15)
MAX_REPEAT_INTERVAL = timedelta(days=365)
MAX_TEMPLATE_NAME = 40
MIN_PIN = timedelta(minutes=5)
MAX_PIN = timedelta(days=30)
DEFAULT_HISTORY_LIMIT = 10
MAX_HISTORY_LIMIT = 100
ERROR_TEXT_LIMIT = 500

EMBED_COLOR = discord.Color.blurple()
STAFF_LOG_COLOR = discord.Color.dark_grey()
FAILED_COLOR = discord.Color.red()

DEFAULT_TITLE = "Announcement"

# --------------------------------------------------------------------------- #
# Durations and send times
# --------------------------------------------------------------------------- #
_UNITS = {
    "": 60,  # bare number = minutes, only when it is the whole input
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}
_DURATION_TOKEN = re.compile(r"\s*(\d+)\s*([a-z]*)\s*")
_ABSOLUTE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})\s*$")


def parse_duration(text: str) -> Optional[int]:
    """``'1d12h'`` -> seconds. Returns ``None`` if the text is not a duration."""
    s = (text or "").strip().lower()
    if not s:
        return None
    total = 0
    pos = 0
    tokens = 0
    while pos < len(s):
        match = _DURATION_TOKEN.match(s, pos)
        if match is None or match.end() == pos:
            return None
        amount, unit = int(match.group(1)), match.group(2)
        if unit not in _UNITS:
            return None
        if unit == "" and (tokens > 0 or match.end() != len(s)):
            return None  # a bare number only makes sense on its own
        total += amount * _UNITS[unit]
        tokens += 1
        pos = match.end()
    return total or None


def resolve_timezone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_send_time(text: str, now: datetime, tz_name: str) -> datetime:
    """Turn what a moderator typed into a UTC send time.

    Raises :class:`ValueError` with a message fit to show the user.
    """
    now = now.astimezone(timezone.utc)
    seconds = parse_duration(text)
    if seconds is not None:
        target = now + timedelta(seconds=seconds)
    else:
        match = _ABSOLUTE.match(text or "")
        if match is None:
            raise ValueError(
                "I could not read that time. Use a delay such as `30m`, `2h` or "
                "`1d12h`, or a date and time such as `2026-10-20 18:00`."
            )
        year, month, day, hour, minute = (int(g) for g in match.groups())
        try:
            local = datetime(year, month, day, hour, minute, tzinfo=resolve_timezone(tz_name))
        except ValueError as exc:
            raise ValueError(f"That date does not exist ({exc}).") from None
        target = local.astimezone(timezone.utc)

    if target - now < MIN_SCHEDULE_AHEAD:
        raise ValueError("That time is too soon. It must be at least one minute away.")
    if target - now > MAX_SCHEDULE_AHEAD:
        raise ValueError("Times can be at most 365 days away.")
    return target


def parse_interval(text: str) -> int:
    """How often a repeating announcement goes out, in seconds."""
    seconds = parse_duration(text)
    if seconds is None:
        raise ValueError("I could not read that interval. Use something like `1d`, `12h` or `30m`.")
    if seconds < MIN_REPEAT_INTERVAL.total_seconds():
        raise ValueError("A repeat must be at least 15 minutes apart.")
    if seconds > MAX_REPEAT_INTERVAL.total_seconds():
        raise ValueError("A repeat can be at most 365 days apart.")
    return seconds


def parse_pin(text: Optional[str]) -> Optional[int]:
    """How long to pin an announcement, in seconds. ``None`` or blank means don't pin."""
    if not (text or "").strip():
        return None
    seconds = parse_duration(text)
    if seconds is None:
        raise ValueError("I could not read the pin time. Use something like `24h` or `3d`.")
    if seconds < MIN_PIN.total_seconds():
        raise ValueError("Pin for at least 5 minutes.")
    if seconds > MAX_PIN.total_seconds():
        raise ValueError("Pin for at most 30 days.")
    return seconds


def validate_timezone(name: str) -> str:
    """Return ``name`` if it is an IANA timezone, else raise :class:`ValueError`."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            f"`{name}` is not a timezone. Use an IANA name such as "
            "`America/Toronto` or `Europe/London`."
        ) from None
    return name


def iso_utc(moment: datetime) -> str:
    """The storage format: second precision, always UTC, sorts as text."""
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_stored(value: str) -> datetime:
    return datetime.fromisoformat(value)


def discord_time(moment: datetime) -> str:
    """A Discord timestamp tag, rendered in each viewer's own timezone."""
    return f"<t:{int(moment.timestamp())}:f>"


# --------------------------------------------------------------------------- #
# Validation of the text a moderator enters
# --------------------------------------------------------------------------- #
def clean_title(title: Optional[str]) -> str:
    cleaned = (title or "").strip()
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise ValueError(f"Keep the title under {MAX_TITLE_LENGTH} characters.")
    return cleaned or DEFAULT_TITLE


def clean_body(body: Optional[str]) -> str:
    cleaned = (body or "").strip()
    if not cleaned:
        raise ValueError("The announcement message can not be empty.")
    if len(cleaned) > MAX_BODY_LENGTH:
        raise ValueError(f"Keep the message under {MAX_BODY_LENGTH} characters.")
    return cleaned


def clean_template_name(name: Optional[str]) -> str:
    cleaned = (name or "").strip().lower()
    if not cleaned or len(cleaned) > MAX_TEMPLATE_NAME:
        raise ValueError(f"Template names are 1 to {MAX_TEMPLATE_NAME} characters.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9 _-]*", cleaned):
        raise ValueError("Template names use letters, numbers, spaces, `-` and `_` only.")
    return cleaned


# --------------------------------------------------------------------------- #
# Embeds and mentions
# --------------------------------------------------------------------------- #
def build_announcement_embed(*, title: str, body: str, author_name: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=body,
        color=EMBED_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Posted by {author_name}")
    return embed


def build_staff_log_embed(
    *,
    heading: str,
    channel_mention: str,
    author_mention: str,
    title: str,
    body: str,
    mention: Optional[str],
    source: str,
    color: discord.Color = STAFF_LOG_COLOR,
) -> discord.Embed:
    embed = discord.Embed(title=heading, color=color, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Channel", value=channel_mention, inline=True)
    embed.add_field(name="By", value=author_mention, inline=True)
    embed.add_field(name="Source", value=source, inline=True)
    embed.add_field(name="Title", value=title[:1024] or "-", inline=False)
    if mention:
        embed.add_field(name="Pinged", value=mention, inline=True)
    preview = body if len(body) <= 300 else body[:297] + "..."
    embed.add_field(name="Message", value=preview or "-", inline=False)
    return embed


def mention_content(role_id: Optional[int]) -> Optional[str]:
    return f"<@&{int(role_id)}>" if role_id else None


def allowed_mentions_for(
    role_id: Optional[int], user_ids: tuple[int, ...] | list[int] = ()
) -> discord.AllowedMentions:
    """The only mentions a post may trigger: its chosen role and listed users.

    ``@everyone`` and ``@here`` are never pinged, whatever the text says.
    Embeds never ping anyway, but ``content`` could.
    """
    users = [discord.Object(id=int(uid)) for uid in user_ids]
    roles = [discord.Object(id=int(role_id))] if role_id else False
    if not roles and not users:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(everyone=False, users=users or False, roles=roles)


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
def record_announcement(
    *,
    guild_id: int,
    channel_id: int,
    message_id: Optional[int],
    author_id: int,
    author_name: str,
    title: str,
    body: str,
    mention_role_id: Optional[int],
    source: str,
    when: Optional[datetime] = None,
) -> int:
    return store.db_insert(
        "INSERT INTO announcements (guild_id, channel_id, message_id, author_id, author_name, "
        "title, body, mention_role_id, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(guild_id),
            int(channel_id),
            message_id,
            int(author_id),
            author_name[:100],
            title,
            body,
            mention_role_id,
            source,
            iso_utc(when or datetime.now(timezone.utc)),
        ),
    )


def list_history(guild_id: int, limit: int = DEFAULT_HISTORY_LIMIT) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), MAX_HISTORY_LIMIT))
    return store.db_fetchall(
        "SELECT * FROM announcements WHERE guild_id = ? AND deleted_at IS NULL "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (int(guild_id), limit),
    )


def get_announcement(guild_id: int, message_id: int) -> Optional[dict[str, Any]]:
    """The recorded announcement for a Discord message, if it is one of ours and not deleted."""
    return store.db_fetchone(
        "SELECT * FROM announcements WHERE guild_id = ? AND message_id = ? AND deleted_at IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (int(guild_id), int(message_id)),
    )


def update_announcement(row_id: int, *, title: str, body: str, now: Optional[datetime] = None) -> None:
    store.db_execute(
        "UPDATE announcements SET title = ?, body = ?, edited_at = ? WHERE id = ?",
        (title, body, iso_utc(now or datetime.now(timezone.utc)), int(row_id)),
    )


def mark_announcement_deleted(row_id: int, now: Optional[datetime] = None) -> None:
    store.db_execute(
        "UPDATE announcements SET deleted_at = ? WHERE id = ?",
        (iso_utc(now or datetime.now(timezone.utc)), int(row_id)),
    )


# --------------------------------------------------------------------------- #
# Scheduling (one-off and repeating share one table)
# --------------------------------------------------------------------------- #
def add_scheduled(
    *,
    guild_id: int,
    channel_id: int,
    author_id: int,
    author_name: str,
    title: str,
    body: str,
    mention_role_id: Optional[int],
    send_at: datetime,
    publish: bool = False,
    interval_seconds: Optional[int] = None,
    pin_seconds: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    return store.db_insert(
        "INSERT INTO scheduled_announcements (guild_id, channel_id, author_id, author_name, title, "
        "body, mention_role_id, publish, send_at, interval_seconds, pin_seconds, runs, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?)",
        (
            int(guild_id),
            int(channel_id),
            int(author_id),
            author_name[:100],
            title,
            body,
            mention_role_id,
            1 if publish else 0,
            iso_utc(send_at),
            interval_seconds,
            pin_seconds,
            iso_utc(now or datetime.now(timezone.utc)),
        ),
    )


def set_send_at(schedule_id: int, when: datetime) -> None:
    """Move a pending announcement to a new time, for example the end of quiet hours."""
    store.db_execute(
        "UPDATE scheduled_announcements SET send_at = ? WHERE id = ? AND status = 'pending'",
        (iso_utc(when), int(schedule_id)),
    )


def list_pending(guild_id: int) -> list[dict[str, Any]]:
    return store.db_fetchall(
        "SELECT * FROM scheduled_announcements WHERE guild_id = ? AND status = 'pending' "
        "ORDER BY send_at, id",
        (int(guild_id),),
    )


def get_scheduled(guild_id: int, schedule_id: int) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM scheduled_announcements WHERE id = ? AND guild_id = ?",
        (int(schedule_id), int(guild_id)),
    )


def cancel_scheduled(guild_id: int, schedule_id: int) -> bool:
    """Cancel a pending announcement. ``False`` if it is not pending here."""
    changed = store.db_execute(
        "UPDATE scheduled_announcements SET status = 'cancelled' "
        "WHERE id = ? AND guild_id = ? AND status = 'pending'",
        (int(schedule_id), int(guild_id)),
    )
    return changed > 0


def list_due_scheduled(now: datetime) -> list[dict[str, Any]]:
    return store.db_fetchall(
        "SELECT * FROM scheduled_announcements WHERE status = 'pending' AND send_at <= ? "
        "ORDER BY send_at, id",
        (iso_utc(now),),
    )


def finish_one_off(schedule_id: int, status: str, error: Optional[str], now: datetime) -> None:
    store.db_execute(
        "UPDATE scheduled_announcements SET status = ?, error = ?, sent_at = ?, runs = runs + 1 "
        "WHERE id = ?",
        (status, error, iso_utc(now), int(schedule_id)),
    )


def advance_repeat(row: dict[str, Any], now: datetime, error: Optional[str]) -> datetime:
    """Move a repeating announcement to its next future run.

    Runs missed while the bot was offline are skipped, not posted in a burst.
    """
    interval = timedelta(seconds=int(row["interval_seconds"]))
    next_run = parse_stored(row["send_at"]) + interval
    while next_run <= now:
        next_run += interval
    store.db_execute(
        "UPDATE scheduled_announcements SET send_at = ?, runs = runs + 1, sent_at = ?, error = ? "
        "WHERE id = ?",
        (iso_utc(next_run), iso_utc(now), error, int(row["id"])),
    )
    return next_run


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #
def save_template(guild_id: int, name: str, title: str, body: str, author_id: int) -> bool:
    """Create or replace a template. Returns ``True`` if it already existed."""
    now = iso_utc(datetime.now(timezone.utc))
    existed = get_template(guild_id, name) is not None
    if existed:
        store.db_execute(
            "UPDATE announcement_templates SET title = ?, body = ?, updated_at = ? "
            "WHERE guild_id = ? AND name = ?",
            (title, body, now, int(guild_id), name),
        )
    else:
        store.db_insert(
            "INSERT INTO announcement_templates (guild_id, name, title, body, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(guild_id), name, title, body, int(author_id), now, now),
        )
    return existed


def get_template(guild_id: int, name: str) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM announcement_templates WHERE guild_id = ? AND name = ? COLLATE NOCASE",
        (int(guild_id), name),
    )


def list_templates(guild_id: int) -> list[dict[str, Any]]:
    return store.db_fetchall(
        "SELECT * FROM announcement_templates WHERE guild_id = ? ORDER BY name COLLATE NOCASE",
        (int(guild_id),),
    )


def delete_template(guild_id: int, name: str) -> bool:
    return store.db_execute(
        "DELETE FROM announcement_templates WHERE guild_id = ? AND name = ? COLLATE NOCASE",
        (int(guild_id), name),
    ) > 0
