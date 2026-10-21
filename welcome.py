"""Welcome messages for new members.

The text is a template with four placeholders:

    {mention}   the new member, as a mention (pings them once)
    {name}      their display name, without a ping
    {server}    the server's name
    {members}   the server's member count, so the new member's number

The welcome is posted as plain text in the channel set with ``/manage welcome set``.
The only ping it can make is the new member, through ``{mention}``.
"""

from __future__ import annotations

MAX_TEMPLATE_LENGTH = 1000
DEFAULT_WELCOME_MESSAGE = "Welcome to **{server}**, {mention}! You are member #{members}."
PLACEHOLDERS = ("{mention}", "{name}", "{server}", "{members}")


def clean_template(text: str | None) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("The welcome message can not be empty.")
    if len(cleaned) > MAX_TEMPLATE_LENGTH:
        raise ValueError(f"Keep the welcome message under {MAX_TEMPLATE_LENGTH} characters.")
    return cleaned


def render_welcome(
    template: str | None,
    *,
    mention: str,
    name: str,
    server: str,
    members: int,
) -> str:
    """Fill in the placeholders. Unknown ``{...}`` text is left exactly as written."""
    text = (template or "").strip() or DEFAULT_WELCOME_MESSAGE
    values = {
        "{mention}": mention,
        "{name}": name,
        "{server}": server,
        "{members}": str(members),
    }
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text[:2000]
