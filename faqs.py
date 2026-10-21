"""Per-server saved replies for common Soundtrail support questions."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

import store
from announcements import iso_utc

MAX_KEY_LENGTH = 40
MAX_TITLE_LENGTH = 100
MAX_BODY_LENGTH = 1800


def normalize_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ValueError(f"FAQ key must be 1 to {MAX_KEY_LENGTH} letters, numbers or hyphens.")
    return key


def clean_title(value: str) -> str:
    title = (value or "").strip()
    if not title or len(title) > MAX_TITLE_LENGTH:
        raise ValueError(f"FAQ title must be 1 to {MAX_TITLE_LENGTH} characters.")
    return title


def clean_body(value: str) -> str:
    body = (value or "").strip()
    if not body or len(body) > MAX_BODY_LENGTH:
        raise ValueError(f"FAQ reply must be 1 to {MAX_BODY_LENGTH} characters.")
    return body


def save(
    guild_id: int,
    key: str,
    title: str,
    body: str,
    author_id: int,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    key = normalize_key(key)
    title = clean_title(title)
    body = clean_body(body)
    stamp = iso_utc(now or datetime.now(timezone.utc))
    store.db_execute(
        "INSERT INTO ticket_faqs (guild_id, key, title, body, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, key) DO UPDATE SET "
        "title = excluded.title, body = excluded.body, created_by = excluded.created_by, created_at = excluded.created_at",
        (int(guild_id), key, title, body, int(author_id), stamp),
    )
    return get(guild_id, key)  # type: ignore[return-value]


def get(guild_id: int, key: str) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM ticket_faqs WHERE guild_id = ? AND key = ?",
        (int(guild_id), normalize_key(key)),
    )


def list_all(guild_id: int) -> list[dict[str, Any]]:
    return store.db_fetchall(
        "SELECT * FROM ticket_faqs WHERE guild_id = ? ORDER BY key LIMIT 100", (int(guild_id),)
    )


def delete(guild_id: int, key: str) -> bool:
    return store.db_execute(
        "DELETE FROM ticket_faqs WHERE guild_id = ? AND key = ?",
        (int(guild_id), normalize_key(key)),
    ) > 0
