"""Polls: parsing, the poll embed, votes and storage.

Two kinds of poll exist side by side:

* ``buttons`` (every poll made from this version on). Each option is a button
  on the poll message. A vote is a row in ``poll_votes``, so the live tally
  and the close-time tally both come from the database. A single-choice poll
  lets a person pick one option (a new pick replaces the old one). A
  multiple-choice poll lets them toggle as many as they like.
* ``reactions`` (polls made before buttons existed). Votes are reactions on the
  message. The bot adds one reaction of each kind itself, so a count of ``n``
  means ``n - 1`` votes. These still close by reading the reaction counts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import discord

import store
from announcements import iso_utc

MAX_QUESTION_LENGTH = 256
MAX_OPTION_LENGTH = 100
MIN_OPTIONS = 2
MAX_OPTIONS = 10
MAX_BUTTON_LABEL = 80

MODE_BUTTONS = "buttons"
MODE_REACTIONS = "reactions"

# Keycap digits 1-9, then the keycap-ten emoji.
EMOJIS = [f"{n}\ufe0f\N{COMBINING ENCLOSING KEYCAP}" for n in range(1, 10)] + ["\N{KEYCAP TEN}"]
OPEN_COLOR = discord.Color.gold()
CLOSED_COLOR = discord.Color.green()


def clean_question(text: Optional[str]) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("A poll needs a question.")
    if len(cleaned) > MAX_QUESTION_LENGTH:
        raise ValueError(f"Keep the question under {MAX_QUESTION_LENGTH} characters.")
    return cleaned


def parse_options(options: list[str] | str) -> list[str]:
    """Accept a list, or text split on ``|`` or new lines. Returns cleaned options."""
    if isinstance(options, str):
        parts = [p for chunk in options.splitlines() for p in chunk.split("|")]
    else:
        parts = list(options)
    cleaned = [str(p).strip() for p in parts]
    cleaned = [p for p in cleaned if p]
    if len(cleaned) < MIN_OPTIONS:
        raise ValueError(f"A poll needs at least {MIN_OPTIONS} options.")
    if len(cleaned) > MAX_OPTIONS:
        raise ValueError(f"A poll can have at most {MAX_OPTIONS} options.")
    for option in cleaned:
        if len(option) > MAX_OPTION_LENGTH:
            raise ValueError(f"Keep each option under {MAX_OPTION_LENGTH} characters.")
    return cleaned


def votes_from_reactions(raw_counts: list[int]) -> list[int]:
    """Remove the bot's own starting reaction from each count (reaction polls only)."""
    return [max(0, int(count) - 1) for count in raw_counts]


def button_label(option: str) -> str:
    return option if len(option) <= MAX_BUTTON_LABEL else option[: MAX_BUTTON_LABEL - 1] + "…"


def button_id(poll_id: int, index: int) -> str:
    """The custom id of one option's button. components.py parses it back."""
    return f"cm:poll:{int(poll_id)}:{int(index)}"


def button_specs(poll_id: int, options: list[str]) -> list[dict[str, Any]]:
    """What the gateway needs to draw the vote buttons for a poll."""
    return [
        {"custom_id": button_id(poll_id, i), "label": button_label(option), "emoji": EMOJIS[i], "style": "primary"}
        for i, option in enumerate(options)
    ]


def build_poll_embed(
    *,
    question: str,
    options: list[str],
    votes: Optional[list[int]] = None,
    closed: bool = False,
    voters: Optional[int] = None,
    ends_at: Optional[datetime] = None,
    author_name: str = "",
    mode: str = MODE_REACTIONS,
    multiple: bool = False,
) -> discord.Embed:
    """The poll message. ``votes`` (one count per option) shows results; ``closed`` marks it finished.

    Shares are taken over ``voters`` (people who voted). Without it they use the
    sum of ``votes``, which is the same thing for a single-choice poll.
    """
    total = voters if voters is not None else (sum(votes) if votes else 0)
    lines = []
    for index, option in enumerate(options):
        if votes is None:
            lines.append(f"{EMOJIS[index]} {option}")
        else:
            count = votes[index]
            share = round(100 * count / total) if total else 0
            bar = "\N{BLACK SQUARE}" * (share // 10)
            lines.append(f"{EMOJIS[index]} **{option}**: {count} ({share}%) {bar}".rstrip())
    embed = discord.Embed(
        title=f"Poll closed: {question}" if closed else f"Poll: {question}",
        description="\n".join(lines),
        color=CLOSED_COLOR if closed else OPEN_COLOR,
    )
    if closed:
        embed.set_footer(text=f"{total} voter(s) in total")
    elif mode == MODE_BUTTONS:
        pick = "Pick as many as you like." if multiple else "Pick one. Picking another option moves your vote."
        closes = " Closes automatically." if ends_at is not None else ""
        embed.set_footer(text=f"Press a button to vote. {pick}{closes}")
    elif ends_at is not None:
        embed.set_footer(text="Vote by adding a reaction. Closes automatically.")
    else:
        embed.set_footer(text=f"Vote by adding a reaction. Started by {author_name}".strip())
    if not closed and ends_at is not None:
        embed.add_field(name="Closes", value=f"<t:{int(ends_at.timestamp())}:R>", inline=False)
    return embed


# --------------------------------------------------------------------------- #
# Storage: polls
# --------------------------------------------------------------------------- #
def create_poll(
    *,
    guild_id: int,
    channel_id: int,
    author_id: int,
    question: str,
    options: list[str],
    ends_at: Optional[datetime],
    now: Optional[datetime] = None,
    mode: str = MODE_BUTTONS,
    multiple: bool = False,
    send_at: Optional[datetime] = None,
    duration_seconds: Optional[int] = None,
) -> int:
    """Store a poll. With ``send_at`` it is ``scheduled`` until the scheduler posts it."""
    scheduled = send_at is not None
    return store.db_insert(
        "INSERT INTO polls (guild_id, channel_id, question, options, author_id, created_at, ends_at, status, "
        "mode, multiple, send_at, duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(guild_id),
            int(channel_id),
            question,
            json.dumps(options),
            int(author_id),
            iso_utc(now or datetime.now(timezone.utc)),
            iso_utc(ends_at) if ends_at and not scheduled else None,
            "scheduled" if scheduled else "open",
            mode,
            1 if multiple else 0,
            iso_utc(send_at) if scheduled else None,
            int(duration_seconds) if duration_seconds else None,
        ),
    )


def activate(poll_id: int, message_id: int, ends_at: Optional[datetime]) -> bool:
    """A scheduled poll has been posted: it is open now, with its close time counted from here."""
    changed = store.db_execute(
        "UPDATE polls SET status = 'open', message_id = ?, ends_at = ? WHERE id = ? AND status = 'scheduled'",
        (int(message_id), iso_utc(ends_at) if ends_at else None, int(poll_id)),
    )
    return changed > 0


def list_due_scheduled(now: datetime) -> list[dict[str, Any]]:
    rows = store.db_fetchall(
        "SELECT * FROM polls WHERE status = 'scheduled' AND send_at <= ? ORDER BY send_at",
        (iso_utc(now),),
    )
    return [_decode(row) for row in rows]


def set_status(poll_id: int, status: str, *, only_from: str) -> bool:
    """Move a poll to ``status`` if it is still in ``only_from``. Returns whether it changed."""
    changed = store.db_execute(
        "UPDATE polls SET status = ? WHERE id = ? AND status = ?", (status, int(poll_id), only_from)
    )
    return changed > 0


def attach_message(poll_id: int, message_id: int) -> None:
    store.db_execute("UPDATE polls SET message_id = ? WHERE id = ?", (int(message_id), int(poll_id)))


def delete_poll(poll_id: int) -> None:
    """Remove a poll that was never posted."""
    store.db_execute("DELETE FROM polls WHERE id = ?", (int(poll_id),))


def _decode(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    row["options"] = json.loads(row["options"])
    row["results"] = json.loads(row["results"]) if row["results"] else None
    row["multiple"] = bool(row.get("multiple"))
    return row


def get_poll(guild_id: int, poll_id: int) -> Optional[dict[str, Any]]:
    return _decode(
        store.db_fetchone(
            "SELECT * FROM polls WHERE id = ? AND guild_id = ?", (int(poll_id), int(guild_id))
        )
    )


def get_poll_by_id(poll_id: int) -> Optional[dict[str, Any]]:
    """Look a poll up without its guild. Used by button presses, which only carry the poll id."""
    return _decode(store.db_fetchone("SELECT * FROM polls WHERE id = ?", (int(poll_id),)))


def list_polls(guild_id: int, *, open_only: bool = False, limit: int = 25) -> list[dict[str, Any]]:
    query = "SELECT * FROM polls WHERE guild_id = ?"
    if open_only:
        query += " AND status = 'open'"
    query += " ORDER BY id DESC LIMIT ?"
    return [_decode(row) for row in store.db_fetchall(query, (int(guild_id), int(limit)))]


def list_due_polls(now: datetime) -> list[dict[str, Any]]:
    rows = store.db_fetchall(
        "SELECT * FROM polls WHERE status = 'open' AND ends_at IS NOT NULL AND ends_at <= ? ORDER BY ends_at",
        (iso_utc(now),),
    )
    return [_decode(row) for row in rows]


def mark_closed(poll_id: int, results: list[int], now: Optional[datetime] = None) -> bool:
    changed = store.db_execute(
        "UPDATE polls SET status = 'closed', results = ?, closed_at = ? WHERE id = ? AND status = 'open'",
        (json.dumps(results), iso_utc(now or datetime.now(timezone.utc)), int(poll_id)),
    )
    return changed > 0


# --------------------------------------------------------------------------- #
# Storage: votes
# --------------------------------------------------------------------------- #
def cast_vote(
    *,
    poll_id: int,
    user_id: int,
    voter_name: str,
    index: int,
    multiple: bool,
    now: Optional[datetime] = None,
) -> tuple[list[int], list[int]]:
    """Record one press of an option. Returns ``(choices before, choices after)``.

    Single choice: the pick replaces any earlier one. Multiple choice: the pick
    toggles. Runs in one transaction so two presses at once can not both win.
    """
    stamp = iso_utc(now or datetime.now(timezone.utc))

    def work(conn):
        rows = conn.execute(
            "SELECT option_index FROM poll_votes WHERE poll_id = ? AND user_id = ? ORDER BY option_index",
            (int(poll_id), int(user_id)),
        ).fetchall()
        before = [int(r["option_index"]) for r in rows]
        if multiple:
            if index in before:
                conn.execute(
                    "DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ? AND option_index = ?",
                    (int(poll_id), int(user_id), index),
                )
                after = [i for i in before if i != index]
            else:
                conn.execute(
                    "INSERT INTO poll_votes (poll_id, user_id, voter_name, option_index, voted_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (int(poll_id), int(user_id), voter_name, index, stamp),
                )
                after = sorted(before + [index])
        elif before == [index]:
            after = before
        else:
            conn.execute("DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ?", (int(poll_id), int(user_id)))
            conn.execute(
                "INSERT INTO poll_votes (poll_id, user_id, voter_name, option_index, voted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (int(poll_id), int(user_id), voter_name, index, stamp),
            )
            after = [index]
        if after:
            conn.execute(
                "UPDATE poll_votes SET voter_name = ? WHERE poll_id = ? AND user_id = ?",
                (voter_name, int(poll_id), int(user_id)),
            )
        return before, after

    return store.db_transaction(work)


def tally(poll_id: int, option_count: int) -> tuple[list[int], int]:
    """Votes per option, and how many distinct people voted."""
    counts = [0] * option_count
    for row in store.db_fetchall(
        "SELECT option_index, COUNT(*) AS n FROM poll_votes WHERE poll_id = ? GROUP BY option_index",
        (int(poll_id),),
    ):
        if 0 <= row["option_index"] < option_count:
            counts[row["option_index"]] = int(row["n"])
    voters_row = store.db_fetchone(
        "SELECT COUNT(DISTINCT user_id) AS n FROM poll_votes WHERE poll_id = ?", (int(poll_id),)
    )
    return counts, int(voters_row["n"]) if voters_row else 0


def voters(poll_id: int) -> list[dict[str, Any]]:
    """Who voted and for which options, oldest vote first. For staff only."""
    people: dict[int, dict[str, Any]] = {}
    for row in store.db_fetchall(
        "SELECT user_id, voter_name, option_index FROM poll_votes WHERE poll_id = ? ORDER BY voted_at, option_index",
        (int(poll_id),),
    ):
        person = people.setdefault(row["user_id"], {"user_id": row["user_id"], "name": row["voter_name"], "choices": []})
        person["name"] = row["voter_name"] or person["name"]
        person["choices"].append(int(row["option_index"]))
    return list(people.values())
