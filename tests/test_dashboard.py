"""
Tests for the web dashboard (dashboard.py): sign-in, the session cookie, CSRF,
the Host-header allowlist, the login throttle, the JSON API and the page's
security headers. The server runs in-process with aiohttp's test client
against a DemoGateway.

Run with either:
    python -m pytest tests/test_dashboard.py
    python tests/test_dashboard.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import audit  # noqa: E402
import paths  # noqa: E402
import store  # noqa: E402
from dashboard import (  # noqa: E402
    LOGIN_MAX_FAILURES,
    SESSION_COOKIE,
    DashboardServer,
    ensure_dashboard_token,
)
from demo import DEMO_GUILDS, DemoGateway, demo_config  # noqa: E402
from audit import Actor  # noqa: E402
import operations as ops  # noqa: E402
import settings  # noqa: E402

TOKEN = "test-token-with-enough-length-1234"
GUILD = str(DEMO_GUILDS[0]["id"])
ANNOUNCE = str(DEMO_GUILDS[0]["channels"][0][0])


class DashboardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("SOUNDTRAIL_SUPPORT_DATA")
        os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._tmp.name
        store.init_db()
        self.gw = DemoGateway()
        self.cfg = demo_config(DEMO_GUILDS)
        self.cfg["dashboard_token"] = TOKEN

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("SOUNDTRAIL_SUPPORT_DATA", None)
        else:
            os.environ["SOUNDTRAIL_SUPPORT_DATA"] = self._old
        self._tmp.cleanup()

    def make_server(self, **overrides) -> DashboardServer:
        options = {"token": TOKEN, "host": "127.0.0.1", "port": 8765}
        options.update(overrides)
        return DashboardServer(self.gw, self.cfg, **options)

    def run_client(self, server: DashboardServer, scenario):
        async def main():
            async with TestClient(TestServer(server.app)) as client:
                return await scenario(client)
        return asyncio.run(main())

    def run_with(self, scenario, **overrides):
        return self.run_client(self.make_server(**overrides), scenario)


async def login(client, token=TOKEN):
    response = await client.post("api/login", json={"token": token})
    body = await response.json() if response.status == 200 else None
    cookie = response.cookies.get(SESSION_COOKIE)
    return response.status, body, (cookie.value if cookie else None)


def auth_headers(cookie: str, csrf: str | None = None) -> dict:
    headers = {"Cookie": f"{SESSION_COOKIE}={cookie}"}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    return headers


class PageTests(DashboardTestCase):
    def test_page_and_static_files_are_served_with_strict_headers(self):
        async def scenario(client):
            page = await client.get("/")
            self.assertEqual(page.status, 200)
            self.assertIn("Soundtrail Support", await page.text())
            self.assertIn("script-src 'self'", page.headers["Content-Security-Policy"])
            self.assertEqual(page.headers["X-Frame-Options"], "DENY")
            self.assertEqual(page.headers["X-Content-Type-Options"], "nosniff")

            script = await client.get("/static/dashboard.js")
            self.assertEqual(script.status, 200)
            self.assertEqual((await client.get("/static/settings.json")).status, 404)
            self.assertEqual((await client.get("/static/..%2Fconfig.json")).status, 404)
        self.run_with(scenario)

    def test_unknown_host_header_is_refused(self):
        async def scenario(client):
            response = await client.get("/api/session", headers={"Host": "evil.example"})
            self.assertEqual(response.status, 400)
            self.assertIn("Unrecognized Host", await response.text())
        self.run_with(scenario)

    def test_configured_allowed_host_is_accepted(self):
        async def scenario(client):
            response = await client.get("/api/session", headers={"Host": "dash.example.com"})
            self.assertEqual(response.status, 200)
        self.run_with(scenario, allowed_hosts={"dash.example.com"})

    def test_api_responses_are_not_cached(self):
        async def scenario(client):
            response = await client.get("/api/session")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.run_with(scenario)


class SignInTests(DashboardTestCase):
    def test_session_starts_logged_out_and_api_needs_login(self):
        async def scenario(client):
            session = await (await client.get("/api/session")).json()
            self.assertFalse(session["authenticated"])
            self.assertEqual((await client.get("/api/guilds")).status, 401)
        self.run_with(scenario)

    def test_wrong_token_is_refused(self):
        async def scenario(client):
            status, body, cookie = await login(client, token="nope")
            self.assertEqual(status, 401)
            self.assertIsNone(cookie)
        self.run_with(scenario)

    def test_login_sets_an_httponly_strict_cookie(self):
        async def scenario(client):
            response = await client.post("api/login", json={"token": TOKEN})
            morsel = response.cookies[SESSION_COOKIE]
            self.assertTrue(morsel["httponly"])
            self.assertEqual(morsel["samesite"], "Strict")
        self.run_with(scenario)

    def test_login_then_list_servers_with_ids_as_strings(self):
        async def scenario(client):
            status, body, cookie = await login(client)
            self.assertEqual(status, 200)
            self.assertTrue(body["csrf"])
            guilds = await (await client.get("/api/guilds", headers=auth_headers(cookie))).json()
            self.assertEqual({g["name"] for g in guilds}, {g["name"] for g in DEMO_GUILDS})
            self.assertIsInstance(guilds[0]["id"], str)  # JavaScript cannot hold 64-bit ids
        self.run_with(scenario)

    def test_logout_ends_the_session(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            await client.post("api/logout", headers=auth_headers(cookie, body["csrf"]))
            self.assertEqual((await client.get("/api/guilds", headers=auth_headers(cookie))).status, 401)
        self.run_with(scenario)

    def test_repeated_failures_are_throttled_even_with_the_right_token(self):
        async def scenario(client):
            for _ in range(LOGIN_MAX_FAILURES):
                status, _, _ = await login(client, token="wrong")
                self.assertEqual(status, 401)
            status, _, _ = await login(client)
            self.assertEqual(status, 429)
        self.run_with(scenario)


class WriteProtectionTests(DashboardTestCase):
    def test_writes_need_the_csrf_header(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            payload = {"message": "Hello from the dashboard", "channel_id": ANNOUNCE}
            missing = await client.post(f"api/guilds/{GUILD}/announce", json=payload,
                                        headers=auth_headers(cookie))
            self.assertEqual(missing.status, 403)
            self.assertEqual(self.gw.posts, [])

            wrong = await client.post(f"api/guilds/{GUILD}/announce", json=payload,
                                      headers=auth_headers(cookie, "not-the-token"))
            self.assertEqual(wrong.status, 403)
            self.assertEqual(self.gw.posts, [])

            good = await client.post(f"api/guilds/{GUILD}/announce", json=payload,
                                     headers=auth_headers(cookie, body["csrf"]))
            self.assertEqual(good.status, 200)
            self.assertEqual(len(self.gw.posts), 2)  # announcement plus the staff-log copy
        self.run_with(scenario)

    def test_dashboard_posts_are_audited_as_dashboard(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            await client.post(f"api/guilds/{GUILD}/announce", json={"message": "Hi", "channel_id": ANNOUNCE},
                              headers=auth_headers(cookie, body["csrf"]))
            self.assertEqual(audit.list_entries(int(GUILD), 1)[0]["source"], "dashboard")
        self.run_with(scenario)


class ApiTests(DashboardTestCase):
    def signed_in(self, client):
        async def go():
            _, body, cookie = await login(client)
            return auth_headers(cookie, body["csrf"]), auth_headers(cookie)
        return go()

    def test_bad_and_unknown_servers(self):
        async def scenario(client):
            _, read = await self.signed_in(client)
            bad = await client.get("api/guilds/abc/overview", headers=read)
            self.assertEqual(bad.status, 400)
            self.assertIn("not valid", (await bad.json())["error"])
            missing = await client.get("api/guilds/1/overview", headers=read)
            self.assertEqual(missing.status, 404)
        self.run_with(scenario)

    def test_overview_and_settings_round_trip(self):
        async def scenario(client):
            write, read = await self.signed_in(client)
            overview = await (await client.get(f"api/guilds/{GUILD}/overview", headers=read)).json()
            self.assertIn("scheduled", overview["stats"])
            self.assertIsInstance(overview["recent"], list)

            bad = await client.put(f"api/guilds/{GUILD}/settings", json={"bot_token": "x"}, headers=write)
            self.assertEqual(bad.status, 400)
            self.assertIn("Unknown setting", (await bad.json())["error"])

            saved = await client.put(f"api/guilds/{GUILD}/settings", json={"timezone": "Europe/London"},
                                     headers=write)
            self.assertEqual(saved.status, 200)
            self.assertEqual((await saved.json())["timezone"], "Europe/London")

            invalid = await client.put(f"api/guilds/{GUILD}/settings", json={"timezone": "Mars/Base"},
                                       headers=write)
            self.assertEqual(invalid.status, 400)
        self.run_with(scenario)

    def test_schedule_then_cancel(self):
        async def scenario(client):
            write, read = await self.signed_in(client)
            created = await client.post(
                f"api/guilds/{GUILD}/scheduled",
                json={"when": "2h", "message": "Restart at 9", "title": "Restart", "channel_id": ANNOUNCE},
                headers=write,
            )
            self.assertEqual(created.status, 201)
            schedule_id = (await created.json())["id"]
            listed = await (await client.get(f"api/guilds/{GUILD}/scheduled", headers=read)).json()
            self.assertEqual(len(listed), 1)
            gone = await client.delete(f"api/guilds/{GUILD}/scheduled/{schedule_id}", headers=write)
            self.assertEqual(gone.status, 200)
            self.assertEqual(await (await client.get(f"api/guilds/{GUILD}/scheduled", headers=read)).json(), [])
        self.run_with(scenario)

    def test_template_poll_event_and_welcome_endpoints(self):
        async def scenario(client):
            write, read = await self.signed_in(client)
            saved = await client.post(f"api/guilds/{GUILD}/templates",
                                      json={"name": "raid", "message": "Tonight"}, headers=write)
            self.assertEqual(saved.status, 200)
            used = await client.post(f"api/guilds/{GUILD}/templates/raid/use",
                                     json={"channel_id": ANNOUNCE}, headers=write)
            self.assertEqual(used.status, 200)

            poll = await client.post(f"api/guilds/{GUILD}/polls",
                                     json={"question": "Which night?", "options": ["Tue", "Fri"]},
                                     headers=write)
            self.assertEqual(poll.status, 201)
            closed = await client.post(f"api/guilds/{GUILD}/polls/{(await poll.json())['id']}/close",
                                       headers=write)
            self.assertEqual(closed.status, 200)

            event = await client.post(f"api/guilds/{GUILD}/events",
                                      json={"title": "Team night", "starts_when": "3h", "remind_minutes": 60},
                                      headers=write)
            self.assertEqual(event.status, 201)
            listed = await (await client.get(f"api/guilds/{GUILD}/events", headers=read)).json()
            self.assertEqual(listed[0]["title"], "Team night")

            welcome_channel = str(DEMO_GUILDS[0]["channels"][1][0])
            set_welcome = await client.post(f"api/guilds/{GUILD}/welcome",
                                            json={"channel_id": welcome_channel, "message": None},
                                            headers=write)
            self.assertEqual(set_welcome.status, 200)
            test = await client.post(f"api/guilds/{GUILD}/welcome/test", headers=write)
            self.assertEqual(test.status, 200)
            off = await client.delete(f"api/guilds/{GUILD}/welcome", headers=write)
            self.assertEqual(off.status, 200)
        self.run_with(scenario)

    def test_audit_endpoint(self):
        async def scenario(client):
            write, read = await self.signed_in(client)
            await client.post(f"api/guilds/{GUILD}/announce", json={"message": "x", "channel_id": ANNOUNCE},
                              headers=write)
            rows = await (await client.get(f"api/guilds/{GUILD}/audit?limit=5", headers=read)).json()
            self.assertEqual(rows[0]["action"], "announce.post")
        self.run_with(scenario)


class DemoModeTests(DashboardTestCase):
    """Demo mode must work even when the browser drops cookies (a framed preview)."""

    def test_demo_works_without_any_cookie(self):
        async def scenario(client):
            session = await (await client.get("/api/session")).json()
            self.assertTrue(session["authenticated"])
            self.assertTrue(session["demo"])
            guilds = await client.get("/api/guilds")
            self.assertEqual(guilds.status, 200)

            csrf = session["csrf"]
            payload = {"message": "Preview post", "channel_id": ANNOUNCE}
            refused = await client.post(f"api/guilds/{GUILD}/announce", json=payload)
            self.assertEqual(refused.status, 403)  # CSRF still applies with no cookie
            posted = await client.post(f"api/guilds/{GUILD}/announce", json=payload,
                                       headers={"X-CSRF-Token": csrf})
            self.assertEqual(posted.status, 200)
        self.run_with(scenario, demo=True, allowed_hosts=["*"])

    def test_live_mode_still_needs_a_token(self):
        async def scenario(client):
            self.assertFalse((await (await client.get("/api/session")).json())["authenticated"])
            self.assertEqual((await client.get("/api/guilds")).status, 401)
        self.run_with(scenario)


class TokenTests(DashboardTestCase):
    def test_token_is_generated_once_and_then_reused(self):
        cfg = {}
        first = ensure_dashboard_token(cfg)
        self.assertGreaterEqual(len(first), 24)
        self.assertEqual(ensure_dashboard_token(cfg), first)
        saved = json.loads(paths.config_path().read_text(encoding="utf-8"))
        self.assertEqual(saved["dashboard_token"], first)

    def test_server_needs_a_token(self):
        with self.assertRaises(ValueError):
            DashboardServer(self.gw, copy.deepcopy(self.cfg), token="")



class FeatureRouteTests(DashboardTestCase):
    """The approval queue, editing and deleting posts, the CSV export and the new settings."""

    def test_approval_round_trip_over_http(self):
        settings.set_guild_value(self.cfg, int(GUILD), "require_approval", True)

        async def scenario(client):
            _, body, cookie = await login(client)
            csrf = body["csrf"]
            posted = await client.post(
                f"api/guilds/{GUILD}/announce", json={"message": "Needs a second pair of eyes"},
                headers=auth_headers(cookie, csrf),
            )
            self.assertEqual(posted.status, 200)
            result = await posted.json()
            self.assertTrue(result["queued"])
            request_id = str(result["approval_id"])

            queue = await (await client.get(f"api/guilds/{GUILD}/approvals", headers=auth_headers(cookie))).json()
            self.assertEqual([row["id"] for row in queue], [request_id])
            self.assertEqual(queue[0]["payload"]["message"], "Needs a second pair of eyes")

            # The dashboard is one identity, so it can not approve its own request.
            own = await client.post(
                f"api/guilds/{GUILD}/approvals/{request_id}/approve", headers=auth_headers(cookie, csrf)
            )
            self.assertEqual(own.status, 400)
            self.assertIn("own request", (await own.json())["error"])

            rejected = await client.post(
                f"api/guilds/{GUILD}/approvals/{request_id}/reject", headers=auth_headers(cookie, csrf)
            )
            self.assertEqual(rejected.status, 200)
            queue = await (await client.get(f"api/guilds/{GUILD}/approvals", headers=auth_headers(cookie))).json()
            self.assertEqual(queue, [])
        self.run_with(scenario)

    def test_a_discord_request_can_be_approved_from_the_dashboard(self):
        settings.set_guild_value(self.cfg, int(GUILD), "require_approval", True)
        queued = asyncio.run(ops.announce_now(
            self.gw, self.cfg, int(GUILD), Actor(43, "Ben", "discord"), message="From Discord"
        ))

        async def scenario(client):
            _, body, cookie = await login(client)
            approved = await client.post(
                f"api/guilds/{GUILD}/approvals/{queued['approval_id']}/approve",
                headers=auth_headers(cookie, body["csrf"]),
            )
            self.assertEqual(approved.status, 200)
            self.assertIsNotNone((await approved.json())["jump_url"])
        self.run_with(scenario)
        self.assertTrue(any(p["channel_id"] == int(ANNOUNCE) for p in self.gw.posts))

    def test_edit_and_delete_need_the_csrf_header(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            posted = await client.post(
                f"api/guilds/{GUILD}/announce", json={"message": "Edit me"}, headers=auth_headers(cookie, body["csrf"])
            )
            message_id = (await posted.json())["message_id"]
            url = f"api/guilds/{GUILD}/announcements/{message_id}"

            refused = await client.patch(url, json={"message": "x"}, headers=auth_headers(cookie))
            self.assertEqual(refused.status, 403)

            edited = await client.patch(
                url, json={"message": "Edited text", "title": "New heading"}, headers=auth_headers(cookie, body["csrf"])
            )
            self.assertEqual(edited.status, 200)
            self.assertEqual((await edited.json())["body"], "Edited text")

            gone = await client.delete(url, headers=auth_headers(cookie, body["csrf"]))
            self.assertEqual(gone.status, 200)
            history = await (await client.get(f"api/guilds/{GUILD}/history", headers=auth_headers(cookie))).json()
            self.assertNotIn(message_id, [row["message_id"] for row in history])
        self.run_with(scenario)

    def test_editing_something_the_bot_did_not_post_is_refused(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            response = await client.patch(
                f"api/guilds/{GUILD}/announcements/123", json={"message": "x"}, headers=auth_headers(cookie, body["csrf"])
            )
            self.assertEqual(response.status, 400)
        self.run_with(scenario)

    def test_audit_export_is_a_csv_download_for_signed_in_staff(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            await client.post(f"api/guilds/{GUILD}/announce", json={"message": "Logged"},
                              headers=auth_headers(cookie, body["csrf"]))
            response = await client.get(f"api/guilds/{GUILD}/audit.csv", headers=auth_headers(cookie))
            self.assertEqual(response.status, 200)
            self.assertTrue(response.headers["Content-Type"].startswith("text/csv"))
            self.assertIn("attachment", response.headers["Content-Disposition"])
            text = await response.text()
            self.assertTrue(text.startswith("created_at,actor_name,source,action,detail,actor_id"))
            self.assertIn("announce.post", text)

            client.session.cookie_jar.clear()
            anonymous = await client.get(f"api/guilds/{GUILD}/audit.csv")
            self.assertEqual(anonymous.status, 401)
        self.run_with(scenario)

    def test_pins_route_lists_active_pins_and_the_hold_setting_saves(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            headers = auth_headers(cookie, body["csrf"])
            posted = await client.post(f"api/guilds/{GUILD}/announce", json={"message": "Pinned one", "pin_for": "24h"},
                                       headers=headers)
            self.assertEqual(posted.status, 200)
            pinned = await (await client.get(f"api/guilds/{GUILD}/pins", headers=auth_headers(cookie))).json()
            self.assertEqual(len(pinned), 1)
            self.assertIn("unpin_at", pinned[0])

            saved = await client.put(
                f"api/guilds/{GUILD}/settings",
                json={"quiet_start": "22:00", "quiet_end": "08:00", "quiet_hold_manual": True},
                headers=headers,
            )
            self.assertEqual(saved.status, 200)
            self.assertTrue((await saved.json())["quiet_hold_manual"])
        self.run_with(scenario)

    def test_settings_carry_the_new_keys_and_refuse_half_a_quiet_window(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            headers = auth_headers(cookie, body["csrf"])
            current = await (await client.get(f"api/guilds/{GUILD}/settings", headers=auth_headers(cookie))).json()
            for key in ("require_approval", "quiet_start", "quiet_end"):
                self.assertIn(key, current)

            half = await client.put(
                f"api/guilds/{GUILD}/settings", json={"quiet_start": "22:00"}, headers=headers
            )
            self.assertEqual(half.status, 400)

            saved = await client.put(
                f"api/guilds/{GUILD}/settings",
                json={"quiet_start": "22:00", "quiet_end": "08:00", "require_approval": True},
                headers=headers,
            )
            self.assertEqual(saved.status, 200)
            result = await saved.json()
            self.assertEqual((result["quiet_start"], result["quiet_end"], result["require_approval"]),
                             ("22:00", "08:00", True))
        self.run_with(scenario)

class VoteTicketRouteTests(DashboardTestCase):
    """Poll details with live votes, tickets, reaction roles and the ticket category over HTTP."""

    def test_poll_detail_shows_live_counts_and_who_voted(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            headers = auth_headers(cookie, body['csrf'])
            created = await client.post(
                f"api/guilds/{GUILD}/polls",
                json={"question": "Which map?", "options": ["Dust II", "Nuke"], "multiple": True},
                headers=headers,
            )
            self.assertEqual(created.status, 201)
            poll = await created.json()
            self.assertTrue(poll["multiple"])
            pid = poll["id"]

            await ops.cast_vote(self.gw, self.cfg, int(GUILD), pid, Actor(7001, "Zed", "discord"), 1)
            await ops.cast_vote(self.gw, self.cfg, int(GUILD), pid, Actor(7001, "Zed", "discord"), 0)

            detail = await (await client.get(f"api/guilds/{GUILD}/polls/{pid}", headers=auth_headers(cookie))).json()
            self.assertEqual(detail["counts"], [1, 1])
            self.assertEqual(detail["voters_count"], 1)
            self.assertEqual(detail["voters"][0]["name"], "Zed")
            self.assertEqual(detail["voters"][0]["choices"], ["Dust II", "Nuke"])
            self.assertIsInstance(detail["voters"][0]["user_id"], str)

            closed = await client.post(f"api/guilds/{GUILD}/polls/{pid}/close", headers=headers)
            self.assertEqual(closed.status, 200)
            final = await (await client.get(f"api/guilds/{GUILD}/polls/{pid}", headers=auth_headers(cookie))).json()
            self.assertEqual(final["status"], "closed")
            self.assertEqual(final["counts"], [1, 1])

            missing = await client.get(f"api/guilds/{GUILD}/polls/999", headers=auth_headers(cookie))
            self.assertEqual(missing.status, 400)
        self.run_with(scenario)

    def test_ticket_routes_panel_list_and_close(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            headers = auth_headers(cookie, body['csrf'])
            panel = await client.post(f"api/guilds/{GUILD}/tickets/panel", json={"channel_id": ANNOUNCE}, headers=headers)
            self.assertEqual(panel.status, 201)

            ticket = await ops.open_ticket(
                self.gw, self.cfg, int(GUILD), Actor(8001, "Ivy", "discord"), category="account"
            )
            await ops.assign_ticket(
                self.gw, self.cfg, int(GUILD), Actor(8002, "Staff", "discord"), ticket["id"],
                Actor(8003, "Bea", "discord"),
            )
            self.gw.say(int(ticket["channel_id"]), "Ivy", "Need help with installation")
            listed = await (await client.get(f"api/guilds/{GUILD}/tickets", headers=auth_headers(cookie))).json()
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["opener_name"], "Ivy")
            self.assertIsInstance(listed[0]["opener_id"], str)
            self.assertEqual(listed[0]["category"], "account")
            self.assertEqual(listed[0]["claimed_by_name"], "Bea")

            closed = await client.post(
                f"api/guilds/{GUILD}/tickets/{ticket['id']}/close",
                json={"reason": "Installation fixed"},
                headers=headers,
            )
            self.assertEqual(closed.status, 200)
            self.assertEqual((await closed.json())["close_reason"], "Installation fixed")
            still_open = await (await client.get(f"api/guilds/{GUILD}/tickets?open=1", headers=auth_headers(cookie))).json()
            self.assertEqual(still_open, [])
            versions = await (
                await client.get(f"api/guilds/{GUILD}/tickets/{ticket['id']}/transcripts", headers=auth_headers(cookie))
            ).json()
            self.assertEqual(len(versions), 1)
            download = await client.get(
                f"api/guilds/{GUILD}/tickets/{ticket['id']}/transcripts/{versions[0]['id']}",
                headers=auth_headers(cookie),
            )
            self.assertEqual(download.status, 200)
            self.assertIn("Ivy", await download.text())
        self.run_with(scenario)

    def test_reaction_role_routes_add_list_and_remove(self):
        member_role = str(DEMO_GUILDS[0]["roles"][1][0])
        message = asyncio.run(self.gw.post(int(GUILD), int(ANNOUNCE), content="Pick a game"))

        async def scenario(client):
            _, body, cookie = await login(client)
            headers = auth_headers(cookie, body['csrf'])
            added = await client.post(
                f"api/guilds/{GUILD}/reaction-roles",
                json={"message": str(message["message_id"]), "emoji": "🎮", "role_id": member_role, "channel_id": ANNOUNCE},
                headers=headers,
            )
            self.assertEqual(added.status, 201)
            rule = await added.json()
            listed = await (await client.get(f"api/guilds/{GUILD}/reaction-roles", headers=auth_headers(cookie))).json()
            self.assertEqual([r["emoji"] for r in listed], ["🎮"])
            self.assertEqual(listed[0]["role_id"], member_role)

            removed = await client.delete(f"api/guilds/{GUILD}/reaction-roles/{rule['id']}", headers=headers)
            self.assertEqual(removed.status, 200)
            self.assertEqual(await (await client.get(f"api/guilds/{GUILD}/reaction-roles", headers=auth_headers(cookie))).json(), [])

            bad = await client.post(
                f"api/guilds/{GUILD}/reaction-roles",
                json={"message": "not a message", "emoji": "🎮", "role_id": member_role, "channel_id": ANNOUNCE},
                headers=headers,
            )
            self.assertEqual(bad.status, 400)
        self.run_with(scenario)

    def test_ticket_category_is_a_setting_and_a_snapshot_dropdown(self):
        support = str(DEMO_GUILDS[0]["categories"][0][0])

        async def scenario(client):
            _, body, cookie = await login(client)
            headers = auth_headers(cookie, body['csrf'])
            snapshot = await (await client.get(f"api/guilds/{GUILD}/snapshot", headers=auth_headers(cookie))).json()
            self.assertEqual([c["name"] for c in snapshot["categories"]], ["Support"])

            saved = await client.put(
                f"api/guilds/{GUILD}/settings", json={"ticket_category_id": support}, headers=headers
            )
            self.assertEqual(saved.status, 200)
            self.assertEqual((await saved.json())["ticket_category_id"], support)

            cleared = await client.put(
                f"api/guilds/{GUILD}/settings", json={"ticket_category_id": None}, headers=headers
            )
            self.assertIsNone((await cleared.json())["ticket_category_id"])
        self.run_with(scenario)



GENERAL = "100000000000000102"


class UpkeepRouteTests(DashboardTestCase):
    """Transcripts, role menus, scheduled polls and the auto-close setting over HTTP."""

    def test_a_closed_ticket_transcript_downloads_as_a_text_file(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            headers = auth_headers(cookie, body["csrf"])
            ticket = await ops.open_ticket(self.gw, self.cfg, int(GUILD), Actor(7001, "Zed", "discord"))
            self.gw.say(int(ticket["channel_id"]), "Zed", "Where do I vote?")
            tid = ticket["id"]

            early = await client.get(f"api/guilds/{GUILD}/tickets/{tid}/transcript", headers=auth_headers(cookie))
            self.assertEqual(early.status, 400)

            closed = await client.post(f"api/guilds/{GUILD}/tickets/{tid}/close", headers=headers)
            self.assertEqual(closed.status, 200)

            saved = await client.get(f"api/guilds/{GUILD}/tickets/{tid}/transcript", headers=auth_headers(cookie))
            self.assertEqual(saved.status, 200)
            self.assertTrue(saved.headers["Content-Type"].startswith("text/plain"))
            self.assertIn(f"ticket-{tid}.txt", saved.headers["Content-Disposition"])
            self.assertIn("Zed: Where do I vote?", await saved.text())
        self.run_with(scenario)

    def test_role_menu_routes_build_post_and_delete_a_menu(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            headers = auth_headers(cookie, body["csrf"])

            empty_title = await client.post(f"api/guilds/{GUILD}/role-menus", json={"title": " "}, headers=headers)
            self.assertEqual(empty_title.status, 400)

            created = await client.post(
                f"api/guilds/{GUILD}/role-menus",
                json={"title": "Games", "description": "Pick your games."},
                headers=headers,
            )
            self.assertEqual(created.status, 201)
            menu_id = (await created.json())["id"]

            no_roles = await client.post(
                f"api/guilds/{GUILD}/role-menus/{menu_id}/post", json={"channel_id": GENERAL}, headers=headers
            )
            self.assertEqual(no_roles.status, 400)

            added = await client.post(
                f"api/guilds/{GUILD}/role-menus/{menu_id}/options",
                json={"role_id": "100000000000000202", "label": "Member"},
                headers=headers,
            )
            self.assertEqual(added.status, 201)
            self.assertIsInstance((await added.json())["role_id"], str)

            listed = await (await client.get(f"api/guilds/{GUILD}/role-menus", headers=auth_headers(cookie))).json()
            self.assertEqual(listed[0]["title"], "Games")
            self.assertEqual(listed[0]["options"][0]["label"], "Member")

            posted = await client.post(
                f"api/guilds/{GUILD}/role-menus/{menu_id}/post", json={"channel_id": GENERAL}, headers=headers
            )
            self.assertEqual(posted.status, 201)

            removed = await client.delete(
                f"api/guilds/{GUILD}/role-menus/{menu_id}/options/100000000000000202", headers=headers
            )
            self.assertEqual(removed.status, 200)

            deleted = await client.delete(f"api/guilds/{GUILD}/role-menus/{menu_id}", headers=headers)
            self.assertEqual(deleted.status, 200)
            self.assertEqual(await (await client.get(f"api/guilds/{GUILD}/role-menus", headers=auth_headers(cookie))).json(), [])
        self.run_with(scenario)

    def test_role_menu_writes_need_the_csrf_header(self):
        async def scenario(client):
            _, _, cookie = await login(client)
            refused = await client.post(
                f"api/guilds/{GUILD}/role-menus", json={"title": "Games"}, headers=auth_headers(cookie)
            )
            self.assertEqual(refused.status, 403)
        self.run_with(scenario)

    def test_a_scheduled_poll_is_created_over_http_and_listed_as_scheduled(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            headers = auth_headers(cookie, body["csrf"])
            created = await client.post(
                f"api/guilds/{GUILD}/polls",
                json={"question": "Pizza?", "options": ["Yes", "No"], "send_at": "2h"},
                headers=headers,
            )
            self.assertEqual(created.status, 201)
            result = await created.json()
            self.assertTrue(result["scheduled"])
            listed = await (await client.get(f"api/guilds/{GUILD}/polls", headers=auth_headers(cookie))).json()
            self.assertEqual(listed[0]["status"], "scheduled")
        self.run_with(scenario)

    def test_the_auto_close_setting_is_saved_and_validated(self):
        async def scenario(client):
            _, body, cookie = await login(client)
            headers = auth_headers(cookie, body["csrf"])
            saved = await client.put(
                f"api/guilds/{GUILD}/settings", json={"ticket_auto_close_days": 3}, headers=headers
            )
            self.assertEqual(saved.status, 200)
            self.assertEqual((await saved.json())["ticket_auto_close_days"], 3)
            bad = await client.put(
                f"api/guilds/{GUILD}/settings", json={"ticket_auto_close_days": 900}, headers=headers
            )
            self.assertEqual(bad.status, 400)
            retention = await client.put(
                f"api/guilds/{GUILD}/settings",
                json={"ticket_transcript_retention_days": 30},
                headers=headers,
            )
            self.assertEqual(retention.status, 200)
            self.assertEqual((await retention.json())["ticket_transcript_retention_days"], 30)
            invalid_retention = await client.put(
                f"api/guilds/{GUILD}/settings",
                json={"ticket_transcript_retention_days": 4000},
                headers=headers,
            )
            self.assertEqual(invalid_retention.status, 400)
            hours = await client.put(
                f"api/guilds/{GUILD}/settings",
                json={
                    "support_hours_start": "09:00",
                    "support_hours_end": "17:00",
                    "after_hours_message": "We will reply when we're back.",
                },
                headers=headers,
            )
            self.assertEqual(hours.status, 200)
            saved_hours = await hours.json()
            self.assertEqual(saved_hours["support_hours_start"], "09:00")
            self.assertEqual(saved_hours["after_hours_message"], "We will reply when we're back.")
            cleared_hours = await client.put(
                f"api/guilds/{GUILD}/settings",
                json={"support_hours_start": None, "support_hours_end": None},
                headers=headers,
            )
            self.assertEqual(cleared_hours.status, 200)
            partial = await client.put(
                f"api/guilds/{GUILD}/settings", json={"support_hours_start": "10:00"}, headers=headers
            )
            self.assertEqual(partial.status, 400)
        self.run_with(scenario)


if __name__ == "__main__":
    unittest.main()
