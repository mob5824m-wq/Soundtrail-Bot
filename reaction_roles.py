"""Reaction roles: an emoji on a message gives (or removes) a role.

The rule is stored in the ``reaction_roles`` table. Emoji are kept in one
normalised form so that the one Discord sends in a reaction event matches the
one staff typed: the variation selector (U+FE0F) is dropped, and server emoji
are written as ``<:name:id>`` or ``<a:name:id>``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

import store
from announcements import iso_utc

MAX_EMOJI_LENGTH = 64
CUSTOM_EMOJI = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:\d{5,25}>")
# discord.com / discordapp.com / ptb / canary message links.
MESSAGE_LINK = re.compile(
    r"(?:https?://)?(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
)


def normalize_emoji(value: Any) -> str:
    return str(value).replace("\ufe0f", "").strip()


def parse_emoji(text: Optional[str]) -> str:
    """Accept a standard emoji or a server emoji like ``<:name:id>``. Returns the normalised form."""
    cleaned = normalize_emoji(text or "")
    if not cleaned:
        raise ValueError("Give the emoji to react with.")
    if len(cleaned) > MAX_EMOJI_LENGTH:
        raise ValueError("That does not look like an emoji.")
    if CUSTOM_EMOJI.fullmatch(cleaned):
        return cleaned
    # A standard emoji is never plain text: reject anything made of ASCII letters or digits.
    if cleaned.isascii() and any(ch.isalnum() for ch in cleaned):
        raise ValueError(
            "That is not an emoji. Use a standard emoji, or a server emoji written as <:name:id>."
        )
    return cleaned


def parse_message_ref(text: Optional[str]) -> tuple[Optional[int], Optional[int], int]:
    """Read a message link or a bare message id. Returns ``(guild_id, channel_id, message_id)``.

    For a bare id the guild and channel are ``None``; the caller supplies the channel.
    """
    cleaned = (text or "").strip()
    link = MESSAGE_LINK.search(cleaned)
    if link:
        guild_id, channel_id, message_id = (int(part) for part in link.groups())
        return guild_id, channel_id, message_id
    if cleaned.isdigit():
        return None, None, int(cleaned)
    raise ValueError(
        "That is not a message link or ID. Right-click the message and choose Copy Message Link."
    )


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def add(
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    emoji: str,
    role_id: int,
    now: Optional[datetime] = None,
) -> int:
    """Store a rule. Raises ``sqlite3.IntegrityError`` if the emoji already has one on that message."""
    return store.db_insert(
        "INSERT INTO reaction_roles (guild_id, channel_id, message_id, emoji, role_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            int(guild_id),
            int(channel_id),
            int(message_id),
            normalize_emoji(emoji),
            int(role_id),
            iso_utc(now or datetime.now(timezone.utc)),
        ),
    )


def find(message_id: int, emoji: str) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM reaction_roles WHERE message_id = ? AND emoji = ?",
        (int(message_id), normalize_emoji(emoji)),
    )


def get(guild_id: int, rule_id: int) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM reaction_roles WHERE id = ? AND guild_id = ?", (int(rule_id), int(guild_id))
    )


def list_rules(guild_id: int) -> list[dict[str, Any]]:
    return store.db_fetchall(
        "SELECT * FROM reaction_roles WHERE guild_id = ? ORDER BY id", (int(guild_id),)
    )


def remove(guild_id: int, rule_id: int) -> bool:
    return store.db_execute(
        "DELETE FROM reaction_roles WHERE id = ? AND guild_id = ?", (int(rule_id), int(guild_id))
    ) > 0
