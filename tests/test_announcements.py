"""
Tests for the pure rules in announcements.py: parsing durations and times,
cleaning text, and the mention rules. Operations that touch the database
and Discord are in test_operations.py.

Run with either:
    python -m pytest tests/test_announcements.py
    python tests/test_announcements.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import announcements as ann  # noqa: E402
import store  # noqa: E402

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


class DurationTests(unittest.TestCase):
    def test_units_and_combinations(self):
        self.assertEqual(ann.parse_duration("30m"), 30 * 60)
        self.assertEqual(ann.parse_duration("2h"), 2 * 3600)
        self.assertEqual(ann.parse_duration("1d12h"), 36 * 3600)
        self.assertEqual(ann.parse_duration("1w"), 7 * 86400)

    def test_bare_number_only_means_minutes_on_its_own(self):
        self.assertEqual(ann.parse_duration("45"), 45 * 60)
        self.assertIsNone(ann.parse_duration("1h30"))

    def test_rejects_garbage_and_zero(self):
        for text in ["", "soon", "0m", "5 fortnights", "2026-10-20"]:
            self.assertIsNone(ann.parse_duration(text), text)


class SendTimeTests(unittest.TestCase):
    def test_delay_is_relative_to_now(self):
        self.assertEqual(ann.parse_send_time("2h", NOW, "America/Toronto"), NOW + timedelta(hours=2))

    def test_absolute_time_uses_the_guild_timezone(self):
        # 09:00 in Toronto on 16 Oct 2026 is 13:00 UTC (daylight time).
        target = ann.parse_send_time("2026-10-16 09:00", NOW, "America/Toronto")
        self.assertEqual(target, datetime(2026, 10, 16, 13, 0, tzinfo=timezone.utc))

    def test_too_soon_and_too_far_are_refused(self):
        with self.assertRaises(ValueError):
            ann.parse_send_time("2026-10-16 12:00", NOW, "UTC")
        with self.assertRaises(ValueError):
            ann.parse_send_time("400d", NOW, "UTC")

    def test_nonexistent_date_is_refused(self):
        with self.assertRaises(ValueError):
            ann.parse_send_time("2026-02-30 10:00", NOW, "UTC")

    def test_unreadable_text_is_refused(self):
        with self.assertRaises(ValueError):
            ann.parse_send_time("next tuesday", NOW, "UTC")


class IntervalTests(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(ann.parse_interval("15m"), 900)
        self.assertEqual(ann.parse_interval("1d"), 86400)
        with self.assertRaises(ValueError):
            ann.parse_interval("14m")
        with self.assertRaises(ValueError):
            ann.parse_interval("366d")


class TextCleaningTests(unittest.TestCase):
    def test_title_defaults_when_blank(self):
        self.assertEqual(ann.clean_title("   "), ann.DEFAULT_TITLE)
        self.assertEqual(ann.clean_title(" Raid "), "Raid")

    def test_body_must_be_present_and_bounded(self):
        with self.assertRaises(ValueError):
            ann.clean_body("   ")
        with self.assertRaises(ValueError):
            ann.clean_body("x" * (ann.MAX_BODY_LENGTH + 1))

    def test_template_names_are_normalised_and_checked(self):
        self.assertEqual(ann.clean_template_name("  Raid Night "), "raid night")
        for bad in ["", "bad!", "x" * 41, "-leading"]:
            with self.assertRaises(ValueError, msg=bad):
                ann.clean_template_name(bad)

    def test_timezone_must_be_iana(self):
        self.assertEqual(ann.validate_timezone("Europe/London"), "Europe/London")
        with self.assertRaises(ValueError):
            ann.validate_timezone("Mars/Base")


class MentionTests(unittest.TestCase):
    def test_content_is_only_the_chosen_role(self):
        self.assertEqual(ann.mention_content(123), "<@&123>")
        self.assertIsNone(ann.mention_content(None))

    def test_allowed_mentions_never_include_everyone(self):
        none = ann.allowed_mentions_for(None)
        self.assertFalse(none.everyone)
        self.assertIs(none.roles, False)
        self.assertIs(none.users, False)

        role_only = ann.allowed_mentions_for(123)
        self.assertFalse(role_only.everyone)
        self.assertEqual([r.id for r in role_only.roles], [123])
        self.assertIs(role_only.users, False)

        welcome = ann.allowed_mentions_for(None, (7,))
        self.assertFalse(welcome.everyone)
        self.assertEqual([u.id for u in welcome.users], [7])
        self.assertIs(welcome.roles, False)


class TemplateStoreTests(unittest.TestCase):
    """Templates are unique per server and ignore case."""

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

    def test_save_update_get_delete(self):
        self.assertFalse(ann.save_template(1, "raid", "Raid", "Tonight", 42))
        self.assertTrue(ann.save_template(1, "raid", "Raid v2", "Tomorrow", 42))
        self.assertEqual(ann.get_template(1, "RAID")["body"], "Tomorrow")
        self.assertIsNone(ann.get_template(2, "raid"))  # other servers see nothing
        self.assertTrue(ann.delete_template(1, "raid"))
        self.assertFalse(ann.delete_template(1, "raid"))


if __name__ == "__main__":
    unittest.main()
