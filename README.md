# Soundtrail Support

A [discord.py](https://discordpy.readthedocs.io/) support bot for the Soundtrail Discord community. Its primary workflow is private support tickets: members open a ticket from a panel, talk with the support team, and can close it when resolved. Staff get private channels, saved transcripts and an audit trail. The bot also includes optional community tools for announcements, polls, events, welcome messages and self-serve roles, plus a web dashboard.

The ticket prompts ask for reproducible troubleshooting details while reminding members not to share passwords or payment details. Configure a staff role and ticket category in `/manage setup`, then post the panel with `/manage ticket panel` in the support channel.

Everything in Discord is one slash-command group, `/manage`. Type `/manage` to see it:

```
/manage announce            post an announcement now
/manage schedule            post an announcement at a later time
/manage repeat              post an announcement again and again
/manage scheduled           list what is waiting to go out
/manage cancel              cancel a scheduled or repeating announcement
/manage history             the most recent announcements
/manage template save|list|delete|use    reusable announcement text
/manage poll create|list|close           polls with vote buttons, single or multiple choice
/manage event create|list|cancel         events with an RSVP reaction and reminders
/manage ticket panel|list|claim|assign|priority|report|close|reopen    support tickets
/manage faq save|list|delete|use         saved troubleshooting replies
/manage rolemenu new|add|remove|post|list|delete    a dropdown that gives and takes roles
/manage reactionrole add|list|remove    give a role when someone reacts to a message
/manage welcome set|off|test             the message posted when someone joins
/manage edit                 change the text of a post the bot made
/manage delete               delete a post the bot made
/manage queue               announcements waiting for approval
/manage approve | reject     approve or turn down a queued announcement
/manage quiet               show, set or clear quiet hours, and whether they hold posts by hand
/manage pins                posts that are pinned right now, and when they unpin
/manage status              this server's settings and what is pending
/manage audit               who did what, newest first
/manage setup               staff role, channels and timezone         (administrators)
/manage fixcommands         re-sync the commands                      (administrators)
```

---

## 1. What it does

* **Announcements are embeds.** The heading and message are shown in an embed,
  with the author in the footer. An optional role can be pinged, and that is
  the only ping the bot sends. `@everyone`, `@here` and individual users are
  never pinged, even if someone types them.
* **Scheduling.** `/manage schedule` takes a delay (`30m`, `2h`, `1d12h`; a bare
  number means minutes) or a local time (`2026-10-20 18:00`, read in the
  server's timezone). The scheduler checks every 30 seconds.
* **Repeating announcements.** `/manage repeat` posts again every interval (at
  least 15 minutes, at most 365 days). A repeat that was missed while the bot
  was offline is skipped, not posted in a burst. A repeat can be pinned too, with
  `pin_for`; each copy is pinned for that long, so the pin must be shorter than the
  gap between posts, and only one copy is pinned at a time. A repeat stays queued until
  you cancel it.
* **Templates.** Save text once with `/manage template save`, then post it with
  `/manage template use`. Names are unique per server, ignore case, and use
  letters, numbers, spaces, `-` and `_` (up to 40 characters).
* **Polls.** Two to ten options, each with its own vote button. Pick one option,
  or let members pick several (`multiple` on `/manage poll create`, or the
  checkbox on the dashboard). In a single-choice poll a new vote replaces the old
  one. Results on the poll update as people vote. A poll closes when you run
  `/manage poll close`, or by itself after the duration you set (1 minute to
  30 days). `send_at` (on `/manage poll create`, or *Post at* on the dashboard)
  posts the poll later instead, such as `2h` or `2026-10-24 18:00`. The duration
  counts from when it is posted, and you can cancel a scheduled poll with
  `/manage poll close` before it goes out. Quiet hours do not hold scheduled
  polls. The final counts are kept and posted on the poll. Polls made before
  this version still count by reaction. The dashboard's Polls tab shows live
  results, with a bar per option and who chose what.
* **Tickets.** `/manage ticket panel` posts a panel with **Technical issue**,
  **Account help** and **General question** buttons. Each creates a private
  channel that only the member, staff role and bot can see; each member can have
  one open ticket at a time. Staff can `/manage ticket claim` to take an
  unassigned ticket, or `/manage ticket assign member:@Staff` to assign or
  reassign it. Both commands accept an optional ticket number; omit it when
  running them inside the ticket channel. `/manage ticket list` shows category
  and assignee. Members can press *Request urgent review* if the issue is urgent;
  staff can set or clear priority with `/manage ticket priority`, and urgent tickets
  are highlighted in lists and staff logs. `/manage ticket report` summarizes
  ticket volume, reopened tickets, urgent open work, and average time to the first
  staff reply over 7, 30, or 90 days. Staff can close with the *Close ticket* button or
  `/manage ticket close` (optionally include a resolution reason); the reason is
  recorded in the audit log and staff log. A closed ticket sends the opener a DM
  asking whether the issue was resolved. The opener can reopen it with
  `/manage ticket reopen ticket_id:123`, optionally adding a note; staff can
  reopen any closed ticket. The ticket keeps its number and category, creates a
  fresh private channel, and clears its previous assignee. Each member can have
  one open ticket at a time. Set the Discord category new channels go in with
  `/manage setup ticket_category:` or on the dashboard.
  **Transcripts.** Before the channel is deleted, the bot saves what was said in
  it: each message with its time, author and attachment links. Every closure is
  retained as a transcript version; the latest is posted to the staff log and kept
  for the dashboard's Tickets tab, where staff can download it. A ticket that
  could not be read is still closed, and the staff log says no transcript was kept.
  Set *Keep local transcripts for* to 0 (forever) or 1–3650 days in the dashboard
  or `/manage setup transcript_retention_days`. Expired local copies and download
  links are removed automatically; files already posted to Discord's staff-log
  channel are not deleted automatically.
  **Idle close.** Set *Close idle tickets after* on the dashboard (in days, 0 for
  never). A ticket with no new messages for that long is closed the same way, and
  the bot says why in the ticket before it goes. Messages from members and staff
  reset the clock. The bot's own messages do not.
* **Support hours.** Set daily opening and closing times on the dashboard or with
  `/manage setup support_start:09:00 support_end:17:00`, using the server timezone.
  Overnight windows are supported. Tickets opened or reopened outside those hours
  include the configured after-hours message; leave both times blank for always-on
  support. Administrators can pass `off` to either setup option to clear both.
* **Saved support replies.** `/manage faq save key title response` creates or
  updates a reusable troubleshooting response; `/manage faq list` shows the saved
  keys, `delete` removes one, and `/manage faq use` sends it into the current open ticket.
  Saved replies can only be sent from a ticket channel, and mentions in reply text
  do not ping members.
* **Resolution feedback.** After a ticket closes, the bot privately asks its
  opener whether the issue was resolved. The response is recorded for staff; if
  the opener selects *Not yet*, they can reopen the same ticket.
* **Reaction roles.** `/manage reactionrole add` links an emoji on a message to a
  role. The bot adds the emoji to that message, and members who press it get the
  role. Taking the reaction away takes the role away too. Give the message as a
  link (*Copy Message Link*), or as an ID with its channel. The bot's role must be
  above the role it gives. `/manage reactionrole remove` stops the emoji giving
  roles, and roles people already have are kept.
* **Role menus.** Use these when members should choose their own roles. A menu is
  one message with a dropdown. `/manage rolemenu new`, then `add` (up to 25 roles,
  each with an optional label and note), then `post`. A member picks the roles they
  want, and any menu role they leave unticked is taken away. Roles outside the menu
  are never touched. Use *reaction roles* for a single role on one message. Use a role menu
  for a list. Changing a menu's roles updates the posted message. Deleting the menu
  removes the message, and people keep the roles they already have.
* **Events.** Members RSVP with a ✅ reaction. The start must be at least five
  minutes away. If you set a reminder (5 minutes to 7 days before the start),
  the bot posts it once and mentions up to 50 people who RSVP'd.
* **Welcome messages.** When someone joins, the bot posts your welcome in the
  channel you choose and mentions the new member. The text can use `{mention}`,
  `{name}`, `{server}` and `{members}`. This feature needs the *Server Members*
  intent (see [Invite the bot](#invite-the-bot)).
* **Approval queue.** When *Require approval* is on in `/manage setup` (or the
  dashboard's Settings), announcements and scheduled posts wait in `/manage queue`
  until someone else approves them. A request still waiting after a day is mentioned
  in the staff log, once a day until it is decided. Nobody can approve their own request. The
  dashboard signs in as one shared identity, so a request made on the dashboard must
  be approved in Discord, and a request made in Discord can be approved on the
  dashboard's Approvals tab. Turn a request down with `/manage reject` or the same tab.
* **Pinning and auto-unpin.** `pin_for` on `/manage announce`, `/manage schedule`
  and `/manage repeat` pins the post for a set time, from 5 minutes to 30 days, then
  unpins it. `/manage pins` lists what is pinned now. If the pin can not be made, the
  post still goes out and the reply says so.
* **Edit and delete.** `/manage edit` changes the heading or text of a post the bot
  made, and `/manage delete` removes it. Copy the message ID with Discord's *Copy
  Message ID*. Only posts the bot recorded can be changed, so a message staff typed by
  hand is refused. A deleted post is also removed from `/manage history`.
* **Quiet hours.** `/manage quiet start:22:00 end:08:00` (in the server's timezone) holds
  scheduled and repeating posts that come due in the window, and posts them when it
  ends. A post made by hand with `/manage announce` or the dashboard's Announce tab
  goes out at once, unless you also set `hold_manual:True`. Then those are saved and
  posted when the window ends. `/manage quiet start:off` clears the window, and a
  window can cross midnight.
* **Publish to followers.** In an announcement (news) channel, tick *publish*
  and the message is also sent to every server that follows the channel.
* **Failures are not silent.** If a scheduled post can not go out (channel
  deleted, missing permission), it is marked `failed` with the reason, and the
  staff log says so. A poll whose message was deleted closes with zero votes.
* **Staff log.** Every announcement post, and every failed scheduled post, is
  also written to the staff log channel, if one is set.
* **Audit log.** Every change staff make, from Discord or from the dashboard,
  is recorded with who did it, when and what. `/manage audit` and the dashboard
  show it. The scheduler's own work is recorded as the *system*.
* **Audit export.** The dashboard's Audit log tab downloads the whole log as CSV,
  oldest first. Cells that start with `=`, `+`, `-` or `@` get a leading apostrophe,
  so a spreadsheet will not run them as formulas.
* **History and overview.** `/manage history` lists posted announcements, and
  `/manage status` (or the dashboard overview) shows counts of what is queued,
  open and upcoming.

## 2. Setup

### Requirements

* Python **3.9 or newer** (developed and tested on 3.11).
* A Discord application with a bot user: <https://discord.com/developers/applications>.
* Node.js is only needed to run the dashboard's JavaScript check in CI. The bot
  itself does not need it.

### Invite the bot

In the Developer Portal, open the bot's page:

* Under *Bot → Privileged Gateway Intents*, switch on **Server Members Intent**
  and **Message Content Intent**. Members is for welcome messages. Message Content
  lets the bot save ticket transcripts, and it does not act on what members write.
  Discord refuses to start the bot if either privileged intent is switched off here
  while the bot asks for it.
* Under *OAuth2 → URL Generator*, select the scopes **`bot`** and
  **`applications.commands`**. Give the bot these permissions in the channels it
  will use:
  * View Channel, Send Messages, Embed Links
  * Read Message History and Add Reactions (polls, events and reaction roles)
  * Manage Channels (tickets), and Manage Roles (reaction roles and role menus)
  * Publish to followers needs the bot to be able to send in that news channel

Invite it to your server with the generated URL.

### Give it a token

Either export the token:

```bash
export DISCORD_TOKEN="your-bot-token"
```

or put it in `data/config.json` (created on first run; it is git-ignored):

```json
{
  "bot_token": "your-bot-token",
  "default_timezone": "America/Toronto"
}
```

`DISCORD_TOKEN` wins if both are set. The repository's `config.json` is a
template and is never written to, so a token can not get committed by accident.

### Run it

macOS / Linux:

```bash
scripts/run_linux.sh
```

Windows:

```bat
scripts\run_windows.bat
```

Both create a virtual environment, install `requirements.txt`, and start the
bot. To run it by hand: `python -m pip install -r requirements.txt`, then
`python bot.py`.

### Configure your server

Run this once, as an administrator:

```
/manage setup staff_role:@Staff announcement_channel:#announcements staff_channel:#staff-log timezone:America/Toronto
```

Every option is optional, and only the ones you pass are changed.
`timezone` takes an IANA name such as `America/Toronto`, `Europe/London` or
`Australia/Sydney`. Welcome messages are set with `/manage welcome set`.
`/manage status` shows what is set.

## 3. Who can use what

| Command | Who |
|---------|-----|
| Staff tools under `/manage` (including FAQ management and ticket claim/assign/close) | Holders of the **staff role**, and administrators |
| `/manage ticket reopen` | The ticket opener, support staff, or administrators |
| `setup`, `fixcommands` | Administrators |
| The dashboard | Anyone with the dashboard token (see [section 4](#4-the-dashboard)) |

The check runs again every time a command is used, so a Discord integration
that shows the commands to more people does not give them access.

**Before a staff role is set,** the staff commands fall back to *Manage
Server*, so a fresh install still works. The refusal message tells you to run
`/manage setup staff_role:` once.

**Holding *Manage Server* alone does not count** once a staff role is set. A
server can grant that permission to anyone, and staff membership is meant to be
explicit.

## 4. The dashboard

The bot starts a web dashboard in the same process. It lists your servers and
offers the same actions as the commands: announce, schedule and repeat, manage
templates, polls (with live results and scheduled polls), tickets (with transcripts
and idle close), reaction roles, role menus and events, set
welcome messages, change settings, and read the audit log. Every button calls the same code as its command, so the two can
not drift apart.

### Get the token and sign in

```bash
python bot.py --dashboard-token
```

This prints the token, creating one on first use and saving it in
`data/config.json` (the file is written with owner-only permissions). Open
`http://127.0.0.1:8765` on the bot's machine and paste it in. The Discord bot
token does not work on the dashboard, and the dashboard token does not work on
Discord.

### Settings

| Key in `data/config.json` | Default | What it does |
|---------------------------|---------|--------------|
| `dashboard_enabled` | `true` | Set to `false` to run the bot without the dashboard. |
| `dashboard_host` | `127.0.0.1` | Address to listen on. Loopback means only this machine can reach it. |
| `dashboard_port` | `8765` | Port to listen on. |
| `dashboard_token` | created on first use | The sign-in token. Change it by editing the file and restarting. |
| `dashboard_secure_cookie` | `false` | Set to `true` when the dashboard is served over HTTPS, so the session cookie is only sent over HTTPS. |
| `dashboard_allowed_hosts` | `[]` | Extra host names the dashboard answers to, e.g. `["staff.example.com"]`. Loopback names are always allowed. Requests with any other `Host` header get a 400. |
| `dashboard_trusted_proxies` | `[]` | Addresses of reverse proxies, e.g. `["127.0.0.1"]`. Their `X-Forwarded-For` header is used to tell visitors apart for the login throttle. Without this, every visitor looks like the proxy. |

### Remote access over SSH

Keep the dashboard on `127.0.0.1` and reach it through an SSH tunnel. Nothing
new is opened on the server, and the port never faces the internet.

1. On the bot's machine, start the bot as usual (`python bot.py`).
2. On your own computer, open a tunnel. Replace `user` and `server.example.com`
   with your SSH login and the server's address:

   ```bash
   ssh -N -L 8765:127.0.0.1:8765 user@server.example.com
   ```

   `-N` means no remote shell, only the tunnel. Leave the window open while you
   use the dashboard. Windows 10 and later include this `ssh` command.
3. Open `http://localhost:8765` in your browser and paste the token.

If port 8765 is already in use on your computer, pick another local port, such
as `-L 9000:127.0.0.1:8765`, and open `http://localhost:9000`. Use `localhost`
rather than an IP address, since loopback names are the ones the dashboard
accepts. You do not need to change `dashboard_allowed_hosts` or
`dashboard_secure_cookie` for a tunnel. The browser talks plain HTTP to your
own machine, and SSH carries the traffic to the server.

### Security

* The session is an **HttpOnly, SameSite=Strict** cookie. Every change also
  needs an `X-CSRF-Token` header that matches the session.
* Failed sign-ins are throttled per client address (after 8 failures in 10
  minutes, sign-in is refused for a while).
* The `Host` header must be a name you have allowed (loopback by default). This
  blocks DNS-rebinding attacks.
* The page sends a strict Content-Security-Policy, and no inline scripts.
* The default is to listen on `127.0.0.1` only. To reach it from another
  machine, use an SSH tunnel, or put a reverse proxy that terminates HTTPS in
  front of it and list the proxy in `dashboard_trusted_proxies`. Do not open the
  port to the internet directly.

### Demo mode

```bash
python bot.py --demo
```

`--demo` runs the dashboard over two simulated servers, with no token, no
Discord connection and no real data. Posts, polls, reactions and reminders all
happen in memory. The database is a temporary directory unless
`SOUNDTRAIL_SUPPORT_DATA` is set. To try it from another machine (for example in a
preview), add `--host 0.0.0.0 --port 8765`. Do not run demo mode on a real
install.

## 5. Times

| You type | Meaning |
|----------|---------|
| `30m`, `2h`, `1d12h`, `45 seconds` | That long from now |
| `15` | 15 minutes from now (bare numbers are minutes) |
| `2026-10-20 18:00` | 18:00 on that date, in the server's timezone |
| `2026-10-20T18:00` | Same, with a `T` between date and time |

A scheduled time must be at least one minute away and at most 365 days away.
Scheduled polls use the same times.
Replies show the time as a Discord timestamp, so each person sees it in their
own timezone. The dashboard shows times in the server's timezone.

## 6. Limits

| Item | Limit |
|------|-------|
| Announcement heading | 256 characters |
| Announcement message | 4000 characters |
| Template name | 40 characters |
| Poll question | 256 characters; 2 to 10 options, 100 characters each |
| Poll duration | 1 minute to 30 days |
| Open tickets | one per member |
| Idle ticket close | 0 (never), or 1 to 365 days |
| Role menu | up to 25 roles; title 256 characters; text above 1000 characters; label and note 100 characters each |
| Ticket transcript | the first 1000 messages per closure; locally retained forever by default, or 1–3650 days; staff-log attachments are not auto-deleted |
| Saved support reply | key 1–40 characters; title up to 100; response up to 1800 |
| Repeat interval | 15 minutes to 365 days |
| Event title | 256 characters; details 1000 characters |
| Event reminder | 5 minutes to 7 days before the start (0 for none); mentions up to 50 people |
| Welcome message | 1000 characters |
| Pin time (`pin_for`) | 5 minutes to 30 days; for a repeat, shorter than the gap between posts |
| Quiet hours | Both a start and an end, e.g. `22:00` and `08:00`; clear both with `off` |
| Lists in Discord | up to 25 entries (`history`, `scheduled`, `poll list`, `event list`) |

## 7. Repository layout

```
bot.py              entry point, the /manage cog, the scheduler loop, command-line options
command_tree.py     the /manage group and the administrator check
operations.py       every staff action (commands and dashboard both call these)
announcements.py    time parsing, announcement embeds, posting rules, history, templates
polls.py            poll options, button and reaction votes, vote counting, poll storage
components.py      the buttons for polls and tickets, which keep working after a restart
tickets.py         ticket channel names, panel, transcripts, close/reopen, feedback
faqs.py            per-server saved support replies
role_menus.py      role menus: the dropdown, its roles, and what a pick changes
reaction_roles.py  emoji and message parsing for reaction roles, and their storage
approvals.py        the approval queue: who asked, who decided, reminders
pins.py             pins that are waiting to be removed
quiet.py            quiet-hours clock times and the overnight window
events.py           event checks, RSVP embeds, reminders, event storage
welcome.py          welcome message templates and placeholders
audit.py            the audit log and the Actor who did each action
discord_gateway.py  the live Discord adapter (discord.py)
demo.py             demo mode: simulated servers and an in-memory Discord
dashboard.py        the dashboard web server and its JSON API
dashboard.html      the dashboard page
static/             dashboard.js and dashboard.css (no build step)
settings.py         per-server config and the staff permission rule
store.py            SQLite access and the schema
paths.py            where the database, log and live config are kept
config.json         template config (no token; copied to data/ on first save)
VERSION             the version number reported by --version
requirements.txt    discord.py, aiohttp and tzdata
scripts/            run_linux.sh and run_windows.bat launchers
tests/              unit tests, including the dashboard API (no Discord connection needed)
.github/workflows/  CI: lint, check the dashboard script, run the tests
```

```bash
python -m pytest -q                       # full test suite
python tests/test_operations.py           # one file, without pytest
node --check static/dashboard.js          # syntax check for the dashboard script
python bot.py --paths                     # where this install keeps its files
python bot.py --version
python bot.py --check                     # check the configuration, do not connect
```

Set `SOUNDTRAIL_SUPPORT_DATA=/some/dir` to keep the database, log and config
somewhere else.

## 8. Troubleshooting

* **Commands do not appear.** The bot syncs its commands to each server when it
  starts and when it joins one. Check `data/bot.log` for a `Synced N command(s)`
  line. Run `/manage fixcommands` to re-sync, and restart Discord (Ctrl+R) if the
  list still looks stale.
* **Commands appear twice.** An older copy of the bot may still be running with
  the same token. Stop it, then run `/manage fixcommands`.
* **"Login failed".** The token is wrong or was reset in the Developer Portal.
  Put the new one in `DISCORD_TOKEN` or `data/config.json`.
* **Welcome messages are not sent.** Turn on the *Server Members* intent in the
  Developer Portal and restart the bot.
* **"I can't send messages in #channel" / "I need Embed Links".** Grant the bot
  View Channel, Send Messages and Embed Links in that channel, or in its category.
* **A role shows in the announcement but does not notify.** The role is not
  mentionable. Turn on *Allow anyone to @mention this role* for it, or grant the
  bot *Mention All Roles*.
* **A scheduled announcement is marked failed.** Run `/manage scheduled` or read
  the staff log: the reason is in the entry. Schedule it again once it is fixed.
* **"Only members with the @Staff role can use this command."** Give the role to
  the member, or have an administrator check `/manage status`.
* **The dashboard says "Unrecognized Host header".** You are reaching it under a
  name that is not loopback. Add that name to `dashboard_allowed_hosts`.
* **The dashboard says "The page is out of date".** The session changed in
  another tab. Reload the page.
* **The dashboard does not open.** Check `data/bot.log`. A port that is already
  in use is reported there. Change `dashboard_port`, or set `dashboard_enabled`
  to `false`.

## 9. Not included

Soundtrail Support is designed for support conversations and community operations in the Soundtrail Discord server. It does not include moderation punishments or warnings, applications, rules acceptance, music, voice, or a streaming dashboard.

## 10. License

MIT. See [LICENSE](LICENSE).
