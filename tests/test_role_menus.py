"""Role menus: a dropdown that gives and takes roles, and the staff commands that build it.

Run with either:
    python -m pytest tests/test_role_menus.py
    python tests/test_role_menus.py
"""

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

import components  # noqa: E402
import operations as ops  # noqa: E402
import role_menus  # noqa: E402
import settings  # noqa: E402
import store  # noqa: E402
from audit import Actor  # noqa: E402
from demo import DEMO_GUILDS, DemoGateway  # noqa: E402
from operations import OperationError  # noqa: E402

GUILD = 100000000000000001
ANNOUNCE = 100000000000000101
GENERAL = 100000000000000102
STAFF_ROLE = 100000000000000201
MEMBER_ROLE = 100000000000000202
EVENT_ROLE = 100000000000000203
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
STAFF = Actor(42, "Ada", "discord")
MEMBER = Actor(1001, "Alice", "discord")


def run(coro):
    return asyncio.run(coro)


class RoleMenuTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._tmp.name
        store.init_db()
        self.gw = DemoGateway()
        self.cfg = copy.deepcopy(settings.DEFAULT_CONFIG)
        settings.set_guild_value(self.cfg, GUILD, "announcement_channel_id", ANNOUNCE)
        settings.set_guild_value(self.cfg, GUILD, "staff_role_id", STAFF_ROLE)
        settings.set_guild_value(self.cfg, GUILD, "timezone", "America/Toronto")

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
        else:
            os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._old
        self._tmp.cleanup()

    def new_menu(self, title="Pick your roles", description=""):
        return run(ops.create_role_menu(self.gw, self.cfg, GUILD, STAFF, title=title, description=description, now=NOW))

    def add(self, menu_id, role_id, **kwargs):
        return run(ops.add_role_menu_option(self.gw, self.cfg, GUILD, STAFF, menu_id=menu_id, role_id=role_id, **kwargs))

    def post(self, menu_id, channel_id=None):
        return run(ops.post_role_menu(self.gw, self.cfg, GUILD, STAFF, menu_id=menu_id, channel_id=channel_id))

    def apply(self, menu_id, held, picked):
        return run(ops.apply_role_menu(self.gw, GUILD, MEMBER.id, held, menu_id, picked))


class MenuBuildingTests(RoleMenuTestCase):
    def test_a_menu_needs_a_title(self):
        with self.assertRaises(OperationError):
            self.new_menu(title="   ")

    def test_long_titles_and_descriptions_are_refused(self):
        with self.assertRaises(OperationError):
            self.new_menu(title="x" * (role_menus.MAX_TITLE_LENGTH + 1))
        with self.assertRaises(OperationError):
            self.new_menu(description="x" * (role_menus.MAX_DESCRIPTION_LENGTH + 1))

    def test_a_role_is_labelled_with_its_name_by_default(self):
        menu = self.new_menu()
        result = self.add(menu["id"], MEMBER_ROLE)
        self.assertEqual(result["label"], "Member")
        option = role_menus.options_for(menu["id"])[0]
        self.assertEqual(option["label"], "Member")

    def test_a_custom_label_and_note_are_kept(self):
        menu = self.new_menu()
        self.add(menu["id"], EVENT_ROLE, label="Event pings", description="Get pinged for events")
        option = role_menus.options_for(menu["id"])[0]
        self.assertEqual((option["label"], option["description"]), ("Event pings", "Get pinged for events"))

    def test_a_role_the_bot_can_not_give_is_refused(self):
        menu = self.new_menu()
        self.gw.unassignable_roles.add(EVENT_ROLE)
        with self.assertRaises(OperationError):
            self.add(menu["id"], EVENT_ROLE)
        self.assertEqual(role_menus.options_for(menu["id"]), [])

    def test_a_role_is_in_a_menu_only_once(self):
        menu = self.new_menu()
        self.add(menu["id"], MEMBER_ROLE)
        with self.assertRaises(OperationError):
            self.add(menu["id"], MEMBER_ROLE)

    def test_a_menu_holds_at_most_25_roles(self):
        guild = copy.deepcopy(DEMO_GUILDS[0])
        guild["roles"] = [(100000000000000300 + i, f"Role {i}", True) for i in range(26)]
        self.gw = DemoGateway(guilds=[guild])
        menu = self.new_menu()
        for i in range(role_menus.MAX_OPTIONS):
            self.add(menu["id"], 100000000000000300 + i)
        with self.assertRaises(OperationError) as caught:
            self.add(menu["id"], 100000000000000300 + role_menus.MAX_OPTIONS)
        self.assertIn("25", str(caught.exception))

    def test_a_menu_with_no_roles_can_not_be_posted(self):
        menu = self.new_menu()
        with self.assertRaises(OperationError):
            self.post(menu["id"], GENERAL)

    def test_posting_puts_a_dropdown_on_the_message(self):
        menu = self.new_menu(description="Choose freely.")
        self.add(menu["id"], MEMBER_ROLE)
        self.add(menu["id"], EVENT_ROLE, label="Event pings")
        posted = self.post(menu["id"], GENERAL)

        message = self.gw.messages[(GENERAL, int(posted["message_id"]))]
        (select,) = message["view"]
        self.assertEqual(select["kind"], "select")
        self.assertEqual(select["custom_id"], f"cm:rolemenu:{menu['id']}")
        self.assertEqual((select["min"], select["max"]), (0, 2))
        self.assertEqual([o["value"] for o in select["options"]], [str(MEMBER_ROLE), str(EVENT_ROLE)])
        self.assertIn("Event pings", [o["label"] for o in select["options"]])

        row = role_menus.get(GUILD, menu["id"])
        self.assertEqual((row["channel_id"], row["message_id"]), (GENERAL, int(posted["message_id"])))

    def test_deleting_removes_the_message_and_the_menu(self):
        menu = self.new_menu()
        self.add(menu["id"], MEMBER_ROLE)
        posted = self.post(menu["id"], GENERAL)
        run(ops.delete_role_menu(self.gw, self.cfg, GUILD, STAFF, menu_id=menu["id"]))
        self.assertIn((GENERAL, int(posted["message_id"])), self.gw.deleted)
        self.assertIsNone(role_menus.get_by_id(menu["id"]))

    def test_changing_roles_refreshes_the_posted_message(self):
        menu = self.new_menu()
        self.add(menu["id"], MEMBER_ROLE)
        posted = self.post(menu["id"], GENERAL)
        self.add(menu["id"], EVENT_ROLE)
        message = self.gw.messages[(GENERAL, int(posted["message_id"]))]
        self.assertEqual(len(message["view"][0]["options"]), 2)

        run(ops.remove_role_menu_option(self.gw, self.cfg, GUILD, STAFF, menu_id=menu["id"], role_id=MEMBER_ROLE))
        message = self.gw.messages[(GENERAL, int(posted["message_id"]))]
        self.assertEqual([o["value"] for o in message["view"][0]["options"]], [str(EVENT_ROLE)])
        self.assertEqual(len(self.gw.edits), 2)

    def test_removing_the_last_role_clears_the_dropdown(self):
        menu = self.new_menu()
        self.add(menu["id"], MEMBER_ROLE)
        posted = self.post(menu["id"], GENERAL)
        run(ops.remove_role_menu_option(self.gw, self.cfg, GUILD, STAFF, menu_id=menu["id"], role_id=MEMBER_ROLE))
        self.assertIsNone(self.gw.messages[(GENERAL, int(posted["message_id"]))]["view"])

    def test_removing_a_role_that_is_not_there_is_refused(self):
        menu = self.new_menu()
        with self.assertRaises(OperationError):
            run(ops.remove_role_menu_option(self.gw, self.cfg, GUILD, STAFF, menu_id=menu["id"], role_id=MEMBER_ROLE))

    def test_a_missing_menu_is_refused(self):
        with self.assertRaises(OperationError):
            self.add(999, MEMBER_ROLE)
        with self.assertRaises(OperationError):
            self.post(999, GENERAL)

    def test_the_list_shows_roles_and_where_it_is_posted(self):
        menu = self.new_menu(title="Games")
        self.add(menu["id"], MEMBER_ROLE)
        listed = ops.list_role_menus(GUILD)
        self.assertEqual(listed[0]["title"], "Games")
        self.assertEqual(len(listed[0]["options"]), 1)
        self.assertIsNone(listed[0]["message_id"])

    def test_building_and_posting_are_audited(self):
        menu = self.new_menu()
        self.add(menu["id"], MEMBER_ROLE)
        self.post(menu["id"], GENERAL)
        actions = [row["action"] for row in store.db_fetchall("SELECT action FROM audit_log WHERE guild_id = ?", (GUILD,))]
        for action in ("rolemenu.create", "rolemenu.add", "rolemenu.post"):
            self.assertIn(action, actions)


class PickingTests(RoleMenuTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.menu = self.new_menu()
        self.add(self.menu["id"], MEMBER_ROLE)
        self.add(self.menu["id"], EVENT_ROLE)

    def test_a_pick_gives_the_chosen_roles_and_takes_the_other_menu_roles(self):
        added, removed = self.apply(self.menu["id"], held=[EVENT_ROLE, STAFF_ROLE], picked=[MEMBER_ROLE])
        self.assertEqual(added, [MEMBER_ROLE])
        self.assertEqual(removed, [EVENT_ROLE])
        self.assertIn((GUILD, MEMBER.id, MEMBER_ROLE, True), self.gw.role_changes)
        self.assertIn((GUILD, MEMBER.id, EVENT_ROLE, False), self.gw.role_changes)
        # A role that is not in the menu is left alone.
        self.assertFalse(any(change[2] == STAFF_ROLE for change in self.gw.role_changes))

    def test_picking_nothing_removes_all_of_the_menu_roles(self):
        added, removed = self.apply(self.menu["id"], held=[MEMBER_ROLE, EVENT_ROLE], picked=[])
        self.assertEqual(added, [])
        self.assertEqual(sorted(removed), sorted([MEMBER_ROLE, EVENT_ROLE]))

    def test_a_role_already_held_is_not_given_again(self):
        added, removed = self.apply(self.menu["id"], held=[MEMBER_ROLE], picked=[MEMBER_ROLE])
        self.assertEqual((added, removed), ([], []))
        self.assertEqual(self.gw.role_changes, [])

    def test_roles_outside_the_menu_are_ignored(self):
        added, removed = self.apply(self.menu["id"], held=[], picked=[STAFF_ROLE])
        self.assertEqual((added, removed), ([], []))

    def test_a_role_the_bot_can_not_give_stops_the_pick_with_a_reason(self):
        self.gw.unassignable_roles.add(MEMBER_ROLE)
        with self.assertRaises(OperationError):
            self.apply(self.menu["id"], held=[], picked=[MEMBER_ROLE])
        self.assertEqual(self.gw.role_changes, [])

    def test_a_menu_that_was_deleted_says_so(self):
        run(ops.delete_role_menu(self.gw, self.cfg, GUILD, STAFF, menu_id=self.menu["id"]))
        with self.assertRaises(OperationError) as caught:
            self.apply(self.menu["id"], held=[], picked=[MEMBER_ROLE])
        self.assertIn("no longer exists", str(caught.exception))


class ComponentTests(RoleMenuTestCase):
    def test_the_dropdown_comes_back_from_its_custom_id(self):
        self.assertEqual(components.RoleMenuSelect.__discord_ui_compiled_template__.match("cm:rolemenu:12").group("mid"), "12")
        item = components.RoleMenuSelect(12)
        self.assertEqual(item.custom_id, "cm:rolemenu:12")

    def test_a_menu_spec_becomes_a_persistent_dropdown(self):
        menu = self.new_menu()
        self.add(menu["id"], MEMBER_ROLE)
        self.add(menu["id"], EVENT_ROLE, label="Event pings")
        options = role_menus.options_for(menu["id"])
        spec = role_menus.select_spec(menu["id"], options)

        async def build():
            return components.build_view([spec])

        view = run(build())
        (item,) = view.children
        self.assertIsInstance(item, components.RoleMenuSelect)
        self.assertEqual(item.item.max_values, 2)
        self.assertEqual(item.item.min_values, 0)
        self.assertEqual(len(item.item.options), 2)
        self.assertEqual(view.timeout, None)

    def test_the_dropdown_is_registered_with_the_bot(self):
        self.assertIn(components.RoleMenuSelect, components.DYNAMIC_ITEMS)


if __name__ == "__main__":
    unittest.main()
