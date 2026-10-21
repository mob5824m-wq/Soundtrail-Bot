"""Role menus: a message with a dropdown that lets members choose their own roles.

This is separate from reaction roles. A reaction role is one emoji on one message.
A role menu is one message listing up to 25 roles; a member picks any of them
from the dropdown, and the bot adds the picked roles and removes the menu's other
roles. Choosing nothing removes all of them.

The rules (who may post, which roles the bot may give) live in operations.py.
This module holds the text, the dropdown spec and the storage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import discord

import store
from announcements import iso_utc

MAX_OPTIONS = 25
MAX_TITLE_LENGTH = 256
MAX_DESCRIPTION_LENGTH = 1000
MAX_LABEL_LENGTH = 100
MAX_OPTION_DESCRIPTION_LENGTH = 100
PLACEHOLDER = "Choose your roles"


def custom_id(menu_id: int) -> str:
    return f"cm:rolemenu:{int(menu_id)}"


def clean_text(value: Optional[str], limit: int, field: str) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) > limit:
        raise ValueError(f"Keep the {field} under {limit} characters.")
    return cleaned


def clean_title(value: Optional[str]) -> str:
    cleaned = clean_text(value, MAX_TITLE_LENGTH, "title")
    if not cleaned:
        raise ValueError("A role menu needs a title.")
    return cleaned


def embed(title: str, description: str, options: list[dict[str, Any]]) -> discord.Embed:
    lines = []
    for option in options:
        line = f"**{option['label']}**"
        if option.get("description"):
            line += f" · {option['description']}"
        lines.append(line)
    body = description.strip()
    if lines:
        body = (body + "\n\n" if body else "") + "\n".join(lines)
    return discord.Embed(
        title=title,
        description=body or "Pick the roles you want from the menu below.",
        color=discord.Color.blurple(),
    )


def select_spec(menu_id: int, options: list[dict[str, Any]]) -> dict[str, Any]:
    """A dropdown spec for ``components.build_view``. Members may pick any number, including none."""
    return {
        "kind": "select",
        "custom_id": custom_id(menu_id),
        "placeholder": PLACEHOLDER,
        "min": 0,
        "max": len(options),
        "options": [
            {"label": o["label"], "value": str(o["role_id"]), "description": o.get("description") or None}
            for o in options
        ],
    }


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def create(guild_id: int, title: str, description: str, author_id: int, now: Optional[datetime] = None) -> int:
    return store.db_insert(
        "INSERT INTO role_menus (guild_id, title, description, author_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (int(guild_id), title, description, int(author_id), iso_utc(now or datetime.now(timezone.utc))),
    )


def get(guild_id: int, menu_id: int) -> Optional[dict[str, Any]]:
    return store.db_fetchone(
        "SELECT * FROM role_menus WHERE id = ? AND guild_id = ?", (int(menu_id), int(guild_id))
    )


def get_by_id(menu_id: int) -> Optional[dict[str, Any]]:
    """Look a menu up without its guild. Used by dropdown presses, which only carry the menu id."""
    return store.db_fetchone("SELECT * FROM role_menus WHERE id = ?", (int(menu_id),))


def list_menus(guild_id: int) -> list[dict[str, Any]]:
    return store.db_fetchall(
        "SELECT * FROM role_menus WHERE guild_id = ? ORDER BY id DESC", (int(guild_id),)
    )


def options_for(menu_id: int) -> list[dict[str, Any]]:
    return store.db_fetchall(
        "SELECT * FROM role_menu_options WHERE menu_id = ? ORDER BY position, role_id", (int(menu_id),)
    )


def count_options(menu_id: int) -> int:
    row = store.db_fetchone("SELECT COUNT(*) AS n FROM role_menu_options WHERE menu_id = ?", (int(menu_id),))
    return int(row["n"]) if row else 0


def add_option(menu_id: int, role_id: int, label: str, description: str) -> bool:
    """Add a role. Returns False if the role is already in this menu."""
    position = count_options(menu_id)
    changed = store.db_execute(
        "INSERT OR IGNORE INTO role_menu_options (menu_id, role_id, label, description, position) "
        "VALUES (?, ?, ?, ?, ?)",
        (int(menu_id), int(role_id), label, description, position),
    )
    return changed > 0


def remove_option(menu_id: int, role_id: int) -> bool:
    changed = store.db_execute(
        "DELETE FROM role_menu_options WHERE menu_id = ? AND role_id = ?", (int(menu_id), int(role_id))
    )
    return changed > 0


def attach_message(menu_id: int, channel_id: int, message_id: int) -> None:
    store.db_execute(
        "UPDATE role_menus SET channel_id = ?, message_id = ? WHERE id = ?",
        (int(channel_id), int(message_id), int(menu_id)),
    )


def delete_menu(menu_id: int) -> None:
    store.db_execute("DELETE FROM role_menu_options WHERE menu_id = ?", (int(menu_id),))
    store.db_execute("DELETE FROM role_menus WHERE id = ?", (int(menu_id),))
