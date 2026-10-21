"""The live Discord implementation of :class:`operations.Gateway`.

This is the only module that turns Discord objects into the plain values the
operations work with. Discord errors become :class:`operations.OperationError`
so every caller handles one exception type.
"""

from __future__ import annotations

import io
from typing import Any, Optional

import discord

import announcements as ann
from components import build_view
from operations import OperationError

# Discord returns some emoji with a variation selector (U+FE0F) and others
# without. Compare them with it removed.
def _message_text(message: discord.Message) -> str:
    """The message's words, or for a bot's embed-only message, the embed's title and text."""
    if message.content:
        return message.content
    parts = []
    for embed in message.embeds:
        text = ": ".join(part for part in (embed.title, embed.description) if part)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _norm(emoji: str) -> str:
    return str(emoji).replace("\ufe0f", "")


class DiscordGateway:
    def __init__(self, client: discord.Client) -> None:
        self.client = client

    # ---- lookups ------------------------------------------------------ #
    def _guild(self, guild_id: int) -> Optional[discord.Guild]:
        return self.client.get_guild(int(guild_id))

    def guilds(self) -> list[dict[str, Any]]:
        return [
            {"id": g.id, "name": g.name, "member_count": g.member_count or 0}
            for g in sorted(self.client.guilds, key=lambda g: g.name.lower())
        ]

    def snapshot(self, guild_id: int) -> Optional[dict[str, Any]]:
        guild = self._guild(guild_id)
        if guild is None:
            return None
        channels = [
            {"id": c.id, "name": c.name, "news": bool(c.is_news())}
            for c in sorted(guild.text_channels, key=lambda c: c.position)
        ]
        roles = [
            {"id": r.id, "name": r.name, "mentionable": bool(r.mentionable)}
            for r in sorted(guild.roles, key=lambda r: -r.position)
            if not r.is_default()
        ]
        categories = [
            {"id": c.id, "name": c.name} for c in sorted(guild.categories, key=lambda c: c.position)
        ]
        return {
            "id": guild.id,
            "name": guild.name,
            "member_count": guild.member_count or 0,
            "channels": channels,
            "categories": categories,
            "roles": roles,
        }

    def channel_problem(self, guild_id: int, channel_id: int) -> Optional[str]:
        guild = self._guild(guild_id)
        if guild is None:
            return "I'm not in that server any more."
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return "That channel does not exist in this server, or it is not a text channel."
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            return (
                f"I can't send messages in {channel.mention}. Grant me View Channel "
                "and Send Messages there."
            )
        if not perms.embed_links:
            return f"I need Embed Links in {channel.mention} to post announcements."
        return None

    def ping_warning(self, guild_id: int, role_id: int) -> Optional[str]:
        guild = self._guild(guild_id)
        if guild is None:
            return None
        role = guild.get_role(int(role_id))
        if role is None or role.mentionable:
            return None
        if guild.me.guild_permissions.mention_everyone:
            return None
        return (
            f"Note: {role.mention} is not mentionable, so it will show but not notify. "
            "Make the role mentionable or grant me Mention All Roles."
        )

    def role_problem(self, guild_id: int, role_id: int) -> Optional[str]:
        guild = self._guild(guild_id)
        if guild is None:
            return "I'm not in that server any more."
        role = guild.get_role(int(role_id))
        if role is None:
            return "That role does not exist in this server any more."
        if role.is_default():
            return "@everyone can not be a reaction role."
        if role.managed:
            return f"{role.mention} is managed by an integration, so I can not give it to members."
        if not guild.me.guild_permissions.manage_roles:
            return "I need the Manage Roles permission to give roles."
        if role >= guild.me.top_role:
            return (
                f"I can not give {role.mention}: move my role above it in "
                "Server Settings → Roles."
            )
        return None

    async def _channel(self, guild_id: int, channel_id: int) -> discord.TextChannel:
        guild = self._guild(guild_id)
        channel = guild.get_channel(int(channel_id)) if guild else None
        if not isinstance(channel, discord.TextChannel):
            raise OperationError("That channel is no longer available.")
        return channel

    async def _message(self, guild_id: int, channel_id: int, message_id: int) -> discord.Message:
        channel = await self._channel(guild_id, channel_id)
        try:
            return await channel.fetch_message(int(message_id))
        except discord.HTTPException as exc:
            raise OperationError(f"Could not find that message on Discord ({exc}).") from None

    # ---- posting ------------------------------------------------------ #
    async def post(
        self,
        guild_id: int,
        channel_id: int,
        *,
        embed: Optional[discord.Embed] = None,
        content: Optional[str] = None,
        role_id: Optional[int] = None,
        user_ids: tuple[int, ...] = (),
        publish: bool = False,
        view: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        channel = await self._channel(guild_id, channel_id)
        try:
            message = await channel.send(
                content=content,
                embed=embed,
                view=build_view(view) if view else discord.utils.MISSING,
                allowed_mentions=ann.allowed_mentions_for(role_id, user_ids),
            )
        except discord.HTTPException as exc:
            raise OperationError(f"Discord refused the message: {exc}") from None

        published: Optional[bool] = None
        if publish:
            published = False
            if channel.is_news():
                try:
                    await message.publish()
                    published = True
                except discord.HTTPException:
                    published = False
        return {"message_id": message.id, "jump_url": message.jump_url, "published": published}

    async def edit_embed(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        embed: discord.Embed,
        *,
        clear_view: bool = False,
    ) -> None:
        message = await self._message(guild_id, channel_id, message_id)
        try:
            if clear_view:
                await message.edit(embed=embed, view=None)  # view=None removes the buttons
            else:
                await message.edit(embed=embed)  # leaving view out keeps them
        except discord.HTTPException as exc:
            raise OperationError(f"Could not update the message ({exc}).") from None

    async def edit_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        *,
        embed: discord.Embed,
        view: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        message = await self._message(guild_id, channel_id, message_id)
        try:
            await message.edit(embed=embed, view=build_view(view) if view else None)
        except discord.HTTPException as exc:
            raise OperationError(f"Could not update the message ({exc}).") from None

    async def read_history(self, guild_id: int, channel_id: int, limit: int) -> list[dict[str, Any]]:
        channel = await self._channel(guild_id, channel_id)
        rows: list[dict[str, Any]] = []
        try:
            async for message in channel.history(limit=int(limit), oldest_first=True):
                rows.append(
                    {
                        "author": message.author.display_name,
                        "author_id": message.author.id,
                        "bot": bool(message.author.bot),
                        "created_at": message.created_at,  # timezone-aware, UTC
                        "content": _message_text(message),
                        "attachments": [attachment.url for attachment in message.attachments],
                    }
                )
        except discord.HTTPException as exc:
            raise OperationError(f"Could not read the channel ({exc}).") from None
        return rows

    async def send_dm(self, user_id: int, *, content: str, view: Optional[list[dict[str, Any]]] = None) -> None:
        try:
            user = self.client.get_user(int(user_id)) or await self.client.fetch_user(int(user_id))
            await user.send(
                content=content,
                view=build_view(view) if view else discord.utils.MISSING,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            raise OperationError("That member does not accept direct messages from this server.") from None
        except discord.HTTPException as exc:
            raise OperationError(f"Could not send the direct message ({exc}).") from None

    async def post_file(self, guild_id: int, channel_id: int, *, content: str, filename: str, data: bytes) -> None:
        channel = await self._channel(guild_id, channel_id)
        try:
            await channel.send(
                content=content,
                file=discord.File(io.BytesIO(data), filename=filename),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            raise OperationError(f"Could not post the file ({exc}).") from None

    async def delete_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        channel = await self._channel(guild_id, channel_id)
        try:
            # A partial message deletes without fetching first, so a message that
            # is already gone (NotFound) simply counts as deleted.
            await channel.get_partial_message(int(message_id)).delete()
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            raise OperationError(f"Could not delete the message ({exc}).") from None

    async def create_ticket_channel(
        self,
        guild_id: int,
        *,
        name: str,
        opener_id: int,
        category_id: Optional[int],
        staff_role_id: Optional[int],
        reason: str,
    ) -> int:
        guild = self._guild(guild_id)
        if guild is None:
            raise OperationError("I'm not in that server any more.")
        category = None
        if category_id:
            category = guild.get_channel(int(category_id))
            if not isinstance(category, discord.CategoryChannel):
                raise OperationError(
                    "The ticket category is no longer in this server. Choose another one in the settings."
                )
        access = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        )
        overwrites: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True, read_message_history=True
            ),
        }
        try:
            member = guild.get_member(int(opener_id)) or await guild.fetch_member(int(opener_id))
            overwrites[member] = access
        except discord.HTTPException:
            pass  # they left: the channel is still made, and staff can see it
        if staff_role_id:
            staff_role = guild.get_role(int(staff_role_id))
            if staff_role is not None:
                overwrites[staff_role] = access
        try:
            channel = await guild.create_text_channel(
                name=name[:100],
                category=category,
                overwrites=overwrites,
                reason=reason,
            )
        except discord.Forbidden:
            raise OperationError(
                "I need the Manage Channels permission to open tickets. Grant it in Server Settings → Roles."
            ) from None
        except discord.HTTPException as exc:
            raise OperationError(f"Discord would not create the ticket channel ({exc}).") from None
        return channel.id

    async def delete_channel(self, guild_id: int, channel_id: int, *, reason: str) -> None:
        guild = self._guild(guild_id)
        channel = guild.get_channel(int(channel_id)) if guild else None
        if channel is None:
            return  # already gone counts as deleted
        try:
            await channel.delete(reason=reason)
        except discord.NotFound:
            return
        except discord.Forbidden:
            raise OperationError("I need the Manage Channels permission to delete that channel.") from None
        except discord.HTTPException as exc:
            raise OperationError(f"Discord would not delete the channel ({exc}).") from None

    async def set_member_role(self, guild_id: int, user_id: int, role_id: int, *, add: bool) -> None:
        guild = self._guild(guild_id)
        if guild is None:
            raise OperationError("I'm not in that server any more.")
        role = guild.get_role(int(role_id))
        if role is None:
            raise OperationError("That role no longer exists.")
        try:
            member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
        except discord.NotFound:
            raise OperationError("That member has left the server.") from None
        except discord.HTTPException as exc:
            raise OperationError(f"Could not find that member ({exc}).") from None
        if member.bot:
            return
        try:
            if add:
                await member.add_roles(role, reason="Reaction role")
            else:
                await member.remove_roles(role, reason="Reaction role removed")
        except discord.Forbidden:
            raise OperationError(f"I can not change {role.name} for that member. Check my role order.") from None
        except discord.HTTPException as exc:
            raise OperationError(f"Discord would not change the role ({exc}).") from None

    async def pin_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        message = await self._message(guild_id, channel_id, message_id)
        try:
            await message.pin()
        except discord.HTTPException as exc:
            raise OperationError(
                f"Discord would not pin it ({exc}). Check that I have Manage Messages in that channel."
            ) from None

    async def unpin_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        message = await self._message(guild_id, channel_id, message_id)
        try:
            await message.unpin()
        except discord.HTTPException as exc:
            raise OperationError(f"Discord would not unpin it ({exc}).") from None

    # ---- reactions ---------------------------------------------------- #
    async def reaction_counts(
        self, guild_id: int, channel_id: int, message_id: int, emojis: list[str]
    ) -> list[int]:
        message = await self._message(guild_id, channel_id, message_id)
        seen = {_norm(r.emoji): r.count for r in message.reactions}
        return [int(seen.get(_norm(e), 0)) for e in emojis]

    async def reactors(
        self, guild_id: int, channel_id: int, message_id: int, emoji: str, limit: int
    ) -> list[int]:
        message = await self._message(guild_id, channel_id, message_id)
        for reaction in message.reactions:
            if _norm(reaction.emoji) == _norm(emoji):
                users = []
                async for user in reaction.users(limit=limit):
                    if not user.bot:
                        users.append(user.id)
                return users
        return []

    async def add_reactions(self, guild_id: int, channel_id: int, message_id: int, emojis: list[str]) -> None:
        message = await self._message(guild_id, channel_id, message_id)
        for emoji in emojis:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException as exc:
                raise OperationError(f"Could not add the reaction {emoji} ({exc}).") from None
