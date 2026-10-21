"""
Tests for operations.py: the staff flows that slash commands and the dashboard
both call. Discord is replaced by DemoGateway, which keeps messages and
reactions in memory, so these run without a connection.

Run with either:
    python -m pytest tests/test_operations.py
    python tests/test_operations.py
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
import audit  # noqa: E402
import events  # noqa: E402
import operations as ops  # noqa: E402
import polls  # noqa: E402
import settings  # noqa: E402
import store  # noqa: E402
from audit import DASHBOARD_ACTOR, Actor  # noqa: E402
from demo import DemoGateway  # noqa: E402
from discord_gateway import _norm  # noqa: E402
from operations import OperationError  # noqa: E402

GUILD = 100000000000000001
ANNOUNCE = 100000000000000101  # a news channel
GENERAL = 100000000000000102
STAFF_LOG = 100000000000000105
STAFF_ROLE = 100000000000000201
EVENT_PINGS = 100000000000000203
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
STAFF = Actor(42, "Ada", "discord")


def run(coro):
    return asyncio.run(coro)


class OpsTestCase(unittest.TestCase):
    """A fresh data directory, database, in-memory Discord and config per test."""

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

    def announce(self, **kwargs):
        args = {"message": "Doors at 8", "title": "Raid night"}
        args.update(kwargs)
        return run(ops.announce_now(self.gw, self.cfg, GUILD, STAFF, now=NOW, **args))

    def scheduler(self, when: datetime):
        return run(ops.run_scheduler(self.gw, self.cfg, now=when))


class AnnounceTests(OpsTestCase):
    def test_post_pings_only_the_chosen_role_and_is_recorded(self):
        result = self.announce(mention_role_id=STAFF_ROLE)
        post = self.gw.posts[0]
        self.assertEqual(post["channel_id"], ANNOUNCE)
        self.assertEqual(post["content"], f"<@&{STAFF_ROLE}>")
        self.assertEqual(result["channel_id"], ANNOUNCE)

        history = ann.list_history(GUILD)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["source"], "now")
        self.assertEqual(history[0]["mention_role_id"], STAFF_ROLE)

        entries = audit.list_entries(GUILD)
        self.assertEqual(entries[0]["action"], "announce.post")
        self.assertEqual(entries[0]["actor_name"], "Ada")
        self.assertEqual(entries[0]["source"], "discord")

    def test_staff_log_gets_a_copy(self):
        self.announce()
        self.assertEqual([p["channel_id"] for p in self.gw.posts], [ANNOUNCE, STAFF_LOG])

    def test_no_role_means_no_content(self):
        self.announce()
        self.assertIsNone(self.gw.posts[0]["content"])

    def test_empty_message_is_refused_and_nothing_is_posted(self):
        with self.assertRaises(OperationError):
            self.announce(message="   ")
        self.assertEqual(self.gw.posts, [])

    def test_no_channel_configured_explains_how_to_fix_it(self):
        settings.set_guild_value(self.cfg, GUILD, "announcement_channel_id", None)
        with self.assertRaises(OperationError) as ctx:
            self.announce()
        self.assertIn("No channel was given", str(ctx.exception))

    def test_channel_without_permission_is_refused(self):
        self.gw.fail_posts_in.add(GENERAL)
        with self.assertRaises(OperationError):
            self.announce(channel_id=GENERAL)

    def test_publish_warns_when_the_channel_is_not_an_announcement_channel(self):
        result = self.announce(channel_id=GENERAL, publish=True)
        self.assertFalse(result["published"])
        self.assertTrue(any("followers" in w for w in result["warnings"]))

    def test_dashboard_actor_is_recorded(self):
        self.announce()
        run(ops.announce_now(self.gw, self.cfg, GUILD, DASHBOARD_ACTOR, message="Hi", now=NOW))
        self.assertEqual(audit.list_entries(GUILD, 1)[0]["source"], "dashboard")


class ScheduleTests(OpsTestCase):
    def schedule(self, when="10m", **kwargs):
        args = {"message": "Server restart at 9", "title": "Restart"}
        args.update(kwargs)
        return run(ops.schedule_announcement(self.gw, self.cfg, GUILD, STAFF, when=when, now=NOW, **args))

    def test_due_post_goes_out_once(self):
        result = self.schedule()
        self.assertEqual(ann.parse_stored(result["send_at"]), NOW + timedelta(minutes=10))

        self.assertEqual(self.scheduler(NOW)["posted"], 0)
        self.assertEqual(self.scheduler(NOW + timedelta(minutes=11))["posted"], 1)
        self.assertEqual(self.scheduler(NOW + timedelta(minutes=12))["posted"], 0)
        self.assertEqual(ops.list_schedule(GUILD), [])
        self.assertEqual(ann.list_history(GUILD)[0]["source"], "scheduled")

    def test_absolute_time_uses_the_guild_timezone(self):
        result = run(ops.schedule_announcement(
            self.gw, self.cfg, GUILD, STAFF, when="2026-10-16 09:00", message="Morning", now=NOW
        ))
        self.assertEqual(ann.parse_stored(result["send_at"]), datetime(2026, 10, 16, 13, 0, tzinfo=timezone.utc))

    def test_failed_post_is_marked_failed_and_logged_to_staff(self):
        self.schedule()
        self.gw.fail_posts_in.add(ANNOUNCE)
        counts = self.scheduler(NOW + timedelta(minutes=11))
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(ops.list_schedule(GUILD), [])
        row = store.db_fetchone("SELECT status, error FROM scheduled_announcements WHERE guild_id = ?", (GUILD,))
        self.assertEqual(row["status"], "failed")
        self.assertIn("can't send", row["error"])
        self.assertEqual(self.gw.posts[-1]["channel_id"], STAFF_LOG)

    def test_repeat_runs_on_its_interval_without_bursting(self):
        result = self.schedule(when="1h", repeat_every="1h")
        self.assertEqual(result["repeat_seconds"], 3600)
        due = NOW + timedelta(hours=1, seconds=1)
        self.assertEqual(self.scheduler(due)["posted"], 1)
        self.assertEqual(self.scheduler(due)["posted"], 0)  # same moment: nothing more
        self.assertEqual(ops.list_schedule(GUILD)[0]["runs"], 1)

        # After a long gap, missed runs are skipped: one post, not five.
        self.assertEqual(self.scheduler(NOW + timedelta(hours=6))["posted"], 1)
        row = ops.list_schedule(GUILD)[0]
        self.assertGreater(ann.parse_stored(row["send_at"]), NOW + timedelta(hours=6))

    def test_cancel_once_then_refuse_unknown(self):
        result = self.schedule()
        ops.cancel_schedule(GUILD, STAFF, result["id"])
        self.assertEqual(ops.list_schedule(GUILD), [])
        with self.assertRaises(OperationError):
            ops.cancel_schedule(GUILD, STAFF, result["id"])

    def test_too_soon_is_refused(self):
        # 07:00 Toronto time is 11:00 UTC, an hour before NOW.
        with self.assertRaises(OperationError):
            self.schedule(when="2026-10-16 07:00")

    def test_repeat_interval_has_a_floor(self):
        with self.assertRaises(OperationError):
            self.schedule(repeat_every="5m")


class TemplateTests(OpsTestCase):
    def test_save_use_and_delete(self):
        saved = run(ops.save_template(GUILD, STAFF, name="Raid Night", title="Raid", message="Tonight at 8"))
        self.assertEqual(saved, {"name": "raid night", "updated": False})
        again = run(ops.save_template(GUILD, STAFF, name="raid night", title="Raid", message="Tonight at 9"))
        self.assertTrue(again["updated"])

        used = run(ops.use_template(self.gw, self.cfg, GUILD, STAFF, name="RAID NIGHT"))
        self.assertEqual(used["channel_id"], ANNOUNCE)
        self.assertEqual(ann.list_history(GUILD)[0]["body"], "Tonight at 9")

        ops.delete_template(GUILD, STAFF, "raid night")
        with self.assertRaises(OperationError):
            ops.delete_template(GUILD, STAFF, "raid night")

    def test_bad_names_are_refused(self):
        with self.assertRaises(OperationError):
            run(ops.save_template(GUILD, STAFF, name="no!", title=None, message="x"))


class PollTests(OpsTestCase):
    def create(self, options=("Tuesday", "Friday"), duration=None):
        return run(ops.create_poll(
            self.gw, self.cfg, GUILD, STAFF, question="Which night?", options=list(options),
            duration=duration, now=NOW,
        ))

    def test_text_options_split_on_bar_or_newline(self):
        result = run(ops.create_poll(
            self.gw, self.cfg, GUILD, STAFF, question="Which night?", options="Tuesday | Friday\nSunday", now=NOW
        ))
        self.assertEqual(result["options"], ["Tuesday", "Friday", "Sunday"])

    def test_legacy_reaction_poll_counts_reactions_minus_the_bots_own(self):
        # A poll made before buttons existed: its votes are reactions, not rows in poll_votes.
        message = run(self.gw.post(GUILD, ANNOUNCE, content="old poll"))
        poll_id = polls.create_poll(
            guild_id=GUILD, channel_id=ANNOUNCE, author_id=STAFF.id, question="Old poll",
            options=["Tuesday", "Friday"], ends_at=None, now=NOW, mode=polls.MODE_REACTIONS,
        )
        polls.attach_message(poll_id, message["message_id"])
        self.gw.counts[(message["message_id"], _norm(polls.EMOJIS[0]))] = 4  # bot + 3 members
        self.gw.counts[(message["message_id"], _norm(polls.EMOJIS[1]))] = 2  # bot + 1 member
        result = run(ops.close_poll(self.gw, self.cfg, GUILD, STAFF, poll_id, now=NOW))
        self.assertEqual(result["votes"], [3, 1])
        self.assertEqual(ops.list_polls(GUILD)[0]["status"], "closed")
        with self.assertRaises(OperationError):
            run(ops.close_poll(self.gw, self.cfg, GUILD, STAFF, poll_id, now=NOW))

    def test_new_polls_are_button_polls_with_no_reactions_to_count(self):
        poll = self.create()
        row = polls.get_poll(GUILD, poll["id"])
        self.assertEqual(row["mode"], polls.MODE_BUTTONS)
        self.assertFalse(row["multiple"])
        view = self.gw.messages[(ANNOUNCE, int(poll["message_id"]))]["view"]
        self.assertEqual([b["custom_id"] for b in view], [f"cm:poll:{poll['id']}:0", f"cm:poll:{poll['id']}:1"])
        # Closing with no votes: the tally comes from the database, and the buttons are removed.
        result = run(ops.close_poll(self.gw, self.cfg, GUILD, STAFF, poll["id"], now=NOW))
        self.assertEqual(result["votes"], [0, 0])
        self.assertEqual(result["voters"], 0)
        self.assertIsNone(self.gw.messages[(ANNOUNCE, int(poll["message_id"]))]["view"])

    def test_option_count_is_checked(self):
        with self.assertRaises(OperationError):
            self.create(options=["only one"])
        with self.assertRaises(OperationError):
            self.create(options=[f"option {i}" for i in range(11)])

    def test_timed_poll_closes_itself(self):
        self.create(duration="1h")
        self.assertEqual(self.scheduler(NOW + timedelta(minutes=30))["polls_closed"], 0)
        self.assertEqual(self.scheduler(NOW + timedelta(hours=1, minutes=1))["polls_closed"], 1)
        self.assertEqual(ops.list_polls(GUILD, open_only=True), [])

    def test_bad_duration_is_refused(self):
        with self.assertRaises(OperationError):
            self.create(duration="31d")


class EventTests(OpsTestCase):
    def create(self, **kwargs):
        args = {"title": "Team night", "starts_when": "3h", "remind_minutes": 60, "description": "Bring headsets"}
        args.update(kwargs)
        return run(ops.create_event(self.gw, self.cfg, GUILD, STAFF, now=NOW, **args))

    def test_rsvp_count_and_reminder_mentions_the_people_going(self):
        event = self.create()
        self.assertEqual(event["reminder_at"], ann.iso_utc(NOW + timedelta(hours=2)))
        message_id = int(event["message_id"])
        self.gw.counts[(message_id, _norm(events.RSVP_EMOJI))] = 5  # bot + 4 going
        listed = run(ops.list_events(self.gw, GUILD, now=NOW))
        self.assertEqual(listed[0]["going"], 4)

        counts = self.scheduler(NOW + timedelta(hours=2, minutes=1))
        self.assertEqual(counts["reminders"], 1)
        reminder = self.gw.posts[-1]
        self.assertEqual(reminder["channel_id"], ANNOUNCE)
        self.assertEqual(reminder["content"].count("<@"), 4)
        self.assertEqual(self.scheduler(NOW + timedelta(hours=3))["reminders"], 0)

    def test_reminder_mentions_are_capped_at_fifty(self):
        event = self.create()
        self.gw.counts[(int(event["message_id"]), _norm(events.RSVP_EMOJI))] = 200
        self.scheduler(NOW + timedelta(hours=2, minutes=1))
        self.assertLessEqual(self.gw.posts[-1]["content"].count("<@"), 50)

    def test_starting_too_soon_is_refused(self):
        with self.assertRaises(OperationError):
            self.create(starts_when="2m")

    def test_cancel_removes_it_from_the_list(self):
        event = self.create(remind_minutes=None)
        self.assertIsNone(event["reminder_at"])
        run(ops.cancel_event(self.gw, self.cfg, GUILD, STAFF, event["id"]))
        self.assertEqual(run(ops.list_events(self.gw, GUILD, now=NOW)), [])
        with self.assertRaises(OperationError):
            run(ops.cancel_event(self.gw, self.cfg, GUILD, STAFF, event["id"]))


class WelcomeTests(OpsTestCase):
    def test_set_send_and_turn_off(self):
        run(ops.set_welcome(self.gw, self.cfg, GUILD, STAFF, channel_id=GENERAL))
        sent = run(ops.send_welcome(self.gw, self.cfg, GUILD, user_id=77, user_name="Newbie", server="Combat", members=10))
        self.assertIsNotNone(sent)
        post = self.gw.posts[-1]
        self.assertEqual(post["channel_id"], GENERAL)
        self.assertIn("<@77>", post["content"])
        self.assertIsNone(post["embed"])  # plain text, so the mention is a real ping
        self.assertIn("member #10", post["content"])

        ops.disable_welcome(self.cfg, GUILD, STAFF)
        before = len(self.gw.posts)
        self.assertIsNone(run(ops.send_welcome(self.gw, self.cfg, GUILD, user_id=78, user_name="x", server="Combat", members=11)))
        self.assertEqual(len(self.gw.posts), before)

    def test_custom_message_and_placeholders(self):
        run(ops.set_welcome(self.gw, self.cfg, GUILD, STAFF, channel_id=GENERAL, message="Hi {name} from {server}"))
        text = ops.render_for(self.cfg, GUILD, mention="@x", name="Sam", server="Combat", members=3)
        self.assertEqual(text, "Hi Sam from Combat")

    def test_blank_message_is_refused(self):
        with self.assertRaises(OperationError):
            run(ops.set_welcome(self.gw, self.cfg, GUILD, STAFF, channel_id=GENERAL, message="   "))

    def test_test_message_needs_a_channel(self):
        with self.assertRaises(OperationError):
            run(ops.send_test_welcome(self.gw, self.cfg, GUILD, STAFF, server="Combat", members=3))
        run(ops.set_welcome(self.gw, self.cfg, GUILD, STAFF, channel_id=GENERAL))
        result = run(ops.send_test_welcome(self.gw, self.cfg, GUILD, STAFF, server="Combat", members=3))
        self.assertEqual(result["channel_id"], GENERAL)


class SettingsTests(OpsTestCase):
    def test_only_whitelisted_fields_are_accepted(self):
        with self.assertRaises(OperationError):
            run(ops.update_settings(self.gw, self.cfg, GUILD, STAFF, {"bot_token": "nope"}))

    def test_timezone_is_validated_and_saved(self):
        with self.assertRaises(OperationError):
            run(ops.update_settings(self.gw, self.cfg, GUILD, STAFF, {"timezone": "Mars/Base"}))
        run(ops.update_settings(self.gw, self.cfg, GUILD, STAFF, {"timezone": "Europe/London"}))
        self.assertEqual(settings.get_timezone_name(self.cfg, GUILD), "Europe/London")
        self.assertEqual(audit.list_entries(GUILD, 1)[0]["action"], "settings.update")

    def test_empty_value_clears_a_setting(self):
        run(ops.update_settings(self.gw, self.cfg, GUILD, STAFF, {"staff_role_id": ""}))
        self.assertIsNone(settings.get_staff_role_id(self.cfg, GUILD))

    def test_unknown_channel_is_refused(self):
        with self.assertRaises(OperationError):
            run(ops.update_settings(self.gw, self.cfg, GUILD, STAFF, {"staff_channel_id": 999}))


class OverviewTests(OpsTestCase):
    def test_counts_follow_what_was_created(self):
        run(ops.schedule_announcement(self.gw, self.cfg, GUILD, STAFF, when="1h", message="a", now=NOW))
        run(ops.save_template(GUILD, STAFF, name="t", title=None, message="b"))
        run(ops.create_poll(self.gw, self.cfg, GUILD, STAFF, question="q", options=["a", "b"], now=NOW))
        run(ops.create_event(self.gw, self.cfg, GUILD, STAFF, title="e", starts_when="3h", now=NOW))
        data = ops.overview(self.cfg, GUILD, now=NOW)
        self.assertEqual(data["scheduled"], 1)
        self.assertEqual(data["templates"], 1)
        self.assertEqual(data["polls_open"], 1)
        self.assertEqual(data["events_upcoming"], 1)
        self.assertFalse(data["welcome_enabled"])


if __name__ == "__main__":
    unittest.main()
