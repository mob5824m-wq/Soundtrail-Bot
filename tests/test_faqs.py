"""Saved replies for support tickets: CRUD, safe sending and audit logging."""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import operations as ops  # noqa: E402
import settings  # noqa: E402
import store  # noqa: E402
from audit import Actor  # noqa: E402
from demo import DemoGateway  # noqa: E402
from operations import OperationError  # noqa: E402

GUILD = 100000000000000001
CHANNEL = 100000000000000102
MEMBER = Actor(1001, "Ivy", "discord")
STAFF = Actor(42, "Ada", "discord")


def run(coro):
    return asyncio.run(coro)


class FaqTests(unittest.TestCase):
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

    def test_save_normalizes_keys_and_saving_again_updates_the_reply(self):
        first = run(ops.save_ticket_faq(GUILD, STAFF, key="Install Help", title="Install", body="Restart the app."))
        self.assertEqual(first["key"], "install-help")
        updated = run(ops.save_ticket_faq(GUILD, STAFF, key="INSTALL HELP", title="Reinstall", body="Update first."))
        self.assertEqual(updated["title"], "Reinstall")
        self.assertEqual(len(ops.list_ticket_faqs(GUILD)), 1)

    def test_invalid_faq_text_is_rejected(self):
        with self.assertRaisesRegex(OperationError, "FAQ key"):
            run(ops.save_ticket_faq(GUILD, STAFF, key="!!!", title="Title", body="Reply"))
        with self.assertRaisesRegex(OperationError, "FAQ reply"):
            run(ops.save_ticket_faq(GUILD, STAFF, key="setup", title="Title", body=""))

    def test_saved_reply_can_only_be_sent_inside_an_open_ticket(self):
        saved = run(ops.save_ticket_faq(GUILD, STAFF, key="install", title="Install steps", body="Try settings."))
        with self.assertRaisesRegex(OperationError, "only be used inside an open ticket"):
            run(ops.use_ticket_faq(self.gateway, GUILD, STAFF, CHANNEL, key="install"))
        ticket = run(ops.open_ticket(self.gateway, self.config, GUILD, MEMBER))
        sent = run(
            ops.use_ticket_faq(self.gateway, GUILD, STAFF, int(ticket["channel_id"]), key=saved["key"])
        )
        message = next(
            post for post in reversed(self.gateway.posts) if post["channel_id"] == int(ticket["channel_id"])
        )
        self.assertEqual(sent["title"], "Install steps")
        self.assertEqual(message["content"], "Try settings.")

    def test_delete_removes_a_saved_reply(self):
        run(ops.save_ticket_faq(GUILD, STAFF, key="install", title="Install", body="Restart."))
        run(ops.delete_ticket_faq(GUILD, STAFF, key="install"))
        self.assertEqual(ops.list_ticket_faqs(GUILD), [])
        with self.assertRaisesRegex(OperationError, "There is no saved reply"):
            run(ops.delete_ticket_faq(GUILD, STAFF, key="install"))


if __name__ == "__main__":
    unittest.main()
