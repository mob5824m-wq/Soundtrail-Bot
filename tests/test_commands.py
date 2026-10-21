"""
Tests for the /manage slash commands (bot.py and command_tree.py).

Two kinds of check:

* the command tree itself: every name and description is inside Discord's
  limits (one over-long description makes Discord reject the whole list,
  HTTP 400 / code 50035), and every payload serialises to JSON;
* the command handlers, called with fake interactions and a DemoGateway, so
  the staff gate, posting, polls, events, welcome and setup are exercised end
  to end without a Discord connection.

Run with either:
    python -m pytest tests/test_commands.py
    python tests/test_commands.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from discord import app_commands  # noqa: E402

import audit  # noqa: E402
import operations as ops  # noqa: E402
import polls  # noqa: E402
from audit import Actor  # noqa: E402
import announcements as ann  # noqa: E402
import bot as bot_module  # noqa: E402
import command_tree  # noqa: E402
import settings  # noqa: E402
import store  # noqa: E402
from demo import DemoGateway  # noqa: E402

GUILD = 100000000000000001
ANNOUNCE = 100000000000000101
GENERAL = 100000000000000102
STAFF_LOG = 100000000000000105
STAFF_ROLE = 100000000000000201
MEMBER_ROLE = 100000000000000202
SUPPORT_CATEGORY = 100000000000000901

_REGISTERED = False


def ensure_cog_registered() -> None:
    """Add the cog to the module-level bot once per test process."""
    global _REGISTERED
    if not _REGISTERED:
        asyncio.run(bot_module._register_cog(bot_module.bot))
        _REGISTERED = True


def leaves(command):
    if isinstance(command, app_commands.Group):
        for child in command.commands:
            yield from leaves(child)
    else:
        yield command


class FakeResponse:
    def __init__(self) -> None:
        self.done = False

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.done = True

    def is_done(self) -> bool:
        return self.done


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, *, embed=None, ephemeral: bool = False, **_) -> None:
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})

    @property
    def last_text(self) -> str:
        return self.sent[-1]["content"] or ""


def fake_interaction(*, admin=False, manage_guild=False, roles=(), user_id=42, name="Ada"):
    perms = SimpleNamespace(administrator=admin, manage_guild=manage_guild)
    user = SimpleNamespace(
        id=user_id,
        display_name=name,
        guild_permissions=perms,
        roles=[SimpleNamespace(id=r) for r in roles],
    )
    guild = SimpleNamespace(id=GUILD, name="Combat Arena", member_count=10)
    return SimpleNamespace(
        user=user,
        guild=guild,
        guild_id=GUILD,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


def fake_channel(channel_id: int):
    return SimpleNamespace(id=channel_id, mention=f"<#{channel_id}>")


def fake_role(role_id: int):
    return SimpleNamespace(id=role_id, mention=f"<@&{role_id}>")


class CommandTreeTests(unittest.TestCase):
    def test_all_names_and_descriptions_fit_discord_limits(self):
        ensure_cog_registered()
        for command in leaves(command_tree.manage_group):
            self.assertLessEqual(len(command.description), 100, command.qualified_name)
            self.assertGreater(len(command.description), 0, command.qualified_name)
            self.assertLessEqual(len(command.name), 32, command.qualified_name)
            for param in command.parameters:
                self.assertLessEqual(len(param.description), 100, f"{command.qualified_name}.{param.name}")

    def test_slash_command_count_is_what_the_readme_lists(self):
        # Update this number and the README command list together when a command is added.
        ensure_cog_registered()
        self.assertEqual(len(list(leaves(command_tree.manage_group))), 51)

    def test_payload_serialises_to_json(self):
        ensure_cog_registered()
        payload = command_tree.manage_group.to_dict(bot_module.bot.tree)
        json.dumps(payload)  # raises if anything is not plain JSON

    def test_expected_groups_and_commands(self):
        ensure_cog_registered()
        top = {c.name: c for c in command_tree.manage_group.commands}
        self.assertEqual(
            set(top),
            {"announce", "schedule", "repeat", "scheduled", "cancel", "history", "template",
             "poll", "event", "welcome", "status", "audit", "setup", "fixcommands",
             "queue", "approve", "reject", "edit", "delete", "quiet", "pins", "ticket", "faq", "reactionrole", "rolemenu"},
        )
        self.assertEqual({c.name for c in top["rolemenu"].commands}, {"new", "add", "remove", "post", "list", "delete"})
        self.assertEqual({c.name for c in top["template"].commands}, {"save", "list", "delete", "use"})
        self.assertEqual({c.name for c in top["poll"].commands}, {"create", "list", "close"})
        self.assertEqual({c.name for c in top["ticket"].commands}, {"panel", "list", "claim", "assign", "priority", "report", "close", "reopen"})
        self.assertEqual({c.name for c in top["faq"].commands}, {"save", "list", "delete", "use"})
        self.assertEqual({c.name for c in top["reactionrole"].commands}, {"add", "list", "remove"})
        self.assertEqual({c.name for c in top["event"].commands}, {"create", "list", "cancel"})
        self.assertEqual({c.name for c in top["welcome"].commands}, {"set", "off", "test"})

    def test_mentions_and_members_intents_are_set(self):
        self.assertTrue(bot_module.intents.members)
        self.assertTrue(hasattr(bot_module.SoundtrailSupportBot, "on_member_join"))


class StaffGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = copy.deepcopy(settings.DEFAULT_CONFIG)

    def test_with_no_staff_role_manage_server_is_enough(self):
        member = fake_interaction(manage_guild=True).user
        self.assertIsNone(settings.staff_denial(member, self.cfg, GUILD))

    def test_with_no_staff_role_plain_members_are_refused(self):
        member = fake_interaction().user
        self.assertIsNotNone(settings.staff_denial(member, self.cfg, GUILD))

    def test_once_a_staff_role_is_set_manage_server_alone_is_not_enough(self):
        settings.set_guild_value(self.cfg, GUILD, "staff_role_id", STAFF_ROLE)
        manager = fake_interaction(manage_guild=True).user
        self.assertIsNotNone(settings.staff_denial(manager, self.cfg, GUILD))
        staff = fake_interaction(roles=[STAFF_ROLE]).user
        self.assertIsNone(settings.staff_denial(staff, self.cfg, GUILD))

    def test_administrators_always_pass(self):
        settings.set_guild_value(self.cfg, GUILD, "staff_role_id", STAFF_ROLE)
        admin = fake_interaction(admin=True).user
        self.assertIsNone(settings.staff_denial(admin, self.cfg, GUILD))
        self.assertTrue(command_tree.is_administrator(fake_interaction(admin=True)))
        self.assertFalse(command_tree.is_administrator(fake_interaction(manage_guild=True)))


class CommandHandlerTests(unittest.TestCase):
    """Run real handlers with a DemoGateway and a temporary data directory."""

    def setUp(self) -> None:
        ensure_cog_registered()
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._tmp.name
        store.init_db()
        self.gw = DemoGateway()
        self.cfg = copy.deepcopy(settings.DEFAULT_CONFIG)
        settings.set_guild_value(self.cfg, GUILD, "announcement_channel_id", ANNOUNCE)
        settings.set_guild_value(self.cfg, GUILD, "staff_channel_id", STAFF_LOG)
        settings.set_guild_value(self.cfg, GUILD, "staff_role_id", STAFF_ROLE)
        bot_module.bot.gateway = self.gw
        bot_module.bot.config = self.cfg
        self.cog = bot_module.bot.get_cog("CombatMasterCog")

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
        else:
            os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._old
        self._tmp.cleanup()

    def call(self, command_name: str, interaction, *args, **kwargs):
        """Invoke a cog command's callback the way discord.py would.

        Every command, including the nested template/poll/event/welcome ones, is
        a class attribute of the cog, so it is looked up by its method name.
        """
        command = vars(bot_module.CombatMasterCog)[command_name]
        return asyncio.run(command.callback(self.cog, interaction, *args, **kwargs))

    def test_outsider_is_refused_and_nothing_is_posted(self):
        interaction = fake_interaction()
        self.call("manage_announce", interaction, message="hi")
        self.assertTrue(interaction.followup.sent, "the refusal should be shown to the user")
        self.assertEqual(self.gw.posts, [])

    def test_staff_announce_posts_and_replies_with_the_link(self):
        interaction = fake_interaction(roles=[STAFF_ROLE])
        self.call("manage_announce", interaction, message="Doors at 8", title="Raid",
                  mention=fake_role(STAFF_ROLE))
        self.assertEqual(self.gw.posts[0]["content"], f"<@&{STAFF_ROLE}>")
        self.assertIn("discord.com/channels", interaction.followup.last_text)
        self.assertEqual(audit.list_entries(GUILD, 1)[0]["actor_name"], "Ada")

    def test_admin_is_allowed_without_the_role(self):
        interaction = fake_interaction(admin=True)
        self.call("manage_announce", interaction, message="Hello")
        self.assertEqual(len(self.gw.posts), 2)  # announcement plus staff log copy

    def test_missing_channel_explains_setup(self):
        settings.set_guild_value(self.cfg, GUILD, "announcement_channel_id", None)
        interaction = fake_interaction(roles=[STAFF_ROLE])
        self.call("manage_announce", interaction, message="Hello")
        self.assertIn("/manage setup", interaction.followup.last_text)

    def test_schedule_then_list_then_cancel(self):
        staff = fake_interaction(roles=[STAFF_ROLE])
        self.call("manage_schedule", staff, when="2h", message="Restart", title=None,
                  channel=None, mention=None, publish=False)
        self.assertIn("Scheduled", staff.followup.last_text)
        listing = fake_interaction(roles=[STAFF_ROLE])
        self.call("manage_scheduled", listing)
        self.assertEqual(listing.followup.sent[-1]["embed"].title, "Scheduled announcements")
        cancel = fake_interaction(roles=[STAFF_ROLE])
        self.call("manage_cancel", cancel, schedule_id=1)
        self.assertIn("Cancelled", cancel.followup.last_text)

    def test_bad_time_is_refused_with_a_message(self):
        interaction = fake_interaction(roles=[STAFF_ROLE])
        self.call("manage_schedule", interaction, when="someday", message="x", title=None,
                  channel=None, mention=None, publish=False)
        self.assertIn("could not read that time", interaction.followup.last_text.lower())

    def test_template_save_and_use(self):
        staff = fake_interaction(roles=[STAFF_ROLE])
        self.call("template_save", staff, name="raid", message="Tonight", title=None)
        self.assertIn("Saved template `raid`", staff.followup.last_text)
        use = fake_interaction(roles=[STAFF_ROLE])
        self.call("template_use", use, name="raid", channel=None, mention=None, publish=False)
        self.assertIn("Posted template `raid`", use.followup.last_text)

    def test_poll_create_and_close(self):
        create = fake_interaction(roles=[STAFF_ROLE])
        self.call("poll_create", create, question="Which night?", options="Tuesday | Friday",
                  duration=None, channel=None)
        self.assertIn("Poll **#1**", create.followup.last_text)
        close = fake_interaction(roles=[STAFF_ROLE])
        self.call("poll_close", close, poll_id=1)
        self.assertIn("Closed poll **#1**", close.followup.last_text)

    def test_event_create_and_list(self):
        create = fake_interaction(roles=[STAFF_ROLE])
        self.call("event_create", create, title="Team night", starts="3h", description=None,
                  channel=None, remind_minutes=30)
        self.assertIn("Event **#1**", create.followup.last_text)
        listing = fake_interaction(roles=[STAFF_ROLE])
        self.call("event_list", listing)
        self.assertIn("Team night", listing.followup.last_text)

    def test_welcome_set_test_and_off(self):
        staff = fake_interaction(roles=[STAFF_ROLE])
        self.call("welcome_set", staff, channel=fake_channel(GENERAL), message=None)
        self.assertIn(f"<#{GENERAL}>", staff.followup.last_text)
        test = fake_interaction(roles=[STAFF_ROLE])
        self.call("welcome_test", test)
        self.assertIn("Sample welcome", test.followup.last_text)
        off = fake_interaction(roles=[STAFF_ROLE])
        self.call("welcome_off", off)
        self.assertIn("off", off.followup.last_text)

    def test_setup_is_administrator_only(self):
        staff = fake_interaction(roles=[STAFF_ROLE])
        self.call("setup_cmd", staff, timezone_name="Europe/London")
        self.assertIn("Only administrators", staff.followup.last_text)
        self.assertEqual(settings.get_timezone_name(self.cfg, GUILD), "America/Toronto")

        admin = fake_interaction(admin=True)
        self.call("setup_cmd", admin, timezone_name="Europe/London")
        self.assertEqual(settings.get_timezone_name(self.cfg, GUILD), "Europe/London")

    def test_status_reports_the_staff_role(self):
        interaction = fake_interaction(roles=[STAFF_ROLE])
        self.call("manage_status", interaction)
        embed = interaction.followup.sent[-1]["embed"]
        fields = {f.name: f.value for f in embed.fields}
        self.assertIn(str(STAFF_ROLE), fields["Staff role"])

    def test_audit_lists_recent_actions(self):
        staff = fake_interaction(roles=[STAFF_ROLE])
        self.call("manage_announce", staff, message="one")
        view = fake_interaction(roles=[STAFF_ROLE])
        self.call("manage_audit", view, limit=5)
        self.assertIn("announce.post", view.followup.sent[-1]["embed"].description)

    def test_fixcommands_is_administrator_only(self):
        interaction = fake_interaction(roles=[STAFF_ROLE])
        self.call("fixcommands_cmd", interaction)
        self.assertIn("Only administrators", interaction.followup.last_text)

    def test_commands_refuse_direct_messages(self):
        interaction = fake_interaction(roles=[STAFF_ROLE])
        interaction.guild = None
        interaction.guild_id = None
        self.call("manage_status", interaction)
        self.assertIn("only work inside a server", interaction.followup.last_text)



class CommandFeatureTests(unittest.TestCase):
    """The approval, edit, delete and quiet-hours commands, run against a DemoGateway."""

    call = CommandHandlerTests.call

    def setUp(self) -> None:
        ensure_cog_registered()
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._tmp.name
        store.init_db()
        self.gw = DemoGateway()
        self.cfg = copy.deepcopy(settings.DEFAULT_CONFIG)
        settings.set_guild_value(self.cfg, GUILD, "announcement_channel_id", ANNOUNCE)
        settings.set_guild_value(self.cfg, GUILD, "staff_channel_id", STAFF_LOG)
        settings.set_guild_value(self.cfg, GUILD, "staff_role_id", STAFF_ROLE)
        bot_module.bot.gateway = self.gw
        bot_module.bot.config = self.cfg
        self.cog = bot_module.bot.get_cog("CombatMasterCog")

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
        else:
            os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._old
        self._tmp.cleanup()

    def ada(self):
        return fake_interaction(roles=[STAFF_ROLE], user_id=42, name="Ada")

    def announcement_posts(self):
        """Posts to the announcement channel only; queued requests are also logged to the staff channel."""
        return [post for post in self.gw.posts if post["channel_id"] == ANNOUNCE]

    def ben(self):
        return fake_interaction(roles=[STAFF_ROLE], user_id=43, name="Ben")

    def test_queued_post_is_approved_by_a_different_staff_member(self):
        settings.set_guild_value(self.cfg, GUILD, "require_approval", True)
        request = self.ada()
        self.call("manage_announce", request, message="Hold this for review")
        self.assertIn("approval queue as **#1**", request.followup.last_text)
        self.assertEqual(self.announcement_posts(), [])

        listing = self.ada()
        self.call("manage_queue", listing)
        self.assertEqual(listing.followup.sent[-1]["embed"].title, "Waiting for approval")

        own = self.ada()
        self.call("manage_approve", own, request_id=1)
        self.assertIn("own request", own.followup.last_text)
        self.assertEqual(self.announcement_posts(), [])

        approved = self.ben()
        self.call("manage_approve", approved, request_id=1)
        self.assertIn("Approved **#1**", approved.followup.last_text)
        self.assertEqual(len(self.announcement_posts()), 1)

    def test_reject_turns_a_queued_post_down(self):
        settings.set_guild_value(self.cfg, GUILD, "require_approval", True)
        self.call("manage_announce", self.ada(), message="Not this one")
        rejected = self.ben()
        self.call("manage_reject", rejected, request_id=1)
        self.assertIn("Turned down **#1**", rejected.followup.last_text)
        self.assertEqual(self.announcement_posts(), [])

    def test_edit_then_delete_a_post_the_bot_made(self):
        self.call("manage_announce", self.ada(), message="Doors at 8")
        message_id = ann.list_history(GUILD, 1)[0]["message_id"]

        edit = self.ada()
        self.call("manage_edit", edit, message_id=str(message_id), message="Doors at 9", title="Raid")
        self.assertIn("Updated the announcement", edit.followup.last_text)

        delete = self.ada()
        self.call("manage_delete", delete, message_id=str(message_id))
        self.assertIn("Deleted the announcement", delete.followup.last_text)
        self.assertEqual(ann.list_history(GUILD, 10), [])

    def test_editing_an_unknown_message_is_explained(self):
        interaction = self.ada()
        self.call("manage_edit", interaction, message_id="555", message="x", title=None)
        self.assertTrue(interaction.followup.last_text)
        self.assertNotIn("Updated", interaction.followup.last_text)

    def test_quiet_hours_show_set_and_turn_off(self):
        shown = self.ada()
        self.call("manage_quiet", shown)
        self.assertIn("Quiet hours are off", shown.followup.last_text)

        set_up = self.ada()
        self.call("manage_quiet", set_up, start="22:00", end="08:00")
        self.assertIn("Quiet hours set: 22:00 to 08:00", set_up.followup.last_text)
        self.assertEqual(settings.get_quiet_hours(bot_module.bot.config, GUILD), ("22:00", "08:00"))

        off = self.ada()
        self.call("manage_quiet", off, start="off", end=None)
        self.assertIn("Quiet hours are off", off.followup.last_text)
        self.assertEqual(settings.get_quiet_hours(bot_module.bot.config, GUILD), (None, None))

    def test_a_bad_quiet_time_changes_nothing(self):
        bad = self.ada()
        self.call("manage_quiet", bad, start="25:00", end="08:00")
        self.assertEqual(settings.get_quiet_hours(bot_module.bot.config, GUILD), (None, None))

    def test_pins_lists_what_is_pinned_now(self):
        empty = self.ada()
        self.call("manage_pins", empty)
        self.assertIn("Nothing is pinned", empty.followup.last_text)

        self.call("manage_announce", self.ada(), message="Pin me", pin_for="24h")
        listing = self.ada()
        self.call("manage_pins", listing)
        self.assertEqual(listing.followup.sent[-1]["embed"].title, "Pinned posts")

    def test_repeat_accepts_a_pin_and_says_so(self):
        interaction = self.ada()
        self.call("manage_repeat", interaction, every="1d", message="Daily", title=None, first_in="1h",
                  channel=None, mention=None, publish=False, pin_for="6h")
        self.assertIn("Repeating", interaction.followup.last_text)
        self.assertIn("pinned when it goes out", interaction.followup.last_text)

    def test_quiet_hold_by_hand_is_set_and_reported(self):
        set_up = self.ada()
        self.call("manage_quiet", set_up, start="22:00", end="08:00", hold_manual=True)
        self.assertIn("Posts made by hand wait too.", set_up.followup.last_text)
        self.assertTrue(settings.get_quiet_hold_manual(bot_module.bot.config, GUILD))

        only_flag = self.ada()
        self.call("manage_quiet", only_flag, start=None, end=None, hold_manual=False)
        self.assertFalse(settings.get_quiet_hold_manual(bot_module.bot.config, GUILD))
        self.assertEqual(settings.get_quiet_hours(bot_module.bot.config, GUILD), ("22:00", "08:00"))

    def test_outsiders_cannot_use_the_new_commands(self):
        outsider = fake_interaction(user_id=77, name="Eve")
        self.call("manage_queue", outsider)
        self.call("manage_approve", outsider, request_id=1)
        self.call("manage_quiet", outsider, start="22:00", end="08:00")
        self.call("manage_pins", outsider)
        self.assertEqual(settings.get_quiet_hours(bot_module.bot.config, GUILD), (None, None))
        self.assertEqual(self.announcement_posts(), [])


class TicketAndRoleCommandTests(unittest.TestCase):
    """The /manage ticket, /manage reactionrole and poll multiple-choice commands, end to end."""

    call = CommandHandlerTests.call

    def setUp(self) -> None:
        ensure_cog_registered()
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._tmp.name
        store.init_db()
        self.gw = DemoGateway()
        self.cfg = copy.deepcopy(settings.DEFAULT_CONFIG)
        settings.set_guild_value(self.cfg, GUILD, "announcement_channel_id", ANNOUNCE)
        settings.set_guild_value(self.cfg, GUILD, "staff_channel_id", STAFF_LOG)
        settings.set_guild_value(self.cfg, GUILD, "staff_role_id", STAFF_ROLE)
        bot_module.bot.gateway = self.gw
        bot_module.bot.config = self.cfg
        self.cog = bot_module.bot.get_cog("CombatMasterCog")

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
        else:
            os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._old
        self._tmp.cleanup()

    def test_ticket_panel_is_staff_only_and_posts_a_button(self):
        outsider = fake_interaction()
        self.call("ticket_panel", outsider, channel=None)
        self.assertEqual(self.gw.posts, [])
        staff = fake_interaction(roles=[STAFF_ROLE])
        self.call("ticket_panel", staff, channel=None)
        self.assertIn("ticket panel posted", staff.followup.last_text.lower())
        self.assertEqual(len([p for p in self.gw.posts if p["channel_id"] == ANNOUNCE]), 1)

    def test_ticket_claim_and_assign_commands_work_inside_a_ticket(self):
        ticket = asyncio.run(
            ops.open_ticket(self.gw, self.cfg, GUILD, Actor(1001, "Alice", "discord"), category="technical")
        )
        claim = fake_interaction(roles=[STAFF_ROLE], user_id=42, name="Ada")
        claim.channel_id = int(ticket["channel_id"])
        self.call("ticket_claim", claim, ticket_id=None)
        self.assertIn("assigned to Ada", claim.followup.last_text)

        target = SimpleNamespace(
            id=43,
            display_name="Bea",
            mention="<@43>",
            bot=False,
            roles=[SimpleNamespace(id=STAFF_ROLE)],
            guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
        )
        assign = fake_interaction(roles=[STAFF_ROLE], user_id=42, name="Ada")
        assign.channel_id = int(ticket["channel_id"])
        self.call("ticket_assign", assign, member=target, ticket_id=None)
        self.assertIn("Assigned ticket", assign.followup.last_text)
        row = ops.find_ticket_by_channel(GUILD, int(ticket["channel_id"]))
        self.assertEqual(row["claimed_by_id"], 43)

    def test_member_can_reopen_a_closed_ticket_with_the_command(self):
        ticket = asyncio.run(
            ops.open_ticket(self.gw, self.cfg, GUILD, Actor(1001, "Alice", "discord"))
        )
        asyncio.run(ops.close_ticket(
            self.gw, self.cfg, GUILD, Actor(42, "Ada", "discord"), ticket["id"], actor_is_staff=True
        ))
        member = fake_interaction(user_id=1001, name="Alice")
        self.call("ticket_reopen", member, ticket_id=ticket["id"], note="Still happening")
        self.assertIn("is open again", member.followup.last_text)

    def test_faq_commands_save_list_use_and_delete_replies(self):
        ticket = asyncio.run(
            ops.open_ticket(self.gw, self.cfg, GUILD, Actor(1001, "Alice", "discord"))
        )
        staff = fake_interaction(roles=[STAFF_ROLE])
        self.call("faq_save", staff, key="install-help", title="Install help", response="Restart Soundtrail.")
        self.assertIn("Saved support reply", staff.followup.last_text)

        listing = fake_interaction(roles=[STAFF_ROLE])
        self.call("faq_list", listing)
        self.assertIn("install-help", listing.followup.last_text)

        use = fake_interaction(roles=[STAFF_ROLE])
        use.channel_id = int(ticket["channel_id"])
        self.call("faq_use", use, key="install-help")
        self.assertIn("Sent **Install help**", use.followup.last_text)
        self.assertEqual(self.gw.posts[-1]["content"], "Restart Soundtrail.")

        delete = fake_interaction(roles=[STAFF_ROLE])
        self.call("faq_delete", delete, key="install-help")
        self.assertIn("Deleted saved reply", delete.followup.last_text)

    def test_staff_can_set_priority_and_view_ticket_report(self):
        ticket = asyncio.run(
            ops.open_ticket(self.gw, self.cfg, GUILD, Actor(1001, "Alice", "discord"))
        )
        staff = fake_interaction(roles=[STAFF_ROLE])
        staff.channel_id = int(ticket["channel_id"])
        self.call(
            "ticket_priority", staff,
            priority=app_commands.Choice(name="Urgent", value="urgent"), ticket_id=None,
        )
        self.assertEqual(ops.find_ticket_by_channel(GUILD, int(ticket["channel_id"]))["priority"], "urgent")

        report = fake_interaction(roles=[STAFF_ROLE])
        self.call("ticket_report", report, days=app_commands.Choice(name="7 days", value=7))
        self.assertIn("Support report", report.followup.sent[-1]["embed"].title)
        self.assertIn("Urgent tickets open now", [field.name for field in report.followup.sent[-1]["embed"].fields])

    def test_ticket_list_and_close_from_inside_the_ticket(self):
        empty = fake_interaction(roles=[STAFF_ROLE])
        self.call("ticket_list", empty)
        self.assertIn("No tickets are open", empty.followup.last_text)

        ticket = asyncio.run(ops.open_ticket(self.gw, self.cfg, GUILD, Actor(1001, "Alice", "discord")))
        listing = fake_interaction(roles=[STAFF_ROLE])
        self.call("ticket_list", listing)
        self.assertIn(f"**#{ticket['id']}**", listing.followup.last_text)

        elsewhere = fake_interaction(roles=[STAFF_ROLE])
        elsewhere.channel_id = ANNOUNCE
        self.call("ticket_close", elsewhere, ticket_id=None)
        self.assertIn("Run this inside a ticket", elsewhere.followup.last_text)

        inside = fake_interaction(roles=[STAFF_ROLE])
        inside.channel_id = int(ticket["channel_id"])
        self.call("ticket_close", inside, ticket_id=None)
        self.assertIn(f"Closed ticket **#{ticket['id']}**", inside.followup.last_text)
        self.assertIn(int(ticket["channel_id"]), self.gw.deleted_channels)

    def test_reaction_role_add_list_and_remove(self):
        message_id = asyncio.run(self.gw.post(GUILD, ANNOUNCE, content="Pick"))["message_id"]
        staff = fake_interaction(roles=[STAFF_ROLE])
        role = SimpleNamespace(id=MEMBER_ROLE, name="Member", mention=f"<@&{MEMBER_ROLE}>")
        self.call("reactionrole_add", staff, message=str(message_id), emoji="🎮", role=role, channel=None)
        self.assertIn("Reaction role **#1**", staff.followup.last_text)

        listing = fake_interaction(roles=[STAFF_ROLE])
        self.call("reactionrole_list", listing)
        self.assertIn(f"<@&{MEMBER_ROLE}>", listing.followup.last_text)

        removal = fake_interaction(roles=[STAFF_ROLE])
        self.call("reactionrole_remove", removal, rule_id=1)
        self.assertIn("Removed reaction role **#1**", removal.followup.last_text)
        self.assertEqual(ops.list_reaction_roles(GUILD), [])

    def test_reaction_role_refusals_are_shown_to_staff(self):
        staff = fake_interaction(roles=[STAFF_ROLE])
        role = SimpleNamespace(id=MEMBER_ROLE, name="Member", mention="")
        self.call("reactionrole_add", staff, message="https://example.com/nope", emoji="🎮", role=role, channel=None)
        self.assertIn("message link", staff.followup.last_text)

    def test_poll_create_with_multiple_choice(self):
        staff = fake_interaction(roles=[STAFF_ROLE])
        self.call("poll_create", staff, question="Which games?", options="Chess | Go",
                  duration=None, channel=None, multiple=True)
        self.assertIn("several options", staff.followup.last_text)
        self.assertTrue(polls.get_poll(GUILD, 1)["multiple"])

    def test_setup_sets_the_ticket_category(self):
        admin = fake_interaction(admin=True)
        category = SimpleNamespace(id=SUPPORT_CATEGORY, name="Support")
        self.call(
            "setup_cmd", admin, ticket_category=category, support_start="9:00", support_end="17:00",
            after_hours_message="We'll reply during support hours.",
        )
        self.assertEqual(settings.get_ticket_category_id(self.cfg, GUILD), SUPPORT_CATEGORY)
        self.assertEqual(settings.get_support_hours(self.cfg, GUILD), ("09:00", "17:00"))
        self.assertEqual(
            settings.get_after_hours_message(self.cfg, GUILD), "We'll reply during support hours."
        )


if __name__ == "__main__":
    unittest.main()
