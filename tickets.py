"""Support tickets: a private channel opened by pressing a button on a panel.

Each member can have one open ticket at a time. Opening one creates a text
channel that only the member, the staff role and the bot can see. Closing it
saves the conversation (a transcript) to the database and the staff log, then
deletes the channel. A ticket that sits idle can be closed automatically after
a number of days (``ticket_auto_close_days``).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

import discord

import store
from announcements import iso_utc, parse_stored

TICKET_OPEN_ID = "st:ticket:open"
TICKET_CLOSE_ID = "st:ticket:close"
MAX_CHANNEL_SLUG = 40
# Category keys are persisted in SQLite and embedded in persistent button IDs.
CATEGORIES = {
    "technical": "Technical issue",
    "account": "Account help",
    "general": "General question",
}
# How many messages a transcript keeps, oldest first. Longer tickets keep the first ones.
TRANSCRIPT_LIMIT = 1000
DEFAULT_AFTER_HOURS_MESSAGE = "Thanks for contacting Soundtrail Support. Our team is currently outside support hours and will reply when we're back."


def category_label(category: Optional[str]) -> str:
    return CATEGORIES.get(str(category or "general"), CATEGORIES["general"])


def open_button_id(category: str) -> str:
    if category not in CATEGORIES:
        raise ValueError(f"Unknown support category: {category}")
    return f"{TICKET_OPEN_ID}:{category}"


def channel_name(opener_name: str) -> str:
    """A Discord channel name for the ticket: ``ticket-<name>``, lower case, ASCII."""
    slug = re.sub(r"[^a-z0-9]+", "-", (opener_name or "").lower()).strip("-")[:MAX_CHANNEL_SLUG].strip("-")
    return f"ticket-{slug or 'member'}"


def panel_embed() -> discord.Embed:
    return discord.Embed(
        title="Soundtrail Support",
        description=(
            "Choose Technical issue, Account help, or General question to open a private ticket. "
            "Please do not share passwords, payment details, or other sensitive information."
        ),
        color=discord.Color.blurple(),
    )


def welcome_embed(
    opener_name: str,
    ticket_id: int,
    category: str = "general",
    after_hours_message: Optional[str] = None,
) -> discord.Embed:
    description = (
        f"**Category:** {category_label(category)}\n\n"
        f"Hi {opener_name}, thanks for contacting Soundtrail Support. Please include:\n"
        "• What you were trying to do and what happened\n"
        "• Your device/operating system and Soundtrail app version, if relevant\n"
        "• Steps to reproduce the issue and any non-sensitive error text\n\n"
        "Please do not post passwords, payment details, or private account information. "
        "A support team member will reply here. Press **Close ticket** when the issue is resolved. "
        "The conversation is saved for staff when the ticket closes, and this channel is deleted."
    )
    if after_hours_message:
        description += f"\n\n🌙 {after_hours_message}"
    return discord.Embed(
        title=f"Soundtrail Support · Ticket #{ticket_id}",
        description=description,
        color=discord.Color.blurple(),
    )


def reopened_embed(
    ticket_id: int,
    category: str,
    note: str,
    after_hours_message: Optional[str] = None,
) -> discord.Embed:
    description = (
        f"**Category:** {category_label(category)}\n"
        f"**Update:** {note}\n\n"
        "This ticket has been reopened. Staff will follow up here. Please do not share passwords, "
        "payment details, or private account information."
    )
    if after_hours_message:
        description += f"\n\n🌙 {after_hours_message}"
    return discord.Embed(
        title=f"Soundtrail Support · Ticket #{ticket_id} reopened",
        description=description,
        color=discord.Color.blurple(),
    )


def idle_embed(days: int) -> discord.Embed:
    return discord.Embed(
        description=(
            f"No one has written here for {days} day{'s' if days != 1 else ''}, so this ticket is closing. "
            "The conversation is saved for staff."
        ),
        color=discord.Color.greyple(),
    )


def panel_buttons() -> list[dict[str, Any]]:
    emoji = {"technical": "🛠️", "account": "👤", "general": "💬"}
    return [
        {
            "custom_id": open_button_id(key),
            "label": label,
            "emoji": emoji[key],
            "style": "primary",
        }
        for key, label in CATEGORIES.items()
    ]


def close_buttons() -> list[dict[str, Any]]:
    return [{"custom_id": TICKET_CLOSE_ID, "label": "Close ticket", "emoji": "\N{LOCK}", "style": "danger"}]


def ticket_buttons(guild_id: int, ticket_id: int) -> list[dict[str, Any]]:
    return close_buttons() + [
        {
            "custom_id": f"st:ticket:urgent:{int(guild_id)}:{int(ticket_id)}",
            "label": "Request urgent review",
            "emoji": "🚨",
            "style": "danger",
        }
    ]


def feedback_buttons(guild_id: int, ticket_id: int) -> list[dict[str, Any]]:
    return [
        {
            "custom_id": f"st:ticket:feedback:{int(guild_id)}:{int(ticket_id)}:{answer}",
            "label": label,
            "emoji": emoji,
            "style": "success" if answer == "yes" else "secondary",
        }
        for answer, label, emoji in (("yes", "Yes, resolved", "✅"), ("no", "Not yet", "🛠️"))
    ]


def render_transcript(messages: list[dict[str, Any]]) -> str:
    """Plain text for a transcript. Each message is a dict from the gateway's history read.

    ``created_at`` is a timezone-aware datetime; ``attachments`` is a list of URLs.
    """
    lines: list[str] = []
    for message in messages:
        moment = message["created_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        who = message.get("author") or "unknown"
        if message.get("bot"):
            who += " (bot)"
        text = (message.get("content") or "").strip()
        body = text.replace("\n", "\n    ") if text else ""
        if body:
            lines.append(f"[{moment}] {who}: {body}")
        elif not message.get("attachments"):
            lines.append(f"[{moment}] {who}: (no text)")
        else:
            lines.append(f"[{moment}] {who}:")
        for url in message.get("attachments") or []:
            lines.append(f"    attachment: {url}")
    return "\n".join(lines) + ("\n" if lines else "")


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def create(
    guild_id: int,
    channel_id: int,
    opener_id: int,
    opener_name: str,
    now: Optional[datetime] = None,
    *,
    category: str = "general",
) -> int:
    stamp = iso_utc(now or datetime.now(timezone.utc))
    return store.db_insert(
        "INSERT INTO tickets (guild_id, channel_id, opener_id, opener_name, category, status, created_at, last_activity_at) "
        "VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
        (int(guild_id), int(channel_id), int(opener_id), opener_name, category, stamp, stamp),
    )


def claim(guild_id: int, ticket_id: int, user_id: int, user_name: str) -> tuple[bool, Optional[dict[str, Any]]]:
    """Claim an open ticket if unassigned. Returns (claimed_now, current_ticket)."""
    changed = store.db_execute(
        "UPDATE tickets SET claimed_by_id = ?, claimed_by_name = ? "
        "WHERE id = ? AND guild_id = ? AND status = 'open' AND claimed_by_id IS NULL",
        (int(user_id), user_name, int(ticket_id), int(guild_id)),
    )
    current = get(guild_id, ticket_id)
    return changed > 0, current


def assign(guild_id: int, ticket_id: int, user_id: int, user_name: str) -> Optional[dict[str, Any]]:
    """Assign an open ticket to a staff member. Returns the updated ticket, if open."""
    store.db_execute(
        "UPDATE tickets SET claimed_by_id = ?, claimed_by_name = ? "
        "WHERE id = ? AND guild_id = ? AND status = 'open'",
        (int(user_id), user_name, int(ticket_id), int(guild_id)),
    )
    current = get(guild_id, ticket_id)
    return current if current is not None and current["status"] == "open" else None


def touch(channel_id: int, now: Optional[datetime] = None, *, staff_reply: bool = False) -> bool:
    """Record ticket activity and the first staff reply, if this was one."""
    stamp = iso_utc(now or datetime.now(timezone.utc))
    changed = store.db_execute(
        "UPDATE tickets SET last_activity_at = ?, "
        "first_staff_reply_at = CASE WHEN ? = 1 AND first_staff_reply_at IS NULL THEN ? "
        "ELSE first_staff_reply_at END WHERE channel_id = ? AND status = 'open'",
        (stamp, int(staff_reply), stamp, int(channel_id)),
    )
    return changed > 0


def set_priority(guild_id: int, ticket_id: int, priority: str) -> Optional[dict[str, Any]]:
    changed = store.db_execute(
        "UPDATE tickets SET priority = ? WHERE id = ? AND guild_id = ? AND status = 'open'",
        (priority, int(ticket_id), int(guild_id)),
    )
    if not changed:
        return None
    return get(guild_id, ticket_id)


def report(guild_id: int, since: datetime) -> dict[str, Any]:
    rows = store.db_fetchall(
        "SELECT created_at, closed_at, reopened_at, status, priority, first_staff_reply_at "
        "FROM tickets WHERE guild_id = ?",
        (int(guild_id),),
    )
    cutoff = iso_utc(since)
    opened = [row for row in rows if row["created_at"] >= cutoff]
    closed_count = sum(bool(row["closed_at"] and row["closed_at"] >= cutoff) for row in rows)
    reopened_count = sum(bool(row["reopened_at"] and row["reopened_at"] >= cutoff) for row in rows)
    urgent_open = sum(row["status"] == "open" and row["priority"] == "urgent" for row in rows)
    waiting = sum(row["status"] == "open" and not row["first_staff_reply_at"] for row in opened)
    response_seconds = [
        max(0.0, (parse_stored(row["first_staff_reply_at"]) - parse_stored(row["created_at"])).total_seconds())
        for row in opened if row["first_staff_reply_at"]
    ]
    return {
        "opened": len(opened),
        "closed": closed_count,
        "reopened": reopened_count,
        "urgent_open": urgent_open,
        "waiting_first_response": waiting,
        "first_responses": len(response_seconds),
        "average_first_response_seconds": (
            sum(response_seconds) / len(response_seconds) if response_seconds else None
        ),
    }


def get(guild_id: int, ticket_id: int) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM tickets WHERE id = ? AND guild_id = ?", (int(ticket_id), int(guild_id))
    )


def get_by_channel(guild_id: int, channel_id: int) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM tickets WHERE channel_id = ? AND guild_id = ?", (int(channel_id), int(guild_id))
    )


def open_for_user(guild_id: int, user_id: int) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM tickets WHERE guild_id = ? AND opener_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
        (int(guild_id), int(user_id)),
    )


def list_open_all() -> list[dict[str, Any]]:
    """Every open ticket in every server, for the idle check."""
    return store.db_fetchall("SELECT * FROM tickets WHERE status = 'open' ORDER BY id")


def list_tickets(guild_id: int, *, open_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    query = "SELECT * FROM tickets WHERE guild_id = ?"
    if open_only:
        query += " AND status = 'open'"
    query += " ORDER BY id DESC LIMIT ?"
    return store.db_fetchall(query, (int(guild_id), int(limit)))


def mark_closed(ticket_id: int, closed_by: str, reason: str, now: Optional[datetime] = None) -> bool:
    changed = store.db_execute(
        "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ?, close_reason = ?, "
        "resolution_feedback = NULL, feedback_at = NULL "
        "WHERE id = ? AND status = 'open'",
        (iso_utc(now or datetime.now(timezone.utc)), closed_by, reason, int(ticket_id)),
    )
    return changed > 0


def mark_reopened(
    guild_id: int,
    ticket_id: int,
    channel_id: int,
    reopened_by_id: int,
    reopened_by_name: str,
    note: str,
    now: Optional[datetime] = None,
) -> bool:
    stamp = iso_utc(now or datetime.now(timezone.utc))
    changed = store.db_execute(
        "UPDATE tickets SET channel_id = ?, status = 'open', closed_at = NULL, closed_by = NULL, close_reason = NULL, "
        "claimed_by_id = NULL, claimed_by_name = NULL, "
        "reopened_count = reopened_count + 1, "
        "reopened_at = ?, reopened_by_id = ?, reopened_by_name = ?, reopen_note = ?, last_activity_at = ? "
        "WHERE id = ? AND guild_id = ? AND status = 'closed'",
        (
            int(channel_id), stamp, int(reopened_by_id), reopened_by_name, note, stamp,
            int(ticket_id), int(guild_id),
        ),
    )
    return changed > 0


def submit_feedback(guild_id: int, ticket_id: int, user_id: int, resolved: bool, now: Optional[datetime] = None) -> bool:
    return store.db_execute(
        "UPDATE tickets SET resolution_feedback = ?, feedback_at = ? "
        "WHERE id = ? AND guild_id = ? AND opener_id = ? AND status = 'closed' "
        "AND resolution_feedback IS NULL",
        (
            int(bool(resolved)), iso_utc(now or datetime.now(timezone.utc)),
            int(ticket_id), int(guild_id), int(user_id),
        ),
    ) > 0


def last_activity(row: dict[str, Any]):
    """When the ticket last had a message from someone other than the bot (falls back to opening)."""
    return parse_stored(row["last_activity_at"] or row["created_at"])


def save_transcript(guild_id: int, ticket_id: int, text: str, message_count: int, now: Optional[datetime] = None) -> None:
    """Keep the latest transcript and append an immutable copy to ticket history."""
    stamp = iso_utc(now or datetime.now(timezone.utc))

    def write(conn):
        conn.execute(
            "INSERT INTO ticket_transcript_history (ticket_id, guild_id, text, message_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(ticket_id), int(guild_id), text, int(message_count), stamp),
        )
        conn.execute(
            "INSERT OR REPLACE INTO ticket_transcripts (ticket_id, guild_id, text, message_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(ticket_id), int(guild_id), text, int(message_count), stamp),
        )

    store.db_transaction(write)


def prune_transcripts(guild_id: int, before: datetime) -> int:
    """Delete local transcript copies older than ``before``; return removed version count."""
    cutoff = iso_utc(before)
    versions = store.db_execute(
        "DELETE FROM ticket_transcript_history WHERE guild_id = ? AND created_at < ?",
        (int(guild_id), cutoff),
    )
    current = store.db_execute(
        "DELETE FROM ticket_transcripts WHERE guild_id = ? AND created_at < ?",
        (int(guild_id), cutoff),
    )
    return versions + current


def transcript_ids(guild_id: int) -> set[int]:
    rows = store.db_fetchall("SELECT ticket_id FROM ticket_transcripts WHERE guild_id = ?", (int(guild_id),))
    return {int(row["ticket_id"]) for row in rows}


def get_transcript(guild_id: int, ticket_id: int) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM ticket_transcripts WHERE ticket_id = ? AND guild_id = ?", (int(ticket_id), int(guild_id))
    )


def transcript_versions(guild_id: int, ticket_id: int) -> list[dict[str, Any]]:
    return store.db_fetchall(
        "SELECT id, ticket_id, guild_id, message_count, created_at FROM ticket_transcript_history "
        "WHERE ticket_id = ? AND guild_id = ? ORDER BY id DESC",
        (int(ticket_id), int(guild_id)),
    )


def get_transcript_version(guild_id: int, ticket_id: int, version_id: int) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM ticket_transcript_history WHERE id = ? AND ticket_id = ? AND guild_id = ?",
        (int(version_id), int(ticket_id), int(guild_id)),
    )
