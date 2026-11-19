"use strict";
/* CloudOps Scheduler - the Dashboard, Overrides, Audit log and Settings pages. */

const AUDIT_LIMITS = [100, 200, 500, 1000];
const ROLE_OPTIONS = [["admin", "Global admin"], ["cloudops", "CloudOps admin"], ["reader", "Reader"]];
const ROLE_HELP = {
  admin: "Everything: also mode, pause, users, connections, email settings, removing VMs or schedules, and overrides that last until cleared.",
  cloudops: "Day-to-day operations: start/stop VMs, timed overrides (hours or until an IST date/time), add or edit VMs and schedules, reports, exports and the audit log. No mode, pause, users, connections, email settings, deletions or until-cleared overrides.",
  reader: "Look only: view resources, schedules, overrides and the audit log.",
};
const MODE_NOTES = {
  readonly: "Read-only mode: CloudOps Scheduler watches and records, but does not start or stop any VM yet.",
  dryrun: "Dry-run mode: CloudOps Scheduler logs what it would do, but does not start or stop any VM.",
  pilot: "Pilot mode: CloudOps Scheduler acts only on the VMs chosen for the pilot.",
};

function statCard(value, label, href, kind) {
  return h("a", {class: "stat" + (kind ? " " + kind : ""), href},
    h("div", {class: "v"}, typeof value === "number" ? fmtNum(value) : value), h("div", {class: "l"}, label));
}
function panelBox(title, body, ...actions) {
  return h("div", {class: "panel"}, h("div", {class: "panel-head"}, title, h("div", {class: "sp"}), actions), body);
}
function noteBox(kind, iconName, ...kids) {
  return h("div", {class: "callout" + (kind ? " " + kind : "")}, icon(iconName), h("div", null, kids));
}
function emptyNote(text) {
  return h("div", {class: "panel-body mute"}, text);
}
function clip(text, n) {
  return text.length > n ? text.slice(0, n - 1) + "\u2026" : text;
}
function vmLink(name, onChange) {
  return h("button", {type: "button", class: "linkbtn", onclick: () => openDrawer(name, onChange)}, name);
}
function toastError(e) {
  toast((e && e.message) || String(e), "err");
}

/* ------------------------------------------------------------------ Dashboard */
async function dashboardPage(ctx) {
  ctx.setTitle("Dashboard");
  const [s, ev, sc] = await Promise.all([api("summary"), api("events?limit=12"), api("schedules")]);

  if (!s.vms) {
    return h("div", null, pageHead("Dashboard"), h("div", {class: "panel"}, emptyState("No VMs yet",
      "Add the VMs that CloudOps Scheduler should start and stop. You can add them one at a time or paste a list.",
      can("cloudops") ? btn("Add resources", {kind: "primary", icon: "plus", onclick: async () => {
        try { addResourcesDialog((await api("schedules")).schedules, refreshPage); } catch (e) { toastError(e); }
      }}) : h("p", null, "Ask an administrator to add them."))));
  }

  // How much of the week the managed VMs are parked, going only by their schedules.
  let vmCount = 0, runTotal = 0;
  for (const x of sc.schedules) {
    if (x.vms && x.grid) { vmCount += x.vms; runTotal += x.vms * runHours(x.grid); }
  }
  const parked = vmCount ? Math.round(100 * (1 - runTotal / (vmCount * 168))) + "%" : "-";

  const cards = h("div", {class: "stats"},
    statCard(s.vms, "Total VMs", "#/resources"),
    statCard(s.azure, h("span", {class: "cloud-label"}, cloudLogo("azure", 14), "Azure"), "#/resources?cloud=azure", "az"),
    statCard(s.gcp, h("span", {class: "cloud-label"}, cloudLogo("gcp", 14), "Google Cloud"), "#/resources?cloud=gcp", "gcp"),
    statCard(s.should_run, "Should be running now", "#/resources?state=run", "run"),
    statCard(s.should_stop, "Should be stopped now", "#/resources?state=stop"),
    statCard(s.overrides, "With an override", "#/resources?state=override", "ovr"),
    statCard(s.unmanaged, "Unmanaged (no schedule)", "#/resources?state=unmanaged", "unm"),
    statCard(s.disabled, "Disabled", "#/resources?state=disabled"),
    statCard(parked, "Parked on average, by schedule", "#/schedules"));

  // "has no schedule" warnings are one per VM; the Unmanaged card and note below already cover them.
  const problems = s.problems.filter(p => !/has no schedule/.test(p.message));
  const attention = [];
  if (s.paused) {
    attention.push(noteBox("warn", "pause", h("b", null, "The scheduler is paused."),
      " Nothing is started or stopped until someone resumes it from the top bar."));
  }
  if (MODE_NOTES[s.mode]) attention.push(noteBox("", "info", MODE_NOTES[s.mode]));
  if (s.unmanaged) {
    attention.push(noteBox("unm", "info", h("b", null, `${plural(s.unmanaged, "VM")} ${s.unmanaged === 1 ? "has" : "have"} no schedule.`),
      ` CloudOps Scheduler never starts or stops ${s.unmanaged === 1 ? "it" : "them"}. `,
      h("a", {href: "#/resources?state=unmanaged"}, "Show")));
  }
  for (const p of problems) attention.push(noteBox(p.level === "ERROR" ? "bad" : "warn", "alert", istText(p.message)));
  if (!attention.length) attention.push(noteBox("ok", "check", "No problems found."));

  const upcoming = s.upcoming.length
    ? h("div", {class: "table-wrap"}, h("table", {class: "t"},
      h("thead", null, h("tr", null, h("th", null, "When (IST)"), h("th", {class: "num"}, "Starts"), h("th", {class: "num"}, "Stops"))),
      h("tbody", null, s.upcoming.map(u => h("tr", null,
        h("td", {class: "nowrap"}, fmtWhen(u.at), h("div", {class: "sub"}, inWords(u.at))),
        h("td", {class: "num"}, u.start ? chip(fmtNum(u.start), "run") : ""),
        h("td", {class: "num"}, u.stop ? chip(fmtNum(u.stop), "stop") : ""))))))
    : emptyNote("No starts or stops are due in the next 24 hours.");

  const ending = s.overrides_ending_soon.length
    ? h("div", {class: "table-wrap"}, h("table", {class: "t"},
      h("thead", null, h("tr", null, h("th", null, "VM"), h("th", null, "Ends"), h("th", null, "Reason"))),
      h("tbody", null, s.overrides_ending_soon.map(o => h("tr", null,
        h("td", null, vmLink(o.name, refreshPage)),
        h("td", {class: "nowrap"}, fmtDate(o.until), h("div", {class: "sub"}, inWords(o.until))),
        h("td", null, o.reason || ""))))))
    : emptyNote(s.overrides ? "No override ends within the next 14 days." : "There are no overrides.");

  const activity = ev.events.length
    ? h("div", {class: "table-wrap"}, h("table", {class: "t"}, h("tbody", null, ev.events.map(e => {
      const detail = detailText(e.detail);
      return h("tr", null,
        h("td", {class: "nowrap", title: fmtStamp(e.ts) + " IST"}, inWords(e.ts)),
        h("td", null, actionChip(e.action)),
        h("td", null, e.target || "", detail ? h("div", {class: "sub", title: detail}, clip(detail, 300)) : null),
        h("td", {class: "sub nowrap"}, e.actor));
    }))))
    : emptyNote("Nothing has been recorded yet.");

  return h("div", null,
    pageHead("Dashboard", btn("Refresh", {icon: "refresh", onclick: () => refreshPage()})),
    cards,
    h("div", {class: "cols"},
      panelBox("Needs attention", h("div", {class: "panel-body"}, attention)),
      panelBox("Next 24 hours", upcoming),
      panelBox("Overrides ending within 14 days", ending, h("a", {href: "#/overrides"}, "All overrides")),
      panelBox("Recent activity", activity, h("a", {href: "#/audit"}, "Audit log"))));
}

/* ------------------------------------------------------------------ Overrides */
async function overridesPage(ctx) {
  ctx.setTitle("Overrides");
  let rows = [];
  let show = ["active", "scheduled", "ended", "all"].includes(ctx.params.show) ? ctx.params.show : "active";
  let query = "";
  const tbody = h("tbody");
  const count = h("span", {class: "mute"});
  const seg = h("div", {class: "seg"});
  // active: in force now; scheduled: set to start later; ended: past its end time
  const stateOf = r => r.active ? "active" : r.scheduled ? "scheduled" : "ended";
  const ended = () => rows.filter(r => stateOf(r) === "ended");

  async function load() {
    rows = (await api("overrides")).overrides;
    // active first (soonest end first, "until cleared" last), then scheduled (soonest start first), then ended (latest first)
    const rank = {active: 0, scheduled: 1, ended: 2};
    rows.sort((a, b) => (rank[stateOf(a)] - rank[stateOf(b)])
      || (a.active ? String(a.expires_at || "~").localeCompare(String(b.expires_at || "~"))
        : a.scheduled ? String(a.starts_at).localeCompare(String(b.starts_at))
        : String(b.expires_at).localeCompare(String(a.expires_at)))
      || collator.compare(a.vm_name, b.vm_name));
  }
  async function reload() {
    await load();
    draw();
  }
  function visible() {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    return rows.filter(r => (show === "all" || show === stateOf(r))
      && terms.every(t => `${r.vm_name} ${r.reason} ${r.set_by} ${r.schedule || ""}`.toLowerCase().includes(t)));
  }

  function clearOne(r) {
    const message = {
      active: `Clear the override on ${r.vm_name}? The schedule takes control of it again.`,
      scheduled: `Cancel the scheduled override on ${r.vm_name}? It has not started, so nothing changes for the VM today.`,
      ended: `Remove the record of the ended override on ${r.vm_name}?`,
    }[stateOf(r)];
    confirmDialog({
      title: "Clear override", confirm: "Clear override", message,
      run: async () => { await api("override/clear", {name: r.vm_name}); toast("Override cleared."); await reload(); },
    });
  }
  const endedBtn = btn("Clear all ended", {title: "Tidy up: remove the records of overrides that have already ended", onclick: () => {
    const names = ended().map(r => r.vm_name);
    confirmDialog({
      title: "Clear ended overrides", confirm: "Clear",
      message: `Remove the records of ${plural(names.length, "ended override")}? This only tidies the list; those VMs are already back on their schedules.`,
      run: async () => { reportBulk("Ended overrides cleared", await api("override/clear", {names})); await reload(); },
    });
  }});

  /* When it applies, in IST: "Until ..." while active, "From ..." (then its end) before it starts, "Ended ..." afterwards. */
  function whenCell(r) {
    const end = r.expires_at ? "until " + fmtDate(r.expires_at) : "until cleared";
    if (r.scheduled) return [`From ${fmtDate(r.starts_at)}`, h("div", {class: "sub"}, `${inWords(r.starts_at)}, ${end}`)];
    if (!r.expires_at) return "Until cleared";
    return r.active ? ["Until " + fmtDate(r.expires_at), h("div", {class: "sub"}, inWords(r.expires_at))]
      : ["Ended " + fmtDate(r.expires_at), h("div", {class: "sub"}, inWords(r.expires_at))];
  }
  function row(r) {
    return h("tr", {class: r.active ? "ovr" : ""},
      h("td", null, r.active ? chip("Active", "ovr") : r.scheduled ? chip("Scheduled", "ovr", "Starts " + fmtDate(r.starts_at)) : chip("Ended")),
      h("td", null, h("div", {class: "nowrap"}, vmLink(r.vm_name, reload), " ", cloudChip(r.provider)),
        h("div", {class: "sub"}, r.schedule || "no schedule")),
      h("td", {class: "nowrap"}, whenCell(r)),
      h("td", null, r.reason || h("span", {class: "mute"}, "(none given)")),
      h("td", {class: "nowrap"}, r.set_by, h("div", {class: "sub"}, fmtDate(r.set_at))),
      h("td", {class: "nowrap"}, can("cloudops") ? btn("Clear", {sm: true, onclick: () => clearOne(r)}) : null));
  }
  function draw() {
    const list = visible();
    const n = {active: 0, scheduled: 0, ended: 0, all: rows.length};
    for (const r of rows) n[stateOf(r)]++;
    seg.replaceChildren(...[["active", "Active"], ["scheduled", "Scheduled"], ["ended", "Ended"], ["all", "All"]].map(([k, l]) =>
      h("button", {type: "button", class: show === k ? "on" : "", onclick: () => { show = k; draw(); }}, `${l} (${fmtNum(n[k])})`)));
    count.textContent = `${fmtNum(list.length)} shown`;
    endedBtn.disabled = !n.ended;
    tbody.replaceChildren(...(list.length ? list.map(row)
      : [h("tr", null, h("td", {class: "empty-cell", colspan: 6}, rows.length ? "No overrides match." : "There are no overrides."))]));
  }

  const search = h("input", {type: "text", placeholder: "Search VM, reason or person\u2026", "aria-label": "Search overrides"});
  search.addEventListener("input", debounce(() => { query = search.value; draw(); }, 160));

  await load();
  draw();
  return h("div", null,
    pageHead("Overrides",
      can("cloudops") ? endedBtn : null,
      can("cloudops") ? btn("Set override\u2026", {kind: "primary", icon: "shield", onclick: () => overrideDialog({askNames: true, onDone: reload})}) : null),
    h("div", {class: "intro"}, "While an override is active, CloudOps Scheduler does not start or stop that VM and sends no block/unblock signal for it."),
    h("div", {class: "toolbar"}, seg, h("div", {class: "search"}, icon("search", 16), search), count),
    h("div", {class: "panel"}, h("div", {class: "table-wrap"}, h("table", {class: "t"},
      h("thead", null, h("tr", null, h("th", null, "Status"), h("th", null, "VM"), h("th", null, "When (IST)"), h("th", null, "Reason"), h("th", null, "Set by"), h("th", null, ""))),
      tbody))));
}

/* ------------------------------------------------------------------ Audit log */
async function auditPage(ctx) {
  ctx.setTitle("Audit log");
  let query = ctx.params.q || "";
  let limit = 200;
  let events = [];
  const tbody = h("tbody");
  const count = h("span", {class: "mute"});
  const search = h("input", {type: "text", value: query, placeholder: "Search action, VM or person\u2026", "aria-label": "Search the audit log"});
  const limitSel = h("select", {"aria-label": "How many events", value: limit}, AUDIT_LIMITS.map(n => h("option", {value: n}, `Latest ${n}`)));

  function draw() {
    count.textContent = `${fmtNum(events.length)} events (times are IST)`;
    tbody.replaceChildren(...(events.length ? events.map(e => h("tr", null,
      h("td", {class: "nowrap"}, fmtStamp(e.ts)),
      h("td", null, actionChip(e.action)),
      h("td", null, e.target || ""),
      h("td", {class: "nowrap"}, e.actor || ""),
      h("td", {class: "detail"}, detailText(e.detail))))
      : [h("tr", null, h("td", {class: "empty-cell", colspan: 5}, query ? "No events match your search." : "Nothing has been recorded yet."))]));
  }
  let seq = 0;
  async function load() {
    const mine = ++seq;
    const r = await api("events" + qs({limit, q: query}));
    if (mine !== seq) return; // a newer search was started; drop this late answer
    events = r.events;
    draw();
  }
  /* A CSV for sharing. Cells that start with = + - @ get a leading quote so a spreadsheet never runs them as a formula. */
  function download() {
    const cell = v => {
      let t = v === null || v === undefined ? "" : String(v);
      if (/^[=+\-@\t\r]/.test(t)) t = "'" + t;
      return `"${t.replace(/"/g, '""')}"`;
    };
    const lines = [["time_ist", "action", "target", "actor", "detail"].map(cell).join(",")]
      .concat(events.map(e => [fmtStamp(e.ts), e.action, e.target, e.actor, detailText(e.detail)].map(cell).join(",")));
    const url = URL.createObjectURL(new Blob([lines.join("\r\n")], {type: "text/csv"}));
    const a = h("a", {href: url, download: `cloudops-scheduler-audit-${fmtStamp(serverNow().toISOString()).slice(0, 10)}.csv`});
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  search.addEventListener("input", debounce(() => { query = search.value.trim(); load().catch(toastError); }, 300));
  limitSel.addEventListener("change", () => { limit = Number(limitSel.value); load().catch(toastError); });

  await load();
  return h("div", null,
    pageHead("Audit log",
      btn("Download CSV", {onclick: download}),
      btn("Refresh", {icon: "refresh", onclick: () => load().then(() => toast("Refreshed."), toastError)})),
    h("div", {class: "intro"}, "Every action CloudOps Scheduler takes, and every change anyone makes, is recorded here. The same events are also written to daily files under logs\\audit."),
    h("div", {class: "toolbar"}, h("div", {class: "search"}, icon("search", 16), search), limitSel, count),
    h("div", {class: "panel"}, h("div", {class: "table-wrap"}, h("table", {class: "t"},
      h("thead", null, h("tr", null, h("th", null, "Time (IST)"), h("th", null, "Action"), h("th", null, "Target"), h("th", null, "By"), h("th", null, "Details"))),
      tbody))));
}

/* ------------------------------------------------------------------ Settings */
async function settingsPage(ctx) {
  ctx.setTitle("Settings");
  const items = [];
  if (can("admin")) {
    items.push({key: "connections", label: "Connections", href: "#/settings/connections"}, {key: "users", label: "Users", href: "#/settings/users"});
  }
  items.push({key: "account", label: "My account", href: "#/settings/account"});
  if (can("admin")) items.splice(2, 0, {key: "email", label: "Email & alerts", href: "#/settings/email"});
  const tab = ctx.parts[1] || items[0].key;
  if (!items.some(i => i.key === tab)) throw new Error("That settings page does not exist, or you do not have access to it.");
  const body = tab === "connections" ? await connectionsTab() : tab === "users" ? await usersTab() : tab === "email" ? await emailTab() : accountTab();
  return h("div", null, pageHead("Settings"), tabs(items, tab), body);
}

/* What may the scheduler do? readonly / dryrun / pilot / live. */
function modeDialog() {
  const cur = state.status ? state.status.mode : "readonly";
  const text = {
    readonly: "Read-only: only reads VM status. Nothing is started or stopped.",
    dryrun: "Dry run: decides and logs what it would do (audit log), but sends nothing.",
    pilot: "Pilot: really starts/stops only the VMs you list below.",
    live: "Live: really starts/stops every managed VM on its schedule.",
  };
  return formDialog({
    title: "Scheduler mode", submit: "Apply mode",
    intro: "Current mode: " + (MODE_LABEL[cur] ? MODE_LABEL[cur][0] : cur) + ". Roll out in this order: Read-only, Dry run, Pilot, Live.",
    fields: [
      {name: "mode", label: "Mode", type: "select", value: cur, options: Object.keys(text).map(k => [k, text[k]])},
      {name: "pilot_vms", label: "Pilot VMs (Pilot mode only)", type: "textarea", rows: 3, placeholder: "vm-one, vm-two", hint: "Names must already be in CloudOps Scheduler."},
      {name: "confirm", label: "Type LIVE to confirm (Live mode only)", placeholder: "LIVE", autocomplete: "off"},
    ],
    run: async v => {
      const r = await api("mode", {mode: v.mode, pilot_vms: v.pilot_vms, confirm: v.confirm.trim()});
      toast(`Mode is now ${v.mode}.`);
      (r.warnings || []).forEach(w => toast(w, "err"));
      await refreshStatus();
      route();
    },
  });
}

async function emailTab() {
  const e = await api("email");
  const reportTo = h("input", {type: "text", value: e.report_to, placeholder: "name@company.com; other@company.com"});
  const alertTo = h("input", {type: "text", value: e.alert_to, placeholder: "name@company.com; other@company.com"});
  const auto = h("input", {type: "checkbox", checked: e.report_auto});
  const time = h("input", {type: "time", value: e.report_time_ist, "aria-label": "Report time in IST"});
  const out = h("div");
  const show = (ok, text) => out.replaceChildren(noteBox(ok ? "ok" : "bad", ok ? "check" : "alert", text));
  const guard = fn => async () => {
    try { await fn(); } catch (x) { show(false, x.message); }
  };
  const save = guard(async () => {
    await api("email/save", {report_to: reportTo.value, alert_to: alertTo.value, report_auto: auto.checked,
      report_time: time.value, report_tz: "Asia/Kolkata"});
    show(true, "Saved.");
  });
  const test = guard(async () => {
    const r = await api("email/test", {});
    show(r.ok, (r.ok ? "Test email sent to " + (r.to || []).join("; ") : "Test email failed: ") + (r.ok ? "" : r.detail));
  });
  const sendNow = guard(async () => {
    await save();
    const r = await api("report/send", {});
    show(r.ok, r.ok ? "Report sent. Check the inbox (attachment: Excel)." : "Report not sent: " + r.detail);
  });
  return h("div", {class: "panel"}, h("div", {class: "panel-body"},
    e.configured ? null : noteBox("bad", "alert", h("b", null, "The email Logic App is not configured."),
      h("div", null, "Add an \"email_logic_app\" section (url or url_secret_name) to config\\settings.json and restart the service.")),
    field("Report recipients", reportTo, "Separate several addresses with ; , or a space. The daily report has an Excel attachment (VMs and schedules)."),
    field("Alert recipients", alertTo, "Gets an email when a start/stop fails, gets stuck, or the scheduler stops responding."),
    h("label", {class: "radio"}, auto, "Send the report automatically every day"),
    field("Report time (IST)", h("div", {class: "nowrap"}, time, " ", h("span", {class: "mute"}, "India Standard Time, GMT+5:30"))),
    e.report_last ? h("p", {class: "mute"}, "Last report sent: " + fmtDay(e.report_last)) : null,
    h("div", {class: "row"}, btn("Save", {kind: "primary", icon: "check", onclick: save}),
      btn("Send test email", {onclick: test}), btn("Send report now", {onclick: sendNow})),
    out));
}

async function connectionsTab() {
  const c = await api("connections");
  const results = h("div");
  const runChecks = btn("Run connection checks", {kind: "primary", icon: "zap", onclick: async () => {
    results.replaceChildren(spinner("Checking Azure, Key Vault, Google Cloud and the Logic App\u2026 this can take a few seconds."));
    runChecks.disabled = true;
    try {
      results.replaceChildren(checksList((await api("connections/test", {})).results));
    } catch (e) {
      results.replaceChildren(errorPanel(e));
    } finally {
      runChecks.disabled = false;
    }
  }});

  let summary;
  if (!c.configured) {
    summary = noteBox("bad", "alert", h("b", null, "The settings file is missing or invalid."), h("div", null, c.error || ""),
      h("div", null, "Copy config\\settings.example.json to config\\settings.json, fill in your values and restart the service."));
  } else {
    const la = c.logic_app;
    summary = kv([
      ["Azure managed identity", c.azure.uami_client_id ? h("code", null, c.azure.uami_client_id) : "(the VM's system-assigned identity)"],
      ["Key Vault", c.azure.key_vault || "(not set)"],
      ["Google Cloud key secret", c.gcp.key_secret_name || "(not set)"],
      ["Logic App", la.configured ? chip("Configured", "run") : chip("Not configured", "bad")],
      la.source ? ["Logic App URL from", la.source] : null,
      la.target ? ["Logic App address", h("code", null, la.target)] : null,
      ["Time-out", `${la.timeout_seconds} seconds`],
      ["Attempts per signal", String(la.retries)],
    ]);
  }

  const vmName = h("input", {type: "text", placeholder: "VM name", autocomplete: "off", "aria-label": "VM name"});
  const action = h("select", {"aria-label": "Signal"}, h("option", {value: "block"}, "block"), h("option", {value: "unblock"}, "unblock"));
  const outcome = h("div");
  const send = () => {
    const name = vmName.value.trim();
    if (!name) { toast("Type the name of a VM first.", "err"); return; }
    confirmDialog({
      title: "Send a real signal?", confirm: "Send signal", kind: "danger",
      message: `This sends a real "${action.value}" request to the Logic App for ${name}, and the Logic App passes it on to Omnibus. Continue?`,
      run: async () => {
        const r = await api("signal/test", {name, action: action.value});
        outcome.replaceChildren(noteBox(r.ok ? "ok" : "bad", r.ok ? "check" : "alert",
          h("b", null, r.ok ? "The Logic App accepted it." : "The signal failed."),
          h("div", null, `${r.action} for ${r.vm}: ${r.detail}`),
          h("div", {class: "sub"}, `${r.status ? "HTTP " + r.status + " \u00b7 " : ""}${plural(r.attempts, "attempt")}`)));
      },
    });
  };
  vmName.addEventListener("keydown", e => { if (e.key === "Enter") send(); });

  return h("div", {class: "cols"},
    panelBox("Configuration", h("div", {class: "panel-body"}, summary,
      h("p", {class: "sub"}, "These come from config\\settings.json on the server. Secrets (the Google key and the Logic App URL's signature) are never shown here."))),
    panelBox("Check the connections", h("div", {class: "panel-body"},
      h("p", {class: "intro"}, "Checks the managed identity, Key Vault, the Google Cloud key and the Logic App address. It changes nothing."),
      runChecks, results)),
    panelBox("Send a test signal", h("div", {class: "panel-body"},
      h("p", {class: "intro"}, "Sends one real block or unblock request to the Logic App for one VM, to prove the path to Omnibus works. It is recorded in the audit log. The VM itself is not touched."),
      h("div", {class: "toolbar"}, vmName, action, btn("Send\u2026", {onclick: send})), outcome)));
}

async function usersTab() {
  let list = (await api("users")).users;
  const tbody = h("tbody");
  async function reload() {
    list = (await api("users")).users;
    draw();
  }
  function oneTimePassword() {
    const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
    const b = new Uint32Array(14);
    crypto.getRandomValues(b);
    return Array.from(b, x => chars[x % chars.length]).join("");
  }
  function addUser() {
    const pw = oneTimePassword();
    formDialog({
      title: "Add user", submit: "Add user",
      intro: "Give the user the one-time password below (copy it now; it is not shown again). " +
        "At first login they are asked to choose their own password.",
      fields: [
        {name: "username", label: "Username", required: true, hint: "No spaces."},
        {name: "password", label: "One-time password", required: true, value: pw, autocomplete: "off", hint: "Generated for you; you can type your own (8+ characters)."},
        {name: "role", label: "Role", type: "select", options: ROLE_OPTIONS, value: "reader"},
      ],
      run: async v => {
        await api("user/save", {create: true, must_change: true, username: v.username.trim(), password: v.password, role: v.role});
        try { await navigator.clipboard.writeText(v.password); } catch (e) { /* clipboard needs HTTPS or localhost */ }
        showResults("User added", `${v.username.trim()} can log in with this one-time password (copied to the clipboard when the browser allows it):`, [v.password]);
        await reload();
      },
    });
  }
  function editUser(u) {
    const self = u.username === state.me.username;
    formDialog({
      title: "Edit " + u.username, submit: "Save",
      fields: [
        {name: "role", label: "Role", type: "select", options: ROLE_OPTIONS, value: u.role},
        {name: "password", label: "New password", type: "password", autocomplete: "new-password",
          hint: "Leave empty to keep the current password. A password set here for another user is one-time: they must change it at next login."},
        {name: "disabled", label: "Disabled (cannot log in)", type: "checkbox", value: !!u.disabled},
      ],
      run: async v => {
        if (self && v.disabled) throw new Error("You cannot disable your own account.");
        const body = {username: u.username, role: v.role, disabled: v.disabled};
        if (v.password) body.password = v.password;
        await api("user/save", body);
        toast("Saved.");
        await reload();
      },
    });
  }
  function removeUser(u) {
    confirmDialog({
      title: "Remove user", confirm: "Remove", kind: "danger",
      message: `Remove ${u.username}? They will no longer be able to log in. The audit log keeps what they did.`,
      run: async () => { await api("user/remove", {username: u.username}); toast(`${u.username} removed.`); await reload(); },
    });
  }
  function draw() {
    tbody.replaceChildren(...list.map(u => {
      const self = u.username === state.me.username;
      return h("tr", null,
        h("td", null, h("b", null, u.username), self ? h("span", {class: "sub"}, " (you)") : null),
        h("td", null, u.role),
        h("td", null, u.disabled ? chip("Disabled", "stop") : u.must_change ? chip("Must set password", "ovr", "Still has a one-time password") : chip("Active", "run")),
        h("td", {class: "nowrap"}, fmtDay(u.created_at)),
        h("td", {class: "nowrap"}, btn("Edit", {sm: true, onclick: () => editUser(u)}), " ",
          btn("Remove", {sm: true, kind: "outline-danger", disabled: self, title: self ? "You cannot remove yourself" : "", onclick: () => removeUser(u)})));
    }));
  }
  draw();
  return h("div", null,
    h("div", {class: "toolbar"}, h("div", {class: "sp"}), btn("Add user", {kind: "primary", icon: "plus", onclick: addUser})),
    h("div", {class: "panel"}, h("div", {class: "table-wrap"}, h("table", {class: "t"},
      h("thead", null, h("tr", null, h("th", null, "Username"), h("th", null, "Role"), h("th", null, "Status"), h("th", null, "Created"), h("th", null, ""))),
      tbody))),
    h("div", {class: "panel"}, h("div", {class: "panel-body"}, h("b", null, "Roles"),
      h("ul", {class: "list-plain"}, ROLE_OPTIONS.map(([k, l]) => h("li", null, h("b", null, l), ": ", ROLE_HELP[k]))))));
}

function accountTab() {
  const old = h("input", {type: "password", autocomplete: "current-password", "aria-label": "Current password"});
  const fresh = h("input", {type: "password", autocomplete: "new-password", "aria-label": "New password"});
  const again = h("input", {type: "password", autocomplete: "new-password", "aria-label": "Repeat the new password"});
  const err = h("div", {class: "form-err", role: "alert"});
  const save = btn("Change password", {kind: "primary", onclick: async () => {
    err.textContent = "";
    if (!old.value || !fresh.value) { err.textContent = "Fill in all three boxes."; return; }
    if (fresh.value !== again.value) { err.textContent = "The new passwords do not match."; return; }
    save.disabled = true;
    try {
      await api("change-password", {old: old.value, new: fresh.value});
      old.value = fresh.value = again.value = "";
      toast("Password changed.");
    } catch (e) {
      err.textContent = e.message;
    } finally {
      save.disabled = false;
    }
  }});
  for (const box of [old, fresh, again]) box.addEventListener("keydown", e => { if (e.key === "Enter") save.click(); });
  return h("div", {class: "cols"},
    panelBox("My account", h("div", {class: "panel-body"}, kv([
      ["Username", h("b", null, state.me.username)], ["Role", chip(state.me.role)], ["Can", ROLE_HELP[state.me.role] || ""]]))),
    panelBox("Change password", h("div", {class: "panel-body"},
      field("Current password", old), field("New password", fresh, "At least 8 characters."), field("Repeat the new password", again),
      err, h("div", null, save))));
}
