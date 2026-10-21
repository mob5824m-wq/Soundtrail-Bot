"""Button polls: one vote per person, single and multiple choice, live results, and the buttons.

Run with either:
    python -m pytest tests/test_poll_votes.py
    python tests/test_poll_votes.py
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

import components  # noqa: E402
import operations as ops  # noqa: E402
import polls  # noqa: E402
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
ALICE = Actor(1001, "Alice", "discord")
BOB = Actor(1002, "Bob", "discord")
CAROL = Actor(1003, "Carol", "discord")


def run(coro):
    return asyncio.run(coro)


class PollVoteTestCase(unittest.TestCase):
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

    def create(self, options=("Tuesday", "Friday", "Sunday"), multiple=False, duration=None):
        return run(ops.create_poll(
            self.gw, self.cfg, GUILD, STAFF, question="Which night?", options=list(options),
            multiple=multiple, duration=duration, now=NOW,
        ))

    def vote(self, poll, actor, index, *, when=NOW):
        return run(ops.cast_vote(self.gw, self.cfg, GUILD, poll["id"], actor, index, now=when))

    def message(self, poll):
        return self.gw.messages[(ANNOUNCE, int(poll["message_id"]))]

    def live_embed_text(self, poll) -> str:
        return self.message(poll)["embed"].description


class SingleChoiceTests(PollVoteTestCase):
    def test_a_new_pick_replaces_the_old_one(self):
        poll = self.create()
        first = self.vote(poll, ALICE, 0)
        self.assertEqual(first["choices"], ["Tuesday"])
        second = self.vote(poll, ALICE, 2)
        self.assertEqual(second["choices"], ["Sunday"])
        self.assertTrue(second["added"])
        counts, voters = polls.tally(poll["id"], 3)
        self.assertEqual(counts, [0, 0, 1])
        self.assertEqual(voters, 1)

    def test_pressing_the_same_option_again_changes_nothing(self):
        poll = self.create()
        self.vote(poll, ALICE, 1)
        again = self.vote(poll, ALICE, 1)
        self.assertTrue(again["unchanged"])
        self.assertEqual(polls.tally(poll["id"], 3)[0], [0, 1, 0])

    def test_one_vote_per_person_even_with_many_people(self):
        poll = self.create()
        for member in (ALICE, BOB, CAROL):
            self.vote(poll, member, 0)
        self.vote(poll, ALICE, 1)
        counts, voters = polls.tally(poll["id"], 3)
        self.assertEqual(counts, [2, 1, 0])
        self.assertEqual(voters, 3)


class MultipleChoiceTests(PollVoteTestCase):
    def test_each_press_toggles_an_option(self):
        poll = self.create(multiple=True)
        self.assertEqual(self.vote(poll, ALICE, 0)["choices"], ["Tuesday"])
        self.assertEqual(self.vote(poll, ALICE, 2)["choices"], ["Tuesday", "Sunday"])
        off = self.vote(poll, ALICE, 0)
        self.assertFalse(off["added"])
        self.assertEqual(off["choices"], ["Sunday"])
        self.assertEqual(polls.tally(poll["id"], 3)[0], [0, 0, 1])

    def test_voters_counts_people_not_picks(self):
        poll = self.create(multiple=True)
        self.vote(poll, ALICE, 0)
        self.vote(poll, ALICE, 1)
        self.vote(poll, BOB, 1)
        counts, voters = polls.tally(poll["id"], 3)
        self.assertEqual(counts, [1, 2, 0])
        self.assertEqual(voters, 2)


class LiveResultsTests(PollVoteTestCase):
    def test_the_poll_message_is_edited_after_each_change(self):
        poll = self.create()
        self.vote(poll, ALICE, 0)
        self.assertIn("**Tuesday**: 1 (100%)", self.live_embed_text(poll))
        self.vote(poll, BOB, 1)
        text = self.live_embed_text(poll)
        self.assertIn("**Tuesday**: 1 (50%)", text)
        self.assertIn("**Friday**: 1 (50%)", text)

    def test_a_repeat_press_does_not_edit_the_message(self):
        poll = self.create()
        self.vote(poll, ALICE, 0)
        embed_before = self.message(poll)["embed"]
        self.vote(poll, ALICE, 0)
        self.assertIs(self.message(poll)["embed"], embed_before)

    def test_the_vote_is_kept_if_the_message_can_not_be_edited(self):
        poll = self.create()
        del self.gw.messages[(ANNOUNCE, int(poll["message_id"]))]
        result = self.vote(poll, ALICE, 0)
        self.assertEqual(result["choices"], ["Tuesday"])
        self.assertEqual(polls.tally(poll["id"], 3)[0], [1, 0, 0])

    def test_poll_details_show_counts_and_who_chose_what(self):
        poll = self.create(multiple=True)
        self.vote(poll, ALICE, 0)
        self.vote(poll, ALICE, 2)
        self.vote(poll, BOB, 2)
        details = ops.poll_details(GUILD, poll["id"])
        self.assertEqual(details["counts"], [1, 0, 2])
        self.assertEqual(details["voters_count"], 2)
        by_name = {person["name"]: person["choices"] for person in details["voters"]}
        self.assertEqual(by_name, {"Alice": ["Tuesday", "Sunday"], "Bob": ["Sunday"]})

    def test_poll_details_for_a_missing_poll_is_refused(self):
        with self.assertRaises(OperationError):
            ops.poll_details(GUILD, 999)


class ClosingTests(PollVoteTestCase):
    def test_closing_reads_the_database_and_removes_the_buttons(self):
        poll = self.create(multiple=True)
        self.vote(poll, ALICE, 0)
        self.vote(poll, BOB, 0)
        self.vote(poll, BOB, 1)
        result = run(ops.close_poll(self.gw, self.cfg, GUILD, STAFF, poll["id"], now=NOW))
        self.assertEqual(result["votes"], [2, 1, 0])
        self.assertEqual(result["voters"], 2)
        self.assertIsNone(self.message(poll)["view"])
        self.assertIn("Poll closed", self.message(poll)["embed"].title)
        self.assertIn("2 voter(s) in total", self.message(poll)["embed"].footer.text)

    def test_votes_are_refused_once_the_poll_is_closed(self):
        poll = self.create()
        run(ops.close_poll(self.gw, self.cfg, GUILD, STAFF, poll["id"], now=NOW))
        with self.assertRaises(OperationError):
            self.vote(poll, ALICE, 0)

    def test_votes_are_refused_after_the_end_time_even_before_the_scheduler_runs(self):
        poll = self.create(duration="1h")
        with self.assertRaises(OperationError):
            self.vote(poll, ALICE, 0, when=NOW + timedelta(hours=1, minutes=1))
        self.assertEqual(polls.tally(poll["id"], 3)[0], [0, 0, 0])

    def test_a_timed_poll_closes_with_its_final_tally(self):
        poll = self.create(duration="1h")
        self.vote(poll, ALICE, 1)
        counts = run(ops.run_scheduler(self.gw, self.cfg, now=NOW + timedelta(hours=1, minutes=1)))
        self.assertEqual(counts["polls_closed"], 1)
        self.assertEqual(polls.get_poll(GUILD, poll["id"])["results"], [0, 1, 0])

    def test_wrong_option_number_is_refused(self):
        poll = self.create()
        with self.assertRaises(OperationError):
            self.vote(poll, ALICE, 7)

    def test_a_vote_on_another_servers_poll_is_refused(self):
        poll = self.create()
        with self.assertRaises(OperationError):
            run(ops.cast_vote(self.gw, self.cfg, 555, poll["id"], ALICE, 0, now=NOW))


class OldDatabaseTests(unittest.TestCase):
    """An install from before buttons: its polls keep closing by reaction, and nothing is lost."""

    def test_an_old_polls_table_is_upgraded_and_its_polls_stay_reaction_polls(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as data:
            old = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")
            os.environ["SOUNDTRAIL_SUPPORT_DATA"] = data
            try:
                conn = sqlite3.connect(store.db_path())
                conn.executescript(
                    """
                    CREATE TABLE polls (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
                        channel_id INTEGER NOT NULL, message_id INTEGER, question TEXT NOT NULL,
                        options TEXT NOT NULL, author_id INTEGER NOT NULL, created_at TEXT NOT NULL,
                        ends_at TEXT, status TEXT NOT NULL DEFAULT 'open', results TEXT, closed_at TEXT
                    );
                    INSERT INTO polls (guild_id, channel_id, question, options, author_id, created_at)
                    VALUES (1, 2, 'Old?', '["a", "b"]', 3, '2026-01-01T00:00:00+00:00');
                    """
                )
                conn.commit()
                conn.close()

                store.init_db()
                row = polls.get_poll(1, 1)
                self.assertEqual(row["question"], "Old?")
                self.assertEqual(row["mode"], polls.MODE_REACTIONS)
                self.assertFalse(row["multiple"])
                # The new vote table exists and is empty for old polls.
                self.assertEqual(polls.tally(1, 2), ([0, 0], 0))
            finally:
                if old is None:
                    os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
                else:
                    os.environ["SOUNDTRAIL_SUPPORT_DATA"] = old


class ButtonWiringTests(unittest.TestCase):
    """The buttons: the custom ids, and the discord.py views built from them."""

    def test_the_poll_button_id_round_trips(self):
        pattern = components.PollButton.__discord_ui_compiled_template__
        match = pattern.match(polls.button_id(12, 3))
        self.assertIsNotNone(match)
        self.assertEqual((int(match["pid"]), int(match["index"])), (12, 3))

    def test_poll_buttons_for_a_view_carry_the_poll_and_option(self):
        async def build():
            view = components.build_view(polls.button_specs(7, ["Tuesday", "Friday"]))
            return list(view.children)

        items = asyncio.run(build())
        self.assertEqual(len(items), 2)
        self.assertEqual([(item.poll_id, item.index) for item in items], [(7, 0), (7, 1)])
        self.assertEqual([item.item.custom_id for item in items], ["cm:poll:7:0", "cm:poll:7:1"])
        self.assertEqual([item.item.label for item in items], ["Tuesday", "Friday"])

    def test_long_option_labels_are_cut_to_the_button_limit(self):
        label = polls.button_label("x" * 200)
        self.assertLessEqual(len(label), polls.MAX_BUTTON_LABEL)

    def test_persistent_views_are_rebuilt_from_custom_ids(self):
        # What discord.py does after a restart: match the custom id and build the item again.
        match = components.PollButton.__discord_ui_compiled_template__.match("cm:poll:9:2")
        item = asyncio.run(components.PollButton.from_custom_id(None, None, match))
        self.assertEqual((item.poll_id, item.index), (9, 2))
        ticket_open = components.TicketOpenButton.__discord_ui_compiled_template__
        self.assertIsNotNone(ticket_open.match(tickets.TICKET_OPEN_ID))
        self.assertIsNone(ticket_open.match("cm:poll:1:0"))

    def test_setup_hook_registers_every_dynamic_item(self):
        # Without this, buttons on messages posted before a restart stop working.
        import bot as bot_module

        bot = bot_module.SoundtrailSupportBot(copy.deepcopy(settings.DEFAULT_CONFIG))
        registered = []
        bot.add_dynamic_items = lambda *items: registered.extend(items)

        async def no_cog(_bot):
            return None

        async def no_dashboard():
            return None

        original_cog = bot_module._register_cog
        bot_module._register_cog = no_cog
        bot._start_dashboard = no_dashboard
        try:
            with unittest.mock.patch.object(bot.scheduler_tick, "start"):
                asyncio.run(bot.setup_hook())
        finally:
            bot_module._register_cog = original_cog
        self.assertEqual(set(registered), set(components.DYNAMIC_ITEMS))


if __name__ == "__main__":
    unittest.main()
