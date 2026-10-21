"""Ticket transcripts, idle auto-close, and what counts as activity.

Run with either:
    python -m pytest tests/test_ticket_upkeep.py
    python tests/test_ticket_upkeep.py
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import operations as ops  # noqa: E402
import settings  # noqa: E402
import store  # noqa: E402
import tickets  # noqa: E402
from audit import Actor  # noqa: E402
from demo import DemoGateway  # noqa: E402
from operations import OperationError  # noqa: E402

GUILD = 100000000000000001
ANNOUNCE = 100000000000000101
STAFF_LOG = 100000000000000105
STAFF_ROLE = 100000000000000201
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
STAFF = Actor(42, "Ada", "discord")
MEMBER = Actor(1001, "Alice", "discord")


def run(coro):
    return asyncio.run(coro)


class UpkeepTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._tmp.name
        store.init_db()
        self.gw = DemoGateway()
        self.cfg = copy.deepcopy(settings.DEFAULT_CONFIG)
        settings.set_guild_value(self.cfg, GUILD, "announcement_channel_id", ANNOUNCE)
        settings.set_guild_value(self.cfg, GUILD, "staff_channel_id", STAFF_LOG)
        settings.set_guild_value(self.cfg, GUILD, "staff_role_id", STAFF_ROLE)
        settings.set_guild_value(self.cfg, GUILD, "timezone", "America/Toronto")

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
        else:
            os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._old
        self._tmp.cleanup()

    def open(self, when=NOW):
        return run(ops.open_ticket(self.gw, self.cfg, GUILD, MEMBER, now=when))

    def close(self, ticket_id, actor=STAFF, *, staff=True, when=NOW):
        return run(ops.close_ticket(self.gw, self.cfg, GUILD, actor, ticket_id, actor_is_staff=staff, now=when))

    def tick(self, when):
        return run(ops.run_scheduler(self.gw, self.cfg, now=when))


class TranscriptTests(UpkeepTestCase):
    def test_closing_keeps_the_conversation_and_posts_it_to_the_staff_log(self):
        ticket = self.open()
        channel = int(ticket["channel_id"])
        self.gw.say(channel, "Alice", "My map pool vote did not count.")
        self.gw.say(channel, "Ada", "Looking into it.")
        self.close(int(ticket["id"]))

        row = tickets.get_transcript(GUILD, int(ticket["id"]))
        self.assertIsNotNone(row)
        self.assertEqual(row["message_count"], 2)
        self.assertIn("Alice: My map pool vote did not count.", row["text"])
        self.assertIn("Ada: Looking into it.", row["text"])

        self.assertEqual(len(self.gw.files), 1)
        filed = self.gw.files[0]
        self.assertEqual(filed["channel_id"], STAFF_LOG)
        self.assertEqual(filed["filename"], f"ticket-{ticket['id']}.txt")
        self.assertIn(b"Looking into it.", filed["data"])

    def test_a_staff_member_can_read_the_saved_transcript_back(self):
        ticket = self.open()
        self.gw.say(int(ticket["channel_id"]), "Alice", "Hello")
        self.close(int(ticket["id"]))
        saved = ops.get_ticket_transcript(GUILD, int(ticket["id"]))
        self.assertIn("Alice: Hello", saved["text"])

    def test_an_open_ticket_has_no_transcript_yet(self):
        ticket = self.open()
        with self.assertRaises(OperationError):
            ops.get_ticket_transcript(GUILD, int(ticket["id"]))

    def test_a_missing_ticket_has_no_transcript(self):
        with self.assertRaises(OperationError):
            ops.get_ticket_transcript(GUILD, 999)

    def test_an_unreadable_channel_still_closes_without_a_transcript(self):
        ticket = self.open()
        channel = int(ticket["channel_id"])
        self.gw.unreadable.add(channel)
        result = self.close(int(ticket["id"]))
        self.assertEqual(result["id"], int(ticket["id"]))
        self.assertIsNone(tickets.get_transcript(GUILD, int(ticket["id"])))
        self.assertIn(channel, self.gw.deleted_channels)
        self.assertEqual(self.gw.files, [])
        logged = [p for p in self.gw.posts if p["channel_id"] == STAFF_LOG][-1]
        self.assertIn("No transcript", logged["embed"].description)

    def test_without_a_staff_log_the_transcript_is_still_kept(self):
        settings.set_guild_value(self.cfg, GUILD, "staff_channel_id", None)
        ticket = self.open()
        self.gw.say(int(ticket["channel_id"]), "Alice", "Hi")
        self.close(int(ticket["id"]))
        self.assertIsNotNone(tickets.get_transcript(GUILD, int(ticket["id"])))
        self.assertEqual(self.gw.files, [])

    def test_a_transcript_saved_again_replaces_the_first(self):
        ticket_id = int(self.open()["id"])
        tickets.save_transcript(GUILD, ticket_id, "first\n", 1, NOW)
        tickets.save_transcript(GUILD, ticket_id, "second\n", 1, NOW)
        rows = store.db_fetchall("SELECT text FROM ticket_transcripts WHERE ticket_id = ?", (ticket_id,))
        self.assertEqual([r["text"] for r in rows], ["second\n"])

    def test_the_transcript_text_format(self):
        messages = [
            {
                "author": "Alice",
                "bot": False,
                "created_at": datetime(2026, 10, 16, 9, 5, tzinfo=timezone(timedelta(hours=-4))),
                "content": "line one\nline two",
                "attachments": ["https://cdn.example/shot.png"],
            },
            {"author": "Soundtrail Support", "bot": True, "created_at": NOW, "content": "", "attachments": []},
        ]
        text = tickets.render_transcript(messages)
        self.assertEqual(
            text.splitlines(),
            [
                "[2026-10-16 13:05 UTC] Alice: line one",
                "    line two",
                "    attachment: https://cdn.example/shot.png",
                "[2026-10-16 12:00 UTC] Soundtrail Support (bot): (no text)",
            ],
        )

    def test_an_empty_ticket_gives_an_empty_transcript(self):
        self.assertEqual(tickets.render_transcript([]), "")


class IdleCloseTests(UpkeepTestCase):
    def set_days(self, days):
        run(ops.update_settings(self.gw, self.cfg, GUILD, STAFF, {"ticket_auto_close_days": days}))

    def test_auto_close_is_off_by_default(self):
        ticket = self.open()
        self.assertEqual(settings.get_ticket_auto_close_days(self.cfg, GUILD), 0)
        self.assertEqual(self.tick(NOW + timedelta(days=365))["tickets_closed"], 0)
        self.assertEqual(ops.list_tickets(GUILD, open_only=True)[0]["id"], int(ticket["id"]))

    def test_an_idle_ticket_is_closed_after_the_set_days(self):
        self.set_days(2)
        ticket = self.open()
        channel = int(ticket["channel_id"])
        self.gw.say(channel, "Alice", "Hello")

        self.assertEqual(self.tick(NOW + timedelta(days=1))["tickets_closed"], 0)
        counts = self.tick(NOW + timedelta(days=2, minutes=1))
        self.assertEqual(counts["tickets_closed"], 1)
        self.assertIn(channel, self.gw.deleted_channels)
        self.assertEqual(ops.list_tickets(GUILD, open_only=True), [])
        notice = [p for p in self.gw.posts if p["channel_id"] == channel]
        self.assertTrue(notice)
        self.assertIn("no one has written here", notice[-1]["embed"].description.lower())
        self.assertIsNotNone(tickets.get_transcript(GUILD, int(ticket["id"])))

    def test_a_message_resets_the_idle_clock(self):
        self.set_days(2)
        ticket = self.open()
        channel = int(ticket["channel_id"])
        ops.note_ticket_activity(channel, now=NOW + timedelta(days=1))

        self.assertEqual(self.tick(NOW + timedelta(days=2, minutes=1))["tickets_closed"], 0)
        self.assertEqual(self.tick(NOW + timedelta(days=3, minutes=1))["tickets_closed"], 1)

    def test_a_ticket_with_no_messages_counts_from_opening(self):
        self.set_days(1)
        self.open()
        self.assertEqual(self.tick(NOW + timedelta(hours=23))["tickets_closed"], 0)
        self.assertEqual(self.tick(NOW + timedelta(hours=25))["tickets_closed"], 1)

    def test_each_server_uses_its_own_setting(self):
        self.set_days(1)
        ticket = self.open()
        settings.set_guild_value(self.cfg, GUILD, "ticket_auto_close_days", 0)
        self.assertEqual(self.tick(NOW + timedelta(days=5))["tickets_closed"], 0)
        self.assertEqual(ops.list_tickets(GUILD, open_only=True)[0]["id"], int(ticket["id"]))

    def test_a_ticket_closed_by_hand_is_not_closed_again(self):
        self.set_days(1)
        ticket = self.open()
        self.close(int(ticket["id"]), when=NOW)
        self.assertEqual(self.tick(NOW + timedelta(days=3))["tickets_closed"], 0)

    def test_the_setting_is_validated(self):
        for bad in (-1, 366, "soon"):
            with self.assertRaises(OperationError):
                run(ops.update_settings(self.gw, self.cfg, GUILD, STAFF, {"ticket_auto_close_days": bad}))
        saved = run(ops.update_settings(self.gw, self.cfg, GUILD, STAFF, {"ticket_auto_close_days": "3"}))
        self.assertEqual(saved["ticket_auto_close_days"], 3)
        cleared = run(ops.update_settings(self.gw, self.cfg, GUILD, STAFF, {"ticket_auto_close_days": None}))
        self.assertEqual(cleared["ticket_auto_close_days"], 0)


class ActivityListenerTests(unittest.TestCase):
    """The bot's message listener: members and staff count, the bot and direct messages do not."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._tmp.name
        store.init_db()

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
        else:
            os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._old
        self._tmp.cleanup()

    def message(self, *, bot=False, guild=True):
        import types

        return types.SimpleNamespace(
            guild=types.SimpleNamespace(id=GUILD) if guild else None,
            author=types.SimpleNamespace(bot=bot),
            channel=types.SimpleNamespace(id=555),
        )

    def listener_calls(self, message):
        import bot as bot_module

        bot = bot_module.SoundtrailSupportBot(copy.deepcopy(settings.DEFAULT_CONFIG))
        with unittest.mock.patch.object(bot_module.ops, "note_ticket_activity") as note:
            run(bot.on_message(message))
        return note

    def test_a_member_message_counts(self):
        note = self.listener_calls(self.message())
        note.assert_called_once_with(555, staff_reply=False)

    def test_the_bots_own_messages_do_not_count(self):
        note = self.listener_calls(self.message(bot=True))
        note.assert_not_called()

    def test_direct_messages_do_not_count(self):
        note = self.listener_calls(self.message(guild=False))
        note.assert_not_called()

    def test_a_failure_in_the_bookkeeping_does_not_escape(self):
        import bot as bot_module

        bot = bot_module.SoundtrailSupportBot(copy.deepcopy(settings.DEFAULT_CONFIG))
        with unittest.mock.patch.object(bot_module.ops, "note_ticket_activity", side_effect=RuntimeError("db")):
            run(bot.on_message(self.message()))  # must not raise


if __name__ == "__main__":
    unittest.main()
