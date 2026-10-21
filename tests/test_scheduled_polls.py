"""Scheduled polls: stored with a send time, posted by the scheduler, close counted from posting.

Run with either:
    python -m pytest tests/test_scheduled_polls.py
    python tests/test_scheduled_polls.py
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import announcements as ann  # noqa: E402
import operations as ops  # noqa: E402
import polls  # noqa: E402
import settings  # noqa: E402
import store  # noqa: E402
from audit import Actor  # noqa: E402
from demo import DemoGateway  # noqa: E402
from operations import OperationError  # noqa: E402

GUILD = 100000000000000001
ANNOUNCE = 100000000000000101
STAFF_LOG = 100000000000000105
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
STAFF = Actor(42, "Ada", "discord")
VOTER = Actor(1001, "Alice", "discord")


def run(coro):
    return asyncio.run(coro)


class ScheduledPollTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._tmp.name
        store.init_db()
        self.gw = DemoGateway()
        self.cfg = copy.deepcopy(settings.DEFAULT_CONFIG)
        settings.set_guild_value(self.cfg, GUILD, "announcement_channel_id", ANNOUNCE)
        settings.set_guild_value(self.cfg, GUILD, "staff_channel_id", STAFF_LOG)
        settings.set_guild_value(self.cfg, GUILD, "timezone", "America/Toronto")

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
        else:
            os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._old
        self._tmp.cleanup()

    def schedule(self, send_at="2h", duration=None, now=NOW):
        return run(
            ops.create_poll(
                self.gw,
                self.cfg,
                GUILD,
                STAFF,
                question="Which map?",
                options=["Dust II", "Inferno"],
                send_at=send_at,
                duration=duration,
                now=now,
            )
        )

    def tick(self, when):
        return run(ops.run_scheduler(self.gw, self.cfg, now=when))

    def row(self, poll_id):
        return polls.get_poll(GUILD, poll_id)


class ScheduleTests(ScheduledPollTestCase):
    def test_a_scheduled_poll_is_stored_and_not_posted_yet(self):
        result = self.schedule()
        self.assertTrue(result["scheduled"])
        self.assertIsNone(result["message_id"])
        self.assertEqual(ann.parse_stored(result["send_at"]), NOW + timedelta(hours=2))
        self.assertEqual(self.row(result["id"])["status"], "scheduled")
        self.assertEqual(self.gw.posts, [])

    def test_the_scheduler_posts_it_once_it_is_due(self):
        poll = self.schedule(duration="1d")
        self.assertEqual(self.tick(NOW + timedelta(hours=1))["polls_posted"], 0)
        self.assertEqual(self.row(poll["id"])["status"], "scheduled")

        counts = self.tick(NOW + timedelta(hours=2, minutes=1))
        self.assertEqual(counts["polls_posted"], 1)
        row = self.row(poll["id"])
        self.assertEqual(row["status"], "open")
        self.assertIsNotNone(row["message_id"])
        self.assertEqual(row["channel_id"], ANNOUNCE)

    def test_the_close_time_counts_from_when_it_is_posted(self):
        poll = self.schedule(duration="1d")
        posted_at = NOW + timedelta(hours=2, minutes=1)
        self.tick(posted_at)
        row = self.row(poll["id"])
        self.assertEqual(ann.parse_stored(row["ends_at"]), posted_at + timedelta(days=1))

    def test_the_posted_poll_has_vote_buttons(self):
        poll = self.schedule()
        self.tick(NOW + timedelta(hours=3))
        message = self.gw.messages[(ANNOUNCE, self.row(poll["id"])["message_id"])]
        self.assertEqual(len(message["view"]), 2)
        self.assertTrue(all(spec["custom_id"].startswith(f"cm:poll:{poll['id']}:") for spec in message["view"]))

    def test_it_is_not_posted_twice(self):
        poll = self.schedule()
        self.tick(NOW + timedelta(hours=3))
        self.tick(NOW + timedelta(hours=4))
        self.assertEqual(len([p for p in self.gw.posts if p["channel_id"] == ANNOUNCE]), 1)
        self.assertEqual(self.row(poll["id"])["status"], "open")

    def test_votes_are_refused_until_it_is_posted(self):
        poll = self.schedule()
        with self.assertRaises(OperationError) as caught:
            run(ops.cast_vote(self.gw, self.cfg, GUILD, poll["id"], VOTER, 0, now=NOW))
        self.assertIn("not posted yet", str(caught.exception))

    def test_a_send_time_that_is_too_soon_or_too_far_is_refused(self):
        with self.assertRaises(OperationError):
            self.schedule(send_at="30s")
        with self.assertRaises(OperationError):
            self.schedule(send_at="400d")
        self.assertEqual(polls.list_polls(GUILD), [])

    def test_scheduling_is_audited(self):
        self.schedule()
        actions = [row["action"] for row in store.db_fetchall("SELECT action FROM audit_log WHERE guild_id = ?", (GUILD,))]
        self.assertIn("poll.schedule", actions)

    def test_a_scheduled_poll_that_can_not_post_is_marked_failed_and_logged(self):
        poll = self.schedule()

        async def refuse(*args, **kwargs):
            raise OperationError("I can't send messages in #announcements.")

        self.gw.post = refuse
        counts = self.tick(NOW + timedelta(hours=3))
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(self.row(poll["id"])["status"], "failed")
        # Not retried every tick.
        self.assertEqual(self.tick(NOW + timedelta(hours=4))["failed"], 0)

    def test_a_failed_post_says_so_in_the_staff_log(self):
        poll = self.schedule()

        async def refuse(*args, **kwargs):
            raise OperationError("I can't send messages there.")

        original = self.gw.post

        async def refuse_except_log(guild_id, channel_id, **kwargs):
            if channel_id == STAFF_LOG:
                return await original(guild_id, channel_id, **kwargs)
            raise OperationError("I can't send messages there.")

        self.gw.post = refuse_except_log
        self.tick(NOW + timedelta(hours=3))
        logged = [p for p in self.gw.posts if p["channel_id"] == STAFF_LOG]
        self.assertTrue(logged)
        self.assertIn("not posted", logged[-1]["embed"].title.lower())
        self.assertEqual(self.row(poll["id"])["status"], "failed")


class CancelTests(ScheduledPollTestCase):
    def test_a_scheduled_poll_can_be_cancelled_and_then_never_posts(self):
        poll = self.schedule()
        result = run(ops.close_poll(self.gw, self.cfg, GUILD, STAFF, poll["id"], now=NOW))
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.row(poll["id"])["status"], "cancelled")
        self.assertEqual(self.tick(NOW + timedelta(days=1))["polls_posted"], 0)
        self.assertEqual(self.gw.posts, [])

    def test_cancelling_twice_is_refused(self):
        poll = self.schedule()
        run(ops.close_poll(self.gw, self.cfg, GUILD, STAFF, poll["id"], now=NOW))
        with self.assertRaises(OperationError):
            run(ops.close_poll(self.gw, self.cfg, GUILD, STAFF, poll["id"], now=NOW))

    def test_a_posted_poll_still_closes_with_results(self):
        poll = self.schedule()
        self.tick(NOW + timedelta(hours=3))
        result = run(ops.close_poll(self.gw, self.cfg, GUILD, STAFF, poll["id"], now=NOW + timedelta(hours=3)))
        self.assertNotIn("cancelled", result)
        self.assertEqual(self.row(poll["id"])["status"], "closed")


if __name__ == "__main__":
    unittest.main()
