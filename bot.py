"""
Soundtrail Support - a discord.py bot for private support tickets and community operations.

Everything staff use is reached through one slash-command group, ``/manage``:

    Announcements
      /manage announce        post now (optionally ping a role, publish to followers)
      /manage schedule        post at a later time
      /manage repeat          post again and again, every N
      /manage scheduled       list what is waiting to go out
      /manage cancel          cancel a scheduled or repeating announcement
      /manage history         recent announcements
      /manage template ...    save, list, delete and use reusable text
      /manage edit            change the text of a post the bot made
      /manage delete          delete a post the bot made

    Polls and events
      /manage poll ...        create, list and close reaction polls
      /manage event ...       create, list and cancel events with RSVP and reminders

    Members
      /manage welcome ...     set, turn off or test the welcome message
      /manage pins            posts that are pinned right now

    Approval and quiet hours
      /manage queue           announcements waiting for a second staff member
      /manage approve         approve a queued announcement
      /manage reject          turn a queued announcement down
      /manage quiet           set or clear the quiet hours

    Server
      /manage status          settings, counts and the next thing due
      /manage audit           who did what, most recent first
      /manage setup           staff role, channels, timezone (administrators)
      /manage fixcommands     re-sync the commands (administrators)

The web dashboard runs in the same process and calls the same operations. See
``dashboard.py``. ``python bot.py --demo`` runs the dashboard with simulated
servers and no token.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import announcements as ann
import audit
import command_tree
import components
import events
import faqs
import operations as ops
import paths
import polls
import role_menus
import settings
import store
import tickets
import welcome
from audit import Actor
from command_tree import is_administrator, manage_group
from dashboard import DashboardServer, ensure_dashboard_token
from discord_gateway import DiscordGateway
from operations import OperationError
from settings import DEFAULT_CONFIG, load_config, resolve_token, staff_denial

SCHEDULER_INTERVAL_SECONDS = 30
MAX_CONTENT_LENGTH = ann.MAX_BODY_LENGTH
MAX_TITLE_LENGTH = ann.MAX_TITLE_LENGTH

# --------------------------------------------------------------------------- #
# Logging: stdout (journal / console) and data/bot.log
# --------------------------------------------------------------------------- #
logger = logging.getLogger("soundtrail_support")
logger.setLevel(logging.INFO)
_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_stream = logging.StreamHandler(sys.stdout)
_stream.setFormatter(_formatter)
logger.addHandler(_stream)


def _attach_file_log() -> None:
    """Also log to data/bot.log. Called from main(), never on import, so that
    --demo, --paths and the tests do not create the real data directory."""
    try:
        handler = logging.FileHandler(paths.log_path(), encoding="utf-8")
    except OSError as exc:  # a broken log file must not stop the bot
        logger.warning("File logging disabled (%s)", exc)
        return
    handler.setFormatter(_formatter)
    logger.addHandler(handler)

# Members are needed for the welcome message. This is a privileged intent:
# it must also be switched on in the Developer Portal.
intents = discord.Intents.default()
intents.members = True
# Ticket transcripts keep what members write, so the bot must read message text.
# This is a privileged intent: it must also be switched on in the Developer Portal.
intents.message_content = True


class SoundtrailSupportBot(commands.Bot):
    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(command_prefix="!", intents=intents, help_command=None)  # slash commands only
        self.config = config
        self.gateway = DiscordGateway(self)
        self.dashboard: Optional[DashboardServer] = None
        self._guild_commands_synced: set[int] = set()
        self._global_registry_cleared = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def setup_hook(self) -> None:
        # Vote and ticket buttons on old messages keep working only if these are registered.
        self.add_dynamic_items(*components.DYNAMIC_ITEMS)
        await _register_cog(self)
        if not self.scheduler_tick.is_running():
            self.scheduler_tick.start()
        await self._start_dashboard()

    async def _start_dashboard(self) -> None:
        if not self.config.get("dashboard_enabled", True):
            logger.info("Dashboard disabled (dashboard_enabled is false).")
            return
        try:
            token = ensure_dashboard_token(self.config)
            self.dashboard = DashboardServer(
                self.gateway,
                self.config,
                token=token,
                host=str(self.config.get("dashboard_host") or "127.0.0.1"),
                port=int(self.config.get("dashboard_port") or 8765),
                allowed_hosts=self.config.get("dashboard_allowed_hosts") or [],
                trusted_proxies=self.config.get("dashboard_trusted_proxies") or [],
                secure_cookie=bool(self.config.get("dashboard_secure_cookie")),
            )
            await self.dashboard.start()
        except Exception:  # the bot keeps running without its dashboard
            logger.exception("Could not start the dashboard; the Discord bot will keep running.")
            self.dashboard = None

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        if not self._global_registry_cleared:
            await self._clear_global_registry()
        for guild in self.guilds:
            await self.sync_guild_commands(guild.id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.sync_guild_commands(guild.id)

    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        guild = member.guild
        await ops.send_welcome(
            self.gateway,
            self.config,
            guild.id,
            user_id=member.id,
            user_name=member.display_name,
            server=guild.name,
            members=guild.member_count or 0,
        )

    async def on_message(self, message: discord.Message) -> None:
        # Activity in an open ticket resets its idle clock. The bot's own messages do not count.
        # The bot has no prefix commands, so there is nothing else to pass this on to.
        if message.guild is None or message.author.bot:
            return
        try:
            staff_reply = settings.staff_denial(
                message.author, self.config, message.guild.id, guild=message.guild
            ) is None
            ops.note_ticket_activity(message.channel.id, staff_reply=staff_reply)
        except Exception:  # never let a bookkeeping failure reach the message pipeline
            logger.exception("Could not record ticket activity.")

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.member is not None and payload.member.bot:
            return
        await self._reaction_changed(payload, added=True)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._reaction_changed(payload, added=False)

    async def _reaction_changed(self, payload: discord.RawReactionActionEvent, *, added: bool) -> None:
        """Give or take a reaction role. Raw events, so messages that are not in the cache still count."""
        if payload.guild_id is None or (self.user is not None and payload.user_id == self.user.id):
            return
        try:
            await ops.handle_reaction(
                self.gateway,
                payload.guild_id,
                payload.channel_id,
                payload.message_id,
                payload.user_id,
                str(payload.emoji),
                added=added,
            )
        except Exception:  # a reaction must never take the bot down
            logger.exception("Reaction role handling failed.")

    async def close(self) -> None:
        if self.scheduler_tick.is_running():
            self.scheduler_tick.cancel()
        if self.dashboard is not None:
            await self.dashboard.close()
        await super().close()

    # ------------------------------------------------------------------ #
    # Slash command sync
    # ------------------------------------------------------------------ #
    async def _clear_global_registry(self) -> None:
        """Empty Discord's *global* command list.

        Commands are published per server only. A command listed in both the
        global and a server registry shows up twice in the Discord client.
        """
        try:
            await self.http.bulk_upsert_global_commands(self.application_id, payload=[])
        except discord.HTTPException as exc:
            logger.warning("Could not clear global commands (%s); will retry on next ready.", exc)
            return
        self._global_registry_cleared = True
        logger.info("Cleared duplicate global command registry.")

    async def sync_guild_commands(self, guild_id: int, *, force: bool = False) -> int:
        """Publish the command tree to one server. Returns how many were synced."""
        guild_id = int(guild_id)
        if not force and guild_id in self._guild_commands_synced:
            return 0
        guild_obj = discord.Object(id=guild_id)
        try:
            self.tree.copy_global_to(guild=guild_obj)
            synced = await self.tree.sync(guild=guild_obj)
        except discord.HTTPException as exc:
            logger.warning("Command sync to guild %s failed (%s).", guild_id, exc)
            return 0
        self._guild_commands_synced.add(guild_id)
        logger.info("Synced %d command(s) to guild %s.", len(synced), guild_id)
        return len(synced)

    # ------------------------------------------------------------------ #
    # Scheduler: scheduled posts, repeats, poll closes and event reminders
    # ------------------------------------------------------------------ #
    @tasks.loop(seconds=SCHEDULER_INTERVAL_SECONDS)
    async def scheduler_tick(self) -> None:
        try:
            counts = await ops.run_scheduler(self.gateway, self.config, now=datetime.now(timezone.utc))
            if any(counts.values()):
                logger.info("Scheduler: %s", counts)
        except Exception:  # the loop must survive any single bad tick
            logger.exception("Scheduler tick failed.")

    @scheduler_tick.before_loop
    async def _wait_until_ready(self) -> None:
        await self.wait_until_ready()


# --------------------------------------------------------------------------- #
# Helpers for the cog
# --------------------------------------------------------------------------- #
def _actor(interaction: discord.Interaction) -> Actor:
    return Actor(interaction.user.id, interaction.user.display_name, "discord")


def _when_text(iso: str) -> str:
    return ann.discord_time(ann.parse_stored(iso))


def _warn_lines(warnings: list[str]) -> list[str]:
    return [f"⚠️ {w}" for w in warnings]


def _held_text(result: dict[str, Any]) -> str:
    return (
        f"Quiet hours are on, so this is held as **#{result['schedule_id']}** and posts "
        f"{_when_text(result['send_at'])}. Cancel it with `/manage cancel` and that number."
    )


def _queued_text(result: dict[str, Any]) -> str:
    return (
        f"Sent to the approval queue as **#{result['approval_id']}**. A different staff member can "
        "approve it with `/manage approve` and that number."
    )


# --------------------------------------------------------------------------- #
# Nested groups. Defined at module level, attached to /manage, and bound by the cog below.
# --------------------------------------------------------------------------- #
template_group = app_commands.Group(
    name="template",
    description="Save and reuse announcement text.",
    parent=manage_group,
    guild_only=True,
)
poll_group = app_commands.Group(
    name="poll",
    description="Polls with vote buttons that close themselves.",
    parent=manage_group,
    guild_only=True,
)
ticket_group = app_commands.Group(
    name="ticket",
    description="Categorized private Soundtrail support tickets.",
    parent=manage_group,
    guild_only=True,
)
faq_group = app_commands.Group(
    name="faq",
    description="Saved troubleshooting replies for support tickets.",
    parent=manage_group,
    guild_only=True,
)
role_menu_group = app_commands.Group(
    name="rolemenu",
    description="Dropdowns that let members pick their own roles.",
    parent=manage_group,
    guild_only=True,
)
reaction_role_group = app_commands.Group(
    name="reactionrole",
    description="Give or take a role when someone reacts to a message.",
    parent=manage_group,
    guild_only=True,
)
event_group = app_commands.Group(
    name="event",
    description="Events with RSVP reactions and reminders.",
    parent=manage_group,
    guild_only=True,
)
welcome_group = app_commands.Group(
    name="welcome",
    description="The message posted when someone joins.",
    parent=manage_group,
    guild_only=True,
)


# --------------------------------------------------------------------------- #
# The shared /manage cog
# --------------------------------------------------------------------------- #
class CombatMasterCog(commands.Cog):
    """Every staff command, under the shared ``/manage`` group."""

    def __init__(self, bot_: SoundtrailSupportBot) -> None:
        self.bot = bot_

    # Top-level group, and the nested groups from the module above.
    manage_group = command_tree.manage_group
    template_group = template_group
    poll_group = poll_group
    ticket_group = ticket_group
    faq_group = faq_group
    reaction_role_group = reaction_role_group
    role_menu_group = role_menu_group
    event_group = event_group
    welcome_group = welcome_group

    # NOTE: Discord rejects the whole command list (HTTP 400, code 50035) if any
    # command or option description is over 100 characters. tests/test_commands.py
    # checks every description against that limit.

    # ================================================================== #
    # Announcements
    # ================================================================== #
    @manage_group.command(name="announce", description="Post an announcement now.")
    @app_commands.describe(
        message="What to announce. Shown inside an embed.",
        title="Optional heading above the message.",
        channel="Where to post. Defaults to the server's announcement channel.",
        mention="Optional role to ping with it.",
        publish="Also publish to followers (announcement channels only).",
        pin_for="Pin it for this long, e.g. 24h. It is unpinned automatically.",
    )
    async def manage_announce(
        self,
        interaction: discord.Interaction,
        message: app_commands.Range[str, 1, MAX_CONTENT_LENGTH],
        title: Optional[app_commands.Range[str, 1, MAX_TITLE_LENGTH]] = None,
        channel: Optional[discord.TextChannel] = None,
        mention: Optional[discord.Role] = None,
        publish: bool = False,
        pin_for: Optional[app_commands.Range[str, 1, 20]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            result = await ops.announce_now(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                message=message,
                title=title,
                channel_id=channel.id if channel else None,
                mention_role_id=mention.id if mention else None,
                publish=publish,
                pin_for=pin_for,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        if result.get("queued"):
            await self._reply(interaction, _queued_text(result))
            return
        if result.get("held"):
            await self._reply(interaction, _held_text(result))
            return
        lines = [f"Posted in <#{result['channel_id']}>: {result['jump_url']}"]
        if publish and result["published"]:
            lines.append("Published to followers.")
        if result.get("pinned_until"):
            lines.append(f"Pinned until {_when_text(result['pinned_until'])}.")
        await self._reply(interaction, "\n".join(lines + _warn_lines(result["warnings"])))

    @manage_group.command(name="schedule", description="Post an announcement at a later time.")
    @app_commands.describe(
        when="A delay like 2h or 1d12h, or a time like 2026-10-20 18:00 (server time).",
        message="What to announce. Shown inside an embed.",
        title="Optional heading above the message.",
        channel="Where to post. Defaults to the server's announcement channel.",
        mention="Optional role to ping when it is posted.",
        publish="Also publish to followers (announcement channels only).",
        pin_for="Pin it for this long once posted, e.g. 24h. Unpinned automatically.",
    )
    async def manage_schedule(
        self,
        interaction: discord.Interaction,
        when: app_commands.Range[str, 1, 40],
        message: app_commands.Range[str, 1, MAX_CONTENT_LENGTH],
        title: Optional[app_commands.Range[str, 1, MAX_TITLE_LENGTH]] = None,
        channel: Optional[discord.TextChannel] = None,
        mention: Optional[discord.Role] = None,
        publish: bool = False,
        pin_for: Optional[app_commands.Range[str, 1, 20]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        await self._schedule(interaction, when, message, title, channel, mention, publish, None, pin_for)

    @manage_group.command(name="repeat", description="Post an announcement again and again, on a schedule.")
    @app_commands.describe(
        every="How often to post. For example 1d, 12h or 30m (at least 15 minutes).",
        message="What to announce. Shown inside an embed.",
        title="Optional heading above the message.",
        first_in="When the first post goes out, e.g. 1h. Defaults to one interval from now.",
        channel="Where to post. Defaults to the server's announcement channel.",
        mention="Optional role to ping each time.",
        publish="Also publish to followers (announcement channels only).",
        pin_for="Pin each post for this long, e.g. 6h. Shorter than the repeat gap.",
    )
    async def manage_repeat(
        self,
        interaction: discord.Interaction,
        every: app_commands.Range[str, 1, 20],
        message: app_commands.Range[str, 1, MAX_CONTENT_LENGTH],
        title: Optional[app_commands.Range[str, 1, MAX_TITLE_LENGTH]] = None,
        first_in: Optional[app_commands.Range[str, 1, 40]] = None,
        channel: Optional[discord.TextChannel] = None,
        mention: Optional[discord.Role] = None,
        publish: bool = False,
        pin_for: Optional[app_commands.Range[str, 1, 20]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        await self._schedule(interaction, first_in or every, message, title, channel, mention, publish, every, pin_for)

    async def _schedule(
        self, interaction, when, message, title, channel, mention, publish, repeat_every, pin_for=None
    ) -> None:
        try:
            result = await ops.schedule_announcement(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                when=when,
                message=message,
                title=title,
                channel_id=channel.id if channel else None,
                mention_role_id=mention.id if mention else None,
                publish=publish,
                repeat_every=repeat_every,
                pin_for=pin_for,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        if result.get("queued"):
            await self._reply(interaction, _queued_text(result))
            return
        if result["repeat_seconds"]:
            text = (
                f"Repeating **#{result['id']}** starts {_when_text(result['send_at'])} "
                f"in <#{result['channel_id']}>. Cancel it with `/manage cancel` and that number."
            )
            if result.get("pinned"):
                text += " Each post is pinned when it goes out."
        else:
            text = (
                f"Scheduled **#{result['id']}** for {_when_text(result['send_at'])} "
                f"in <#{result['channel_id']}>. Cancel it with `/manage cancel` and that number."
            )
            if result.get("pinned"):
                text += " It will be pinned when it posts."
        await self._reply(interaction, "\n".join([text] + _warn_lines(result["warnings"])))

    @manage_group.command(name="scheduled", description="List announcements waiting to be posted.")
    async def manage_scheduled(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        pending = ops.list_schedule(interaction.guild_id)
        if not pending:
            await self._reply(interaction, "Nothing is scheduled.")
            return
        lines = []
        for row in pending[:25]:
            if row["interval_seconds"]:
                kind = f"every {_human_seconds(row['interval_seconds'])}"
            else:
                kind = "once"
            lines.append(
                f"**#{row['id']}** · {kind} · next {_when_text(row['send_at'])} · "
                f"<#{row['channel_id']}> · {row['title']}"
            )
        if len(pending) > 25:
            lines.append(f"...and {len(pending) - 25} more.")
        embed = discord.Embed(title="Scheduled announcements", description="\n".join(lines), color=ann.EMBED_COLOR)
        await self._reply(interaction, embed=embed)

    @manage_group.command(name="cancel", description="Cancel a scheduled or repeating announcement.")
    @app_commands.describe(schedule_id="The number shown by /manage scheduled.")
    async def manage_cancel(
        self, interaction: discord.Interaction, schedule_id: app_commands.Range[int, 1, 2_000_000_000]
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            ops.cancel_schedule(interaction.guild_id, _actor(interaction), schedule_id)
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Cancelled **#{schedule_id}**.")

    @manage_group.command(name="history", description="Show the most recent announcements.")
    @app_commands.describe(limit="How many to show (1-25, default 10).")
    async def manage_history(
        self, interaction: discord.Interaction, limit: Optional[app_commands.Range[int, 1, 25]] = None
    ) -> None:
        if not await self._staff(interaction):
            return
        rows = ops.list_history(interaction.guild_id, limit or ann.DEFAULT_HISTORY_LIMIT)
        if not rows:
            await self._reply(interaction, "No announcements have been posted yet.")
            return
        lines = []
        for row in rows:
            how = {"scheduled": "scheduled", "repeat": "repeated"}.get(row["source"], "posted")
            jump = ""
            if row["message_id"]:
                jump = f" · [jump](https://discord.com/channels/{interaction.guild_id}/{row['channel_id']}/{row['message_id']})"
            lines.append(
                f"{_when_text(row['created_at'])} · {how} in <#{row['channel_id']}> · **{row['title']}**{jump}"
            )
        embed = discord.Embed(title="Recent announcements", description="\n".join(lines), color=ann.EMBED_COLOR)
        await self._reply(interaction, embed=embed)

    # ================================================================== #
    # Templates
    # ================================================================== #
    @template_group.command(name="save", description="Save announcement text under a name.")
    @app_commands.describe(
        name="A short name, for example raid-night.",
        message="The text to save.",
        title="Optional heading.",
    )
    async def template_save(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 40],
        message: app_commands.Range[str, 1, MAX_CONTENT_LENGTH],
        title: Optional[app_commands.Range[str, 1, MAX_TITLE_LENGTH]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            result = await ops.save_template(
                interaction.guild_id, _actor(interaction), name=name, title=title, message=message
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        verb = "Updated" if result["updated"] else "Saved"
        await self._reply(interaction, f"{verb} template `{result['name']}`.")

    @template_group.command(name="list", description="List the saved templates.")
    async def template_list(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        rows = ops.list_templates(interaction.guild_id)
        if not rows:
            await self._reply(interaction, "No templates yet. Save one with `/manage template save`.")
            return
        lines = [f"`{row['name']}` · {row['title']}" for row in rows]
        await self._reply(interaction, "\n".join(lines[:50]))

    @template_group.command(name="delete", description="Delete a saved template.")
    @app_commands.describe(name="The template's name.")
    async def template_delete(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._staff(interaction):
            return
        try:
            ops.delete_template(interaction.guild_id, _actor(interaction), name)
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Deleted template `{name}`.")

    @template_group.command(name="use", description="Post a saved template now.")
    @app_commands.describe(
        name="The template's name.",
        channel="Where to post. Defaults to the server's announcement channel.",
        mention="Optional role to ping with it.",
        publish="Also publish to followers (announcement channels only).",
    )
    async def template_use(
        self,
        interaction: discord.Interaction,
        name: str,
        channel: Optional[discord.TextChannel] = None,
        mention: Optional[discord.Role] = None,
        publish: bool = False,
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            result = await ops.use_template(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                name=name,
                channel_id=channel.id if channel else None,
                mention_role_id=mention.id if mention else None,
                publish=publish,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        if result.get("queued"):
            await self._reply(interaction, _queued_text(result))
            return
        await self._reply(
            interaction,
            "\n".join([f"Posted template `{name}` in <#{result['channel_id']}>: {result['jump_url']}"]
                      + _warn_lines(result["warnings"])),
        )

    # ================================================================== #
    # Polls
    # ================================================================== #
    @poll_group.command(name="create", description="Post a poll with a button for each option.")
    @app_commands.describe(
        question="The question to ask.",
        options="Choices separated by | (2 to 10), for example: Tuesday | Friday.",
        duration="Optional: how long it stays open, e.g. 2h or 3d. Otherwise close it by hand.",
        channel="Where to post. Defaults to the server's announcement channel.",
        multiple="Let members pick more than one option. Off means one pick each.",
        send_at="Optional: post later, e.g. 2h or 2026-10-24 18:00 (server time). Otherwise post now.",
    )
    async def poll_create(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, polls.MAX_QUESTION_LENGTH],
        options: app_commands.Range[str, 1, 1000],
        duration: Optional[app_commands.Range[str, 1, 20]] = None,
        channel: Optional[discord.TextChannel] = None,
        multiple: bool = False,
        send_at: Optional[app_commands.Range[str, 1, 40]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            result = await ops.create_poll(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                question=question,
                options=options,
                channel_id=channel.id if channel else None,
                duration=duration,
                multiple=multiple,
                send_at=send_at,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        if result["scheduled"]:
            await self._reply(
                interaction,
                f"Poll **#{result['id']}** is scheduled for {_when_text(result['send_at'])} in <#{result['channel_id']}>. "
                "Cancel it before then with `/manage poll close`.",
            )
            return
        closes = f" It closes {_when_text(result['ends_at'])}." if result["ends_at"] else " Close it with `/manage poll close`."
        choice = " Members can pick several options." if result["multiple"] else ""
        await self._reply(interaction, f"Poll **#{result['id']}** posted in <#{result['channel_id']}>.{choice}{closes}")

    @poll_group.command(name="list", description="List recent polls.")
    async def poll_list(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        rows = ops.list_polls(interaction.guild_id)
        if not rows:
            await self._reply(interaction, "No polls yet.")
            return
        lines = [
            f"**#{row['id']}** · {row['status']} · {row['question']} · <#{row['channel_id']}>"
            for row in rows[:25]
        ]
        await self._reply(interaction, "\n".join(lines))

    # ================================================================== #
    # Tickets
    # ================================================================== #
    @ticket_group.command(name="panel", description="Post the categorized support ticket panel.")
    @app_commands.describe(channel="Where to post the button. Defaults to the announcement channel.")
    async def ticket_panel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None) -> None:
        if not await self._staff(interaction):
            return
        try:
            result = await ops.post_ticket_panel(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                channel_id=channel.id if channel else None,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Categorized ticket panel posted in <#{result['channel_id']}>: {result['jump_url']}")

    @ticket_group.command(name="list", description="List open tickets, their category and assigned staff member.")
    async def ticket_list(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        rows = ops.list_tickets(interaction.guild_id, open_only=True)
        if not rows:
            await self._reply(interaction, "No tickets are open.")
            return
        lines = [
            f"**#{row['id']}** · <#{row['channel_id']}> · {row['opener_name'] or 'a member'} · "
            f"{tickets.category_label(row.get('category'))} · "
            f"{'🚨 urgent' if row.get('priority') == 'urgent' else 'normal'} · "
            f"{row.get('claimed_by_name') or 'unassigned'} · {_when_text(row['created_at'])}"
            for row in rows[:25]
        ]
        await self._reply(interaction, "\n".join(lines))

    @ticket_group.command(name="claim", description="Claim an unassigned ticket for yourself.")
    @app_commands.describe(ticket_id="Ticket number. Leave blank to use the current ticket channel.")
    async def ticket_claim(
        self,
        interaction: discord.Interaction,
        ticket_id: Optional[app_commands.Range[int, 1, 2_000_000_000]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        if ticket_id is None:
            ticket = ops.find_ticket_by_channel(interaction.guild_id, interaction.channel_id)
            if ticket is None:
                await self._reply(interaction, "Run this inside an open ticket, or give its number.")
                return
            ticket_id = ticket["id"]
        try:
            ticket = await ops.claim_ticket(
                self.bot.gateway, self.bot.config, interaction.guild_id, _actor(interaction), ticket_id
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Ticket **#{ticket_id}** is assigned to {ticket['claimed_by_name']}.")

    @ticket_group.command(name="assign", description="Assign an open ticket to a support staff member.")
    @app_commands.describe(
        member="Support staff member who will handle the ticket.",
        ticket_id="Ticket number. Leave blank to use the current ticket channel.",
    )
    async def ticket_assign(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        ticket_id: Optional[app_commands.Range[int, 1, 2_000_000_000]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        if member.bot or settings.staff_denial(member, self.bot.config, interaction.guild_id, guild=interaction.guild):
            await self._reply(interaction, "Choose a human member who has permission to use the staff commands.")
            return
        if ticket_id is None:
            ticket = ops.find_ticket_by_channel(interaction.guild_id, interaction.channel_id)
            if ticket is None:
                await self._reply(interaction, "Run this inside an open ticket, or give its number.")
                return
            ticket_id = ticket["id"]
        try:
            await ops.assign_ticket(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                ticket_id,
                Actor(member.id, member.display_name, "discord"),
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Assigned ticket **#{ticket_id}** to {member.mention}.")

    @ticket_group.command(name="priority", description="Set an open ticket's priority to normal or urgent.")
    @app_commands.describe(
        priority="Urgent tickets are highlighted in lists and staff logs.",
        ticket_id="Ticket number. Leave blank to use the current ticket channel.",
    )
    @app_commands.choices(
        priority=[
            app_commands.Choice(name="Normal", value="normal"),
            app_commands.Choice(name="Urgent", value="urgent"),
        ]
    )
    async def ticket_priority(
        self,
        interaction: discord.Interaction,
        priority: app_commands.Choice[str],
        ticket_id: Optional[app_commands.Range[int, 1, 2_000_000_000]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        if ticket_id is None:
            ticket = ops.find_ticket_by_channel(interaction.guild_id, interaction.channel_id)
            if ticket is None:
                await self._reply(interaction, "Run this inside an open ticket, or give its number.")
                return
            ticket_id = ticket["id"]
        try:
            await ops.change_ticket_priority(
                self.bot.gateway, self.bot.config, interaction.guild_id, _actor(interaction), ticket_id,
                priority.value, actor_is_staff=True,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Ticket **#{ticket_id}** priority is now **{priority.value}**.")

    @ticket_group.command(name="report", description="Show ticket volume and first-response times.")
    @app_commands.describe(days="How far back to report.")
    @app_commands.choices(
        days=[
            app_commands.Choice(name="7 days", value=7),
            app_commands.Choice(name="30 days", value=30),
            app_commands.Choice(name="90 days", value=90),
        ]
    )
    async def ticket_report(self, interaction: discord.Interaction, days: app_commands.Choice[int]) -> None:
        if not await self._staff(interaction):
            return
        report = ops.ticket_report(interaction.guild_id, days.value)
        avg = report["average_first_response_seconds"]
        if avg is None:
            avg_text = "No staff replies recorded"
        elif avg < 3600:
            avg_text = f"{avg / 60:.0f} min"
        else:
            avg_text = f"{avg / 3600:.1f} hr"
        embed = discord.Embed(title=f"Support report · last {days.value} days", color=ann.EMBED_COLOR)
        embed.add_field(name="Opened", value=str(report["opened"]), inline=True)
        embed.add_field(name="Closed", value=str(report["closed"]), inline=True)
        embed.add_field(name="Reopened", value=str(report["reopened"]), inline=True)
        embed.add_field(name="Urgent tickets open now", value=str(report["urgent_open"]), inline=True)
        embed.add_field(name="Average first staff reply", value=avg_text, inline=True)
        embed.add_field(name="Still awaiting first reply", value=str(report["waiting_first_response"]), inline=True)
        await self._reply(interaction, embed=embed)

    @ticket_group.command(name="reopen", description="Reopen your closed ticket or a ticket as staff.")
    @app_commands.describe(
        ticket_id="The closed ticket number.",
        note="Briefly say what still needs help.",
    )
    async def ticket_reopen(
        self,
        interaction: discord.Interaction,
        ticket_id: app_commands.Range[int, 1, 2_000_000_000],
        note: Optional[app_commands.Range[str, 1, 300]] = None,
    ) -> None:
        if not await self._begin(interaction):
            return
        actor = _actor(interaction)
        actor_is_staff = settings.staff_denial(
            interaction.user, self.bot.config, interaction.guild_id, guild=interaction.guild
        ) is None
        try:
            result = await ops.reopen_ticket(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                actor,
                ticket_id,
                actor_is_staff=actor_is_staff,
                note=note,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Ticket **#{result['id']}** is open again: <#{result['channel_id']}>.")

    @ticket_group.command(name="close", description="Close a ticket and delete its channel.")
    @app_commands.describe(
        ticket_id="The ticket's number. Leave it out when you are inside the ticket.",
        reason="Briefly say how the issue was resolved or why the ticket is closing.",
    )
    async def ticket_close(
        self,
        interaction: discord.Interaction,
        ticket_id: Optional[app_commands.Range[int, 1, 2_000_000_000]] = None,
        reason: Optional[app_commands.Range[str, 1, 300]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        if ticket_id is None:
            ticket = ops.find_ticket_by_channel(interaction.guild_id, interaction.channel_id)
            if ticket is None:
                await self._reply(interaction, "Run this inside a ticket's channel, or give the ticket's number.")
                return
            ticket_id = ticket["id"]
        try:
            result = await ops.close_ticket(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                ticket_id,
                actor_is_staff=True,
                reason=reason,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Closed ticket **#{result['id']}** and deleted its channel.")

    # ================================================================== #
    # Saved support replies
    # ================================================================== #
    @faq_group.command(name="save", description="Create or update a saved support reply.")
    @app_commands.describe(
        key="Short name for the reply, such as install-help.",
        title="A short description staff see in the FAQ list.",
        response="The text sent into the current ticket.",
    )
    async def faq_save(
        self,
        interaction: discord.Interaction,
        key: app_commands.Range[str, 1, faqs.MAX_KEY_LENGTH],
        title: app_commands.Range[str, 1, faqs.MAX_TITLE_LENGTH],
        response: app_commands.Range[str, 1, faqs.MAX_BODY_LENGTH],
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            saved = await ops.save_ticket_faq(
                interaction.guild_id, _actor(interaction), key=key, title=title, body=response
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Saved support reply `{saved['key']}`: {saved['title']}.")

    @faq_group.command(name="list", description="List saved support replies.")
    async def faq_list(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        rows = ops.list_ticket_faqs(interaction.guild_id)
        if not rows:
            await self._reply(interaction, "No saved support replies yet. Use `/manage faq save` to add one.")
            return
        await self._reply(interaction, "\\n".join(f"`{row['key']}` · {row['title']}" for row in rows[:25]))

    @faq_group.command(name="delete", description="Delete a saved support reply.")
    @app_commands.describe(key="Name of the saved reply.")
    async def faq_delete(
        self,
        interaction: discord.Interaction,
        key: app_commands.Range[str, 1, faqs.MAX_KEY_LENGTH],
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            await ops.delete_ticket_faq(interaction.guild_id, _actor(interaction), key=key)
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Deleted saved reply `{key}`.")

    @faq_group.command(name="use", description="Send a saved reply into the current ticket.")
    @app_commands.describe(key="Name of the saved reply.")
    async def faq_use(
        self,
        interaction: discord.Interaction,
        key: app_commands.Range[str, 1, faqs.MAX_KEY_LENGTH],
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            saved = await ops.use_ticket_faq(
                self.bot.gateway, interaction.guild_id, _actor(interaction), interaction.channel_id, key=key
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Sent **{saved['title']}** into this ticket.")

    # ================================================================== #
    # Reaction roles
    # ================================================================== #
    @reaction_role_group.command(name="add", description="Give a role to members who react with an emoji.")
    @app_commands.describe(
        message="A message link (Copy Message Link), or the message's ID.",
        emoji="The emoji to react with: a standard emoji or a server emoji.",
        role="The role to give. My role must be above it.",
        channel="Only needed with a message ID: the channel the message is in.",
    )
    async def reactionrole_add(
        self,
        interaction: discord.Interaction,
        message: app_commands.Range[str, 1, 200],
        emoji: app_commands.Range[str, 1, 64],
        role: discord.Role,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            result = await ops.add_reaction_role(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                message=message,
                emoji=emoji,
                role_id=role.id,
                channel_id=channel.id if channel else None,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(
            interaction,
            f"Reaction role **#{result['id']}** is set. Reacting {result['emoji']} on that message gives @{role.name}.",
        )

    @reaction_role_group.command(name="list", description="List the reaction roles.")
    async def reactionrole_list(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        rows = ops.list_reaction_roles(interaction.guild_id)
        if not rows:
            await self._reply(interaction, "No reaction roles yet.")
            return
        lines = [
            f"**#{row['id']}** · {row['emoji']} · <@&{row['role_id']}> · message {row['message_id']} in <#{row['channel_id']}>"
            for row in rows[:25]
        ]
        await self._reply(interaction, "\n".join(lines))

    @reaction_role_group.command(name="remove", description="Stop an emoji giving a role. Roles already given stay.")
    @app_commands.describe(rule_id="The number shown by /manage reactionrole list.")
    async def reactionrole_remove(
        self, interaction: discord.Interaction, rule_id: app_commands.Range[int, 1, 2_000_000_000]
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            ops.remove_reaction_role(interaction.guild_id, _actor(interaction), rule_id)
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Removed reaction role **#{rule_id}**.")

    # ================================================================== #
    # Role menus
    # ================================================================== #
    @role_menu_group.command(name="new", description="Start a role menu, then add roles and post it.")
    @app_commands.describe(
        title="The heading members see above the menu.",
        description="Optional text shown above the list of roles.",
    )
    async def rolemenu_new(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, role_menus.MAX_TITLE_LENGTH],
        description: Optional[app_commands.Range[str, 1, role_menus.MAX_DESCRIPTION_LENGTH]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            result = await ops.create_role_menu(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                title=title,
                description=description or "",
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(
            interaction,
            f"Role menu **#{result['id']}** is ready. Add roles with `/manage rolemenu add`, "
            "then post it with `/manage rolemenu post`.",
        )

    @role_menu_group.command(name="add", description="Add a role that members can pick from a menu.")
    @app_commands.describe(
        menu_id="The number of the menu, from /manage rolemenu list.",
        role="The role members can pick. My role must be above it.",
        label="Optional name shown in the menu. Defaults to the role's name.",
        description="Optional short note shown beside the role.",
    )
    async def rolemenu_add(
        self,
        interaction: discord.Interaction,
        menu_id: app_commands.Range[int, 1, 2_000_000_000],
        role: discord.Role,
        label: Optional[app_commands.Range[str, 1, role_menus.MAX_LABEL_LENGTH]] = None,
        description: Optional[app_commands.Range[str, 1, role_menus.MAX_OPTION_DESCRIPTION_LENGTH]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            result = await ops.add_role_menu_option(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                menu_id=menu_id,
                role_id=role.id,
                label=label,
                description=description,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Added **{result['label']}** ({role.mention}) to menu **#{menu_id}**.")

    @role_menu_group.command(name="remove", description="Take a role out of a menu.")
    @app_commands.describe(menu_id="The number of the menu.", role="The role to take out.")
    async def rolemenu_remove(
        self,
        interaction: discord.Interaction,
        menu_id: app_commands.Range[int, 1, 2_000_000_000],
        role: discord.Role,
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            await ops.remove_role_menu_option(
                self.bot.gateway, self.bot.config, interaction.guild_id, _actor(interaction), menu_id=menu_id, role_id=role.id
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Removed {role.mention} from menu **#{menu_id}**.")

    @role_menu_group.command(name="post", description="Post a role menu's dropdown in a channel.")
    @app_commands.describe(
        menu_id="The number of the menu.",
        channel="Where to post. Defaults to the server's announcement channel.",
    )
    async def rolemenu_post(
        self,
        interaction: discord.Interaction,
        menu_id: app_commands.Range[int, 1, 2_000_000_000],
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            result = await ops.post_role_menu(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                menu_id=menu_id,
                channel_id=channel.id if channel else None,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Menu **#{menu_id}** posted: {result['jump_url']}")

    @role_menu_group.command(name="list", description="List the role menus.")
    async def rolemenu_list(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        rows = ops.list_role_menus(interaction.guild_id)
        if not rows:
            await self._reply(interaction, "No role menus yet.")
            return
        lines = []
        for row in rows[:25]:
            where = f"posted in <#{row['channel_id']}>" if row["message_id"] else "not posted"
            lines.append(f"**#{row['id']}** · {row['title']} · {len(row['options'])} role(s) · {where}")
        await self._reply(interaction, "\n".join(lines))

    @role_menu_group.command(name="delete", description="Delete a role menu and its message.")
    @app_commands.describe(menu_id="The number of the menu.")
    async def rolemenu_delete(
        self, interaction: discord.Interaction, menu_id: app_commands.Range[int, 1, 2_000_000_000]
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            await ops.delete_role_menu(self.bot.gateway, self.bot.config, interaction.guild_id, _actor(interaction), menu_id=menu_id)
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Deleted role menu **#{menu_id}**.")

    @poll_group.command(name="close", description="Close a poll now and post its results, or cancel a scheduled one.")
    @app_commands.describe(poll_id="The number shown by /manage poll list.")
    async def poll_close(self, interaction: discord.Interaction, poll_id: app_commands.Range[int, 1, 2_000_000_000]) -> None:
        if not await self._staff(interaction):
            return
        try:
            result = await ops.close_poll(self.bot.gateway, self.bot.config, interaction.guild_id, _actor(interaction), poll_id)
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        if result.get("cancelled"):
            await self._reply(interaction, f"Cancelled the scheduled poll **#{result['id']}**: {result['question']}")
            return
        lines = [f"Closed poll **#{result['id']}**: {result['question']}"]
        for option, count in zip(result["options"], result["votes"]):
            lines.append(f"{option}: {count}")
        await self._reply(interaction, "\n".join(lines))

    # ================================================================== #
    # Events
    # ================================================================== #
    @event_group.command(name="create", description="Post an event with an RSVP reaction.")
    @app_commands.describe(
        title="The event's name.",
        starts="A delay like 2h, or a time like 2026-10-20 19:00 (server time).",
        description="Optional details shown on the event.",
        channel="Where to post. Defaults to the server's announcement channel.",
        remind_minutes="Minutes before the start to post a reminder. 0 for none.",
    )
    async def event_create(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, events.MAX_TITLE_LENGTH],
        starts: app_commands.Range[str, 1, 40],
        description: Optional[app_commands.Range[str, 0, events.MAX_DESCRIPTION_LENGTH]] = None,
        channel: Optional[discord.TextChannel] = None,
        remind_minutes: Optional[app_commands.Range[int, 0, events.MAX_REMIND_MINUTES]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            result = await ops.create_event(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                title=title,
                starts_when=starts,
                description=description or "",
                channel_id=channel.id if channel else None,
                remind_minutes=remind_minutes,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        reminder = f" Reminder {_when_text(result['reminder_at'])}." if result["reminder_at"] else ""
        await self._reply(
            interaction,
            f"Event **#{result['id']}** posted in <#{result['channel_id']}> for {_when_text(result['starts_at'])}.{reminder}",
        )

    @event_group.command(name="list", description="List upcoming events and their RSVP counts.")
    async def event_list(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        rows = await ops.list_events(self.bot.gateway, interaction.guild_id)
        if not rows:
            await self._reply(interaction, "No upcoming events.")
            return
        lines = []
        for row in rows[:25]:
            going = "?" if row["going"] is None else str(row["going"])
            lines.append(f"**#{row['id']}** · {row['title']} · {_when_text(row['starts_at'])} · going: {going}")
        await self._reply(interaction, "\n".join(lines))

    @event_group.command(name="cancel", description="Cancel an upcoming event.")
    @app_commands.describe(event_id="The number shown by /manage event list.")
    async def event_cancel(self, interaction: discord.Interaction, event_id: app_commands.Range[int, 1, 2_000_000_000]) -> None:
        if not await self._staff(interaction):
            return
        try:
            await ops.cancel_event(self.bot.gateway, self.bot.config, interaction.guild_id, _actor(interaction), event_id)
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Cancelled event **#{event_id}**.")

    # ================================================================== #
    # Welcome
    # ================================================================== #
    @welcome_group.command(name="set", description="Post a welcome message when members join.")
    @app_commands.describe(
        channel="Where to post the welcome.",
        message="Optional. Use {mention}, {name}, {server} or {members}.",
    )
    async def welcome_set(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: Optional[app_commands.Range[str, 1, welcome.MAX_TEMPLATE_LENGTH]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            await ops.set_welcome(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                channel_id=channel.id,
                message=message,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"New members will be welcomed in {channel.mention}.")

    @welcome_group.command(name="off", description="Turn off the welcome message.")
    async def welcome_off(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        ops.disable_welcome(self.bot.config, interaction.guild_id, _actor(interaction))
        await self._reply(interaction, "Welcome messages are off.")

    @welcome_group.command(name="test", description="Post a sample welcome for yourself.")
    async def welcome_test(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        guild = interaction.guild
        try:
            result = await ops.send_test_welcome(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                server=guild.name if guild else "this server",
                members=(guild.member_count or 0) if guild else 0,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Sample welcome posted in <#{result['channel_id']}>.")

    # ================================================================== #
    # Server
    # ================================================================== #
    @manage_group.command(name="status", description="Show this server's settings and what is due next.")
    async def manage_status(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        guild_id = interaction.guild_id
        cfg = settings.get_guild_config(self.bot.config, guild_id)
        data = ops.overview(self.bot.config, guild_id)
        embed = discord.Embed(title="Soundtrail Support status", color=ann.EMBED_COLOR)
        embed.add_field(
            name="Staff role",
            value=f"<@&{cfg['staff_role_id']}>" if cfg["staff_role_id"] else "Not set (Manage Server is used)",
            inline=False,
        )
        embed.add_field(name="Announcements", value=_or_unset(cfg["announcement_channel_id"]), inline=True)
        embed.add_field(name="Staff log", value=_or_unset(cfg["staff_channel_id"]), inline=True)
        embed.add_field(name="Welcome", value=_or_unset(cfg["welcome_channel_id"]), inline=True)
        embed.add_field(name="Timezone", value=cfg["timezone"], inline=True)
        embed.add_field(
            name="Queued",
            value=f"{data['scheduled']} scheduled · {data['repeats']} repeating",
            inline=True,
        )
        embed.add_field(
            name="Open",
            value=f"{data['polls_open']} polls · {data['events_upcoming']} upcoming events",
            inline=True,
        )
        embed.add_field(name="Templates", value=str(data["templates"]), inline=True)
        embed.add_field(
            name="Approval queue",
            value=f"{'On' if cfg['require_approval'] else 'Off'} · {data['approvals_pending']} waiting",
            inline=True,
        )
        quiet_start, quiet_end = settings.get_quiet_hours(self.bot.config, guild_id)
        quiet_text = f"{quiet_start} to {quiet_end}" if quiet_start else "Off"
        if quiet_start and settings.get_quiet_hold_manual(self.bot.config, guild_id):
            quiet_text += " · holds posts by hand too"
        embed.add_field(name="Quiet hours", value=quiet_text, inline=True)
        embed.add_field(name="Pinned", value=str(data["pins_active"]), inline=True)
        retention = settings.get_ticket_transcript_retention_days(self.bot.config, guild_id)
        retention_text = f"{retention} day(s)" if retention else "Forever"
        embed.add_field(name="Ticket transcript retention", value=retention_text, inline=True)
        support_start, support_end = settings.get_support_hours(self.bot.config, guild_id)
        support_text = f"{support_start}–{support_end} ({cfg['timezone']})" if support_start else "Always on"
        embed.add_field(name="Support hours", value=support_text, inline=True)
        await self._reply(interaction, embed=embed)

    @manage_group.command(name="audit", description="Show recent staff actions, newest first.")
    @app_commands.describe(limit="How many to show (1-25, default 10).")
    async def manage_audit(self, interaction: discord.Interaction, limit: Optional[app_commands.Range[int, 1, 25]] = None) -> None:
        if not await self._staff(interaction):
            return
        rows = audit.list_entries(interaction.guild_id, limit or 10)
        if not rows:
            await self._reply(interaction, "Nothing has been recorded yet.")
            return
        lines = [
            f"{_when_text(row['created_at'])} · **{row['actor_name']}** ({row['source']}) · `{row['action']}` {row['detail']}"
            for row in rows
        ]
        embed = discord.Embed(title="Recent staff actions", description="\n".join(lines)[:4000], color=ann.EMBED_COLOR)
        await self._reply(interaction, embed=embed)

    # ================================================================== #
    # Approvals, editing and deleting, quiet hours
    # ================================================================== #
    @manage_group.command(name="queue", description="List announcements waiting for approval.")
    async def manage_queue(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        pending = ops.list_approvals(interaction.guild_id)
        if not pending:
            await self._reply(interaction, "Nothing is waiting for approval.")
            return
        lines = [
            f"**#{row['id']}** · {row['kind']} · requested by {row['requester_name']} · "
            f"{_when_text(row['created_at'])} · {row['summary']}"
            for row in pending[:25]
        ]
        if len(pending) > 25:
            lines.append(f"...and {len(pending) - 25} more.")
        embed = discord.Embed(title="Waiting for approval", description="\n".join(lines)[:4000], color=ann.EMBED_COLOR)
        embed.set_footer(text="Approve with /manage approve, or turn one down with /manage reject.")
        await self._reply(interaction, embed=embed)

    @manage_group.command(name="approve", description="Approve an announcement waiting in the queue.")
    @app_commands.describe(request_id="The number shown by /manage queue.")
    async def manage_approve(
        self, interaction: discord.Interaction, request_id: app_commands.Range[int, 1, 2_000_000_000]
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            result = await ops.approve_request(
                self.bot.gateway, self.bot.config, interaction.guild_id, _actor(interaction), request_id
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        lines = [f"Approved **#{request_id}**."]
        if result.get("jump_url"):
            lines.append(f"Posted: {result['jump_url']}")
        if result.get("held"):
            lines.append(f"Quiet hours are on, so it is held as **#{result['schedule_id']}** until {_when_text(result['send_at'])}.")
        elif result.get("send_at"):
            lines.append(f"Scheduled for {_when_text(result['send_at'])}.")
        await self._reply(interaction, "\n".join(lines + _warn_lines(result.get("warnings", []))))

    @manage_group.command(name="reject", description="Turn down an announcement waiting in the queue.")
    @app_commands.describe(request_id="The number shown by /manage queue.")
    async def manage_reject(
        self, interaction: discord.Interaction, request_id: app_commands.Range[int, 1, 2_000_000_000]
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            await ops.reject_request(interaction.guild_id, _actor(interaction), request_id)
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Turned down **#{request_id}**.")

    @manage_group.command(name="edit", description="Change the text of an announcement the bot posted.")
    @app_commands.describe(
        message_id="The message's ID. Right-click it and choose Copy Message ID.",
        message="New text. Leave out to keep the current text.",
        title="New heading. Leave out to keep the current heading.",
    )
    async def manage_edit(
        self,
        interaction: discord.Interaction,
        message_id: app_commands.Range[str, 1, 25],
        message: Optional[app_commands.Range[str, 1, MAX_CONTENT_LENGTH]] = None,
        title: Optional[app_commands.Range[str, 1, MAX_TITLE_LENGTH]] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            result = await ops.edit_announcement(
                self.bot.gateway,
                self.bot.config,
                interaction.guild_id,
                _actor(interaction),
                message_id=message_id,
                message=message,
                title=title,
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, f"Updated the announcement “{result['title']}”.")

    @manage_group.command(name="delete", description="Delete an announcement the bot posted.")
    @app_commands.describe(message_id="The message's ID. Right-click it and choose Copy Message ID.")
    async def manage_delete(
        self, interaction: discord.Interaction, message_id: app_commands.Range[str, 1, 25]
    ) -> None:
        if not await self._staff(interaction):
            return
        try:
            await ops.delete_announcement(
                self.bot.gateway, self.bot.config, interaction.guild_id, _actor(interaction), message_id=message_id
            )
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, "Deleted the announcement.")

    @manage_group.command(name="quiet", description="Quiet hours for scheduled posts, and optionally for posts by hand.")
    @app_commands.describe(
        start="When quiet time starts, 24-hour, e.g. 22:00. Type off to turn it off.",
        end="When it ends, 24-hour, e.g. 08:00.",
        hold_manual="Also hold posts made by hand until quiet hours end.",
    )
    async def manage_quiet(
        self,
        interaction: discord.Interaction,
        start: Optional[app_commands.Range[str, 1, 10]] = None,
        end: Optional[app_commands.Range[str, 1, 10]] = None,
        hold_manual: Optional[bool] = None,
    ) -> None:
        if not await self._staff(interaction):
            return
        guild_id = interaction.guild_id
        if start is None and end is None and hold_manual is None:
            quiet_start, quiet_end = settings.get_quiet_hours(self.bot.config, guild_id)
            if quiet_start and quiet_end:
                text = f"Quiet hours are {quiet_start} to {quiet_end} (server time). Scheduled posts wait for them to end."
                text += (
                    " Posts made by hand wait too."
                    if settings.get_quiet_hold_manual(self.bot.config, guild_id)
                    else " Posts made by hand go out at once."
                )
            else:
                text = "Quiet hours are off."
            await self._reply(interaction, text)
            return
        changes: dict[str, Any] = {}
        if start is not None and start.strip().lower() == "off":
            changes.update({"quiet_start": None, "quiet_end": None})
        elif start is not None or end is not None:
            changes.update({"quiet_start": start, "quiet_end": end})
        if hold_manual is not None:
            changes["quiet_hold_manual"] = hold_manual
        try:
            await ops.update_settings(self.bot.gateway, self.bot.config, guild_id, _actor(interaction), changes)
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        quiet_start, quiet_end = settings.get_quiet_hours(self.bot.config, guild_id)
        holding = settings.get_quiet_hold_manual(self.bot.config, guild_id)
        if not quiet_start:
            await self._reply(interaction, "Quiet hours are off.")
            return
        manual = "Posts made by hand wait too." if holding else "Posts made by hand go out at once."
        await self._reply(
            interaction,
            f"Quiet hours set: {quiet_start} to {quiet_end} (server time). Scheduled posts due in that "
            f"window wait for it to end. {manual}",
        )

    @manage_group.command(name="pins", description="Show posts that are pinned right now, and when they unpin.")
    async def manage_pins(self, interaction: discord.Interaction) -> None:
        if not await self._staff(interaction):
            return
        rows = ops.list_pins(interaction.guild_id)
        if not rows:
            await self._reply(interaction, "Nothing is pinned right now.")
            return
        lines = [
            f"<#{row['channel_id']}> · [jump to post](https://discord.com/channels/"
            f"{row['guild_id']}/{row['channel_id']}/{row['message_id']}) · unpins {_when_text(row['unpin_at'])}"
            for row in rows[:25]
        ]
        if len(rows) > 25:
            lines.append(f"...and {len(rows) - 25} more.")
        embed = discord.Embed(title="Pinned posts", description="\n".join(lines)[:4000], color=ann.EMBED_COLOR)
        await self._reply(interaction, embed=embed)

    @manage_group.command(name="setup", description="Set the staff role, channels, timezone and dashboard links.")
    @app_commands.describe(
        staff_role="Members with this role can run /manage commands.",
        announcement_channel="Default channel for announcements, polls and events.",
        staff_channel="Channel that logs everything that is posted.",
        timezone_name="IANA timezone for typed times, e.g. America/Toronto.",
        require_approval="Hold announcements until another staff member approves them.",
        ticket_category="Category that ticket channels are created in.",
        transcript_retention_days="Local transcript lifetime in days; 0 keeps transcripts forever.",
        support_start="Daily support opening time in server timezone; use off with support_end to clear.",
        support_end="Daily support closing time in server timezone; use off with support_start to clear.",
        after_hours_message="Message shown on new tickets outside support hours; use off for the default.",
    )
    @app_commands.rename(timezone_name="timezone")
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        staff_role: Optional[discord.Role] = None,
        announcement_channel: Optional[discord.TextChannel] = None,
        staff_channel: Optional[discord.TextChannel] = None,
        timezone_name: Optional[app_commands.Range[str, 1, 64]] = None,
        require_approval: Optional[bool] = None,
        ticket_category: Optional[discord.CategoryChannel] = None,
        transcript_retention_days: Optional[app_commands.Range[int, 0, ops.MAX_TRANSCRIPT_RETENTION_DAYS]] = None,
        support_start: Optional[app_commands.Range[str, 1, 5]] = None,
        support_end: Optional[app_commands.Range[str, 1, 5]] = None,
        after_hours_message: Optional[app_commands.Range[str, 1, 1000]] = None,
    ) -> None:
        if not await self._begin(interaction):
            return
        if not is_administrator(interaction):
            await self._reply(interaction, "Only administrators can change the setup.")
            return
        changes: dict[str, Any] = {}
        if staff_role:
            changes["staff_role_id"] = staff_role.id
        if announcement_channel:
            changes["announcement_channel_id"] = announcement_channel.id
        if staff_channel:
            changes["staff_channel_id"] = staff_channel.id
        if timezone_name:
            changes["timezone"] = timezone_name
        if require_approval is not None:
            changes["require_approval"] = require_approval
        if ticket_category:
            changes["ticket_category_id"] = ticket_category.id
        if transcript_retention_days is not None:
            changes["ticket_transcript_retention_days"] = transcript_retention_days
        if support_start is not None or support_end is not None:
            if (support_start or "").strip().lower() == "off" or (support_end or "").strip().lower() == "off":
                changes.update({"support_hours_start": None, "support_hours_end": None})
            else:
                if support_start is not None:
                    changes["support_hours_start"] = support_start
                if support_end is not None:
                    changes["support_hours_end"] = support_end
        if after_hours_message is not None:
            changes["after_hours_message"] = (
                None if after_hours_message.strip().lower() == "off" else after_hours_message
            )
        if not changes:
            await self._reply(interaction, "Nothing changed. Pass at least one option to update it.")
            return
        try:
            await ops.update_settings(self.bot.gateway, self.bot.config, interaction.guild_id, _actor(interaction), changes)
        except OperationError as exc:
            await self._reply(interaction, str(exc))
            return
        await self._reply(interaction, "Setup saved. Run `/manage status` to see it all.")

    @manage_group.command(name="fixcommands", description="Re-sync this server's /manage commands.")
    async def fixcommands_cmd(self, interaction: discord.Interaction) -> None:
        if not await self._begin(interaction):
            return
        if not is_administrator(interaction):
            await self._reply(interaction, "Only administrators can re-sync commands.")
            return
        count = await self.bot.sync_guild_commands(interaction.guild_id, force=True)
        await self._reply(
            interaction,
            f"Re-synced {count} command(s). If Discord still shows duplicates, restart the Discord client (Ctrl+R).",
        )

    # ================================================================== #
    # Shared checks and replies
    # ================================================================== #
    async def _begin(self, interaction: discord.Interaction) -> bool:
        """Defer the reply (ephemeral) and refuse anything outside a server."""
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            return False
        if interaction.guild is None or interaction.guild_id is None:
            await self._reply(interaction, "Soundtrail Support commands only work inside a server.")
            return False
        return True

    async def _staff(self, interaction: discord.Interaction) -> bool:
        if not await self._begin(interaction):
            return False
        denial = staff_denial(interaction.user, self.bot.config, interaction.guild_id, guild=interaction.guild)
        if denial is None:
            return True
        await self._reply(interaction, denial)
        return False

    async def _reply(
        self, interaction: discord.Interaction, content: Optional[str] = None, *, embed: Optional[discord.Embed] = None
    ) -> None:
        """Ephemeral answer to the command. Never raises."""
        try:
            await interaction.followup.send(content, embed=embed, ephemeral=True)
        except discord.HTTPException as exc:
            logger.warning("Could not reply to a /manage command: %s", exc)


def _or_unset(value: Any) -> str:
    return f"<#{value}>" if value else "Not set"


def _human_seconds(seconds: int) -> str:
    for unit, size in (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return f"{count} {unit}{'s' if count != 1 else ''}"
    return f"{seconds} seconds"


async def _register_cog(bot_: SoundtrailSupportBot) -> None:
    if bot_.get_cog("CombatMasterCog") is None:
        await bot_.add_cog(CombatMasterCog(bot_))


# --------------------------------------------------------------------------- #
# Module-level bot. Created at import so the command tree can be inspected.
# It starts from the defaults; main() loads the live config before anything
# reads it, so importing this module never touches the data directory.
# --------------------------------------------------------------------------- #
bot = SoundtrailSupportBot(copy.deepcopy(DEFAULT_CONFIG))


# --------------------------------------------------------------------------- #
# Slash command error handling
# --------------------------------------------------------------------------- #
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CheckFailure):
        msg = "You don't have permission to run that command."
    elif isinstance(error, app_commands.CommandNotFound):
        return
    elif isinstance(error, app_commands.MissingPermissions):
        msg = f"You're missing permissions: {', '.join(error.missing_permissions)}."
    elif isinstance(error, app_commands.BotMissingPermissions):
        msg = f"I'm missing permissions: {', '.join(error.missing_permissions)}."
    elif isinstance(error, app_commands.TransformerError):
        msg = f"Invalid value for `{error.param.name}`."
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = "This command can only be used in a server."
    else:
        logger.exception("Unhandled slash command error: %s", error)
        msg = "Something went wrong while running that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException as exc:
        logger.warning("Failed to deliver error message: %s", exc)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _help() -> str:
    return (
        "usage: python bot.py [--paths | --version | --dashboard-token | --demo [--host H] [--port P] | --check]\n"
        "  (no option)        run the Discord bot and its dashboard\n"
        "  --paths            show where this install keeps its files\n"
        "  --version          show the version\n"
        "  --dashboard-token  print the dashboard login token (creates one if needed)\n"
        "  --demo             run the dashboard over simulated servers (no token, no Discord)\n"
        "  --check            check the configuration without connecting to Discord\n"
    )


def _option(args: list[str], name: str, default: str) -> str:
    if name in args:
        index = args.index(name)
        if index + 1 < len(args):
            return args[index + 1]
    return default


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--help" in args or "-h" in args:
        print(_help())
        return 0
    if "--paths" in args:
        print(paths.describe())
        return 0
    if "--version" in args:
        print(paths.app_version())
        return 0
    if "--demo" in args:
        from demo import run_demo

        host = _option(args, "--host", "127.0.0.1")
        port = int(_option(args, "--port", "8765"))
        try:
            asyncio.run(run_demo(host, port))
        except KeyboardInterrupt:
            pass
        return 0

    _attach_file_log()
    bot.config = load_config()
    logger.info("Soundtrail Support %s", paths.app_version())
    logger.info("Data directory: %s", paths.data_dir())
    store.init_db()
    if "--dashboard-token" in args:
        print(ensure_dashboard_token(bot.config))
        return 0
    if "--check" in args:
        token = resolve_token(bot.config)
        print("Token: " + ("set" if token else "MISSING (set DISCORD_TOKEN or bot_token)"))
        print(f"Dashboard: {'enabled' if bot.config.get('dashboard_enabled', True) else 'disabled'} on "
              f"{bot.config.get('dashboard_host')}:{bot.config.get('dashboard_port')}")
        print(f"Configured servers: {len(bot.config.get('guilds') or {})}")
        return 0 if token else 1

    token = resolve_token(bot.config)
    if not token:
        logger.error(
            "No Discord token. Set DISCORD_TOKEN, or put it in %s under 'bot_token'.",
            paths.config_path(),
        )
        return 1
    if bot.config.get("dashboard_enabled", True):
        ensure_dashboard_token(bot.config)
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        logger.error("Login failed - is the token valid?")
        return 1
    except OSError as exc:  # aiohttp connection errors are OSError subclasses
        logger.error("Could not reach Discord (%s). Check the network connection.", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
