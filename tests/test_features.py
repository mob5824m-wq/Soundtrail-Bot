"""Tests for the approval queue, auto-unpin, editing and deleting posts, quiet hours
and the audit export. Discord is replaced by DemoGateway, as in test_operations.

Run with either:
    python -m pytest tests/test_features.py
    python tests/test_features.py
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
import approvals  # noqa: E402
import audit  # noqa: E402
import operations as ops  # noqa: E402
import pins  # noqa: E402
import quiet  # noqa: E402
import settings  # noqa: E402
import store  # noqa: E402
from audit import DASHBOARD_ACTOR, Actor  # noqa: E402
from demo import DemoGateway  # noqa: E402
from operations import OperationError  # noqa: E402

GUILD = 100000000000000001
ANNOUNCE = 100000000000000101
STAFF_LOG = 100000000000000105
STAFF_ROLE = 100000000000000201
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)  # 08:00 in Toronto (EDT, UTC-4)
ADA = Actor(42, "Ada", "discord")
BEN = Actor(43, "Ben", "discord")


def run(coro):
    return asyncio.run(coro)


class FeatureTestCase(unittest.TestCase):
    """A fresh database, in-memory Discord and config per test."""

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

    def announce(self, actor=ADA, **kwargs):
        args = {"message": "Doors at 8", "title": "Raid night", "now": NOW}
        args.update(kwargs)
        return run(ops.announce_now(self.gw, self.cfg, GUILD, actor, **args))

    def require_approval(self, on: bool = True) -> None:
        settings.set_guild_value(self.cfg, GUILD, "require_approval", on)

    def scheduler(self, when: datetime):
        return run(ops.run_scheduler(self.gw, self.cfg, now=when))

    def posts_in_announce(self) -> list[dict]:
        """Posts to the announcement channel only (the staff log is posted to too)."""
        return [p for p in self.gw.posts if p["channel_id"] == ANNOUNCE]

    def audit_actions(self) -> list[str]:
        return [row["action"] for row in audit.list_entries(GUILD, 200)]


# --------------------------------------------------------------------------- #
# Quiet hours (pure time logic)
# --------------------------------------------------------------------------- #
class QuietHoursTests(unittest.TestCase):
    def test_parse_clock(self):
        self.assertEqual(quiet.parse_clock("7:05"), "07:05")
        self.assertEqual(quiet.parse_clock(" 22:00 "), "22:00")
        for bad in ("24:00", "12:60", "noon", "", "2200"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                quiet.parse_clock(bad)

    def test_validate_pair(self):
        self.assertEqual(quiet.validate_pair(None, None), (None, None))
        self.assertEqual(quiet.validate_pair("22:00", "08:00"), ("22:00", "08:00"))
        with self.assertRaises(ValueError):
            quiet.validate_pair("22:00", None)  # only one end
        with self.assertRaises(ValueError):
            quiet.validate_pair("09:00", "09:00")  # empty window

    def test_daytime_window(self):
        # 13:00 to 15:00 Toronto. At 13:30 local (17:30 UTC) the window ends at 15:00 local (19:00 UTC).
        now = datetime(2026, 10, 16, 17, 30, tzinfo=timezone.utc)
        end = quiet.window_end(now, "America/Toronto", "13:00", "15:00")
        self.assertEqual(end, datetime(2026, 10, 16, 19, 0, tzinfo=timezone.utc))
        # 08:00 local is outside it.
        self.assertIsNone(quiet.window_end(NOW, "America/Toronto", "13:00", "15:00"))

    def test_overnight_window_evening_and_morning(self):
        # 22:00 to 08:00. 23:00 local on the 16th ends at 08:00 on the 17th (12:00 UTC).
        evening = datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc)  # 23:00 local, 16th
        self.assertEqual(
            quiet.window_end(evening, "America/Toronto", "22:00", "08:00"),
            datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc),
        )
        # 06:00 local on the 17th is in the morning part; it also ends at 08:00 that day.
        morning = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(
            quiet.window_end(morning, "America/Toronto", "22:00", "08:00"),
            datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc),
        )
        # 12:00 local is outside.
        self.assertIsNone(quiet.window_end(NOW, "America/Toronto", "22:00", "08:00"))

    def test_off_when_unset(self):
        self.assertIsNone(quiet.window_end(NOW, "America/Toronto", None, None))


# --------------------------------------------------------------------------- #
# Approval queue
# --------------------------------------------------------------------------- #
class ApprovalTests(FeatureTestCase):
    def test_announcement_waits_instead_of_posting(self):
        self.require_approval()
        result = self.announce(ADA)
        self.assertTrue(result["queued"])
        self.assertEqual(self.posts_in_announce(), [])
        self.assertEqual(ann.list_history(GUILD), [])
        pending = ops.list_approvals(GUILD)
        self.assertEqual([row["id"] for row in pending], [result["approval_id"]])
        self.assertEqual(pending[0]["requester_name"], "Ada")
        self.assertEqual(pending[0]["payload"]["message"], "Doors at 8")
        # The staff log says it is waiting.
        self.assertTrue(any(p["channel_id"] == STAFF_LOG for p in self.gw.posts))

    def test_another_staff_member_approves_and_it_posts_under_the_requester(self):
        self.require_approval()
        queued = self.announce(ADA)
        result = run(ops.approve_request(self.gw, self.cfg, GUILD, BEN, queued["approval_id"], now=NOW))
        self.assertIsNotNone(result["jump_url"])
        self.assertEqual(len(self.posts_in_announce()), 1)
        history = ann.list_history(GUILD)
        self.assertEqual(history[0]["author_name"], "Ada")  # the author stays the requester
        self.assertEqual(ops.list_approvals(GUILD), [])
        row = approvals.get(GUILD, queued["approval_id"])
        self.assertEqual(row["status"], approvals.APPROVED)
        self.assertEqual(row["decided_name"], "Ben")
        self.assertIn("approval.approve", self.audit_actions())

    def test_nobody_can_approve_their_own_request(self):
        self.require_approval()
        queued = self.announce(ADA)
        with self.assertRaisesRegex(OperationError, "own request"):
            run(ops.approve_request(self.gw, self.cfg, GUILD, ADA, queued["approval_id"], now=NOW))
        self.assertEqual(self.posts_in_announce(), [])

    def test_dashboard_requests_are_approved_from_discord_not_from_the_dashboard(self):
        self.require_approval()
        queued = run(ops.announce_now(self.gw, self.cfg, GUILD, DASHBOARD_ACTOR,
                                      message="From the web", now=NOW))
        with self.assertRaisesRegex(OperationError, "own request"):
            run(ops.approve_request(self.gw, self.cfg, GUILD, DASHBOARD_ACTOR, queued["approval_id"], now=NOW))
        run(ops.approve_request(self.gw, self.cfg, GUILD, BEN, queued["approval_id"], now=NOW))
        self.assertEqual(len(self.posts_in_announce()), 1)

    def test_reject_turns_it_down_and_it_can_not_be_approved_after(self):
        self.require_approval()
        queued = self.announce(ADA)
        run(ops.reject_request(GUILD, BEN, queued["approval_id"], now=NOW))
        self.assertEqual(self.posts_in_announce(), [])
        self.assertEqual(approvals.get(GUILD, queued["approval_id"])["status"], approvals.REJECTED)
        with self.assertRaisesRegex(OperationError, "no pending request"):
            run(ops.approve_request(self.gw, self.cfg, GUILD, BEN, queued["approval_id"], now=NOW))

    def test_a_request_can_only_be_decided_once(self):
        self.require_approval()
        queued = self.announce(ADA)
        run(ops.approve_request(self.gw, self.cfg, GUILD, BEN, queued["approval_id"], now=NOW))
        with self.assertRaisesRegex(OperationError, "no pending request"):
            run(ops.approve_request(self.gw, self.cfg, GUILD, BEN, queued["approval_id"], now=NOW))
        self.assertEqual(len(self.posts_in_announce()), 1)

    def test_a_failed_approval_stays_in_the_queue(self):
        self.require_approval()
        queued = self.announce(ADA)
        self.gw.fail_posts_in.add(ANNOUNCE)
        with self.assertRaises(OperationError):
            run(ops.approve_request(self.gw, self.cfg, GUILD, BEN, queued["approval_id"], now=NOW))
        row = approvals.get(GUILD, queued["approval_id"])
        self.assertEqual(row["status"], approvals.PENDING)
        self.gw.fail_posts_in.clear()
        run(ops.approve_request(self.gw, self.cfg, GUILD, BEN, queued["approval_id"], now=NOW))
        self.assertEqual(len(self.posts_in_announce()), 1)

    def test_schedules_wait_and_then_are_created_for_the_requester(self):
        self.require_approval()
        queued = run(ops.schedule_announcement(
            self.gw, self.cfg, GUILD, ADA, when="2h", message="Tonight", title="Later", now=NOW
        ))
        self.assertTrue(queued["queued"])
        self.assertEqual(ann.list_pending(GUILD), [])
        result = run(ops.approve_request(self.gw, self.cfg, GUILD, BEN, queued["approval_id"], now=NOW))
        self.assertEqual(result["send_at"], ann.iso_utc(NOW + timedelta(hours=2)))
        rows = ann.list_pending(GUILD)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["author_name"], "Ada")

    def test_an_approval_that_comes_too_late_for_its_time_is_refused(self):
        self.require_approval()
        queued = run(ops.schedule_announcement(
            self.gw, self.cfg, GUILD, ADA, when="30m", message="Soon", now=NOW
        ))
        late = NOW + timedelta(hours=1)
        with self.assertRaisesRegex(OperationError, "already passed"):
            run(ops.approve_request(self.gw, self.cfg, GUILD, BEN, queued["approval_id"], now=late))
        self.assertEqual(approvals.get(GUILD, queued["approval_id"])["status"], approvals.PENDING)

    def test_turning_the_queue_off_posts_straight_away_again(self):
        self.require_approval(False)
        result = self.announce(ADA)
        self.assertNotIn("queued", result)
        self.assertEqual(len(self.posts_in_announce()), 1)

    def test_queue_is_per_server(self):
        self.require_approval()
        queued = self.announce(ADA)
        other = 100000000000000002
        self.assertEqual(ops.list_approvals(other), [])
        with self.assertRaises(OperationError):
            run(ops.approve_request(self.gw, self.cfg, other, BEN, queued["approval_id"], now=NOW))


# --------------------------------------------------------------------------- #
# Auto-unpin
# --------------------------------------------------------------------------- #
class PinTests(FeatureTestCase):
    def test_pinned_for_a_while_then_unpinned_by_the_scheduler(self):
        result = self.announce(pin_for="24h")
        message_id = int(result["message_id"])
        self.assertIn((ANNOUNCE, message_id), self.gw.pinned)
        self.assertEqual(result["pinned_until"], ann.iso_utc(NOW + timedelta(hours=24)))

        counts = self.scheduler(NOW + timedelta(hours=23))
        self.assertEqual(counts["unpinned"], 0)
        self.assertIn((ANNOUNCE, message_id), self.gw.pinned)

        counts = self.scheduler(NOW + timedelta(hours=25))
        self.assertEqual(counts["unpinned"], 1)
        self.assertNotIn((ANNOUNCE, message_id), self.gw.pinned)
        self.assertEqual(pins.list_active(GUILD), [])
        self.assertIn("pin.expire", self.audit_actions())

    def test_a_scheduled_one_off_is_pinned_when_it_posts(self):
        run(ops.schedule_announcement(self.gw, self.cfg, GUILD, ADA, when="1h", message="Hi", pin_for="2h", now=NOW))
        self.scheduler(NOW + timedelta(hours=1, minutes=1))
        self.assertEqual(len(self.gw.pinned), 1)
        self.scheduler(NOW + timedelta(hours=3, minutes=1))
        self.assertEqual(self.gw.pinned, set())

    def test_a_repeat_pin_must_end_before_the_next_post(self):
        with self.assertRaisesRegex(OperationError, "shorter"):
            run(ops.schedule_announcement(
                self.gw, self.cfg, GUILD, ADA, when="1h", message="Again", repeat_every="1d", pin_for="1d", now=NOW
            ))

    def test_each_repeat_is_pinned_and_unpinned_in_turn(self):
        run(ops.schedule_announcement(
            self.gw, self.cfg, GUILD, ADA, when="1h", message="Again", repeat_every="1d", pin_for="6h", now=NOW
        ))
        self.scheduler(NOW + timedelta(hours=1, minutes=1))
        self.assertEqual(len(self.gw.pinned), 1)
        first = next(iter(self.gw.pinned))

        self.scheduler(NOW + timedelta(hours=8))  # the first copy's pin has run out
        self.assertNotIn(first, self.gw.pinned)
        self.assertEqual(self.gw.pinned, set())

        self.scheduler(NOW + timedelta(days=1, hours=1, minutes=1))  # the next day's post
        self.assertEqual(len(self.gw.pinned), 1)
        self.assertNotIn(first, self.gw.pinned)

    def test_list_pins_shows_only_active_pins(self):
        result = self.announce(pin_for="24h")
        rows = ops.list_pins(GUILD)
        self.assertEqual([int(row["message_id"]) for row in rows], [int(result["message_id"])])
        self.scheduler(NOW + timedelta(hours=25))
        self.assertEqual(ops.list_pins(GUILD), [])

    def test_a_pin_that_is_too_short_is_refused(self):
        with self.assertRaisesRegex(OperationError, "at least 5 minutes"):
            self.announce(pin_for="1m")

    def test_a_failed_pin_is_only_a_warning(self):
        self.gw.fail_pins = True
        result = self.announce(pin_for="24h")
        self.assertIsNotNone(result["message_id"])
        self.assertIsNone(result["pinned_until"])
        self.assertTrue(any("could not pin" in w for w in result["warnings"]))
        self.assertEqual(pins.list_active(GUILD), [])

    def test_parse_pin(self):
        self.assertIsNone(ann.parse_pin(""))
        self.assertIsNone(ann.parse_pin(None))
        self.assertEqual(ann.parse_pin("1d"), 86400)
        with self.assertRaises(ValueError):
            ann.parse_pin("31d")
        with self.assertRaises(ValueError):
            ann.parse_pin("soon")


# --------------------------------------------------------------------------- #
# Editing and deleting
# --------------------------------------------------------------------------- #
class EditDeleteTests(FeatureTestCase):
    def test_edit_changes_the_post_and_the_record(self):
        result = self.announce()
        message_id = result["message_id"]
        run(ops.edit_announcement(self.gw, self.cfg, GUILD, BEN, message_id=message_id, message="Doors at 9"))
        embed = self.gw.messages[(ANNOUNCE, int(message_id))]["embed"]
        self.assertEqual(embed.description, "Doors at 9")
        self.assertEqual(embed.title, "Raid night")  # the heading is kept when left out
        row = ann.list_history(GUILD)[0]
        self.assertEqual(row["body"], "Doors at 9")
        self.assertIsNotNone(row["edited_at"])
        self.assertIn("announce.edit", self.audit_actions())

    def test_edit_can_change_only_the_heading(self):
        message_id = self.announce()["message_id"]
        run(ops.edit_announcement(self.gw, self.cfg, GUILD, BEN, message_id=message_id, title="Doors open"))
        embed = self.gw.messages[(ANNOUNCE, int(message_id))]["embed"]
        self.assertEqual(embed.title, "Doors open")
        self.assertEqual(embed.description, "Doors at 8")

    def test_only_recorded_announcements_can_be_edited_or_deleted(self):
        with self.assertRaisesRegex(OperationError, "not an announcement the bot recorded"):
            run(ops.edit_announcement(self.gw, self.cfg, GUILD, BEN, message_id="123", message="x"))
        with self.assertRaisesRegex(OperationError, "not an announcement the bot recorded"):
            run(ops.delete_announcement(self.gw, self.cfg, GUILD, BEN, message_id="123"))

    def test_bad_message_ids_and_empty_edits_are_refused(self):
        message_id = self.announce()["message_id"]
        with self.assertRaisesRegex(OperationError, "not a message ID"):
            run(ops.edit_announcement(self.gw, self.cfg, GUILD, BEN, message_id="abc", message="x"))
        with self.assertRaisesRegex(OperationError, "Give a new message"):
            run(ops.edit_announcement(self.gw, self.cfg, GUILD, BEN, message_id=message_id))
        with self.assertRaises(OperationError):
            run(ops.edit_announcement(self.gw, self.cfg, GUILD, BEN, message_id=message_id, message="   "))

    def test_delete_removes_it_from_discord_and_history(self):
        message_id = self.announce(pin_for="1d")["message_id"]
        self.assertIn((ANNOUNCE, int(message_id)), self.gw.pinned)
        run(ops.delete_announcement(self.gw, self.cfg, GUILD, BEN, message_id=message_id))
        self.assertNotIn((ANNOUNCE, int(message_id)), self.gw.messages)
        self.assertNotIn((ANNOUNCE, int(message_id)), self.gw.pinned)
        self.assertEqual(ann.list_history(GUILD), [])
        self.assertEqual(pins.list_active(GUILD), [])
        self.assertIn("announce.delete", self.audit_actions())

    def test_scheduled_posts_can_be_edited_and_deleted_too(self):
        run(ops.schedule_announcement(self.gw, self.cfg, GUILD, ADA, when="1h", message="Scheduled", now=NOW))
        self.scheduler(NOW + timedelta(hours=1, minutes=1))
        message_id = ann.list_history(GUILD)[0]["message_id"]
        self.assertIsNotNone(message_id)  # scheduled posts record their message now
        run(ops.edit_announcement(self.gw, self.cfg, GUILD, BEN, message_id=message_id, message="Edited"))
        run(ops.delete_announcement(self.gw, self.cfg, GUILD, BEN, message_id=message_id))
        self.assertEqual(ann.list_history(GUILD), [])

    def test_deleting_something_already_gone_still_clears_it(self):
        message_id = self.announce()["message_id"]
        del self.gw.messages[(ANNOUNCE, int(message_id))]  # deleted by hand in Discord
        run(ops.delete_announcement(self.gw, self.cfg, GUILD, BEN, message_id=message_id))
        self.assertEqual(ann.list_history(GUILD), [])


# --------------------------------------------------------------------------- #
# Quiet hours in the scheduler
# --------------------------------------------------------------------------- #
class QuietScheduleTests(FeatureTestCase):
    def test_scheduled_post_due_in_quiet_hours_waits_for_the_window_to_end(self):
        settings.set_guild_value(self.cfg, GUILD, "quiet_start", "13:00")
        settings.set_guild_value(self.cfg, GUILD, "quiet_end", "15:00")
        due = datetime(2026, 10, 16, 17, 30, tzinfo=timezone.utc)  # 13:30 Toronto
        ann.add_scheduled(
            guild_id=GUILD, channel_id=ANNOUNCE, author_id=ADA.id, author_name="Ada", title="Quiet",
            body="Later please", mention_role_id=None, send_at=due, now=NOW,
        )
        counts = self.scheduler(due)
        self.assertEqual(counts["deferred"], 1)
        self.assertEqual(counts["posted"], 0)
        self.assertEqual(self.posts_in_announce(), [])
        pending = ann.list_pending(GUILD)
        self.assertEqual(pending[0]["send_at"], ann.iso_utc(datetime(2026, 10, 16, 19, 0, tzinfo=timezone.utc)))

        counts = self.scheduler(datetime(2026, 10, 16, 19, 0, tzinfo=timezone.utc))
        self.assertEqual(counts["posted"], 1)
        self.assertEqual(len(self.posts_in_announce()), 1)

    def test_quiet_hours_do_not_hold_back_a_post_made_by_hand(self):
        settings.set_guild_value(self.cfg, GUILD, "quiet_start", "13:00")
        settings.set_guild_value(self.cfg, GUILD, "quiet_end", "15:00")
        self.announce(now=datetime(2026, 10, 16, 17, 30, tzinfo=timezone.utc))
        self.assertEqual(len(self.posts_in_announce()), 1)

    def test_no_quiet_hours_means_no_deferral(self):
        due = datetime(2026, 10, 16, 17, 30, tzinfo=timezone.utc)
        ann.add_scheduled(
            guild_id=GUILD, channel_id=ANNOUNCE, author_id=ADA.id, author_name="Ada", title="Normal",
            body="Go", mention_role_id=None, send_at=due, now=NOW,
        )
        self.assertEqual(self.scheduler(due)["posted"], 1)


# --------------------------------------------------------------------------- #
# Settings rules
# --------------------------------------------------------------------------- #
class SettingsRuleTests(FeatureTestCase):
    def test_turning_approval_off_stores_false_not_nothing(self):
        self.require_approval(True)
        run(ops.update_settings(self.gw, self.cfg, GUILD, ADA, {"require_approval": False}))
        self.assertIs(settings.get_guild_config(self.cfg, GUILD)["require_approval"], False)
        run(ops.update_settings(self.gw, self.cfg, GUILD, ADA, {"require_approval": "true"}))
        self.assertTrue(settings.get_require_approval(self.cfg, GUILD))

    def test_quiet_hours_must_be_set_as_a_pair(self):
        with self.assertRaisesRegex(OperationError, "both"):
            run(ops.update_settings(self.gw, self.cfg, GUILD, ADA, {"quiet_start": "22:00"}))
        run(ops.update_settings(self.gw, self.cfg, GUILD, ADA, {"quiet_start": "22:00", "quiet_end": "08:00"}))
        self.assertEqual(settings.get_quiet_hours(self.cfg, GUILD), ("22:00", "08:00"))
        run(ops.update_settings(self.gw, self.cfg, GUILD, ADA, {"quiet_start": None, "quiet_end": None}))
        self.assertEqual(settings.get_quiet_hours(self.cfg, GUILD), (None, None))

    def test_quiet_times_are_checked(self):
        with self.assertRaisesRegex(OperationError, "24-hour"):
            run(ops.update_settings(self.gw, self.cfg, GUILD, ADA, {"quiet_start": "25:00", "quiet_end": "08:00"}))

    def test_overview_counts_new_things(self):
        self.require_approval()
        self.announce(ADA, pin_for="1d")  # queued, so nothing is pinned yet
        overview = ops.overview(self.cfg, GUILD, now=NOW)
        self.assertEqual(overview["approvals_pending"], 1)
        self.assertEqual(overview["pins_active"], 0)


# --------------------------------------------------------------------------- #
# Audit export
# --------------------------------------------------------------------------- #
class QueueSummaryTests(FeatureTestCase):
    """What the approval queue shows for each held request."""

    def test_untitled_request_uses_the_default_heading(self):
        settings.set_guild_value(self.cfg, GUILD, "require_approval", True)
        self.announce(actor=ADA, title=None, message="Doors open at eight, bring a friend")
        rows = ops.list_approvals(GUILD)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("None", rows[0]["summary"])
        self.assertIn("Announcement", rows[0]["summary"])
        self.assertIn(str(ANNOUNCE), rows[0]["summary"])

    def test_long_heading_is_cut_short_in_the_summary(self):
        settings.set_guild_value(self.cfg, GUILD, "require_approval", True)
        self.announce(actor=ADA, title="word " * 40, message="Body")
        summary = ops.list_approvals(GUILD)[0]["summary"]
        self.assertLessEqual(len(summary), 120)
        self.assertTrue(summary.endswith("…"))


class QuietHoldManualTests(FeatureTestCase):
    """Quiet hours that also hold posts made by hand."""

    IN_WINDOW = datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc)  # 23:00 on 16 October in Toronto
    WINDOW_END = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)  # 08:00 on 17 October

    def setUp(self) -> None:
        super().setUp()
        settings.set_guild_value(self.cfg, GUILD, "quiet_start", "22:00")
        settings.set_guild_value(self.cfg, GUILD, "quiet_end", "08:00")

    def hold_by_hand(self, on: bool = True) -> None:
        settings.set_guild_value(self.cfg, GUILD, "quiet_hold_manual", on)

    def test_off_by_default_a_hand_post_goes_out_in_quiet_hours(self):
        self.announce(now=self.IN_WINDOW)
        self.assertEqual(len(self.posts_in_announce()), 1)

    def test_when_on_a_hand_post_in_quiet_hours_is_held_until_they_end(self):
        self.hold_by_hand()
        result = self.announce(now=self.IN_WINDOW)
        self.assertTrue(result["held"])
        self.assertEqual(self.posts_in_announce(), [])
        rows = ann.list_pending(GUILD)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["send_at"], ann.iso_utc(self.WINDOW_END))
        self.assertIn("quiet.hold", self.audit_actions())

        self.scheduler(self.WINDOW_END - timedelta(minutes=1))
        self.assertEqual(self.posts_in_announce(), [])
        self.scheduler(self.WINDOW_END + timedelta(minutes=1))
        self.assertEqual(len(self.posts_in_announce()), 1)

    def test_when_on_a_hand_post_outside_quiet_hours_goes_out_at_once(self):
        self.hold_by_hand()
        result = self.announce(now=NOW)
        self.assertFalse(result.get("held"))
        self.assertEqual(len(self.posts_in_announce()), 1)

    def test_a_held_post_keeps_its_pin_and_author(self):
        self.hold_by_hand()
        self.announce(now=self.IN_WINDOW, actor=ADA, pin_for="24h")
        row = ann.list_pending(GUILD)[0]
        self.assertEqual(row["author_name"], "Ada")
        self.assertEqual(row["pin_seconds"], 86400)
        self.scheduler(self.WINDOW_END + timedelta(minutes=1))
        self.assertEqual(len(self.gw.pinned), 1)

    def test_an_approved_hand_post_is_held_too(self):
        self.hold_by_hand()
        self.require_approval()
        self.announce(now=self.IN_WINDOW, actor=ADA)
        self.assertEqual(self.posts_in_announce(), [])
        result = run(ops.approve_request(self.gw, self.cfg, GUILD, BEN, 1, now=self.IN_WINDOW))
        self.assertTrue(result["held"])
        self.assertEqual(self.posts_in_announce(), [])
        self.assertEqual(len(ann.list_pending(GUILD)), 1)

    def test_the_setting_is_saved_through_update_settings(self):
        run(ops.update_settings(self.gw, self.cfg, GUILD, ADA, {"quiet_hold_manual": True}))
        self.assertTrue(settings.get_quiet_hold_manual(self.cfg, GUILD))
        run(ops.update_settings(self.gw, self.cfg, GUILD, ADA, {"quiet_hold_manual": "no"}))
        self.assertFalse(settings.get_quiet_hold_manual(self.cfg, GUILD))


class ApprovalReminderTests(FeatureTestCase):
    """A request left waiting for a day is mentioned in the staff log, once a day."""

    def queue_one(self) -> None:
        self.require_approval()
        self.announce(actor=ADA, title="Waiting")

    def reminders(self) -> list[dict]:
        return [
            p for p in self.gw.posts
            if p["channel_id"] == STAFF_LOG and p["embed"] is not None and "still waiting" in p["embed"].title
        ]

    def test_no_reminder_before_a_day(self):
        self.queue_one()
        counts = self.scheduler(NOW + timedelta(hours=23))
        self.assertEqual(counts["approval_reminders"], 0)
        self.assertEqual(self.reminders(), [])

    def test_one_reminder_after_a_day_then_not_again_that_day(self):
        self.queue_one()
        counts = self.scheduler(NOW + timedelta(hours=25))
        self.assertEqual(counts["approval_reminders"], 1)
        self.assertEqual(len(self.reminders()), 1)
        again = self.scheduler(NOW + timedelta(hours=26))
        self.assertEqual(again["approval_reminders"], 0)
        self.assertEqual(len(self.reminders()), 1)

    def test_reminded_again_a_day_later_while_still_pending(self):
        self.queue_one()
        self.scheduler(NOW + timedelta(hours=25))
        counts = self.scheduler(NOW + timedelta(hours=50))
        self.assertEqual(counts["approval_reminders"], 1)
        self.assertEqual(len(self.reminders()), 2)

    def test_a_decided_request_is_not_reminded(self):
        self.queue_one()
        run(ops.reject_request(GUILD, BEN, 1, now=NOW + timedelta(hours=1)))
        counts = self.scheduler(NOW + timedelta(hours=25))
        self.assertEqual(counts["approval_reminders"], 0)

    def test_without_a_staff_log_the_reminder_waits_until_there_is_one(self):
        settings.set_guild_value(self.cfg, GUILD, "staff_channel_id", None)
        self.queue_one()
        self.assertEqual(self.scheduler(NOW + timedelta(hours=25))["approval_reminders"], 0)
        settings.set_guild_value(self.cfg, GUILD, "staff_channel_id", STAFF_LOG)
        self.assertEqual(self.scheduler(NOW + timedelta(hours=26))["approval_reminders"], 1)


class AuditExportTests(FeatureTestCase):
    def test_csv_has_a_header_and_one_row_per_entry(self):
        self.announce()
        text = audit.to_csv(GUILD)
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "created_at,actor_name,source,action,detail,actor_id")
        self.assertTrue(any("announce.post" in line for line in lines[1:]))

    def test_cells_that_could_run_as_formulas_are_neutralised(self):
        audit.record(GUILD, Actor(1, "=HYPERLINK(\"x\")", "discord"), "settings.update", "+SUM(1,1)")
        audit.record(GUILD, ADA, "announce.post", "@cmd")
        text = audit.to_csv(GUILD)
        self.assertIn("'=HYPERLINK", text)
        self.assertIn("'+SUM", text)
        self.assertIn("'@cmd", text)

    def test_csv_quotes_commas_and_newlines(self):
        audit.record(GUILD, ADA, "announce.post", "a, b\nc")
        self.assertIn('"a, b\nc"', audit.to_csv(GUILD))

    def test_csv_only_covers_one_server(self):
        audit.record(GUILD, ADA, "announce.post", "mine")
        audit.record(100000000000000002, ADA, "announce.post", "theirs")
        text = audit.to_csv(GUILD)
        self.assertIn("mine", text)
        self.assertNotIn("theirs", text)


if __name__ == "__main__":
    unittest.main()
