"""
Tests for per-server configuration and the staff permission rule (settings.py).

Run with either:
    python -m pytest tests/test_settings.py
    python tests/test_settings.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import settings  # noqa: E402

GUILD = 1234


def member(*, roles=(), admin=False, manage_guild=False):
    perms = SimpleNamespace(administrator=admin, manage_guild=manage_guild)
    return SimpleNamespace(
        guild_permissions=perms,
        roles=[SimpleNamespace(id=r) for r in roles],
    )


class GuildConfigTests(unittest.TestCase):
    def test_unconfigured_server_gets_defaults(self):
        cfg = {"guilds": {}}
        guild = settings.get_guild_config(cfg, GUILD)
        self.assertIsNone(guild["staff_role_id"])
        self.assertEqual(guild["timezone"], settings.DEFAULT_TIMEZONE)

    def test_default_timezone_from_config(self):
        cfg = {"default_timezone": "Europe/London", "guilds": {}}
        self.assertEqual(settings.get_timezone_name(cfg, GUILD), "Europe/London")

    def test_set_guild_value_is_per_server(self):
        cfg = {"guilds": {}}
        settings.set_guild_value(cfg, GUILD, "staff_role_id", 55)
        settings.set_guild_value(cfg, 999, "staff_role_id", 66)
        self.assertEqual(settings.get_staff_role_id(cfg, GUILD), 55)
        self.assertEqual(settings.get_staff_role_id(cfg, 999), 66)
        self.assertEqual(cfg["guilds"][str(GUILD)]["staff_role_id"], 55)

    def test_unknown_key_is_refused(self):
        with self.assertRaises(KeyError):
            settings.set_guild_value({"guilds": {}}, GUILD, "bot_token", "x")

    def test_channel_lookups_return_ints(self):
        cfg = {"guilds": {str(GUILD): {"announcement_channel_id": "77", "staff_channel_id": 88}}}
        self.assertEqual(settings.get_announcement_channel_id(cfg, GUILD), 77)
        self.assertEqual(settings.get_staff_channel_id(cfg, GUILD), 88)


class StaffDenialTests(unittest.TestCase):
    def test_administrator_always_passes(self):
        cfg = {"guilds": {str(GUILD): {"staff_role_id": 5}}}
        self.assertIsNone(settings.staff_denial(member(admin=True), cfg, GUILD))

    def test_staff_role_holder_passes(self):
        cfg = {"guilds": {str(GUILD): {"staff_role_id": 5}}}
        self.assertIsNone(settings.staff_denial(member(roles=[5]), cfg, GUILD))

    def test_manage_server_alone_is_not_enough_once_a_staff_role_exists(self):
        cfg = {"guilds": {str(GUILD): {"staff_role_id": 5}}}
        denial = settings.staff_denial(member(manage_guild=True), cfg, GUILD)
        self.assertIsNotNone(denial)
        self.assertIn("<@&5>", denial)

    def test_fallback_to_manage_server_before_setup(self):
        cfg = {"guilds": {}}
        self.assertIsNone(settings.staff_denial(member(manage_guild=True), cfg, GUILD))
        denial = settings.staff_denial(member(), cfg, GUILD)
        self.assertIn("/manage setup", denial)


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
        else:
            os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._old
        self._tmp.cleanup()

    def test_save_then_load_round_trip_and_private_permissions(self):
        cfg = settings.load_config()
        cfg["bot_token"] = "secret"
        settings.set_guild_value(cfg, GUILD, "staff_role_id", 9)
        settings.save_config(cfg)

        path = Path(self._tmp.name) / "config.json"
        self.assertEqual(json.loads(path.read_text())["bot_token"], "secret")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(settings.load_config()["guilds"][str(GUILD)]["staff_role_id"], 9)

    def test_corrupt_live_config_is_ignored_not_fatal(self):
        Path(self._tmp.name, "config.json").write_text("{not json")
        cfg = settings.load_config()
        self.assertEqual(cfg["guilds"], {})

    def test_token_from_environment_wins(self):
        old = os.environ.get("DISCORD_TOKEN")
        os.environ["DISCORD_TOKEN"] = "from-env"
        try:
            self.assertEqual(settings.resolve_token({"bot_token": "from-file"}), "from-env")
        finally:
            if old is None:
                os.environ.pop("DISCORD_TOKEN", None)
            else:
                os.environ["DISCORD_TOKEN"] = old


if __name__ == "__main__":
    unittest.main()
