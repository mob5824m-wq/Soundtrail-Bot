"""The approval queue: announcements that wait for a second staff member.

While ``require_approval`` is on, an announcement or a schedule is saved here
instead of going out. Another staff member approves it, and only then is it
posted or scheduled, under the original author's name.

Who may approve is decided in :mod:`operations`. The rule is that the approver
must be a different person: a different ``(source, id)`` from the requester.
Dashboard requests all share one identity, since the dashboard has a single
token, so the dashboard can not approve its own requests.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import store
from audit import Actor

KIND_ANNOUNCE = "announce"
KIND_SCHEDULE = "schedule"
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
SUMMARY_LIMIT = 200
# A request still pending after this long gets a reminder in the staff log, and again each day.
REMIND_AFTER = timedelta(hours=24)


def _iso(moment: Optional[datetime]) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _decode(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if row is not None:
        row["payload"] = json.loads(row["payload"])
    return row


def create(
    *,
    guild_id: int,
    kind: str,
    payload: dict[str, Any],
    summary: str,
    requester: Actor,
    now: Optional[datetime] = None,
) -> int:
    return store.db_insert(
        "INSERT INTO approval_requests (guild_id, kind, payload, summary, requester_id, "
        "requester_source, requester_name, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(guild_id),
            kind,
            json.dumps(payload),
            summary[:SUMMARY_LIMIT],
            int(requester.id),
            requester.source,
            requester.name[:100],
            PENDING,
            _iso(now),
        ),
    )


def get(guild_id: int, request_id: int) -> Optional[dict[str, Any]]:
    return _decode(
        store.db_fetchone(
            "SELECT * FROM approval_requests WHERE id = ? AND guild_id = ?",
            (int(request_id), int(guild_id)),
        )
    )


def list_pending(guild_id: int) -> list[dict[str, Any]]:
    rows = store.db_fetchall(
        "SELECT * FROM approval_requests WHERE guild_id = ? AND status = ? ORDER BY id",
        (int(guild_id), PENDING),
    )
    return [_decode(row) for row in rows]


def list_due_reminders(now: datetime) -> list[dict[str, Any]]:
    """Pending requests that have waited long enough, and have not been reminded recently."""
    cutoff = _iso(now - REMIND_AFTER)
    rows = store.db_fetchall(
        "SELECT * FROM approval_requests WHERE status = ? AND created_at <= ? "
        "AND (reminded_at IS NULL OR reminded_at <= ?) ORDER BY guild_id, id",
        (PENDING, cutoff, cutoff),
    )
    return [_decode(row) for row in rows]


def mark_reminded(request_ids: list[int], now: Optional[datetime] = None) -> None:
    for request_id in request_ids:
        store.db_execute(
            "UPDATE approval_requests SET reminded_at = ? WHERE id = ?", (_iso(now), int(request_id))
        )


def requester_of(row: dict[str, Any]) -> Actor:
    return Actor(int(row["requester_id"]), row["requester_name"], row["requester_source"])


def decide(guild_id: int, request_id: int, status: str, decider: Actor, now: Optional[datetime] = None) -> bool:
    """Move a pending request to ``approved`` or ``rejected``.

    The update only succeeds while the request is still pending, so two staff
    members can not both approve it. Returns ``False`` if it was already decided.
    """
    changed = store.db_execute(
        "UPDATE approval_requests SET status = ?, decided_id = ?, decided_source = ?, "
        "decided_name = ?, decided_at = ? WHERE id = ? AND guild_id = ? AND status = ?",
        (
            status,
            int(decider.id),
            decider.source,
            decider.name[:100],
            _iso(now),
            int(request_id),
            int(guild_id),
            PENDING,
        ),
    )
    return changed > 0


def reopen(guild_id: int, request_id: int) -> None:
    """Undo a claim when the approved action then failed, so it can be tried again."""
    store.db_execute(
        "UPDATE approval_requests SET status = ?, decided_id = NULL, decided_source = NULL, "
        "decided_name = NULL, decided_at = NULL WHERE id = ? AND guild_id = ?",
        (PENDING, int(request_id), int(guild_id)),
    )
