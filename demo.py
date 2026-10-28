"""Demo mode: the dashboard and scheduler with simulated servers and no token.

    python bot.py --demo

It runs the same operations as the live bot, against :class:`DemoGateway`,
which keeps posted messages and reactions in memory. Nothing is sent to
Discord. The database is a temporary directory unless ``SOUNDTRAIL_SUPPORT_DATA``
is set, so a demo never touches a real install.

:class:`DemoGateway` is also what the test suite uses as its fake Discord.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Optional

import discord

import settings
import store
from operations import OperationError
from discord_gateway import _norm

logger = logging.getLogger("soundtrail_support.demo")

DEMO_GUILDS = [
    {
        "id": 100000000000000001,
        "name": "Combat Arena",
        "member_count": 1284,
        "channels": [
            (100000000000000101, "announcements", True),
            (100000000000000102, "general", False),
            (100000000000000103, "events", False),
            (100000000000000104, "polls", False),
            (100000000000000105, "staff-log", False),
        ],
        "categories": [(100000000000000901, "Support")],
        "roles": [
            (100000000000000201, "Staff", True),
            (100000000000000202, "Member", True),
            (100000000000000203, "Event Pings", False),
        ],
    },
    {
        "id": 100000000000000002,
        "name": "Lofi Study Hall",
        "member_count": 312,
        "channels": [
            (100000000000000301, "news", True),
            (100000000000000302, "chat", False),
            (100000000000000303, "mod-log", False),
        ],
        "categories": [],
        "roles": [
            (100000000000000401, "Moderators", True),
            (100000000000000402, "Regulars", True),
        ],
    },
]

DEMO_MEMBER_IDS = [100000000000001000 + i for i in range(60)]


class DemoGateway:
    """An in-memory Discord. Votes are simulated unless a test sets them."""

    def __init__(self, guilds: Optional[list[dict[str, Any]]] = None) -> None:
        self._guilds = copy.deepcopy(guilds if guilds is not None else DEMO_GUILDS)
        self._channel_index: dict[int, tuple[int, str]] = {}
        for guild in self._guilds:
            for cid, name, *_ in guild["channels"]:
                self._channel_index[cid] = (guild["id"], name)
        self._next_message = 900000000000000000
        self.messages: dict[tuple[int, int], dict[str, Any]] = {}
        # Explicit vote counts for tests: (message_id, emoji) -> raw count.
        self.counts: dict[tuple[int, str], int] = {}
        self.posts: list[dict[str, Any]] = []
        self.fail_posts_in: set[int] = set()
        self.pinned: set[tuple[int, int]] = set()
        self.deleted: list[tuple[int, int]] = []
        self.fail_pins = False  # tests set this to simulate missing Manage Messages
        # Buttons and roles. Tests read these; they do not change what the demo shows.
        self.deleted_channels: list[int] = []
        self.created_channels: list[dict[str, Any]] = []
        self.role_changes: list[tuple[int, int, int, bool]] = []  # (guild, user, role, add)
        self.unassignable_roles: set[int] = set()  # tests set this to simulate a role above the bot
        self.fail_ticket_creation = False  # tests set this to simulate missing Manage Channels
        # Transcripts, files and menus. say() simulates someone writing in a channel.
        self.history: dict[int, list[dict[str, Any]]] = {}
        self.files: list[dict[str, Any]] = []
        self.dm_posts: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.unreadable: set[int] = set()  # tests set this to simulate a channel that can not be read

    # ---- lookups ------------------------------------------------------ #
    def _find(self, guild_id: int) -> Optional[dict[str, Any]]:
        return next((g for g in self._guilds if g["id"] == int(guild_id)), None)

    def guilds(self) -> list[dict[str, Any]]:
        return [{"id": g["id"], "name": g["name"], "member_count": g["member_count"]} for g in self._guilds]

    def snapshot(self, guild_id: int) -> Optional[dict[str, Any]]:
        guild = self._find(guild_id)
        if guild is None:
            return None
        return {
            "id": guild["id"],
            "name": guild["name"],
            "member_count": guild["member_count"],
            "channels": [{"id": cid, "name": name, "news": news} for cid, name, news in guild["channels"]],
            "categories": [{"id": cid, "name": name} for cid, name in guild.get("categories", [])],
            "roles": [{"id": rid, "name": name, "mentionable": m} for rid, name, m in guild["roles"]],
        }

    def channel_problem(self, guild_id: int, channel_id: int) -> Optional[str]:
        if self._find(guild_id) is None:
            return "I'm not in that server any more."
        entry = self._channel_index.get(int(channel_id))
        if entry is None or entry[0] != int(guild_id):
            return "That channel does not exist in this server, or it is not a text channel."
        if int(channel_id) in self.fail_posts_in:
            return f"I can't send messages in #{entry[1]}. Grant me View Channel and Send Messages there."
        return None

    def ping_warning(self, guild_id: int, role_id: int) -> Optional[str]:
        guild = self._find(guild_id)
        if guild is None:
            return None
        for rid, name, mentionable in guild["roles"]:
            if rid == int(role_id) and not mentionable:
                return f"Note: @{name} is not mentionable, so it will show but not notify."
        return None

    def role_problem(self, guild_id: int, role_id: int) -> Optional[str]:
        guild = self._find(guild_id)
        if guild is None:
            return "I'm not in that server any more."
        for rid, name, _ in guild["roles"]:
            if rid == int(role_id):
                if rid in self.unassignable_roles:
                    return f"I can not give @{name}: move my role above it in Server Settings → Roles."
                return None
        return "That role does not exist in this server any more."

    # ---- posting ------------------------------------------------------ #
    def _check(self, guild_id: int, channel_id: int) -> None:
        problem = self.channel_problem(guild_id, channel_id)
        if problem:
            raise OperationError(problem)

    def _new_id(self) -> int:
        self._next_message += 1
        return self._next_message

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
        self._check(guild_id, channel_id)
        message_id = self._new_id()
        published: Optional[bool] = None
        if publish:
            published = any(
                cid == int(channel_id) and is_news for cid, _, is_news in self._guild_channels(guild_id)
            )
        self.messages[(int(channel_id), message_id)] = {
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "embed": embed,
            "content": content,
            "role_id": role_id,
            "user_ids": list(user_ids),
            "view": list(view) if view else None,
        }
        self.posts.append({"channel_id": int(channel_id), "message_id": message_id, "content": content, "embed": embed})
        return {
            "message_id": message_id,
            "jump_url": f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}",
            "published": published,
        }

    def _guild_channels(self, guild_id: int):
        guild = self._find(guild_id)
        return guild["channels"] if guild else []

    async def edit_embed(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        embed: discord.Embed,
        *,
        clear_view: bool = False,
    ) -> None:
        record = self.messages.get((int(channel_id), int(message_id)))
        if record is None:
            raise OperationError("Could not find that message on Discord.")
        record["embed"] = embed
        if clear_view:
            record["view"] = None

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
        guild = self._find(guild_id)
        if guild is None:
            raise OperationError("I'm not in that server any more.")
        if self.fail_ticket_creation:
            raise OperationError("I need the Manage Channels permission to open tickets.")
        if category_id and int(category_id) not in {cid for cid, _ in guild.get("categories", [])}:
            raise OperationError("The ticket category is no longer in this server. Choose another one in the settings.")
        channel_id = self._new_id()
        self._channel_index[channel_id] = (int(guild_id), name)
        self.created_channels.append(
            {
                "id": channel_id,
                "guild_id": int(guild_id),
                "name": name,
                "opener_id": int(opener_id),
                "category_id": int(category_id) if category_id else None,
                "staff_role_id": int(staff_role_id) if staff_role_id else None,
                "reason": reason,
            }
        )
        return channel_id

    async def delete_channel(self, guild_id: int, channel_id: int, *, reason: str) -> None:
        if int(channel_id) not in self._channel_index:
            return  # already gone counts as deleted
        del self._channel_index[int(channel_id)]
        self.deleted_channels.append(int(channel_id))

    async def set_member_role(self, guild_id: int, user_id: int, role_id: int, *, add: bool) -> None:
        if self._find(guild_id) is None:
            raise OperationError("I'm not in that server any more.")
        self.role_changes.append((int(guild_id), int(user_id), int(role_id), bool(add)))

    def _simulated(self, message_id: int, emoji: str, index: int) -> int:
        key = (int(message_id), _norm(emoji))
        if key in self.counts:
            return self.counts[key]
        # Deterministic demo votes: the bot's own reaction plus a few members.
        return 1 + ((int(message_id) + index * 7) % 6)

    async def reaction_counts(self, guild_id: int, channel_id: int, message_id: int, emojis: list[str]) -> list[int]:
        if (int(channel_id), int(message_id)) not in self.messages:
            raise OperationError("Could not find that message on Discord.")
        return [self._simulated(message_id, e, i) for i, e in enumerate(emojis)]

    async def reactors(self, guild_id: int, channel_id: int, message_id: int, emoji: str, limit: int) -> list[int]:
        if (int(channel_id), int(message_id)) not in self.messages:
            raise OperationError("Could not find that message on Discord.")
        count = max(0, self._simulated(message_id, emoji, 0) - 1)
        return DEMO_MEMBER_IDS[: min(count, limit)]

    async def add_reactions(self, guild_id: int, channel_id: int, message_id: int, emojis: list[str]) -> None:
        if (int(channel_id), int(message_id)) not in self.messages:
            raise OperationError("Could not find that message on Discord.")

    def say(
        self,
        channel_id: int,
        author: str,
        text: str,
        *,
        bot: bool = False,
        attachments: tuple[str, ...] = (),
        when: Optional[datetime] = None,
    ) -> None:
        """Simulate someone writing in a channel, so transcripts have something to read."""
        self.history.setdefault(int(channel_id), []).append(
            {
                "author": author,
                "author_id": 0,
                "bot": bot,
                "created_at": when or datetime.now(timezone.utc),
                "content": text,
                "attachments": list(attachments),
            }
        )

    async def read_history(self, guild_id: int, channel_id: int, limit: int) -> list[dict[str, Any]]:
        if int(channel_id) in self.unreadable or int(channel_id) not in self._channel_index:
            raise OperationError("That channel is no longer available.")
        return [dict(m) for m in self.history.get(int(channel_id), [])][: int(limit)]

    async def send_dm(self, user_id: int, *, content: str, view: Optional[list[dict[str, Any]]] = None) -> None:
        self.dm_posts.append({"user_id": int(user_id), "content": content, "view": list(view or [])})

    async def post_file(self, guild_id: int, channel_id: int, *, content: str, filename: str, data: bytes) -> None:
        self._check(guild_id, channel_id)
        self.files.append({"channel_id": int(channel_id), "content": content, "filename": filename, "data": data})

    async def edit_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        *,
        embed: Optional[discord.Embed] = None,
        view: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        key = (int(channel_id), int(message_id))
        if key not in self.messages:
            raise OperationError("Could not find that message on Discord.")
        self.messages[key]["embed"] = embed
        self.messages[key]["view"] = list(view) if view else None
        self.edits.append({"channel_id": int(channel_id), "message_id": int(message_id), "view": view})

    async def delete_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        key = (int(channel_id), int(message_id))
        if key in self.messages:  # a message that is already gone counts as deleted
            del self.messages[key]
            self.pinned.discard(key)
            self.deleted.append(key)

    async def pin_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        key = (int(channel_id), int(message_id))
        if self.fail_pins:
            raise OperationError("Discord would not pin it. Check that I have Manage Messages in that channel.")
        if key not in self.messages:
            raise OperationError("Could not find that message on Discord.")
        self.pinned.add(key)

    async def unpin_message(self, guild_id: int, channel_id: int, message_id: int) -> None:
        key = (int(channel_id), int(message_id))
        if key not in self.messages:
            raise OperationError("Could not find that message on Discord.")
        self.pinned.discard(key)


# --------------------------------------------------------------------------- #
# Running the demo
# --------------------------------------------------------------------------- #
def demo_config(guilds: list[dict[str, Any]]) -> dict[str, Any]:
    """A config where every demo server already has its roles and channels set."""
    cfg = copy.deepcopy(settings.DEFAULT_CONFIG)
    for guild in guilds:
        names = {name: cid for cid, name, _ in guild["channels"]}
        roles = {name: rid for rid, name, _ in guild["roles"]}
        staff_role = roles.get("Staff") or roles.get("Moderators")
        announce = next((cid for cid, _, news in guild["channels"] if news), None)
        entry = {
            "staff_role_id": staff_role,
            "announcement_channel_id": announce,
            "staff_channel_id": names.get("staff-log") or names.get("mod-log"),
            "timezone": "America/Toronto",
            "welcome_channel_id": names.get("general") or names.get("chat"),
            "welcome_message": None,
        }
        cfg["guilds"][str(guild["id"])] = entry
    return cfg


async def seed_demo(gateway: DemoGateway, cfg: dict[str, Any]) -> None:
    """One live poll with some votes, so the results view has something to show."""
    import operations as ops
    from audit import Actor

    guild_id = DEMO_GUILDS[0]["id"]
    staff = Actor(DEMO_MEMBER_IDS[0], "Demo staff", "discord")
    poll = await ops.create_poll(
        gateway,
        cfg,
        guild_id,
        staff,
        question="Which map next Friday?",
        options=["Dust II", "Inferno", "Nuke", "Overpass"],
        duration="2d",
        multiple=True,
    )
    # Eight members pick one to three options each. Deterministic, so the demo looks the same each time.
    picks = [[0], [1, 2], [1], [0, 3], [2], [1], [0, 1, 2], [3]]
    for offset, choices in enumerate(picks):
        member = Actor(DEMO_MEMBER_IDS[offset + 1], f"Member {offset + 1}", "discord")
        for index in choices:
            await ops.cast_vote(gateway, cfg, guild_id, poll["id"], member, index)

    # A poll that is scheduled for tomorrow, so the scheduled state shows.
    await ops.create_poll(
        gateway,
        cfg,
        guild_id,
        staff,
        question="Pizza or tacos on Friday?",
        options=["Pizza", "Tacos"],
        channel_id=100000000000000104,
        send_at="1d",
    )

    # A role menu, posted in general.
    menu = await ops.create_role_menu(
        gateway,
        cfg,
        guild_id,
        staff,
        title="Pick your roles",
        description="Choose the roles you want. Pick none to remove them all.",
    )
    await ops.add_role_menu_option(
        gateway, cfg, guild_id, staff, menu_id=menu["id"], role_id=100000000000000202, label="Member"
    )
    await ops.add_role_menu_option(
        gateway,
        cfg,
        guild_id,
        staff,
        menu_id=menu["id"],
        role_id=100000000000000203,
        label="Event pings",
        description="Get pinged when an event is posted",
    )
    await ops.post_role_menu(gateway, cfg, guild_id, staff, menu_id=menu["id"], channel_id=100000000000000102)

    # A ticket that was closed, so its transcript is saved and can be downloaded.
    member = Actor(DEMO_MEMBER_IDS[2], "Member 3", "discord")
    ticket = await ops.open_ticket(gateway, cfg, guild_id, member)
    channel = int(ticket["channel_id"])
    gateway.say(channel, "Member 3", "Hi, the event pings role did not come through from the menu.")
    gateway.say(channel, "Demo staff", "Thanks for the report. Which menu did you use?")
    gateway.say(channel, "Member 3", "The Pick your roles menu in general.")
    await ops.close_ticket(gateway, cfg, guild_id, staff, int(ticket["id"]), actor_is_staff=True)


async def run_demo(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the dashboard over simulated servers until interrupted."""
    import paths
    from dashboard import DashboardServer
    from operations import run_scheduler

    if "SOUNDTRAIL_SUPPORT_DATA" not in os.environ:
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = tempfile.mkdtemp(prefix="soundtrail-support-demo-")
    store.init_db()

    gateway = DemoGateway()
    cfg = demo_config(DEMO_GUILDS)
    await seed_demo(gateway, cfg)
    dashboard = DashboardServer(
        gateway,
        cfg,
        host=host,
        port=port,
        allowed_hosts=["*"],
        demo=True,
    )
    await dashboard.start()
    print(f"Demo dashboard: http://{host if host != '0.0.0.0' else 'localhost'}:{port}  (no sign-in needed)", flush=True)
    print(f"Data directory: {paths.data_dir()} (temporary)", flush=True)
    try:
        while True:
            try:
                counts = await run_scheduler(gateway, cfg, now=datetime.now(timezone.utc))
                if any(counts.values()):
                    logger.info("Demo scheduler: %s", counts)
            except Exception:
                logger.exception("Demo scheduler tick failed.")
            await asyncio.sleep(15)
    finally:
        await dashboard.close()
