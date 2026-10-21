"""Announcements pinned for a while, then unpinned by the scheduler.

Only one-off posts are pinned. A repeating announcement would leave a pinned
copy behind on every run, so :mod:`operations` refuses that combination.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import store
from announcements import iso_utc

PINNED = "pinned"
UNPINNED = "unpinned"
FAILED = "failed"


def add(
    *, guild_id: int, channel_id: int, message_id: int, unpin_at: datetime, now: Optional[datetime] = None
) -> int:
    return store.db_insert(
        "INSERT INTO pinned_posts (guild_id, channel_id, message_id, unpin_at, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            int(guild_id),
            int(channel_id),
            int(message_id),
            iso_utc(unpin_at),
            PINNED,
            iso_utc(now or datetime.now(timezone.utc)),
        ),
    )


def list_due(now: datetime) -> list[dict[str, Any]]:
    return store.db_fetchall(
        "SELECT * FROM pinned_posts WHERE status = ? AND unpin_at <= ? ORDER BY unpin_at, id",
        (PINNED, iso_utc(now)),
    )


def list_active(guild_id: int) -> list[dict[str, Any]]:
    return store.db_fetchall(
        "SELECT * FROM pinned_posts WHERE guild_id = ? AND status = ? ORDER BY unpin_at, id",
        (int(guild_id), PINNED),
    )


def mark(pin_id: int, status: str, error: Optional[str] = None) -> None:
    store.db_execute(
        "UPDATE pinned_posts SET status = ?, error = ? WHERE id = ?",
        (status, error, int(pin_id)),
    )


def close_for_message(guild_id: int, message_id: int) -> None:
    """Forget a pin whose message has been deleted, so the scheduler does not chase it."""
    store.db_execute(
        "UPDATE pinned_posts SET status = ? WHERE guild_id = ? AND message_id = ? AND status = ?",
        (UNPINNED, int(guild_id), int(message_id), PINNED),
    )
