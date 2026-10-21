"""The single ``/manage`` slash-command group.

Every staff command hangs off this group, so a staff member can learn the
whole bot by typing ``/manage`` and reading the list. The group lives here
(not in ``bot.py``) so that any module can attach to it.

Discord applies ``default_member_permissions`` to the *top-level* command
only, and a role cannot be expressed as a permission. So the group is left
visible to everyone in the server and each command checks the caller itself
(see :func:`settings.staff_denial` and :func:`is_administrator`).
"""

from __future__ import annotations

import discord
from discord import app_commands

__all__ = ["MANAGE_GROUP_NAME", "is_administrator", "manage_group"]


MANAGE_GROUP_NAME = "manage"

manage_group = app_commands.Group(
    name=MANAGE_GROUP_NAME,
    description="Soundtrail support tickets and community tools for staff.",
    # Every command targets a specific server, so never offer them in DMs.
    guild_only=True,
)


def is_administrator(interaction: discord.Interaction) -> bool:
    """Whether the caller has *Administrator* in this server."""
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is None:
        return False
    return bool(getattr(perms, "administrator", False))
