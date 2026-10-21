"""SQLite access shared by :mod:`bot`, :mod:`operations` and :mod:`dashboard`.

Deliberately small: one connection per statement, opened ``with
closing(...)`` so it is always released, rows returned as plain dicts. The
database path is read from :mod:`paths` on every call, so a test that sets
``SOUNDTRAIL_SUPPORT_DATA`` before importing is honoured.

Schema changes are additive. Tables are created with ``IF NOT EXISTS`` and
columns added later are added by :func:`_ensure_columns`, so an existing
``bot.db`` upgrades in place.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any, Optional

import paths

SCHEMA = """
-- Every announcement that was actually posted, whether sent right away or
-- from the schedule. This is what history and the dashboard read.
CREATE TABLE IF NOT EXISTS announcements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER,
    author_id       INTEGER NOT NULL,
    author_name     TEXT,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    mention_role_id INTEGER,
    source          TEXT NOT NULL,          -- now | scheduled | repeat
    created_at      TEXT NOT NULL           -- UTC, ISO 8601
);
CREATE INDEX IF NOT EXISTS idx_announcements_guild
    ON announcements (guild_id, created_at DESC);

-- Announcements waiting to be posted. interval_seconds is NULL for a one-off
-- and set for a repeating announcement, which stays 'pending' until cancelled.
CREATE TABLE IF NOT EXISTS scheduled_announcements (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    channel_id       INTEGER NOT NULL,
    author_id        INTEGER NOT NULL,
    author_name      TEXT,
    title            TEXT NOT NULL,
    body             TEXT NOT NULL,
    mention_role_id  INTEGER,
    publish          INTEGER NOT NULL DEFAULT 0,
    send_at          TEXT NOT NULL,          -- UTC, the next time it goes out
    interval_seconds INTEGER,
    runs             INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|failed|cancelled
    error            TEXT,
    created_at       TEXT NOT NULL,
    sent_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_scheduled_due
    ON scheduled_announcements (status, send_at);

-- Reusable announcement text. Names are unique per server, ignoring case.
CREATE TABLE IF NOT EXISTS announcement_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT NOT NULL COLLATE NOCASE,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_by  INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (guild_id, name)
);

-- Reaction polls. Votes are reactions on the poll message, so the tally is
-- read from Discord when the poll closes; `results` keeps the final counts.
CREATE TABLE IF NOT EXISTS polls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    question    TEXT NOT NULL,
    options     TEXT NOT NULL,              -- JSON list of strings
    author_id   INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    ends_at     TEXT,                       -- UTC; NULL = closed by hand only
    status      TEXT NOT NULL DEFAULT 'open',  -- open | closed
    results     TEXT,                       -- JSON list of vote counts
    closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_polls_guild ON polls (guild_id, status);

-- Events with an RSVP reaction. Reminders are posted once, before start.
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    starts_at       TEXT NOT NULL,          -- UTC
    remind_minutes  INTEGER,                -- NULL = no reminder
    reminder_at     TEXT,                   -- UTC, derived from starts_at
    reminded        INTEGER NOT NULL DEFAULT 0,
    author_id       INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'   -- open | cancelled
);
CREATE INDEX IF NOT EXISTS idx_events_guild ON events (guild_id, status, starts_at);

-- Who changed what. Written by every staff action, from Discord or the
-- dashboard, and by the scheduler when it posts or closes something.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    actor_id    INTEGER NOT NULL,
    actor_name  TEXT NOT NULL,
    source      TEXT NOT NULL,              -- discord | dashboard | system
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_guild ON audit_log (guild_id, created_at DESC);

-- Announcements waiting for a second staff member. payload is the JSON the
-- operation needs to run once approved. The requester and the decider are
-- recorded as (source, id) pairs, so the same person can not approve their own.
CREATE TABLE IF NOT EXISTS approval_requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    kind              TEXT NOT NULL,            -- announce | schedule
    payload           TEXT NOT NULL,            -- JSON
    summary           TEXT NOT NULL,
    requester_id      INTEGER NOT NULL,
    requester_source  TEXT NOT NULL,
    requester_name    TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    decided_id        INTEGER,
    decided_source    TEXT,
    decided_name      TEXT,
    decided_at        TEXT,
    created_at        TEXT NOT NULL,
    reminded_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_guild ON approval_requests (guild_id, status);

-- Announcements pinned for a while. The scheduler unpins them when unpin_at passes.
CREATE TABLE IF NOT EXISTS pinned_posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    unpin_at    TEXT NOT NULL,                -- UTC
    status      TEXT NOT NULL DEFAULT 'pinned',  -- pinned | unpinned | failed
    error       TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pins_due ON pinned_posts (status, unpin_at);
-- One row per vote. A single-choice poll has at most one row per person; a
-- multiple-choice poll can have one per option they picked.
CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    voter_name    TEXT NOT NULL DEFAULT '',
    option_index  INTEGER NOT NULL,
    voted_at      TEXT NOT NULL,            -- UTC
    PRIMARY KEY (poll_id, user_id, option_index)
);
-- Support tickets: each opens a private channel. channel_id is unique, so a
-- channel maps to exactly one ticket.
CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL UNIQUE,
    opener_id    INTEGER NOT NULL,
    opener_name  TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT 'general',
    priority     TEXT NOT NULL DEFAULT 'normal',
    first_staff_reply_at TEXT,
    claimed_by_id INTEGER,
    claimed_by_name TEXT,
    status       TEXT NOT NULL DEFAULT 'open',  -- open | closed
    created_at   TEXT NOT NULL,              -- UTC
    closed_at    TEXT,
    closed_by    TEXT,
    close_reason TEXT,
    reopened_count INTEGER NOT NULL DEFAULT 0,
    reopened_at TEXT,
    reopened_by_id INTEGER,
    reopened_by_name TEXT,
    reopen_note TEXT,
    resolution_feedback INTEGER,
    feedback_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_guild ON tickets (guild_id, status);
-- Give a role when a member adds an emoji to a message, and take it away when
-- they remove it. The emoji is stored in the normalised form from reaction_roles.
CREATE TABLE IF NOT EXISTS reaction_roles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    emoji        TEXT NOT NULL,
    role_id      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (message_id, emoji)
);
-- The saved conversation of a closed ticket, kept for staff to read or download.
CREATE TABLE IF NOT EXISTS ticket_transcripts (
    ticket_id      INTEGER PRIMARY KEY,
    guild_id       INTEGER NOT NULL,
    text           TEXT NOT NULL,
    message_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL              -- UTC
);
CREATE TABLE IF NOT EXISTS ticket_transcript_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id      INTEGER NOT NULL,
    guild_id       INTEGER NOT NULL,
    text           TEXT NOT NULL,
    message_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL              -- UTC
);
CREATE INDEX IF NOT EXISTS idx_ticket_transcript_history_guild_date
    ON ticket_transcript_history (guild_id, created_at);
CREATE TABLE IF NOT EXISTS ticket_faqs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    key            TEXT NOT NULL,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    created_by     INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (guild_id, key)
);
CREATE INDEX IF NOT EXISTS idx_ticket_faqs_guild ON ticket_faqs (guild_id, key);
-- Role menus: a message with a dropdown that lets members pick their own roles.
-- message_id is set once the menu is posted; a menu can be re-posted.
CREATE TABLE IF NOT EXISTS role_menus (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    channel_id   INTEGER,
    message_id   INTEGER,
    author_id    INTEGER NOT NULL,
    created_at   TEXT NOT NULL                -- UTC
);
CREATE INDEX IF NOT EXISTS idx_role_menus_guild ON role_menus (guild_id);
CREATE TABLE IF NOT EXISTS role_menu_options (
    menu_id      INTEGER NOT NULL,
    role_id      INTEGER NOT NULL,
    label        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    position     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (menu_id, role_id)
);
"""

# Columns added after a first release. (table, column, declaration)
_ADDED_COLUMNS = [
    ("approval_requests", "reminded_at", "TEXT"),
    ("scheduled_announcements", "publish", "INTEGER NOT NULL DEFAULT 0"),
    ("scheduled_announcements", "interval_seconds", "INTEGER"),
    ("scheduled_announcements", "runs", "INTEGER NOT NULL DEFAULT 0"),
    ("scheduled_announcements", "author_name", "TEXT"),
    ("announcements", "author_name", "TEXT"),
    ("announcements", "edited_at", "TEXT"),
    ("announcements", "deleted_at", "TEXT"),
    ("scheduled_announcements", "pin_seconds", "INTEGER"),
    # 'reactions' is the default so polls made before buttons existed keep their old behaviour.
    ("polls", "mode", "TEXT NOT NULL DEFAULT 'reactions'"),  # buttons | reactions
    ("polls", "multiple", "INTEGER NOT NULL DEFAULT 0"),
    # Scheduled polls: send_at is when to post; the close time is counted from posting.
    ("polls", "send_at", "TEXT"),
    ("polls", "duration_seconds", "INTEGER"),
    # Ticket auto-close counts idle time from the last message by someone other than the bot.
    ("tickets", "last_activity_at", "TEXT"),
    ("tickets", "category", "TEXT NOT NULL DEFAULT 'general'"),
    ("tickets", "priority", "TEXT NOT NULL DEFAULT 'normal'"),
    ("tickets", "first_staff_reply_at", "TEXT"),
    ("tickets", "claimed_by_id", "INTEGER"),
    ("tickets", "claimed_by_name", "TEXT"),
    ("tickets", "close_reason", "TEXT"),
    ("tickets", "reopened_count", "INTEGER NOT NULL DEFAULT 0"),
    ("tickets", "reopened_at", "TEXT"),
    ("tickets", "reopened_by_id", "INTEGER"),
    ("tickets", "reopened_by_name", "TEXT"),
    ("tickets", "reopen_note", "TEXT"),
    ("tickets", "resolution_feedback", "INTEGER"),
    ("tickets", "feedback_at", "TEXT"),
]


def db_path() -> str:
    return str(paths.db_path())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    """Create the tables if they do not exist yet, and add any new columns."""
    with closing(_connect()) as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        # Preserve transcripts from installations that predate version history.
        conn.execute(
            "INSERT INTO ticket_transcript_history (ticket_id, guild_id, text, message_count, created_at) "
            "SELECT current.ticket_id, current.guild_id, current.text, current.message_count, current.created_at "
            "FROM ticket_transcripts AS current WHERE NOT EXISTS ("
            "SELECT 1 FROM ticket_transcript_history AS history "
            "WHERE history.ticket_id = current.ticket_id AND history.created_at = current.created_at)"
        )
        conn.commit()
        conn.commit()


def db_execute(query: str, params: tuple = ()) -> int:
    """Run one write statement and commit. Returns the affected row count."""
    with closing(_connect()) as conn:
        cur = conn.execute(query, params)
        conn.commit()
        return cur.rowcount


def db_insert(query: str, params: tuple = ()) -> int:
    """Run one INSERT and commit. Returns the new row id."""
    with closing(_connect()) as conn:
        cur = conn.execute(query, params)
        conn.commit()
        return int(cur.lastrowid)


def db_transaction(work):
    """Run ``work(conn)`` as one transaction: commit if it returns, roll back if it raises."""
    with closing(_connect()) as conn:
        try:
            result = work(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def db_fetchall(query: str, params: tuple = ()) -> list[dict[str, Any]]:
    with closing(_connect()) as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def db_fetchone(query: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    with closing(_connect()) as conn:
        row = conn.execute(query, params).fetchone()
        return dict(row) if row is not None else None
