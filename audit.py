"""The staff audit log: who did what, from where, and when.

Every state-changing action goes through :func:`record`, whether it came from
a slash command, the dashboard, or the scheduler. ``/manage audit`` and the
dashboard's Audit page read it back.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import store

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class Actor:
    """Who asked for an action. ``source`` is discord, dashboard or system."""

    id: int
    name: str
    source: str = "discord"


DASHBOARD_ACTOR = Actor(0, "Dashboard", "dashboard")
SCHEDULER_ACTOR = Actor(0, "Scheduler", "system")


def record(guild_id: int, actor: Actor, action: str, detail: str = "") -> int:
    return store.db_insert(
        "INSERT INTO audit_log (guild_id, actor_id, actor_name, source, action, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            int(guild_id),
            int(actor.id),
            actor.name[:100],
            actor.source,
            action,
            detail[:500],
            datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        ),
    )


def list_entries(guild_id: int, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), MAX_LIMIT))
    return store.db_fetchall(
        "SELECT * FROM audit_log WHERE guild_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (int(guild_id), limit),
    )


CSV_COLUMNS = ("created_at", "actor_name", "source", "action", "detail", "actor_id")
# Spreadsheets run a cell that starts with one of these as a formula.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> str:
    """Text for a CSV cell, with a leading apostrophe on anything that looks like a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def to_csv(guild_id: int) -> str:
    """The whole audit log for one server, oldest first, as CSV text."""
    rows = store.db_fetchall(
        "SELECT * FROM audit_log WHERE guild_id = ? ORDER BY created_at, id",
        (int(guild_id),),
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([csv_safe(row[column]) for column in CSV_COLUMNS])
    return buffer.getvalue()
