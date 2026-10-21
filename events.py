"""Events with an RSVP reaction and a reminder before they start.

Members RSVP by adding the ✅ reaction to the event message. The count shown is
the live reaction count minus the bot's own reaction. Before the start time the
scheduler posts a reminder that mentions the people who RSVP'd (up to a cap).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord

import store
from announcements import iso_utc

RSVP_EMOJI = "\N{WHITE HEAVY CHECK MARK}"
MAX_TITLE_LENGTH = 256
MAX_DESCRIPTION_LENGTH = 1000
MIN_REMIND_MINUTES = 5
MAX_REMIND_MINUTES = 7 * 24 * 60
MAX_REMINDER_MENTIONS = 50
MIN_LEAD_TIME = timedelta(minutes=5)
OPEN_COLOR = discord.Color.teal()
CANCELLED_COLOR = discord.Color.dark_grey()


def clean_title(text: Optional[str]) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("An event needs a title.")
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise ValueError(f"Keep the title under {MAX_TITLE_LENGTH} characters.")
    return cleaned


def clean_description(text: Optional[str]) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"Keep the description under {MAX_DESCRIPTION_LENGTH} characters.")
    return cleaned


def clean_remind_minutes(value: Optional[int]) -> Optional[int]:
    if value is None or value == 0:
        return None
    value = int(value)
    if not MIN_REMIND_MINUTES <= value <= MAX_REMIND_MINUTES:
        raise ValueError(
            f"A reminder must be between {MIN_REMIND_MINUTES} minutes and "
            f"{MAX_REMIND_MINUTES // 60 // 24} days before the event (or 0 for none)."
        )
    return value


def check_start_time(starts_at: datetime, now: datetime) -> None:
    if starts_at - now < MIN_LEAD_TIME:
        raise ValueError("An event must start at least five minutes from now.")


def build_event_embed(
    *,
    title: str,
    description: str,
    starts_at: datetime,
    author_name: str,
    going: Optional[int] = None,
    cancelled: bool = False,
) -> discord.Embed:
    heading = f"Cancelled: {title}" if cancelled else title
    embed = discord.Embed(
        title=heading,
        description=description or None,
        color=CANCELLED_COLOR if cancelled else OPEN_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="When", value=f"<t:{int(starts_at.timestamp())}:F>", inline=False)
    embed.add_field(name="Starts", value=f"<t:{int(starts_at.timestamp())}:R>", inline=True)
    if not cancelled:
        embed.add_field(
            name="Going",
            value=f"{going} (react {RSVP_EMOJI} to join)" if going is not None else f"react {RSVP_EMOJI} to join",
            inline=True,
        )
    embed.set_footer(text=f"Hosted by {author_name}")
    return embed


def reminder_time(starts_at: datetime, remind_minutes: Optional[int]) -> Optional[datetime]:
    if remind_minutes is None:
        return None
    return starts_at - timedelta(minutes=remind_minutes)


def create_event(
    *,
    guild_id: int,
    channel_id: int,
    author_id: int,
    title: str,
    description: str,
    starts_at: datetime,
    remind_minutes: Optional[int],
    now: Optional[datetime] = None,
) -> int:
    return store.db_insert(
        "INSERT INTO events (guild_id, channel_id, title, description, starts_at, remind_minutes, "
        "reminder_at, reminded, author_id, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'open')",
        (
            int(guild_id),
            int(channel_id),
            title,
            description,
            iso_utc(starts_at),
            remind_minutes,
            iso_utc(reminder_time(starts_at, remind_minutes)) if remind_minutes else None,
            int(author_id),
            iso_utc(now or datetime.now(timezone.utc)),
        ),
    )


def attach_message(event_id: int, message_id: int) -> None:
    store.db_execute("UPDATE events SET message_id = ? WHERE id = ?", (int(message_id), int(event_id)))


def get_event(guild_id: int, event_id: int) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM events WHERE id = ? AND guild_id = ?", (int(event_id), int(guild_id))
    )


def list_upcoming(guild_id: int, now: Optional[datetime] = None, limit: int = 25) -> list[dict[str, Any]]:
    """Open events that have not started yet, soonest first."""
    return store.db_fetchall(
        "SELECT * FROM events WHERE guild_id = ? AND status = 'open' AND starts_at > ? "
        "ORDER BY starts_at LIMIT ?",
        (int(guild_id), iso_utc(now or datetime.now(timezone.utc)), int(limit)),
    )


def cancel_event(guild_id: int, event_id: int) -> bool:
    return store.db_execute(
        "UPDATE events SET status = 'cancelled' WHERE id = ? AND guild_id = ? AND status = 'open'",
        (int(event_id), int(guild_id)),
    ) > 0


def list_due_reminders(now: datetime) -> list[dict[str, Any]]:
    return store.db_fetchall(
        "SELECT * FROM events WHERE status = 'open' AND reminded = 0 AND reminder_at IS NOT NULL "
        "AND reminder_at <= ? AND starts_at > ? ORDER BY reminder_at",
        (iso_utc(now), iso_utc(now)),
    )


def mark_reminded(event_id: int) -> None:
    store.db_execute("UPDATE events SET reminded = 1 WHERE id = ?", (int(event_id),))
