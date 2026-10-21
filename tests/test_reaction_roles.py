"""Reaction roles: parsing emoji and message links, storing rules, and giving or taking roles.

Run with either:
    python -m pytest tests/test_reaction_roles.py
    python tests/test_reaction_roles.py
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import operations as ops  # noqa: E402
import reaction_roles  # noqa: E402
import settings  # noqa: E402
import store  # noqa: E402
from audit import Actor  # noqa: E402
from demo import DemoGateway  # noqa: E402
from operations import OperationError  # noqa: E402

GUILD = 100000000000000001
OTHER_GUILD = 100000000000000002
ANNOUNCE = 100000000000000101
GENERAL = 100000000000000102
STAFF_ROLE = 100000000000000201
MEMBER_ROLE = 100000000000000202
EVENT_PINGS = 100000000000000203
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
STAFF = Actor(42, "Ada", "discord")


def run(coro):
    return asyncio.run(coro)


class ReactionRoleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._tmp.name
        store.init_db()
        self.gw = DemoGateway()
        self.cfg = copy.deepcopy(settings.DEFAULT_CONFIG)
        settings.set_guild_value(self.cfg, GUILD, "announcement_channel_id", ANNOUNCE)
        settings.set_guild_value(self.cfg, GUILD, "staff_role_id", STAFF_ROLE)
        settings.set_guild_value(self.cfg, GUILD, "timezone", "America/Toronto")
        # A message in the announcement channel that members will react to.
        self.message_id = run(self.gw.post(GUILD, ANNOUNCE, content="Pick your games"))["message_id"]

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
        else:
            os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._old
        self._tmp.cleanup()

    def add(self, emoji="🎮", role=MEMBER_ROLE, message=None, channel_id=None):
        return run(ops.add_reaction_role(
            self.gw,
            self.cfg,
            GUILD,
            STAFF,
            message=message if message is not None else str(self.message_id),
            emoji=emoji,
            role_id=role,
            channel_id=channel_id,
        ))

    def react(self, user_id, emoji="🎮", *, added=True):
        return run(ops.handle_reaction(
            self.gw, GUILD, ANNOUNCE, self.message_id, user_id, emoji, added=added
        ))


class ParsingTests(unittest.TestCase):
    def test_a_message_link_gives_guild_channel_and_message(self):
        link = "https://discord.com/channels/100000000000000001/100000000000000101/100000000000000999"
        self.assertEqual(
            reaction_roles.parse_message_ref(link),
            (100000000000000001, 100000000000000101, 100000000000000999),
        )

    def test_a_bare_id_needs_the_channel_from_elsewhere(self):
        self.assertEqual(reaction_roles.parse_message_ref(" 555 "), (None, None, 555))

    def test_anything_else_is_refused(self):
        with self.assertRaises(ValueError):
            reaction_roles.parse_message_ref("the pinned one")

    def test_standard_emoji_drop_the_variation_selector(self):
        self.assertEqual(reaction_roles.parse_emoji("🎮\ufe0f"), "🎮")
        self.assertEqual(reaction_roles.normalize_emoji("✅\ufe0f"), "✅")

    def test_server_emoji_are_accepted(self):
        self.assertEqual(reaction_roles.parse_emoji("<:castle:123456789012345678>"), "<:castle:123456789012345678>")
        self.assertEqual(reaction_roles.parse_emoji("<a:spin:123456789012345678>"), "<a:spin:123456789012345678>")

    def test_plain_words_are_not_emoji(self):
        for text in ("gaming", "yes", ""):
            with self.assertRaises(ValueError):
                reaction_roles.parse_emoji(text)


class AddTests(ReactionRoleTestCase):
    def test_a_rule_is_stored_and_listed(self):
        result = self.add()
        self.assertEqual(result["emoji"], "🎮")
        self.assertEqual(result["role_id"], str(MEMBER_ROLE))
        rows = ops.list_reaction_roles(GUILD)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], self.message_id)
        self.assertEqual(ops.overview(self.cfg, GUILD, now=NOW)["reaction_roles"], 1)

    def test_the_bot_reacts_so_members_have_something_to_press(self):
        # The fake records nothing for reactions, so the check is that the add does not refuse.
        self.add(emoji="✅")
        self.assertEqual(len(ops.list_reaction_roles(GUILD)), 1)

    def test_a_message_link_sets_the_channel_too(self):
        link = f"https://discord.com/channels/{GUILD}/{ANNOUNCE}/{self.message_id}"
        result = self.add(message=link)
        self.assertEqual(result["channel_id"], str(ANNOUNCE))

    def test_a_bare_id_without_a_channel_uses_the_announcement_channel(self):
        self.assertEqual(self.add()["channel_id"], str(ANNOUNCE))

    def test_a_bare_id_in_another_channel_needs_that_channel(self):
        with self.assertRaises(OperationError):
            self.add(message=str(self.message_id), channel_id=GENERAL)  # the message is not in #general

    def test_a_link_from_another_server_is_refused(self):
        link = f"https://discord.com/channels/{OTHER_GUILD}/{ANNOUNCE}/{self.message_id}"
        with self.assertRaises(OperationError):
            self.add(message=link)

    def test_the_same_emoji_can_not_give_two_roles_on_one_message(self):
        self.add(emoji="🎮")
        with self.assertRaises(OperationError):
            self.add(emoji="🎮\ufe0f", role=EVENT_PINGS)
        self.assertEqual(len(ops.list_reaction_roles(GUILD)), 1)

    def test_a_role_the_bot_can_not_give_is_refused(self):
        self.gw.unassignable_roles.add(EVENT_PINGS)
        with self.assertRaises(OperationError) as caught:
            self.add(role=EVENT_PINGS)
        self.assertIn("move my role above it", str(caught.exception))
        self.assertEqual(ops.list_reaction_roles(GUILD), [])

    def test_a_role_that_does_not_exist_is_refused(self):
        with self.assertRaises(OperationError):
            self.add(role=999)

    def test_a_message_that_does_not_exist_is_refused(self):
        with self.assertRaises(OperationError):
            self.add(message="123")

    def test_no_channel_and_no_announcement_channel_is_refused(self):
        settings.set_guild_value(self.cfg, GUILD, "announcement_channel_id", None)
        with self.assertRaises(OperationError):
            self.add()

    def test_the_add_is_audited(self):
        self.add()
        actions = [row["action"] for row in store.db_fetchall("SELECT action FROM audit_log WHERE guild_id = ?", (GUILD,))]
        self.assertIn("reactionrole.add", actions)


class ReactionTests(ReactionRoleTestCase):
    def test_adding_the_emoji_gives_the_role(self):
        self.add()
        self.assertEqual(self.react(1001), MEMBER_ROLE)
        self.assertEqual(self.gw.role_changes, [(GUILD, 1001, MEMBER_ROLE, True)])

    def test_removing_the_emoji_takes_the_role(self):
        self.add()
        self.react(1001)
        self.assertEqual(self.react(1001, added=False), MEMBER_ROLE)
        self.assertEqual(self.gw.role_changes[-1], (GUILD, 1001, MEMBER_ROLE, False))

    def test_the_variation_selector_does_not_matter(self):
        self.add(emoji="🎮")
        self.assertEqual(self.react(1001, "🎮\ufe0f"), MEMBER_ROLE)

    def test_other_emoji_and_other_messages_do_nothing(self):
        self.add()
        self.assertIsNone(self.react(1001, "🔥"))
        other = run(self.gw.post(GUILD, ANNOUNCE, content="other"))["message_id"]
        self.assertIsNone(run(ops.handle_reaction(self.gw, GUILD, ANNOUNCE, other, 1001, "🎮", added=True)))
        self.assertEqual(self.gw.role_changes, [])

    def test_a_failed_role_change_is_logged_not_raised(self):
        self.add()

        class Refusing(DemoGateway):
            async def set_member_role(self, guild_id, user_id, role_id, *, add):
                raise OperationError("I can not change that role for that member.")

        self.gw = Refusing()
        self.assertIsNone(self.react(1001))


class RemoveTests(ReactionRoleTestCase):
    def test_a_removed_rule_stops_giving_roles(self):
        rule = self.add()
        ops.remove_reaction_role(GUILD, STAFF, int(rule["id"]))
        self.assertEqual(ops.list_reaction_roles(GUILD), [])
        self.assertIsNone(self.react(1001))

    def test_removing_a_missing_rule_is_refused(self):
        with self.assertRaises(OperationError):
            ops.remove_reaction_role(GUILD, STAFF, 999)

    def test_rules_belong_to_their_server(self):
        rule = self.add()
        with self.assertRaises(OperationError):
            ops.remove_reaction_role(OTHER_GUILD, STAFF, int(rule["id"]))
        self.assertEqual(len(ops.list_reaction_roles(GUILD)), 1)


if __name__ == "__main__":
    unittest.main()
