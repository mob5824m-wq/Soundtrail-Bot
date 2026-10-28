/* Soundtrail Support dashboard.
 *
 * Plain JavaScript, no build step. Every request is relative (api/...) so the
 * page works behind a path prefix or a reverse proxy. Nothing from the server
 * is written with innerHTML: all text goes through textContent.
 */
(() => {
  "use strict";

  const state = {
    csrf: null,
    demo: false,
    guilds: [],
    guildId: null,
    snapshot: null,
    settings: null,
    timezone: "UTC",
    tab: "overview",
    templateName: null,
    pollDetailId: null,
    pollTimer: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  // ------------------------------------------------------------------ //
  // Small helpers
  // ------------------------------------------------------------------ //
  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of [].concat(children)) {
      if (child === undefined || child === null) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function notice(message, kind = "ok") {
    const box = $("#notice");
    box.textContent = message;
    box.className = `notice ${kind === "err" ? "err" : "ok"}`;
    box.hidden = false;
    clearTimeout(notice.timer);
    notice.timer = setTimeout(() => { box.hidden = true; }, kind === "err" ? 10000 : 5000);
  }

  // Form values: an empty field is sent as null so the server clears it.
  function opt(formData, key) {
    const value = String(formData.get(key) ?? "").trim();
    return value === "" ? null : value;
  }

  function optNumber(formData, key) {
    const value = opt(formData, key);
    return value === null ? null : Number(value);
  }

  function withWarnings(message, result) {
    const warnings = (result && result.warnings) || [];
    return [message, ...warnings].join(" ");
  }

  function fmtTime(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    try {
      return new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: state.timezone,
      }).format(date);
    } catch (_) {
      return date.toLocaleString();
    }
  }

  function humanInterval(seconds) {
    for (const [name, size] of [["day", 86400], ["hour", 3600], ["minute", 60]]) {
      if (seconds >= size && seconds % size === 0) {
        const count = seconds / size;
        return `${count} ${name}${count === 1 ? "" : "s"}`;
      }
    }
    return `${seconds} seconds`;
  }

  function channelName(id) {
    if (!id || !state.snapshot) return String(id || "");
    const channel = state.snapshot.channels.find((c) => c.id === String(id));
    return channel ? `#${channel.name}` : `#${id}`;
  }

  function truncate(text, length = 300) {
    const value = String(text || "");
    return value.length > length ? `${value.slice(0, length)}…` : value;
  }

  function renderList(container, rows, rowFn, emptyText) {
    if (!rows || rows.length === 0) {
      container.replaceChildren(el("li", { class: "empty", text: emptyText }));
      return;
    }
    container.replaceChildren(...rows.map(rowFn));
  }

  // Run one action: disable its button, show the outcome, refresh the view.
  async function runAction(button, work, message, after) {
    if (button) button.disabled = true;
    try {
      const result = await work();
      if (message) notice(typeof message === "function" ? message(result) : message, "ok");
      if (after) await after(result);
      return result;
    } catch (err) {
      notice(err.message || "Something went wrong.", "err");
      return null;
    } finally {
      if (button) button.disabled = false;
    }
  }

  function submitForm(form, work, message, after) {
    return runAction(form.querySelector('button[type="submit"]'), work, message, after);
  }

  // ------------------------------------------------------------------ //
  // API
  // ------------------------------------------------------------------ //
  async function api(path, { method = "GET", body, retried = false } = {}) {
    const headers = { Accept: "application/json" };
    const init = { method, headers, credentials: "same-origin", cache: "no-store" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    if (method !== "GET") headers["X-CSRF-Token"] = state.csrf || "";

    let response;
    try {
      response = await fetch(`api/${path}`, init);
    } catch (_) {
      throw new Error("Could not reach the bot. Check that it is still running.");
    }
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      data = null;
    }
    if (response.status === 403 && !retried && path !== "session") {
      // The CSRF token changes when the bot restarts. Fetch the new one and
      // retry once rather than asking for a reload.
      const session = await api("session");
      if (session.csrf) {
        state.csrf = session.csrf;
        return api(path, { method, body, retried: true });
      }
    }
    if (!response.ok) {
      throw new Error((data && data.error) || `Request failed (${response.status}).`);
    }
    return data;
  }

  // ------------------------------------------------------------------ //
  // Startup and servers
  // ------------------------------------------------------------------ //
  // There is no sign-in: the page is only reachable from this machine or an
  // SSH tunnel (see dashboard.py), so it opens straight into the app.
  async function boot() {
    const session = await api("session");
    state.csrf = session.csrf;
    state.demo = Boolean(session.demo);
    $("#demo-badge").hidden = !state.demo;
    $("#version").textContent = session.version ? `v${session.version}` : "";
    await loadGuilds();
  }

  async function loadGuilds() {
    state.guilds = await api("guilds");
    const select = $("#guild-select");
    select.replaceChildren(...state.guilds.map((g) => el("option", { value: g.id, text: g.name })));
    select.disabled = state.guilds.length === 0;
    if (state.guilds.length === 0) {
      notice("The bot is not in any server yet. Invite it to a server, then reload this page.", "err");
      return;
    }
    await selectGuild(state.guilds[0].id);
  }

  $("#guild-select").addEventListener("change", (event) => selectGuild(event.currentTarget.value));

  async function selectGuild(guildId) {
    state.guildId = String(guildId);
    state.pollDetailId = null;
    $("#poll-detail").hidden = true;
    $("#guild-select").value = state.guildId;
    try {
      const [snapshot, settings] = await Promise.all([
        api(`guilds/${state.guildId}/snapshot`),
        api(`guilds/${state.guildId}/settings`),
      ]);
      state.snapshot = snapshot;
      state.settings = settings;
      state.timezone = settings.timezone || "UTC";
      fillSelects();
      fillSettingsForm();
      fillWelcomeForm();
      await refreshTab();
    } catch (err) {
      notice(err.message, "err");
    }
  }

  async function reloadSettings() {
    state.settings = await api(`guilds/${state.guildId}/settings`);
    state.timezone = state.settings.timezone || "UTC";
    fillSettingsForm();
    fillWelcomeForm();
  }

  // ------------------------------------------------------------------ //
  // Dropdowns and form fill-in
  // ------------------------------------------------------------------ //
  function blankLabel(form, kind) {
    const id = form ? form.id : "";
    if (id === "welcome-form") return "Choose a channel";
    if (id === "settings-form") return "Not set";
    return kind === "role" ? "No role" : "Server default";
  }

  function fillSelects() {
    const snapshot = state.snapshot;
    for (const select of $$("select.channel-select")) {
      const form = select.closest("form");
      const options = [el("option", { value: "", text: blankLabel(form, "channel") })];
      for (const channel of snapshot.channels) {
        options.push(el("option", {
          value: channel.id,
          text: `#${channel.name}${channel.news ? " (announcements)" : ""}`,
        }));
      }
      replaceOptions(select, options);
    }
    for (const select of $$("select.role-select")) {
      const form = select.closest("form");
      const options = [el("option", { value: "", text: blankLabel(form, "role") })];
      for (const role of snapshot.roles) {
        options.push(el("option", { value: role.id, text: `@${role.name}` }));
      }
      replaceOptions(select, options);
    }
    for (const select of $$("select.category-select")) {
      const options = [el("option", { value: "", text: blankLabel(select.closest("form"), "category") })];
      for (const category of snapshot.categories || []) {
        options.push(el("option", { value: category.id, text: category.name }));
      }
      replaceOptions(select, options);
    }
  }

  // Rebuilding a dropdown must not lose what is chosen in it. Other forms share these
  // lists, so posting an announcement used to reset the settings form's channel to "Not set".
  function replaceOptions(select, options) {
    const chosen = select.value;
    select.replaceChildren(...options);
    if (options.some((option) => option.value === chosen)) select.value = chosen;
  }

  function fillSettingsForm() {
    const form = $("#settings-form");
    const s = state.settings;
    if (!s) return;
    form.elements.staff_role_id.value = s.staff_role_id ? String(s.staff_role_id) : "";
    form.elements.announcement_channel_id.value = s.announcement_channel_id ? String(s.announcement_channel_id) : "";
    form.elements.staff_channel_id.value = s.staff_channel_id ? String(s.staff_channel_id) : "";
    form.elements.timezone.value = s.timezone || "";
    form.elements.require_approval.checked = Boolean(s.require_approval);
    form.elements.quiet_start.value = s.quiet_start || "";
    form.elements.quiet_end.value = s.quiet_end || "";
    form.elements.quiet_hold_manual.checked = Boolean(s.quiet_hold_manual);
    form.elements.ticket_category_id.value = s.ticket_category_id ? String(s.ticket_category_id) : "";
    form.elements.ticket_auto_close_days.value = String(s.ticket_auto_close_days || 0);
    form.elements.ticket_transcript_retention_days.value = String(s.ticket_transcript_retention_days || 0);
    form.elements.support_hours_start.value = s.support_hours_start || "";
    form.elements.support_hours_end.value = s.support_hours_end || "";
    form.elements.after_hours_message.value = s.after_hours_message || "";
  }

  function fillWelcomeForm() {
    const form = $("#welcome-form");
    const s = state.settings;
    if (!s) return;
    form.elements.channel_id.value = s.welcome_channel_id ? String(s.welcome_channel_id) : "";
    form.elements.message.value = s.welcome_message || "";
    form.elements.message.placeholder = s.default_welcome_message || "";
    $("#welcome-status").textContent = s.welcome_channel_id
      ? `Welcome messages are on in ${channelName(s.welcome_channel_id)}.`
      : "Welcome messages are off. Choose a channel and save to turn them on.";
    $("#welcome-placeholders").textContent =
      `Placeholders: ${(s.placeholders || []).join("  ")}. Leave the message empty to use the default: ${s.default_welcome_message || ""}`;
  }

  // ------------------------------------------------------------------ //
  // Tabs
  // ------------------------------------------------------------------ //
  $("#tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (button) showTab(button.dataset.tab);
  });

  function showTab(name) {
    state.tab = name;
    for (const button of $$("#tabs button")) {
      button.classList.toggle("active", button.dataset.tab === name);
    }
    for (const panel of $$("[data-panel]")) {
      panel.hidden = panel.dataset.panel !== name;
    }
    if (name === "polls") startPollRefresh();
    else stopPollRefresh();
    refreshTab();
  }

  const LOADERS = {
    overview: loadOverview,
    announce: loadAnnounceHistory,
    scheduled: loadScheduled,
    approvals: loadApprovals,
    templates: loadTemplates,
    polls: loadPolls,
    tickets: loadTickets,
    roles: async () => {
      await loadRoles();
      await loadRoleMenus();
    },
    events: loadEvents,
    welcome: async () => {},
    settings: async () => {},
    audit: loadAudit,
  };

  async function refreshTab() {
    if (!state.guildId) return;
    try {
      await LOADERS[state.tab]();
    } catch (err) {
      notice(err.message, "err");
    }
  }

  // ------------------------------------------------------------------ //
  // Overview
  // ------------------------------------------------------------------ //
  async function loadOverview() {
    const data = await api(`guilds/${state.guildId}/overview`);
    const s = data.stats;
    const cards = [
      ["Scheduled", s.scheduled],
      ["Repeating", s.repeats],
      ["Templates", s.templates],
      ["Open polls", s.polls_open],
      ["Upcoming events", s.events_upcoming],
      ["Awaiting approval", s.approvals_pending],
      ["Pinned", s.pins_active],
      ["Welcome", s.welcome_enabled ? "On" : "Off"],
      ["Open tickets", s.tickets_open],
      ["Reaction roles", s.reaction_roles],
    ];
    $("#stats").replaceChildren(...cards.map(([label, value]) =>
      el("div", { class: "stat" }, [
        el("div", { class: "value", text: value }),
        el("div", { class: "label", text: label }),
      ])));
    renderList($("#overview-recent"), data.recent, announcementRow, "Nothing has been posted yet.");
  }

  function announcementRow(row) {
    const buttons = [];
    if (row.message_id) {
      const edit = el("button", { type: "button", class: "ghost small", text: "Edit" });
      edit.addEventListener("click", () => {
        if (state.tab !== "announce") showTab("announce");
        openEdit(row);
      });
      const remove = el("button", { type: "button", class: "danger small", text: "Delete" });
      remove.addEventListener("click", () => {
        if (!window.confirm(`Delete “${row.title}” from Discord? This can not be undone.`)) return;
        runAction(remove, () => api(`guilds/${state.guildId}/announcements/${row.message_id}`, { method: "DELETE" }),
          "Deleted the announcement.", refreshTab);
      });
      buttons.push(edit, remove);
    }
    return el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: row.title }),
        el("div", {
          class: "meta",
          text: `${fmtTime(row.created_at)} · ${row.source} · ${channelName(row.channel_id)} · by ${row.author_name || "staff"}`,
        }),
        el("div", { class: "body", text: truncate(row.body) }),
      ]),
      ...buttons,
    ]);
  }

  // Editing a posted announcement reuses the form on the Announce tab.
  function openEdit(row) {
    const form = $("#edit-form");
    form.hidden = false;
    form.elements.message_id.value = String(row.message_id);
    form.elements.title.value = row.title || "";
    form.elements.message.value = row.body || "";
    $("#edit-where").textContent = `in ${channelName(row.channel_id)}`;
    form.scrollIntoView({ block: "center" });
    form.elements.message.focus();
  }

  function closeEdit() {
    const form = $("#edit-form");
    form.reset();
    form.hidden = true;
  }

  function heldNotice(result) {
    return `Quiet hours are on, so this is held as #${result.schedule_id} and posts ${fmtTime(result.send_at)}.`;
  }

  function approvalNotice(result) {
    return `Sent for approval as #${result.approval_id}. Someone other than you can approve it in the Approvals tab.`;
  }

  $("#edit-cancel").addEventListener("click", closeEdit);

  $("#edit-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/announcements/${f.get("message_id")}`, {
        method: "PATCH",
        body: { title: f.get("title"), message: f.get("message") },
      }),
      "Saved the changes.",
      async () => { closeEdit(); await refreshTab(); },
    );
  });

  // ------------------------------------------------------------------ //
  // Announce now
  // ------------------------------------------------------------------ //
  $("#announce-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/announce`, {
        method: "POST",
        body: {
          title: opt(f, "title"),
          message: f.get("message"),
          channel_id: opt(f, "channel_id"),
          mention_role_id: opt(f, "mention_role_id"),
          publish: f.has("publish"),
          pin_for: opt(f, "pin_for"),
        },
      }),
      (r) => (r.queued ? approvalNotice(r) : r.held ? withWarnings(heldNotice(r), r) : withWarnings(
        `Posted${r.jump_url ? `: ${r.jump_url}` : "."}${r.pinned_until ? ` Pinned until ${fmtTime(r.pinned_until)}.` : ""}`,
        r,
      )),
      async () => { form.reset(); fillSelects(); await loadAnnounceHistory(); },
    );
  });

  async function loadAnnounceHistory() {
    const rows = await api(`guilds/${state.guildId}/history?limit=10`);
    renderList($("#announce-history"), rows, announcementRow, "Nothing has been posted yet.");
  }

  // ------------------------------------------------------------------ //
  // Scheduled and repeating
  // ------------------------------------------------------------------ //
  $("#schedule-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/scheduled`, {
        method: "POST",
        body: {
          when: f.get("when"),
          repeat_every: opt(f, "repeat_every"),
          title: opt(f, "title"),
          message: f.get("message"),
          channel_id: opt(f, "channel_id"),
          mention_role_id: opt(f, "mention_role_id"),
          publish: f.has("publish"),
          pin_for: opt(f, "pin_for"),
        },
      }),
      (r) => (r.queued ? approvalNotice(r) : withWarnings(
        r.repeat_seconds
          ? `Repeating #${r.id} starts ${fmtTime(r.send_at)}.`
          : `Scheduled #${r.id} for ${fmtTime(r.send_at)}.${r.pinned ? " It will be pinned when it posts." : ""}`,
        r,
      )),
      async () => { form.reset(); fillSelects(); await loadScheduled(); },
    );
  });

  async function loadScheduled() {
    const [rows, pinned] = await Promise.all([
      api(`guilds/${state.guildId}/scheduled`),
      api(`guilds/${state.guildId}/pins`),
    ]);
    renderList($("#scheduled-list"), rows, scheduledRow, "Nothing is waiting to go out.");
    renderList($("#pin-list"), pinned, pinRow, "Nothing is pinned right now.");
  }

  function pinRow(row) {
    return el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: `Pinned post in channel ${row.channel_id}` }),
        el("div", { class: "meta", text: `unpins ${fmtTime(row.unpin_at)}` }),
      ]),
    ]);
  }

  function scheduledRow(row) {
    const kind = row.interval_seconds
      ? `every ${humanInterval(row.interval_seconds)} · posted ${row.runs} time(s) so far`
      : "once";
    const button = el("button", { type: "button", class: "danger small", text: "Cancel" });
    button.addEventListener("click", () => {
      if (!window.confirm(`Cancel #${row.id} “${row.title}”?`)) return;
      runAction(button, () => api(`guilds/${state.guildId}/scheduled/${row.id}`, { method: "DELETE" }),
        `Cancelled #${row.id}.`, loadScheduled);
    });
    return el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: `#${row.id} · ${row.title}` }),
        el("div", {
          class: "meta",
          text: `${kind} · next ${fmtTime(row.send_at)} · ${channelName(row.channel_id)}${row.publish ? " · publishes" : ""}`,
        }),
        el("div", { class: "body", text: truncate(row.body) }),
      ]),
      button,
    ]);
  }

  // ------------------------------------------------------------------ //
  // Approval queue
  // ------------------------------------------------------------------ //
  async function loadApprovals() {
    const rows = await api(`guilds/${state.guildId}/approvals`);
    renderList($("#approval-list"), rows, approvalRow, "Nothing is waiting for approval.");
  }

  function approvalRow(row) {
    const approve = el("button", { type: "button", class: "small", text: "Approve" });
    approve.addEventListener("click", () => {
      if (!window.confirm(`Approve #${row.id} and send it now?`)) return;
      runAction(approve, () => api(`guilds/${state.guildId}/approvals/${row.id}/approve`, { method: "POST" }),
        (r) => (r.held ? heldNotice(r) : r.jump_url ? `Approved and posted: ${r.jump_url}` : `Approved #${row.id}.`), loadApprovals);
    });
    const reject = el("button", { type: "button", class: "danger small", text: "Reject" });
    reject.addEventListener("click", () => {
      if (!window.confirm(`Turn down #${row.id}?`)) return;
      runAction(reject, () => api(`guilds/${state.guildId}/approvals/${row.id}/reject`, { method: "POST" }),
        `Turned down #${row.id}.`, loadApprovals);
    });
    const what = row.kind === "schedule" ? "Schedule" : "Announcement";
    return el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: `#${row.id} · ${what} · ${row.summary}` }),
        el("div", {
          class: "meta",
          text: `requested by ${row.requester_name} (${row.requester_source}) · ${fmtTime(row.created_at)}`,
        }),
        el("div", { class: "body", text: truncate(row.payload && row.payload.message) }),
      ]),
      approve,
      reject,
    ]);
  }

  // ------------------------------------------------------------------ //
  // Templates
  // ------------------------------------------------------------------ //
  $("#template-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/templates`, {
        method: "POST",
        body: { name: f.get("name"), title: opt(f, "title"), message: f.get("message") },
      }),
      (r) => `${r.updated ? "Updated" : "Saved"} template “${r.name}”.`,
      async () => { form.reset(); await loadTemplates(); },
    );
  });

  async function loadTemplates() {
    const rows = await api(`guilds/${state.guildId}/templates`);
    renderList($("#template-list"), rows, templateRow, "No templates yet. Save one above.");
  }

  function templateRow(row) {
    const useButton = el("button", { type: "button", class: "small", text: "Post…" });
    useButton.addEventListener("click", () => openTemplateUse(row.name));
    const deleteButton = el("button", { type: "button", class: "danger small", text: "Delete" });
    deleteButton.addEventListener("click", () => {
      if (!window.confirm(`Delete template “${row.name}”?`)) return;
      runAction(deleteButton, () => api(`guilds/${state.guildId}/templates/${encodeURIComponent(row.name)}`, { method: "DELETE" }),
        `Deleted template “${row.name}”.`, loadTemplates);
    });
    return el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: row.name }),
        el("div", { class: "meta", text: row.title }),
        el("div", { class: "body", text: truncate(row.body) }),
      ]),
      useButton,
      deleteButton,
    ]);
  }

  function openTemplateUse(name) {
    state.templateName = name;
    $("#template-use-name").textContent = name;
    const form = $("#template-use-form");
    form.hidden = false;
    form.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  $("#template-use-cancel").addEventListener("click", () => {
    $("#template-use-form").hidden = true;
    state.templateName = null;
  });

  $("#template-use-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    const name = state.templateName;
    if (!name) return;
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/templates/${encodeURIComponent(name)}/use`, {
        method: "POST",
        body: {
          channel_id: opt(f, "channel_id"),
          mention_role_id: opt(f, "mention_role_id"),
          publish: f.has("publish"),
        },
      }),
      (r) => withWarnings(`Posted “${name}”${r.jump_url ? `: ${r.jump_url}` : "."}`, r),
      () => { form.reset(); form.hidden = true; state.templateName = null; fillSelects(); },
    );
  });

  // ------------------------------------------------------------------ //
  // Polls
  // ------------------------------------------------------------------ //
  $("#poll-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    const options = String(f.get("options") || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/polls`, {
        method: "POST",
        body: {
          question: f.get("question"),
          options,
          duration: opt(f, "duration"),
          send_at: opt(f, "send_at"),
          channel_id: opt(f, "channel_id"),
          multiple: f.has("multiple"),
        },
      }),
      (r) => {
        if (r.scheduled) return `Poll #${r.id} is scheduled. It posts ${fmtTime(r.send_at)}.`;
        return `Poll #${r.id} posted.${r.ends_at ? ` It closes ${fmtTime(r.ends_at)}.` : ""}`;
      },
      async () => { form.reset(); fillSelects(); await loadPolls(); },
    );
  });

  async function loadPolls() {
    const rows = await api(`guilds/${state.guildId}/polls`);
    renderList($("#poll-list"), rows, pollRow, "No polls yet.");
  }

  function pollTiming(row) {
    if (row.status === "scheduled" && row.send_at) return ` · posts ${fmtTime(row.send_at)}`;
    if (row.status === "open" && row.ends_at) return ` · closes ${fmtTime(row.ends_at)}`;
    return "";
  }

  function pollRow(row) {
    const options = row.options || [];
    const results = row.results || [];
    const detail = row.status === "closed"
      ? options.map((choice, i) => `${choice}: ${results[i] ?? 0}`).join(" · ")
      : options.join(" · ");
    const children = [
      el("div", { class: "grow" }, [
        el("strong", { text: `#${row.id} · ${row.question}` }),
        el("div", {
          class: "meta",
          text: `${row.status}${pollTiming(row)} · ${channelName(row.channel_id)}`,
        }),
        el("div", { class: "body", text: detail }),
      ]),
    ];
    if (row.status === "open" || row.status === "scheduled") {
      const scheduled = row.status === "scheduled";
      const button = el("button", { type: "button", class: "small", text: scheduled ? "Cancel" : "Close now" });
      button.addEventListener("click", () => {
        if (scheduled && !window.confirm(`Cancel poll #${row.id}? It will not be posted.`)) return;
        runAction(
          button,
          () => api(`guilds/${state.guildId}/polls/${row.id}/close`, { method: "POST" }),
          (r) => (r.cancelled ? `Cancelled scheduled poll #${r.id}.` : `Closed poll #${r.id}: ${r.question}`),
          loadPolls,
        );
      });
      children.push(button);
    }
    const resultsButton = el("button", { type: "button", class: "ghost small", text: "Results" });
    resultsButton.addEventListener("click", () => openPollDetail(row.id));
    children.push(resultsButton);
    return el("li", {}, children);
  }

  // ------------------------------------------------------------------ //
  // Poll results: live while the polls tab is open
  // ------------------------------------------------------------------ //
  function openPollDetail(pollId) {
    state.pollDetailId = String(pollId);
    $("#poll-detail").hidden = false;
    loadPollDetail().catch((err) => notice(err.message, "err"));
  }

  $("#poll-detail-close").addEventListener("click", () => {
    state.pollDetailId = null;
    $("#poll-detail").hidden = true;
  });

  async function loadPollDetail() {
    if (!state.pollDetailId) return;
    const poll = await api(`guilds/${state.guildId}/polls/${state.pollDetailId}`);
    const options = poll.options || [];
    const counts = poll.counts || [];
    const total = poll.voters_count === null || poll.voters_count === undefined
      ? counts.reduce((sum, n) => sum + n, 0)
      : poll.voters_count;
    $("#poll-detail-title").textContent = `#${poll.id} · ${poll.question}`;
    $("#poll-detail-live").textContent = poll.status === "open"
      ? `Live · ${total} voter(s) · refreshes every few seconds`
      : `Final · ${total} voter(s)`;
    const rows = options.map((option, i) => {
      const count = counts[i] ?? 0;
      const share = total ? Math.round((100 * count) / total) : 0;
      const fill = el("div", { class: "poll-bar-fill" });
      fill.style.width = `${share}%`;
      return el("li", {}, [
        el("div", { class: "grow" }, [
          el("strong", { text: option }),
          el("div", { class: "meta", text: `${count} vote(s) · ${share}%` }),
          el("div", { class: "poll-bar" }, [fill]),
        ]),
      ]);
    });
    if (poll.mode !== "buttons") {
      rows.push(el("li", { class: "empty", text: "This poll was made before vote buttons. Its votes are reactions, so they are not listed by person." }));
    } else if ((poll.voters || []).length === 0) {
      rows.push(el("li", { class: "empty", text: "Nobody has voted yet." }));
    } else {
      for (const person of poll.voters) {
        rows.push(el("li", {}, [
          el("div", { class: "grow" }, [
            el("div", { class: "meta", text: `${person.name || "A member"}: ${person.choices.join(", ")}` }),
          ]),
        ]));
      }
    }
    $("#poll-detail-body").replaceChildren(...rows);
  }

  function startPollRefresh() {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(async () => {
      if (state.tab !== "polls" || document.hidden || !state.guildId) return;
      try {
        await loadPolls();
        await loadPollDetail();
      } catch (_) {
        // A missed refresh is not worth a message. The next tick tries again.
      }
    }, 5000);
  }

  function stopPollRefresh() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  // ------------------------------------------------------------------ //
  // Tickets
  // ------------------------------------------------------------------ //
  $("#ticket-panel-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/tickets/panel`, {
        method: "POST",
        body: { channel_id: opt(f, "channel_id") },
      }),
      "Ticket panel posted. Members choose a category to open a ticket.",
    );
  });

  async function loadTickets() {
    const [open, all] = await Promise.all([
      api(`guilds/${state.guildId}/tickets?open=1`),
      api(`guilds/${state.guildId}/tickets`),
    ]);
    renderList($("#ticket-list"), open, ticketRow, "No open tickets.");
    const closed = (all || []).filter((row) => row.status === "closed");
    renderList($("#ticket-closed-list"), closed, closedTicketRow, "No closed tickets yet.");
  }

  function closedTicketRow(row) {
    const feedback = row.resolution_feedback === null ? "" : ` · feedback: ${row.resolution_feedback ? "resolved" : "not resolved"}`;
    const priority = row.priority === "urgent" ? " · 🚨 urgent" : "";
    const meta = `opened by ${row.opener_name || "a member"} · ${ticketCategory(row.category)}${priority}${row.claimed_by_name ? ` · handled by ${row.claimed_by_name}` : ""}${row.closed_at ? ` · closed ${fmtTime(row.closed_at)}` : ""}${feedback}`;
    const children = [
      el("div", { class: "grow" }, [
        el("strong", { text: `Ticket #${row.id}` }),
        el("div", { class: "meta", text: meta }),
      ]),
    ];
    if (row.has_transcript) {
      children.push(el("a", {
        class: "button-link small",
        text: "Latest transcript",
        href: `api/guilds/${state.guildId}/tickets/${row.id}/transcript`,
        download: `ticket-${row.id}.txt`,
      }));
      const versions = el("button", { type: "button", class: "small", text: "Transcript history" });
      versions.addEventListener("click", async () => {
        versions.disabled = true;
        try {
          const saved = await api(`guilds/${state.guildId}/tickets/${row.id}/transcripts`);
          const links = (saved || []).map((version) => el("a", {
            class: "button-link small",
            text: `${fmtTime(version.created_at)} · ${version.message_count} message(s)`,
            href: `api/guilds/${state.guildId}/tickets/${row.id}/transcripts/${version.id}`,
            download: `ticket-${row.id}-version-${version.id}.txt`,
          }));
          versions.replaceWith(...links);
        } catch (error) {
          versions.disabled = false;
          versions.textContent = `Could not load versions: ${error.message}`;
        }
      });
      children.push(versions);
    } else {
      children.push(el("span", { class: "muted", text: "No transcript saved" }));
    }
    return el("li", {}, children);
  }

  function ticketCategory(category) {
    return ({ technical: "Technical issue", account: "Account help", general: "General question" })[category] || "General question";
  }

  function ticketRow(row) {
    const close = el("button", { type: "button", class: "danger small", text: "Close ticket" });
    close.addEventListener("click", () => {
      if (!window.confirm(`Close ticket #${row.id}? Its channel is deleted and can not be recovered.`)) return;
      const reason = window.prompt("Optional close reason (up to 300 characters):", "");
      if (reason === null) return;
      runAction(
        close,
        () => api(`guilds/${state.guildId}/tickets/${row.id}/close`, { method: "POST", body: { reason } }),
        (r) => `Closed ticket #${r.id}.`,
        loadTickets,
      );
    });
    return el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: `Ticket #${row.id}` }),
        el("div", { class: "meta", text: `${fmtTime(row.created_at)} · opened by ${row.opener_name || "a member"} · ${ticketCategory(row.category)} · ${row.priority === "urgent" ? "🚨 urgent" : "normal"} · ${row.claimed_by_name ? `handled by ${row.claimed_by_name}` : "unassigned"}` }),
      ]),
      close,
    ]);
  }

  // ------------------------------------------------------------------ //
  // Reaction roles
  // ------------------------------------------------------------------ //
  $("#role-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/reaction-roles`, {
        method: "POST",
        body: {
          message: opt(f, "message"),
          emoji: opt(f, "emoji"),
          role_id: opt(f, "role_id"),
          channel_id: opt(f, "channel_id"),
        },
      }),
      (r) => `Reaction role #${r.id} is set. Reacting ${r.emoji} on that message gives the role.`,
      async () => {
        form.reset();
        fillSelects();
        await loadRoles();
      },
    );
  });

  async function loadRoles() {
    const rows = await api(`guilds/${state.guildId}/reaction-roles`);
    renderList($("#role-list"), rows, roleRuleRow, "No reaction roles yet.");
  }

  function roleName(id) {
    const role = state.snapshot && state.snapshot.roles.find((r) => r.id === String(id));
    return role ? `@${role.name}` : `role ${id}`;
  }

  function roleRuleRow(row) {
    const remove = el("button", { type: "button", class: "danger small", text: "Remove" });
    remove.addEventListener("click", () => {
      if (!window.confirm("Remove this reaction role? Roles members already have are kept.")) return;
      runAction(
        remove,
        () => api(`guilds/${state.guildId}/reaction-roles/${row.id}`, { method: "DELETE" }),
        "Removed the reaction role.",
        loadRoles,
      );
    });
    return el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: `${row.emoji} gives ${roleName(row.role_id)}` }),
        el("div", { class: "meta", text: `message ${row.message_id} · ${channelName(row.channel_id)}` }),
      ]),
      remove,
    ]);
  }

  // ------------------------------------------------------------------ //
  // Role menus: one message with a dropdown that gives and takes roles
  // ------------------------------------------------------------------ //
  $("#menu-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/role-menus`, {
        method: "POST",
        body: { title: f.get("title"), description: opt(f, "description") },
      }),
      (r) => `Role menu #${r.id} is ready. Add roles, then post it.`,
      async () => { form.reset(); await loadRoleMenus(); },
    );
  });

  async function loadRoleMenus() {
    const rows = await api(`guilds/${state.guildId}/role-menus`);
    renderList($("#menu-list"), rows, roleMenuRow, "No role menus yet.");
  }

  // Selects for a menu row are built from the snapshot, like fillSelects does.
  function menuSelect(kind) {
    const snapshot = state.snapshot || { roles: [], channels: [] };
    const select = el("select", { class: kind === "role" ? "role-select" : "channel-select" });
    if (kind === "role") {
      select.append(el("option", { value: "", text: "Choose a role" }));
      for (const role of snapshot.roles) select.append(el("option", { value: role.id, text: `@${role.name}` }));
    } else {
      select.append(el("option", { value: "", text: "Server default" }));
      for (const channel of snapshot.channels) {
        select.append(el("option", { value: channel.id, text: `#${channel.name}` }));
      }
    }
    return select;
  }

  function roleMenuRow(menu) {
    const options = menu.options || [];
    const posted = menu.message_id ? `posted in ${channelName(menu.channel_id)}` : "not posted yet";
    const optionRows = options.map((option) => {
      const remove = el("button", { type: "button", class: "ghost small", text: "Remove" });
      remove.addEventListener("click", () => runAction(
        remove,
        () => api(`guilds/${state.guildId}/role-menus/${menu.id}/options/${option.role_id}`, { method: "DELETE" }),
        `Removed ${option.label} from the menu.`,
        loadRoleMenus,
      ));
      return el("div", { class: "menu-option" }, [
        el("span", { class: "grow", text: `${option.label} gives ${roleName(option.role_id)}` }),
        remove,
      ]);
    });

    const roleSelect = menuSelect("role");
    const labelInput = el("input", { name: "label", maxlength: "100", placeholder: "Label (optional)" });
    const addRole = el("button", { type: "button", class: "small", text: "Add role" });
    addRole.addEventListener("click", () => {
      if (!roleSelect.value) return notice("Choose a role first.", "err");
      const body = { role_id: roleSelect.value };
      if (labelInput.value.trim()) body.label = labelInput.value.trim();
      runAction(
        addRole,
        () => api(`guilds/${state.guildId}/role-menus/${menu.id}/options`, { method: "POST", body }),
        "Role added to the menu.",
        loadRoleMenus,
      );
    });

    const channelSelect = menuSelect("channel");
    const post = el("button", { type: "button", class: "small", text: menu.message_id ? "Post again" : "Post menu" });
    post.addEventListener("click", () => {
      if (options.length === 0) return notice("Add at least one role before posting.", "err");
      runAction(
        post,
        () => api(`guilds/${state.guildId}/role-menus/${menu.id}/post`, {
          method: "POST",
          body: { channel_id: channelSelect.value || null },
        }),
        (r) => `Menu posted in ${channelName(r.channel_id)}. Members can pick roles from it.`,
        loadRoleMenus,
      );
    });

    const remove = el("button", { type: "button", class: "danger small", text: "Delete menu" });
    remove.addEventListener("click", () => {
      if (!window.confirm(`Delete role menu #${menu.id}? Its message is removed. Roles members already have are kept.`)) return;
      runAction(
        remove,
        () => api(`guilds/${state.guildId}/role-menus/${menu.id}`, { method: "DELETE" }),
        "Deleted the role menu.",
        loadRoleMenus,
      );
    });

    return el("li", { class: "menu-row" }, [
      el("div", { class: "grow" }, [
        el("strong", { text: `#${menu.id} · ${menu.title}` }),
        el("div", { class: "meta", text: `${options.length} role${options.length === 1 ? "" : "s"} · ${posted}` }),
        menu.description ? el("div", { class: "body", text: menu.description }) : null,
        ...(optionRows.length ? optionRows : [el("div", { class: "muted", text: "No roles yet." })]),
        el("div", { class: "actions" }, [roleSelect, labelInput, addRole]),
        el("div", { class: "actions" }, [channelSelect, post, remove]),
      ]),
    ]);
  }

  // ------------------------------------------------------------------ //
  // Events
  // ------------------------------------------------------------------ //
  $("#event-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/events`, {
        method: "POST",
        body: {
          title: f.get("title"),
          starts_when: f.get("starts_when"),
          description: opt(f, "description"),
          channel_id: opt(f, "channel_id"),
          remind_minutes: optNumber(f, "remind_minutes"),
        },
      }),
      (r) => `Event #${r.id} posted for ${fmtTime(r.starts_at)}.${r.reminder_at ? ` Reminder ${fmtTime(r.reminder_at)}.` : ""}`,
      async () => { form.reset(); fillSelects(); await loadEvents(); },
    );
  });

  async function loadEvents() {
    const rows = await api(`guilds/${state.guildId}/events`);
    renderList($("#event-list"), rows, eventRow, "No upcoming events.");
  }

  function eventRow(row) {
    const going = row.going === null || row.going === undefined ? "unknown" : String(row.going);
    const button = el("button", { type: "button", class: "danger small", text: "Cancel event" });
    button.addEventListener("click", () => {
      if (!window.confirm(`Cancel event “${row.title}”?`)) return;
      runAction(button, () => api(`guilds/${state.guildId}/events/${row.id}/cancel`, { method: "POST" }),
        `Cancelled event #${row.id}.`, loadEvents);
    });
    return el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: `#${row.id} · ${row.title}` }),
        el("div", {
          class: "meta",
          text: `${fmtTime(row.starts_at)} · going: ${going} · ${channelName(row.channel_id)}${row.remind_minutes ? ` · reminder ${row.remind_minutes} min before` : ""}`,
        }),
        el("div", { class: "body", text: truncate(row.description, 200) }),
      ]),
      button,
    ]);
  }

  // ------------------------------------------------------------------ //
  // Welcome
  // ------------------------------------------------------------------ //
  $("#welcome-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/welcome`, {
        method: "POST",
        body: { channel_id: opt(f, "channel_id"), message: f.get("message") ?? "" },
      }),
      "Welcome messages saved.",
      reloadSettings,
    );
  });

  $("#welcome-off").addEventListener("click", () => {
    if (!window.confirm("Turn off welcome messages?")) return;
    runAction(
      $("#welcome-off"),
      () => api(`guilds/${state.guildId}/welcome`, { method: "DELETE" }),
      "Welcome messages are off.",
      reloadSettings,
    );
  });

  $("#welcome-test").addEventListener("click", () => {
    runAction(
      $("#welcome-test"),
      () => api(`guilds/${state.guildId}/welcome/test`, { method: "POST" }),
      (r) => `Sample welcome posted in ${channelName(r.channel_id)}.`,
    );
  });

  // ------------------------------------------------------------------ //
  // Settings
  // ------------------------------------------------------------------ //
  $("#settings-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/settings`, {
        method: "PUT",
        body: {
          staff_role_id: opt(f, "staff_role_id"),
          announcement_channel_id: opt(f, "announcement_channel_id"),
          staff_channel_id: opt(f, "staff_channel_id"),
          timezone: opt(f, "timezone"),
          require_approval: f.has("require_approval"),
          quiet_start: opt(f, "quiet_start"),
          quiet_end: opt(f, "quiet_end"),
          quiet_hold_manual: f.has("quiet_hold_manual"),
          ticket_category_id: opt(f, "ticket_category_id"),
          ticket_auto_close_days: opt(f, "ticket_auto_close_days") ?? "0",
          ticket_transcript_retention_days: opt(f, "ticket_transcript_retention_days") ?? "0",
          support_hours_start: opt(f, "support_hours_start"),
          support_hours_end: opt(f, "support_hours_end"),
          after_hours_message: opt(f, "after_hours_message"),
        },
      }),
      "Settings saved.",
      reloadSettings,
    );
  });

  // ------------------------------------------------------------------ //
  // Audit log
  // ------------------------------------------------------------------ //
  $("#audit-refresh").addEventListener("click", () => loadAudit());

  $("#audit-export").addEventListener("click", (event) => {
    runAction(event.currentTarget, async () => {
      const response = await fetch(`api/guilds/${state.guildId}/audit.csv`, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`The export failed (${response.status}). Reload the page and try again.`);
      const url = URL.createObjectURL(await response.blob());
      const link = el("a", { href: url, download: `soundtrail-support-audit-${state.guildId}.csv` });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }, "Downloaded the audit log as CSV.");
  });

  async function loadAudit() {
    const rows = await api(`guilds/${state.guildId}/audit?limit=50`);
    renderList($("#audit-list"), rows, (row) => el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: `${row.action} ${row.detail || ""}`.trim() }),
        el("div", { class: "meta", text: `${fmtTime(row.created_at)} · ${row.actor_name} (${row.source})` }),
      ]),
    ]), "Nothing has been recorded yet.");
  }

  // ------------------------------------------------------------------ //
  // Start
  // ------------------------------------------------------------------ //
  boot().catch((err) => notice(err.message || "Could not load the dashboard.", "err"));
})();
