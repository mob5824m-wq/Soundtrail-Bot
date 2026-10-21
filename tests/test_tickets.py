"""Tickets: opening, the one-per-member rule, who may close, and what gets logged.

Run with either:
    python -m pytest tests/test_tickets.py
    python tests/test_tickets.py
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import bot as bot_module  # noqa: E402
import components  # noqa: E402
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
SUPPORT_CATEGORY = 100000000000000901
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
STAFF = Actor(42, "Ada", "discord")
MEMBER = Actor(1001, "Alice", "discord")
OTHER = Actor(1002, "Bob", "discord")


def run(coro):
    return asyncio.run(coro)


class TicketTestCase(unittest.TestCase):
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

    def open(self, actor=MEMBER, *, category="general"):
        return run(ops.open_ticket(self.gw, self.cfg, GUILD, actor, category=category, now=NOW))

    def close(self, ticket_id, actor, *, staff=False, reason=None):
        return run(
            ops.close_ticket(
                self.gw, self.cfg, GUILD, actor, ticket_id, actor_is_staff=staff, reason=reason, now=NOW
            )
        )

    def staff_log_posts(self):
        return [p for p in self.gw.posts if p["channel_id"] == STAFF_LOG]


class OpenTicketTests(TicketTestCase):
    def test_opening_creates_a_private_channel_and_a_greeting(self):
        result = self.open()
        channel = self.gw.created_channels[-1]
        self.assertEqual(int(result["channel_id"]), channel["id"])
        self.assertEqual(channel["name"], "ticket-alice")
        self.assertEqual(channel["opener_id"], MEMBER.id)
        self.assertEqual(channel["staff_role_id"], STAFF_ROLE)
        self.assertIsNone(channel["category_id"])
        greeting = self.gw.messages[(channel["id"], next(
            mid for (cid, mid) in self.gw.messages if cid == channel["id"]
        ))]
        self.assertEqual(
            [b["custom_id"] for b in greeting["view"]],
            [b["custom_id"] for b in tickets.ticket_buttons(GUILD, result["id"])],
        )
        self.assertIn(f"<@{MEMBER.id}>", greeting["content"])
        self.assertIn(f"<@&{STAFF_ROLE}>", greeting["content"])
        row = tickets.get(GUILD, result["id"])
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["opener_id"], MEMBER.id)

    def test_category_is_saved_and_shown_in_the_greeting(self):
        ticket = self.open(category="technical")
        row = tickets.get(GUILD, ticket["id"])
        self.assertEqual(row["category"], "technical")
        greeting = next(
            message for (channel_id, _), message in self.gw.messages.items()
            if channel_id == int(ticket["channel_id"])
        )
        self.assertIn("Technical issue", greeting["embed"].description)

    def test_invalid_category_is_rejected(self):
        with self.assertRaisesRegex(OperationError, "valid support category"):
            self.open(category="billing")

    def test_the_channel_goes_in_the_ticket_category_when_one_is_set(self):
        settings.set_guild_value(self.cfg, GUILD, "ticket_category_id", SUPPORT_CATEGORY)
        self.open()
        self.assertEqual(self.gw.created_channels[-1]["category_id"], SUPPORT_CATEGORY)

    def test_a_category_that_has_gone_is_refused_clearly(self):
        settings.set_guild_value(self.cfg, GUILD, "ticket_category_id", 999)
        with self.assertRaises(OperationError) as caught:
            self.open()
        self.assertIn("ticket category", str(caught.exception))
        self.assertEqual(ops.list_tickets(GUILD), [])

    def test_one_open_ticket_per_member(self):
        first = self.open()
        with self.assertRaises(OperationError) as caught:
            self.open()
        self.assertIn(f"#{first['id']}", str(caught.exception))
        self.assertEqual(len(self.gw.created_channels), 1)
        # Someone else can still open one.
        self.open(OTHER)
        self.assertEqual(len(ops.list_tickets(GUILD, open_only=True)), 2)

    def test_missing_manage_channels_leaves_no_ticket_behind(self):
        self.gw.fail_ticket_creation = True
        with self.assertRaises(OperationError):
            self.open()
        self.assertEqual(ops.list_tickets(GUILD), [])

    def test_opening_is_logged_to_the_staff_channel(self):
        self.open()
        self.assertEqual(len(self.staff_log_posts()), 1)
        self.assertIn("Ticket #1 opened", self.staff_log_posts()[0]["embed"].title)

    def test_a_failed_greeting_does_not_undo_the_ticket(self):
        original = self.gw.post

        async def failing_post(guild_id, channel_id, **kwargs):
            if kwargs.get("view"):  # the greeting carries the close button
                raise OperationError("Discord refused the message")
            return await original(guild_id, channel_id, **kwargs)

        self.gw.post = failing_post
        result = self.open()
        self.assertEqual(tickets.get(GUILD, result["id"])["status"], "open")


class TicketAssignmentTests(TicketTestCase):
    def test_staff_can_claim_an_unassigned_ticket_once(self):
        ticket = self.open(category="account")
        claimed = run(ops.claim_ticket(self.gw, self.cfg, GUILD, STAFF, ticket["id"]))
        self.assertEqual(claimed["claimed_by_id"], STAFF.id)
        self.assertEqual(claimed["claimed_by_name"], STAFF.name)
        notice = next(
            post for post in reversed(self.gw.posts) if post["channel_id"] == int(ticket["channel_id"])
        )
        self.assertEqual(notice["content"], f"<@{STAFF.id}> is now handling this ticket.")
        # Claiming it again by the same person is harmless.
        same = run(ops.claim_ticket(self.gw, self.cfg, GUILD, STAFF, ticket["id"]))
        self.assertEqual(same["claimed_by_id"], STAFF.id)

    def test_claim_refuses_a_ticket_already_owned_by_another_staff_member(self):
        ticket = self.open()
        run(ops.claim_ticket(self.gw, self.cfg, GUILD, STAFF, ticket["id"]))
        with self.assertRaisesRegex(OperationError, "already assigned"):
            run(ops.claim_ticket(self.gw, self.cfg, GUILD, OTHER, ticket["id"]))

    def test_staff_can_assign_or_reassign_a_ticket(self):
        ticket = self.open()
        assigned = run(ops.assign_ticket(self.gw, self.cfg, GUILD, STAFF, ticket["id"], OTHER))
        self.assertEqual(assigned["claimed_by_id"], OTHER.id)
        self.assertEqual(assigned["claimed_by_name"], OTHER.name)
        reassigned = run(ops.assign_ticket(self.gw, self.cfg, GUILD, STAFF, ticket["id"], STAFF))
        self.assertEqual(reassigned["claimed_by_id"], STAFF.id)

    def test_closed_tickets_cannot_be_claimed_or_assigned(self):
        ticket = self.open()
        self.close(ticket["id"], STAFF, staff=True)
        with self.assertRaisesRegex(OperationError, "There is no open ticket"):
            run(ops.claim_ticket(self.gw, self.cfg, GUILD, STAFF, ticket["id"]))
        with self.assertRaisesRegex(OperationError, "There is no open ticket"):
            run(ops.assign_ticket(self.gw, self.cfg, GUILD, STAFF, ticket["id"], OTHER))


class ReopenAndFeedbackTests(TicketTestCase):
    def test_close_reason_is_recorded_and_feedback_prompt_is_sent_by_dm(self):
        ticket = self.open()
        result = self.close(ticket["id"], STAFF, staff=True, reason="Fixed after resetting the app.")
        row = tickets.get(GUILD, ticket["id"])
        self.assertEqual(row["close_reason"], "Fixed after resetting the app.")
        self.assertEqual(result["close_reason"], row["close_reason"])
        self.assertEqual(self.gw.dm_posts[-1]["user_id"], MEMBER.id)
        self.assertIn("Was your issue resolved?", self.gw.dm_posts[-1]["content"])
        self.assertEqual(len(self.gw.dm_posts[-1]["view"]), 2)

    def test_feedback_is_only_accepted_once_from_the_opener(self):
        ticket = self.open()
        self.close(ticket["id"], STAFF, staff=True)
        result = ops.submit_ticket_feedback(GUILD, ticket["id"], MEMBER, True)
        self.assertTrue(result["resolved"])
        self.assertEqual(tickets.get(GUILD, ticket["id"])["resolution_feedback"], 1)
        with self.assertRaisesRegex(OperationError, "already sent"):
            ops.submit_ticket_feedback(GUILD, ticket["id"], MEMBER, False)
        with self.assertRaisesRegex(OperationError, "Only the member"):
            ops.submit_ticket_feedback(GUILD, ticket["id"], OTHER, True)

    def test_not_resolved_feedback_reopens_the_ticket_automatically(self):
        ticket = self.open()
        self.close(ticket["id"], STAFF, staff=True)
        bot_module.bot.gateway = self.gw
        bot_module.bot.config = self.cfg

        class Response:
            done = False

            def is_done(self):
                return self.done

            async def defer(self, **_kwargs):
                self.done = True

        class Followup:
            last_text = ""

            async def send(self, content, **_kwargs):
                self.last_text = content

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=MEMBER.id, display_name=MEMBER.name),
            client=bot_module.bot,
            response=Response(),
            followup=Followup(),
        )
        run(components.handle_ticket_feedback(interaction, GUILD, ticket["id"], False))
        row = tickets.get(GUILD, ticket["id"])
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["resolution_feedback"], 0)
        self.assertIn("open again", interaction.followup.last_text)

    def test_member_can_reopen_the_same_ticket_and_keep_each_transcript(self):
        ticket = self.open(category="technical")
        self.gw.say(int(ticket["channel_id"]), "Alice", "It still crashes")
        self.close(ticket["id"], STAFF, staff=True, reason="Restarted the service")
        first_channel = int(ticket["channel_id"])
        reopened = run(
            ops.reopen_ticket(
                self.gw, self.cfg, GUILD, MEMBER, ticket["id"], actor_is_staff=False,
                note="The crash came back", now=NOW,
            )
        )
        row = tickets.get(GUILD, ticket["id"])
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["category"], "technical")
        self.assertEqual(row["reopened_count"], 1)
        self.assertEqual(row["reopen_note"], "The crash came back")
        self.assertNotEqual(int(reopened["channel_id"]), first_channel)
        self.assertEqual(len(tickets.transcript_versions(GUILD, ticket["id"])), 1)
        self.gw.say(int(reopened["channel_id"]), "Alice", "It crashed again")
        self.close(ticket["id"], STAFF, staff=True, reason="Patched the crash")
        versions = tickets.transcript_versions(GUILD, ticket["id"])
        self.assertEqual(len(versions), 2)
        self.assertIn("It still crashes", tickets.get_transcript_version(GUILD, ticket["id"], versions[1]["id"])["text"])

    def test_other_members_cannot_reopen_a_closed_ticket(self):
        ticket = self.open()
        self.close(ticket["id"], STAFF, staff=True)
        with self.assertRaisesRegex(OperationError, "Only the person who opened"):
            run(ops.reopen_ticket(self.gw, self.cfg, GUILD, OTHER, ticket["id"], actor_is_staff=False))


class CloseTicketTests(TicketTestCase):
    def test_the_opener_can_close_their_own_ticket(self):
        ticket = self.open()
        channel_id = int(ticket["channel_id"])
        result = self.close(ticket["id"], MEMBER)
        self.assertEqual(result["id"], ticket["id"])
        self.assertIn(channel_id, self.gw.deleted_channels)
        row = tickets.get(GUILD, ticket["id"])
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["closed_by"], "Alice")

    def test_another_member_can_not_close_it(self):
        ticket = self.open()
        with self.assertRaises(OperationError):
            self.close(ticket["id"], OTHER)
        self.assertEqual(tickets.get(GUILD, ticket["id"])["status"], "open")
        self.assertEqual(self.gw.deleted_channels, [])

    def test_staff_can_close_any_ticket(self):
        ticket = self.open()
        self.close(ticket["id"], STAFF, staff=True)
        self.assertEqual(tickets.get(GUILD, ticket["id"])["closed_by"], "Ada")

    def test_a_closed_ticket_can_not_be_closed_again(self):
        ticket = self.open()
        self.close(ticket["id"], MEMBER)
        with self.assertRaises(OperationError):
            self.close(ticket["id"], MEMBER)

    def test_after_closing_the_member_can_open_another(self):
        ticket = self.open()
        self.close(ticket["id"], MEMBER)
        again = self.open()
        self.assertNotEqual(again["id"], ticket["id"])

    def test_a_channel_that_is_already_gone_counts_as_closed(self):
        ticket = self.open()
        del self.gw._channel_index[int(ticket["channel_id"])]  # someone deleted it by hand
        self.close(ticket["id"], STAFF, staff=True)
        self.assertEqual(tickets.get(GUILD, ticket["id"])["status"], "closed")

    def test_closing_is_logged_and_audited(self):
        ticket = self.open()
        self.close(ticket["id"], STAFF, staff=True)
        self.assertIn("Ticket #1 closed", self.staff_log_posts()[-1]["embed"].title)

    def test_a_missing_ticket_is_refused(self):
        with self.assertRaises(OperationError):
            self.close(999, STAFF, staff=True)


class LookupTests(TicketTestCase):
    def test_find_ticket_by_channel_only_returns_open_tickets(self):
        ticket = self.open()
        channel_id = int(ticket["channel_id"])
        self.assertEqual(ops.find_ticket_by_channel(GUILD, channel_id)["id"], ticket["id"])
        self.close(ticket["id"], STAFF, staff=True)
        self.assertIsNone(ops.find_ticket_by_channel(GUILD, channel_id))
        self.assertIsNone(ops.find_ticket_by_channel(GUILD, ANNOUNCE))

    def test_list_can_show_only_open_tickets(self):
        first = self.open()
        self.open(OTHER)
        self.close(first["id"], STAFF, staff=True)
        self.assertEqual(len(ops.list_tickets(GUILD)), 2)
        self.assertEqual(len(ops.list_tickets(GUILD, open_only=True)), 1)

    def test_open_ticket_count_is_on_the_overview(self):
        self.open()
        self.assertEqual(ops.overview(self.cfg, GUILD, now=NOW)["tickets_open"], 1)


class PanelTests(TicketTestCase):
    def test_the_panel_has_an_open_button_and_is_audited(self):
        result = run(ops.post_ticket_panel(self.gw, self.cfg, GUILD, STAFF, channel_id=None))
        self.assertEqual(result["channel_id"], ANNOUNCE)
        view = self.gw.messages[(ANNOUNCE, int(result["message_id"]))]["view"]
        self.assertEqual(
            [b["custom_id"] for b in view],
            [tickets.open_button_id(key) for key in tickets.CATEGORIES],
        )

    def test_category_buttons_and_feedback_buttons_build_persistent_views(self):
        panel = components.build_view(tickets.panel_buttons())
        self.assertEqual(
            [item.item.custom_id for item in panel.children],
            [tickets.open_button_id(key) for key in tickets.CATEGORIES],
        )
        ticket_view = components.build_view(tickets.ticket_buttons(GUILD, 7))
        self.assertEqual(len(ticket_view.children), 2)
        self.assertTrue(any(item.item.custom_id.startswith("st:ticket:urgent:") for item in ticket_view.children))
        feedback = components.build_view(tickets.feedback_buttons(GUILD, 7))
        self.assertEqual(len(feedback.children), 2)
        self.assertTrue(all(item.item.custom_id.startswith("st:ticket:feedback:") for item in feedback.children))

    def test_the_panel_needs_a_channel_it_can_post_in(self):
        with self.assertRaises(OperationError):
            run(ops.post_ticket_panel(self.gw, self.cfg, GUILD, STAFF, channel_id=999))


class PriorityAndReportingTests(TicketTestCase):
    def test_opener_can_request_urgent_and_staff_can_clear_it(self):
        ticket = self.open()
        urgent = run(ops.change_ticket_priority(
            self.gw, self.cfg, GUILD, MEMBER, ticket["id"], "urgent", actor_is_staff=False
        ))
        self.assertEqual(urgent["priority"], "urgent")
        with self.assertRaisesRegex(OperationError, "only staff can clear"):
            run(ops.change_ticket_priority(
                self.gw, self.cfg, GUILD, MEMBER, ticket["id"], "normal", actor_is_staff=False
            ))
        normal = run(ops.change_ticket_priority(
            self.gw, self.cfg, GUILD, STAFF, ticket["id"], "normal", actor_is_staff=True
        ))
        self.assertEqual(normal["priority"], "normal")

    def test_report_calculates_volume_priority_reopens_and_first_reply_time(self):
        ticket = self.open()
        response_at = NOW + timedelta(minutes=20)
        self.assertTrue(ops.note_ticket_activity(int(ticket["channel_id"]), staff_reply=True, now=response_at))
        # Later staff messages must not move the first-response timestamp.
        ops.note_ticket_activity(int(ticket["channel_id"]), staff_reply=True, now=NOW + timedelta(hours=2))
        run(ops.change_ticket_priority(
            self.gw, self.cfg, GUILD, STAFF, ticket["id"], "urgent", actor_is_staff=True
        ))
        report = ops.ticket_report(GUILD, 7, now=NOW + timedelta(days=1))
        self.assertEqual(report["opened"], 1)
        self.assertEqual(report["urgent_open"], 1)
        self.assertEqual(report["first_responses"], 1)
        self.assertEqual(report["average_first_response_seconds"], 1200)
        self.assertEqual(report["waiting_first_response"], 0)

        self.close(ticket["id"], STAFF, staff=True)
        report = ops.ticket_report(GUILD, 7, now=NOW + timedelta(days=1))
        self.assertEqual(report["closed"], 1)
        self.assertEqual(report["urgent_open"], 0)


class TranscriptRetentionTests(TicketTestCase):
    def test_scheduler_prunes_current_and_archived_transcripts_after_retention(self):
        ticket = self.open()
        self.close(ticket["id"], STAFF, staff=True)
        settings.set_guild_value(self.cfg, GUILD, "ticket_transcript_retention_days", 30)
        counts = run(ops.run_scheduler(self.gw, self.cfg, now=NOW + timedelta(days=31)))
        self.assertEqual(counts["transcripts_pruned"], 2)
        self.assertEqual(tickets.transcript_versions(GUILD, ticket["id"]), [])
        self.assertIsNone(tickets.get_transcript(GUILD, ticket["id"]))


class ChannelNameTests(unittest.TestCase):
    def test_names_are_lower_case_ascii_slugs(self):
        self.assertEqual(tickets.channel_name("Ada Lovelace!"), "ticket-ada-lovelace")
        self.assertEqual(tickets.channel_name("  "), "ticket-member")
        self.assertEqual(tickets.channel_name("名前"), "ticket-member")

    def test_long_names_are_cut(self):
        name = tickets.channel_name("x" * 200)
        self.assertLessEqual(len(name), 100)
        self.assertTrue(name.startswith("ticket-"))


if __name__ == "__main__":
    unittest.main()
