"""Server-local support-hour schedules and after-hours ticket notices."""

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
import quiet  # noqa: E402
import settings  # noqa: E402
import store  # noqa: E402
from audit import Actor  # noqa: E402
from demo import DemoGateway  # noqa: E402
from operations import OperationError  # noqa: E402

GUILD = 100000000000000001
MEMBER = Actor(1001, "Ivy", "discord")


def run(coro):
    return asyncio.run(coro)


class SupportHoursTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self.temp.name
        store.init_db()
        self.gateway = DemoGateway()
        self.config = copy.deepcopy(settings.DEFAULT_CONFIG)

    def tearDown(self) -> None:
        if self.old is None:
            os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
        else:
            os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self.old
        self.temp.cleanup()

    def test_normal_and_overnight_hours_use_the_server_timezone(self):
        morning = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)  # 09:00 in Toronto
        evening = datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc)  # 17:00 in Toronto
        self.assertTrue(quiet.support_is_open(morning, "America/Toronto", "09:00", "17:00"))
        self.assertFalse(quiet.support_is_open(evening, "America/Toronto", "09:00", "17:00"))
        overnight = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)  # 23:00 in Toronto
        self.assertTrue(quiet.support_is_open(overnight, "America/Toronto", "22:00", "06:00"))
        self.assertTrue(quiet.support_is_open(morning, "America/Toronto", None, None))

    def test_settings_require_a_valid_pair_and_store_the_after_hours_copy(self):
        actor = Actor(42, "Ada", "discord")
        with self.assertRaisesRegex(OperationError, "both support-hours"):
            run(ops.update_settings(
                self.gateway, self.config, GUILD, actor, {"support_hours_start": "09:00"}
            ))
        run(ops.update_settings(
            self.gateway, self.config, GUILD, actor,
            {
                "support_hours_start": "9:00",
                "support_hours_end": "17:00",
                "after_hours_message": "We're offline now; we'll reply soon.",
            },
        ))
        config = settings.get_guild_config(self.config, GUILD)
        self.assertEqual(config["support_hours_start"], "09:00")
        self.assertEqual(config["support_hours_end"], "17:00")
        self.assertEqual(config["after_hours_message"], "We're offline now; we'll reply soon.")

    def test_new_ticket_greeting_includes_custom_message_only_outside_hours(self):
        settings.set_guild_value(self.config, GUILD, "timezone", "America/Toronto")
        settings.set_guild_value(self.config, GUILD, "support_hours_start", "09:00")
        settings.set_guild_value(self.config, GUILD, "support_hours_end", "17:00")
        settings.set_guild_value(self.config, GUILD, "after_hours_message", "We reopen at nine.")
        after_hours = run(ops.open_ticket(
            self.gateway, self.config, GUILD, MEMBER,
            now=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc),  # 08:00 Toronto
        ))
        greeting = next(
            post["embed"] for post in reversed(self.gateway.posts)
            if post["channel_id"] == int(after_hours["channel_id"])
        )
        self.assertIn("We reopen at nine.", greeting.description)

        # Close the first ticket, then open another during business hours.
        run(ops.close_ticket(
            self.gateway, self.config, GUILD, Actor(42, "Ada", "discord"), after_hours["id"],
            actor_is_staff=True,
        ))
        during_hours = run(ops.open_ticket(
            self.gateway, self.config, GUILD, MEMBER,
            now=datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc),  # 10:00 Toronto
        ))
        daytime_greeting = next(
            post["embed"] for post in reversed(self.gateway.posts)
            if post["channel_id"] == int(during_hours["channel_id"])
        )
        self.assertNotIn("We reopen at nine.", daytime_greeting.description)


if __name__ == "__main__":
    unittest.main()
