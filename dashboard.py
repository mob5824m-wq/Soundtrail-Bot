"""Authenticated web dashboard for Soundtrail Support.

The dashboard is a single page (``dashboard.html`` with ``static/``) backed by
a JSON API under ``/api``. Every button on the page calls the same
:mod:`operations` functions as the slash commands, so the two can not drift
apart, and every action is recorded in the audit log as ``dashboard``.

Security
    * The dashboard token is a bot-wide credential. It is generated on first
      run, kept in ``data/config.json``, and printed by ``--dashboard-token``.
      The Discord bot token will not work here.
    * The session is an HttpOnly, SameSite=Strict cookie. Every write also needs
      an ``X-CSRF-Token`` header that matches the session.
    * The ``Host`` header must be a loopback name or one of
      ``dashboard_allowed_hosts``. This blocks DNS rebinding.
    * Login attempts are throttled per client address. Set
      ``dashboard_trusted_proxies`` so a reverse proxy's ``X-Forwarded-For``
      counts; without it every visitor looks like the proxy.
    * The default bind is 127.0.0.1. Reach it from another machine through an
      SSH tunnel or a reverse proxy that terminates HTTPS.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from aiohttp import web

import audit
import operations as ops
import paths
import settings
import welcome
from audit import DASHBOARD_ACTOR
from operations import OperationError

logger = logging.getLogger("soundtrail_support.dashboard")

APP_DIR = Path(__file__).resolve().parent
INDEX_PATH = APP_DIR / "dashboard.html"
STATIC_DIR = APP_DIR / "static"
STATIC_FILES = {"dashboard.js", "dashboard.css"}

SESSION_COOKIE = "cm_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
LOGIN_WINDOW_SECONDS = 10 * 60
LOGIN_MAX_FAILURES = 8
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
MAX_BODY_BYTES = 64 * 1024
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def ensure_dashboard_token(cfg: dict[str, Any]) -> str:
    """Return the dashboard token, generating and saving one on first use."""
    if cfg.get("dashboard_token"):
        return str(cfg["dashboard_token"])
    cfg["dashboard_token"] = secrets.token_urlsafe(24)
    settings.save_config(cfg)
    return cfg["dashboard_token"]


def _hostname(header: str) -> str:
    host = (header or "").strip().lower()
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host
    return host.split(":", 1)[0]


def _jsonable(value: Any, key: Optional[str] = None) -> Any:
    """Send Discord ids as strings: JavaScript numbers can not hold 64-bit ids exactly."""
    if isinstance(value, dict):
        return {k: _jsonable(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v, key) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int) and key and (key == "id" or key.endswith("_id") or key.endswith("_ids")):
        return str(value)
    return value


def _http(cls, message: str):
    return cls(text=json.dumps({"error": message}), content_type="application/json")


def _as_int(value: Any, field: str) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise OperationError(f"`{field}` must be a number.") from None


def _as_bool(value: Any) -> bool:
    return value in (True, "true", "1", 1, "on")


class DashboardServer:
    def __init__(
        self,
        gateway: ops.Gateway,
        config: dict[str, Any],
        *,
        token: str,
        host: str = "127.0.0.1",
        port: int = 8765,
        allowed_hosts: list[str] | tuple[str, ...] = (),
        trusted_proxies: list[str] | tuple[str, ...] = (),
        secure_cookie: bool = False,
        demo: bool = False,
    ) -> None:
        if not token:
            raise ValueError("The dashboard needs a token.")
        self.gateway = gateway
        self.config = config
        self.token = token
        self.host = host
        self.port = int(port)
        self.secure_cookie = secure_cookie
        self.demo = demo
        self.allowed_hosts = {h.lower() for h in allowed_hosts} | LOOPBACK_NAMES
        self._trusted = self._parse_networks(trusted_proxies)
        self._sessions: dict[str, dict[str, Any]] = {}
        self._failures: dict[str, deque[float]] = {}
        self._runner: Optional[web.AppRunner] = None
        self._app: Optional[web.Application] = None
        # Demo mode serves simulated data only, with no secrets. It does not use
        # the cookie at all: browsers drop SameSite=Strict cookies set inside a
        # framed preview, which would leave every request looking signed out.
        self._demo_session: Optional[dict[str, Any]] = (
            {"csrf": secrets.token_urlsafe(24), "expires": float("inf")} if demo else None
        )

    # ------------------------------------------------------------------ #
    # Application
    # ------------------------------------------------------------------ #
    @property
    def app(self) -> web.Application:
        if self._app is None:
            self._app = self._build_app()
        return self._app

    def _build_app(self) -> web.Application:
        app = web.Application(
            middlewares=[self._host_middleware, self._error_middleware],
            client_max_size=MAX_BODY_BYTES,
        )
        app.on_response_prepare.append(self._security_headers)
        r = app.router
        r.add_get("/", self._index)
        r.add_get("/static/{name}", self._static)
        r.add_get("/api/session", self._session_info)
        r.add_post("/api/login", self._login)
        r.add_post("/api/logout", self._logout)
        r.add_get("/api/guilds", self._guilds)

        g = "/api/guilds/{gid}"
        r.add_get(f"{g}/overview", self._overview)
        r.add_get(f"{g}/snapshot", self._snapshot)
        r.add_get(f"{g}/settings", self._settings_get)
        r.add_put(f"{g}/settings", self._settings_put)
        r.add_get(f"{g}/history", self._history)
        r.add_post(f"{g}/announce", self._announce)
        r.add_get(f"{g}/scheduled", self._scheduled_list)
        r.add_get(f"{g}/pins", self._pins_list)
        r.add_post(f"{g}/scheduled", self._scheduled_create)
        r.add_delete(f"{g}/scheduled/{{sid}}", self._scheduled_cancel)
        r.add_get(f"{g}/templates", self._templates_list)
        r.add_post(f"{g}/templates", self._templates_save)
        r.add_delete(f"{g}/templates/{{name}}", self._templates_delete)
        r.add_post(f"{g}/templates/{{name}}/use", self._templates_use)
        r.add_get(f"{g}/polls", self._polls_list)
        r.add_post(f"{g}/polls", self._polls_create)
        r.add_get(f"{g}/polls/{{pid}}", self._poll_detail)
        r.add_post(f"{g}/polls/{{pid}}/close", self._polls_close)
        r.add_get(f"{g}/tickets", self._tickets_list)
        r.add_post(f"{g}/tickets/panel", self._ticket_panel)
        r.add_post(f"{g}/tickets/{{tid}}/close", self._ticket_close)
        r.add_get(f"{g}/tickets/{{tid}}/transcript", self._ticket_transcript)
        r.add_get(f"{g}/tickets/{{tid}}/transcripts", self._ticket_transcripts_list)
        r.add_get(f"{g}/tickets/{{tid}}/transcripts/{{vid}}", self._ticket_transcript_version)
        r.add_get(f"{g}/role-menus", self._role_menus_list)
        r.add_post(f"{g}/role-menus", self._role_menu_create)
        r.add_delete(f"{g}/role-menus/{{mid}}", self._role_menu_delete)
        r.add_post(f"{g}/role-menus/{{mid}}/options", self._role_menu_option_add)
        r.add_delete(f"{g}/role-menus/{{mid}}/options/{{rid}}", self._role_menu_option_remove)
        r.add_post(f"{g}/role-menus/{{mid}}/post", self._role_menu_post)
        r.add_get(f"{g}/reaction-roles", self._reaction_roles_list)
        r.add_post(f"{g}/reaction-roles", self._reaction_role_add)
        r.add_delete(f"{g}/reaction-roles/{{rid}}", self._reaction_role_remove)
        r.add_get(f"{g}/events", self._events_list)
        r.add_post(f"{g}/events", self._events_create)
        r.add_post(f"{g}/events/{{eid}}/cancel", self._events_cancel)
        r.add_post(f"{g}/welcome", self._welcome_set)
        r.add_delete(f"{g}/welcome", self._welcome_off)
        r.add_post(f"{g}/welcome/test", self._welcome_test)
        r.add_get(f"{g}/audit", self._audit)
        r.add_get(f"{g}/audit.csv", self._audit_csv)
        r.add_patch(f"{g}/announcements/{{mid}}", self._announcement_edit)
        r.add_delete(f"{g}/announcements/{{mid}}", self._announcement_delete)
        r.add_get(f"{g}/approvals", self._approvals_list)
        r.add_post(f"{g}/approvals/{{rid}}/approve", self._approval_approve)
        r.add_post(f"{g}/approvals/{{rid}}/reject", self._approval_reject)
        return app

    # ------------------------------------------------------------------ #
    # Middleware and headers
    # ------------------------------------------------------------------ #
    @web.middleware
    async def _host_middleware(self, request: web.Request, handler):
        if "*" not in self.allowed_hosts and _hostname(request.host) not in self.allowed_hosts:
            return web.Response(status=400, text="400: Unrecognized Host header")
        return await handler(request)

    @web.middleware
    async def _error_middleware(self, request: web.Request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except OperationError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except ValueError as exc:
            return web.json_response({"error": str(exc) or "Invalid request."}, status=400)
        except Exception:
            logger.exception("Dashboard request %s %s failed", request.method, request.path)
            return web.json_response(
                {"error": "Something went wrong. Check the bot log for details."}, status=500
            )

    async def _security_headers(self, request: web.Request, response: web.StreamResponse) -> None:
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"

    # ------------------------------------------------------------------ #
    # Sessions, throttling and addresses
    # ------------------------------------------------------------------ #
    def _session(self, request: web.Request) -> Optional[dict[str, Any]]:
        if self._demo_session is not None:
            return self._demo_session
        sid = request.cookies.get(SESSION_COOKIE)
        if not sid:
            return None
        session = self._sessions.get(sid)
        if session is None:
            return None
        if session["expires"] < time.time():
            self._sessions.pop(sid, None)
            return None
        return session

    def _authed(self, request: web.Request) -> dict[str, Any]:
        """The session, or a 401. Writes also need the CSRF header (403)."""
        session = self._session(request)
        if session is None:
            raise _http(web.HTTPUnauthorized, "Log in with the dashboard token first.")
        if request.method != "GET":
            supplied = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(supplied.encode(), session["csrf"].encode()):
                raise _http(web.HTTPForbidden, "The page is out of date. Reload it and try again.")
        return session

    def _parse_networks(self, entries) -> list:
        networks = []
        for entry in entries or ():
            try:
                networks.append(ipaddress.ip_network(str(entry), strict=False))
            except ValueError:
                logger.warning("Ignoring invalid dashboard_trusted_proxies entry %r", entry)
        return networks

    def _is_trusted(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in network for network in self._trusted)

    def _client_ip(self, request: web.Request) -> str:
        remote = request.remote or "unknown"
        if self._is_trusted(remote):
            hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
            for hop in reversed(hops):
                if not self._is_trusted(hop):
                    return hop
        return remote

    def _throttled(self, ip: str) -> bool:
        failures = self._failures.get(ip)
        if not failures:
            return False
        cutoff = time.time() - LOGIN_WINDOW_SECONDS
        while failures and failures[0] < cutoff:
            failures.popleft()
        return len(failures) >= LOGIN_MAX_FAILURES

    def _prune_sessions(self) -> None:
        now = time.time()
        for sid in [s for s, data in self._sessions.items() if data["expires"] < now]:
            self._sessions.pop(sid, None)

    async def _guild(self, request: web.Request) -> dict[str, Any]:
        try:
            gid = int(request.match_info["gid"])
        except ValueError:
            raise _http(web.HTTPBadRequest, "That server id is not valid.") from None
        for guild in self.gateway.guilds():
            if guild["id"] == gid:
                return guild
        raise _http(web.HTTPNotFound, "The bot is not in that server.")

    async def _body(self, request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise _http(web.HTTPBadRequest, "The request body must be JSON.") from None
        if not isinstance(data, dict):
            raise _http(web.HTTPBadRequest, "The request body must be a JSON object.")
        return data

    def _json(self, payload: Any, status: int = 200) -> web.Response:
        return web.json_response(_jsonable(payload), status=status, dumps=lambda o: json.dumps(o, default=str))

    # ------------------------------------------------------------------ #
    # Static pages and session
    # ------------------------------------------------------------------ #
    async def _index(self, request: web.Request) -> web.StreamResponse:
        if not INDEX_PATH.is_file():
            raise _http(web.HTTPNotFound, "dashboard.html is missing from the installation.")
        return web.FileResponse(INDEX_PATH)

    async def _static(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if name not in STATIC_FILES or not (STATIC_DIR / name).is_file():
            raise _http(web.HTTPNotFound, "No such file.")
        return web.FileResponse(STATIC_DIR / name)

    async def _session_info(self, request: web.Request) -> web.Response:
        session = self._session(request)
        return self._json(
            {
                "authenticated": session is not None,
                "csrf": session["csrf"] if session else None,
                "demo": self.demo,
                "version": paths.app_version(),
            }
        )

    async def _login(self, request: web.Request) -> web.Response:
        ip = self._client_ip(request)
        if self._throttled(ip):
            raise _http(web.HTTPTooManyRequests, "Too many attempts. Wait a few minutes and try again.")
        body = await self._body(request)
        supplied = str(body.get("token") or "")
        if not supplied or not hmac.compare_digest(supplied.encode(), self.token.encode()):
            self._failures.setdefault(ip, deque()).append(time.time())
            raise _http(web.HTTPUnauthorized, "That dashboard token is not right.")

        self._failures.pop(ip, None)
        sid, session = self._open_session()
        response = self._json({"csrf": session["csrf"], "expires_in": SESSION_TTL_SECONDS, "demo": self.demo})
        self._attach_session(response, sid)
        return response

    def _open_session(self) -> tuple[str, dict[str, Any]]:
        self._prune_sessions()
        sid = secrets.token_urlsafe(32)
        session = {"csrf": secrets.token_urlsafe(24), "expires": time.time() + SESSION_TTL_SECONDS}
        self._sessions[sid] = session
        return sid, session

    def _attach_session(self, response: web.StreamResponse, sid: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            sid,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="Strict",
            secure=self.secure_cookie,
            path="/",
        )

    async def _logout(self, request: web.Request) -> web.Response:
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            self._sessions.pop(sid, None)
        response = self._json({"ok": True})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    # ------------------------------------------------------------------ #
    # Servers and overview
    # ------------------------------------------------------------------ #
    async def _guilds(self, request: web.Request) -> web.Response:
        self._authed(request)
        return self._json(self.gateway.guilds())

    async def _overview(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        data = ops.overview(self.config, guild["id"])
        return self._json(
            {
                "guild": guild,
                "stats": {k: v for k, v in data.items() if k != "recent"},
                "recent": data["recent"],
            }
        )

    async def _snapshot(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        snapshot = self.gateway.snapshot(guild["id"])
        if snapshot is None:
            raise _http(web.HTTPNotFound, "The bot is not in that server.")
        return self._json(snapshot)

    def _settings_payload(self, guild_id: int) -> dict[str, Any]:
        cfg = settings.get_guild_config(self.config, guild_id)
        return {
            "staff_role_id": cfg["staff_role_id"],
            "announcement_channel_id": cfg["announcement_channel_id"],
            "staff_channel_id": cfg["staff_channel_id"],
            "timezone": cfg["timezone"],
            "welcome_channel_id": cfg["welcome_channel_id"],
            "welcome_message": cfg["welcome_message"],
            "require_approval": cfg["require_approval"],
            "quiet_start": cfg["quiet_start"],
            "quiet_end": cfg["quiet_end"],
            "quiet_hold_manual": cfg["quiet_hold_manual"],
            "ticket_category_id": cfg["ticket_category_id"],
            "ticket_auto_close_days": cfg["ticket_auto_close_days"],
            "ticket_transcript_retention_days": cfg["ticket_transcript_retention_days"],
            "support_hours_start": cfg["support_hours_start"],
            "support_hours_end": cfg["support_hours_end"],
            "after_hours_message": cfg["after_hours_message"],
            "default_welcome_message": welcome.DEFAULT_WELCOME_MESSAGE,
            "placeholders": list(welcome.PLACEHOLDERS),
        }

    async def _settings_get(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        return self._json(self._settings_payload(guild["id"]))

    async def _settings_put(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        changes = {k: v for k, v in body.items() if k in ops.SETTING_FIELDS}
        unknown = [k for k in body if k not in ops.SETTING_FIELDS]
        if unknown:
            raise OperationError(f"Unknown setting: {', '.join(sorted(unknown))}.")
        await ops.update_settings(self.gateway, self.config, guild["id"], DASHBOARD_ACTOR, changes)
        return self._json(self._settings_payload(guild["id"]))

    # ------------------------------------------------------------------ #
    # Announcements
    # ------------------------------------------------------------------ #
    async def _history(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        limit = _as_int(request.query.get("limit"), "limit") or 25
        return self._json(ops.list_history(guild["id"], limit))

    async def _announce(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        result = await ops.announce_now(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            message=str(body.get("message") or ""),
            title=body.get("title") or None,
            channel_id=_as_int(body.get("channel_id"), "channel_id"),
            mention_role_id=_as_int(body.get("mention_role_id"), "mention_role_id"),
            publish=_as_bool(body.get("publish")),
            pin_for=body.get("pin_for") or None,
        )
        return self._json(result)

    async def _scheduled_list(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        return self._json(ops.list_schedule(guild["id"]))

    async def _pins_list(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        return self._json(ops.list_pins(guild["id"]))

    async def _scheduled_create(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        result = await ops.schedule_announcement(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            when=str(body.get("when") or ""),
            message=str(body.get("message") or ""),
            title=body.get("title") or None,
            channel_id=_as_int(body.get("channel_id"), "channel_id"),
            mention_role_id=_as_int(body.get("mention_role_id"), "mention_role_id"),
            publish=_as_bool(body.get("publish")),
            repeat_every=body.get("repeat_every") or None,
            pin_for=body.get("pin_for") or None,
        )
        return self._json(result, status=201)

    async def _scheduled_cancel(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        sid = _as_int(request.match_info["sid"], "id")
        ops.cancel_schedule(guild["id"], DASHBOARD_ACTOR, sid)
        return self._json({"ok": True})

    # ------------------------------------------------------------------ #
    # Templates
    # ------------------------------------------------------------------ #
    async def _templates_list(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        return self._json(ops.list_templates(guild["id"]))

    async def _templates_save(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        result = await ops.save_template(
            guild["id"],
            DASHBOARD_ACTOR,
            name=str(body.get("name") or ""),
            title=body.get("title") or None,
            message=str(body.get("message") or ""),
        )
        return self._json(result)

    async def _templates_delete(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        ops.delete_template(guild["id"], DASHBOARD_ACTOR, request.match_info["name"])
        return self._json({"ok": True})

    async def _templates_use(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        result = await ops.use_template(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            name=request.match_info["name"],
            channel_id=_as_int(body.get("channel_id"), "channel_id"),
            mention_role_id=_as_int(body.get("mention_role_id"), "mention_role_id"),
            publish=_as_bool(body.get("publish")),
        )
        return self._json(result)

    # ------------------------------------------------------------------ #
    # Polls and events
    # ------------------------------------------------------------------ #
    async def _polls_list(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        return self._json(ops.list_polls(guild["id"]))

    async def _polls_create(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        result = await ops.create_poll(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            question=str(body.get("question") or ""),
            options=body.get("options") or [],
            channel_id=_as_int(body.get("channel_id"), "channel_id"),
            duration=body.get("duration") or None,
            multiple=_as_bool(body.get("multiple")),
            send_at=body.get("send_at") or None,
        )
        return self._json(result, status=201)

    async def _poll_detail(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        pid = _as_int(request.match_info["pid"], "id")
        return self._json(ops.poll_details(guild["id"], pid))

    async def _polls_close(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        pid = _as_int(request.match_info["pid"], "id")
        result = await ops.close_poll(self.gateway, self.config, guild["id"], DASHBOARD_ACTOR, pid)
        return self._json(result)

    # ------------------------------------------------------------------ #
    # Tickets and reaction roles
    # ------------------------------------------------------------------ #
    async def _tickets_list(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        return self._json(ops.list_tickets(guild["id"], open_only=request.query.get("open") == "1"))

    async def _ticket_panel(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        result = await ops.post_ticket_panel(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            channel_id=_as_int(body.get("channel_id"), "channel_id"),
        )
        return self._json(result, status=201)

    async def _ticket_close(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        tid = _as_int(request.match_info["tid"], "id")
        body = await self._body(request) if request.content_length else {}
        result = await ops.close_ticket(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            tid,
            actor_is_staff=True,
            reason=str(body.get("reason") or ""),
        )
        return self._json(result)

    async def _reaction_roles_list(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        return self._json(ops.list_reaction_roles(guild["id"]))

    async def _reaction_role_add(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        result = await ops.add_reaction_role(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            message=str(body.get("message") or ""),
            emoji=str(body.get("emoji") or ""),
            role_id=_as_int(body.get("role_id"), "role_id"),
            channel_id=_as_int(body.get("channel_id"), "channel_id"),
        )
        return self._json(result, status=201)

    async def _ticket_transcript(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        tid = _as_int(request.match_info["tid"], "id")
        row = ops.get_ticket_transcript(guild["id"], tid)
        return web.Response(
            text=row["text"],
            content_type="text/plain",
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="ticket-{tid}.txt"'},
        )

    async def _ticket_transcripts_list(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        tid = _as_int(request.match_info["tid"], "id")
        return self._json(ops.list_ticket_transcripts(guild["id"], tid))

    async def _ticket_transcript_version(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        tid = _as_int(request.match_info["tid"], "id")
        version_id = _as_int(request.match_info["vid"], "id")
        row = ops.get_ticket_transcript_version(guild["id"], tid, version_id)
        return web.Response(
            text=row["text"],
            content_type="text/plain",
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="ticket-{tid}-version-{version_id}.txt"'},
        )

    async def _role_menus_list(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        return self._json(ops.list_role_menus(guild["id"]))

    async def _role_menu_create(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        result = await ops.create_role_menu(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            title=str(body.get("title") or ""),
            description=str(body.get("description") or ""),
        )
        return self._json(result, status=201)

    async def _role_menu_delete(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        mid = _as_int(request.match_info["mid"], "id")
        result = await ops.delete_role_menu(self.gateway, self.config, guild["id"], DASHBOARD_ACTOR, menu_id=mid)
        return self._json(result)

    async def _role_menu_option_add(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        result = await ops.add_role_menu_option(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            menu_id=_as_int(request.match_info["mid"], "id"),
            role_id=_as_int(body.get("role_id"), "role_id"),
            label=body.get("label") or None,
            description=body.get("description") or None,
        )
        return self._json(result, status=201)

    async def _role_menu_option_remove(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        result = await ops.remove_role_menu_option(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            menu_id=_as_int(request.match_info["mid"], "id"),
            role_id=_as_int(request.match_info["rid"], "id"),
        )
        return self._json(result)

    async def _role_menu_post(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        result = await ops.post_role_menu(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            menu_id=_as_int(request.match_info["mid"], "id"),
            channel_id=_as_int(body.get("channel_id"), "channel_id"),
        )
        return self._json(result, status=201)

    async def _reaction_role_remove(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        ops.remove_reaction_role(guild["id"], DASHBOARD_ACTOR, _as_int(request.match_info["rid"], "id"))
        return self._json({"ok": True})

    async def _events_list(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        return self._json(await ops.list_events(self.gateway, guild["id"]))

    async def _events_create(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        result = await ops.create_event(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            title=str(body.get("title") or ""),
            starts_when=str(body.get("starts_when") or ""),
            description=str(body.get("description") or ""),
            channel_id=_as_int(body.get("channel_id"), "channel_id"),
            remind_minutes=_as_int(body.get("remind_minutes"), "remind_minutes"),
        )
        return self._json(result, status=201)

    async def _events_cancel(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        eid = _as_int(request.match_info["eid"], "id")
        await ops.cancel_event(self.gateway, self.config, guild["id"], DASHBOARD_ACTOR, eid)
        return self._json({"ok": True})

    # ------------------------------------------------------------------ #
    # Welcome, audit
    # ------------------------------------------------------------------ #
    async def _welcome_set(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        channel_id = _as_int(body.get("channel_id"), "channel_id")
        if not channel_id:
            raise OperationError("Choose the channel where welcomes should be posted.")
        message = body.get("message")
        result = await ops.set_welcome(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            channel_id=channel_id,
            message=None if message is None else str(message),
        )
        return self._json(result)

    async def _welcome_off(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        ops.disable_welcome(self.config, guild["id"], DASHBOARD_ACTOR)
        return self._json({"ok": True})

    async def _welcome_test(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        result = await ops.send_test_welcome(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            server=guild["name"],
            members=guild["member_count"],
        )
        return self._json(result)

    async def _announcement_edit(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        body = await self._body(request)
        result = await ops.edit_announcement(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            message_id=_as_int(request.match_info["mid"], "message_id"),
            message=str(body.get("message") or "") if "message" in body else None,
            title=str(body.get("title") or "") if "title" in body else None,
        )
        return self._json(result)

    async def _announcement_delete(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        await ops.delete_announcement(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            message_id=_as_int(request.match_info["mid"], "message_id"),
        )
        return self._json({"ok": True})

    async def _approvals_list(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        return self._json(ops.list_approvals(guild["id"]))

    async def _approval_approve(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        result = await ops.approve_request(
            self.gateway,
            self.config,
            guild["id"],
            DASHBOARD_ACTOR,
            _as_int(request.match_info["rid"], "id"),
        )
        return self._json(result)

    async def _approval_reject(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        await ops.reject_request(guild["id"], DASHBOARD_ACTOR, _as_int(request.match_info["rid"], "id"))
        return self._json({"ok": True})

    async def _audit_csv(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        return web.Response(
            text=ops.export_audit_csv(guild["id"]),
            content_type="text/csv",
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="soundtrail-support-audit-{guild["id"]}.csv"'},
        )

    async def _audit(self, request: web.Request) -> web.Response:
        self._authed(request)
        guild = await self._guild(request)
        limit = _as_int(request.query.get("limit"), "limit") or audit.DEFAULT_LIMIT
        return self._json(audit.list_entries(guild["id"], limit))

    # ------------------------------------------------------------------ #
    # Running the server
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info("Dashboard listening on http://%s:%s", self.host, self.port)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
