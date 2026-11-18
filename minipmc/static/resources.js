"use strict";
/* Mini PMC - Resources: the VM table (search, filters, sort, paging, selection, bulk actions),
   the override dialog, the add-resources dialogs and the details drawer. */

const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});
const PAGE_SIZES = [50, 100, 250];
const NO_SCHEDULE = "(none)";
let durationSeq = 0;

const STATE_FILTERS = {
  override: r => r.override,
  unmanaged: r => !r.schedule,
  disabled: r => !r.enabled,
  run: r => r.says === "R" && r.enabled && !r.override,
  stop: r => r.says === "S" && r.enabled && !r.override,
  mismatch: r => mismatch(r),
  running: r => r.status === "RUNNING",
  down: r => r.status === "STOPPED" || r.status === "DEALLOCATED",
  unchecked: r => !r.status,
};
const STATE_OPTIONS = [["", "Any state"], ["override", "Has an override"], ["unmanaged", "Unmanaged (no schedule)"],
  ["disabled", "Disabled"], ["run", "Should be running now"], ["stop", "Should be stopped now"],
  ["mismatch", "Status differs from schedule"], ["running", "Currently running"], ["down", "Currently stopped"], ["unchecked", "Status not checked"]];

const STATUS_KIND = {RUNNING: "run", STOPPED: "stop", DEALLOCATED: "stop", STARTING: "ovr", STOPPING: "ovr", UNKNOWN: ""};
const STATUS_BATCH = 200;
const STATUS_STALE_MS = 20 * 60 * 1000;

/* The VM's power state as last read from the cloud, with its age in the tooltip. */
function statusChip(r) {
  if (!r.status) return chip("Not checked", "", "Mini PMC has not read this VM's state yet. Use the refresh icon.");
  const age = r.status_checked ? ` \u00b7 checked ${inWords(r.status_checked)}` : "";
  const stale = r.status_checked && Date.now() + state.skew - new Date(r.status_checked).getTime() > STATUS_STALE_MS;
  const label = r.status.charAt(0) + r.status.slice(1).toLowerCase();
  return chip(label, STATUS_KIND[r.status] || "", (r.status_error || r.status_raw || r.status) + age + (stale ? " (old)" : ""));
}
/* True when the VM is managed, has no override and its state is the opposite of what the schedule says. */
function mismatch(r) {
  if (!r.schedule || !r.enabled || r.override) return false;
  const down = r.status === "STOPPED" || r.status === "DEALLOCATED";
  return (r.says === "R" && down) || (r.says === "S" && r.status === "RUNNING");
}
/* Names pasted as a list (comma / semicolon / new line, or several exact VM names separated by spaces). */
function splitNames(q) {
  return [...new Set(q.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean))];
}

function overrideRank(r) {
  if (r.override) return r.override_until ? 0 : 1; // timed first, then "until cleared"
  return r.override_by ? 2 : 3; // an ended override, then none
}
const COMPARE = {
  name: (a, b) => collator.compare(a.name, b.name),
  group: (a, b) => collator.compare(a.group, b.group) || collator.compare(a.name, b.name),
  schedule: (a, b) => (!a.schedule - !b.schedule) || collator.compare(a.schedule || "", b.schedule || "") || collator.compare(a.name, b.name),
  says: (a, b) => (a.says || "~").localeCompare(b.says || "~") || collator.compare(a.name, b.name),
  status: (a, b) => (a.status || "~").localeCompare(b.status || "~") || collator.compare(a.name, b.name),
  override: (a, b) => (overrideRank(a) - overrideRank(b)) || String(a.override_until || "").localeCompare(String(b.override_until || ""))
    || collator.compare(a.name, b.name),
};

function scheduleOptions(schedules) {
  return [["", "(none - not managed)"]].concat(schedules.map(s => [s.name, `${s.name}  \u00b7  ${s.tz}`]));
}

/* Tell the user what a bulk call did: a quiet toast when all went well, a list when some VMs failed. */
function reportBulk(title, r) {
  const errors = r.errors || [];
  if (errors.length) showResults(title, `${plural(r.done || 0, "VM")} done, ${errors.length} failed:`, errors);
  else toast(`${title}: ${plural(r.done || 0, "VM")}.`);
}

/* ------------------------------------------------------------------ override dialog */
/* overrideDialog({names, askNames, onDone}) - "For N days", "Until a date" or "Until cleared", with a required reason. */
function overrideDialog(o) {
  const names = o.names || [];
  const group = "dur" + (++durationSeq);
  const days = h("input", {type: "number", min: 1, max: 3650, value: 7, class: "w80", "aria-label": "Number of days"});
  const date = h("input", {type: "date", "aria-label": "End date"});
  const rDays = h("input", {type: "radio", name: group, checked: true});
  const rDate = h("input", {type: "radio", name: group});
  const rForever = h("input", {type: "radio", name: group});
  days.addEventListener("focus", () => { rDays.checked = true; });
  date.addEventListener("focus", () => { rDate.checked = true; });
  const duration = h("div", {class: "fld"},
    h("span", {class: "fld-l"}, "How long?"),
    h("label", {class: "radio"}, rDays, "For", days, "days"),
    h("label", {class: "radio"}, rDate, "Until the end of", date, h("span", {class: "mute"}, "(UTC)")),
    h("label", {class: "radio"}, rForever, "Until someone clears it"));
  const readDuration = () => {
    if (rForever.checked) return {forever: true};
    if (rDate.checked) {
      if (!date.value) throw new Error("Choose the end date.");
      if (date.value < serverNow().toISOString().slice(0, 10)) throw new Error("That date has already passed. Choose today or a later date.");
      return {until: date.value};
    }
    const n = Number(days.value);
    if (!(n > 0)) throw new Error("Enter how many days (1 or more).");
    return {days: n};
  };

  const fields = [];
  if (o.askNames) {
    fields.push({name: "names", label: "VM names", type: "textarea", rows: 3, required: true,
      placeholder: "One per line, or separated by commas", hint: "Names must already be in Mini PMC."});
  }
  fields.push({name: "when", label: "How long?", node: duration, read: readDuration});
  fields.push({name: "reason", label: "Reason", required: true, maxlength: 200, placeholder: "e.g. UAT until the release is signed off",
    hint: "Everyone can see this reason, and it is written to the audit log."});

  const who = o.askNames ? "the VMs you name below" : names.length === 1 ? names[0] : plural(names.length, "VM");
  return formDialog({
    title: "Set override",
    intro: `While an override is active, Mini PMC does not start or stop ${who}, and sends no block/unblock signal for it. ` +
      "It carries on following the schedule after the override ends or is cleared.",
    fields,
    submit: "Set override",
    run: async v => {
      const list = o.askNames ? [...new Set(v.names.split(/[\s,;]+/).filter(Boolean))] : names;
      if (!list.length) throw new Error("Enter at least one VM name.");
      const r = await api("override/set", Object.assign({names: list, reason: v.reason.trim()}, v.when));
      const bad = r.errors || [];
      if (!r.done && bad.length) { // nothing was set: keep the dialog open so the mistake can be fixed
        throw new Error(bad.slice(0, 6).join("\n") + (bad.length > 6 ? `\n\u2026and ${bad.length - 6} more` : ""));
      }
      reportBulk("Override set", r);
      if (o.onDone) await o.onDone();
    },
  });
}

/* ------------------------------------------------------------------ add resources (admin) */
function addResourcesDialog(schedules, onDone, mode) {
  mode = mode || "one";
  let m = null;
  const go = to => { if (to !== mode) { m.close(); addResourcesDialog(schedules, onDone, to); } };
  const switcher = h("div", {class: "seg"},
    h("button", {type: "button", class: mode === "one" ? "on" : "", onclick: () => go("one")}, "One VM"),
    h("button", {type: "button", class: mode === "list" ? "on" : "", onclick: () => go("list")}, "Paste a list"));
  const options = scheduleOptions(schedules);

  if (mode === "list") {
    const bProvider = h("select", {"aria-label": "Cloud"}, h("option", {value: "azure"}, "Azure"), h("option", {value: "gcp"}, "Google Cloud"));
    const bScope = h("input", {type: "text", autocomplete: "off", placeholder: "Subscription ID / project ID"});
    const bGroup = h("input", {type: "text", autocomplete: "off", placeholder: "Resource group / zone"});
    m = formDialog({
      title: "Add many VMs", wide: true, submit: "Add VMs",
      intro: "Fill in the three shared boxes and paste only VM names (one per line). " +
        "Or leave them empty and paste full lines: name,cloud,subscription-or-project,resource-group-or-zone[,schedule]. Excel paste works too.",
      fields: [
        {node: switcher},
        {name: "provider", label: "Cloud (shared)", node: field("Cloud (shared)", bProvider), read: () => bProvider.value},
        {name: "scope", label: "Subscription / project (shared)", node: field("Subscription / project (shared)", bScope), read: () => bScope.value},
        {name: "group", label: "Resource group / zone (shared)", node: field("Resource group / zone (shared)", bGroup), read: () => bGroup.value},
        {name: "text", label: "VMs, one per line", type: "textarea", rows: 8, required: true,
          placeholder: "vm-one\nvm-two,SCHEDULE_NAME\nvm-three",
          hint: "With shared boxes filled: name or name,schedule per line."},
        {name: "schedule", label: "Default schedule", type: "select", options, value: "", hint: "Used for lines that do not name a schedule."},
      ],
      run: async v => {
        const r = await api("vm/bulk", {text: v.text, schedule: v.schedule || null, provider: v.provider,
          scope: v.scope.trim(), group: v.group.trim()});
        if (r.errors.length) showResults("Add resources", `${plural(r.added.length, "VM")} added, ${plural(r.errors.length, "line")} skipped:`, r.errors);
        else toast(`${plural(r.added.length, "VM")} added.`);
        if (r.added.length && onDone) await onDone();
      },
    });
    return m;
  }

  const provider = h("select", {"aria-label": "Cloud"}, h("option", {value: "azure"}, "Azure"), h("option", {value: "gcp"}, "Google Cloud"));
  const scopeLbl = h("span", {class: "fld-l"}, "Subscription ID");
  const scopeIn = h("input", {type: "text", autocomplete: "off", placeholder: "00000000-0000-0000-0000-000000000000"});
  const groupLbl = h("span", {class: "fld-l"}, "Resource group");
  const groupIn = h("input", {type: "text", autocomplete: "off", placeholder: "my-resource-group"});
  provider.addEventListener("change", () => {
    const azure = provider.value === "azure";
    scopeLbl.textContent = azure ? "Subscription ID" : "Project ID";
    scopeIn.placeholder = azure ? "00000000-0000-0000-0000-000000000000" : "my-gcp-project";
    groupLbl.textContent = azure ? "Resource group" : "Zone";
    groupIn.placeholder = azure ? "my-resource-group" : "europe-west4-a";
  });
  m = formDialog({
    title: "Add resources", submit: "Add VM",
    fields: [
      {node: switcher},
      {name: "provider", label: "Cloud", node: field("Cloud", provider), read: () => provider.value},
      {name: "name", label: "VM name", required: true, placeholder: "exactly as it appears in the cloud"},
      {name: "scope", label: "Subscription / project", required: true, node: h("label", {class: "fld"}, scopeLbl, scopeIn), read: () => scopeIn.value},
      {name: "group", label: "Resource group / zone", required: true, node: h("label", {class: "fld"}, groupLbl, groupIn), read: () => groupIn.value},
      {name: "schedule", label: "Schedule", type: "select", options, value: ""},
      {name: "notes", label: "Notes (optional)"},
    ],
    run: async v => {
      const name = v.name.trim();
      await api("vm/add", {name, provider: v.provider, scope: v.scope.trim(), group: v.group.trim(), schedule: v.schedule || null, notes: v.notes.trim()});
      toast(`${name} added.`);
      if (onDone) await onDone();
    },
  });
  return m;
}

/* ------------------------------------------------------------------ Resources page */
async function resourcesPage(ctx) {
  ctx.setTitle("Resources");
  let vms = [], schedules = [], schedTz = new Map(), schedGrid = new Map();
  const p = ctx.params;
  const f = {q: p.q || "", cloud: p.cloud === "azure" || p.cloud === "gcp" ? p.cloud : "", schedule: p.schedule || "",
    state: Object.hasOwn(STATE_FILTERS, p.state) ? p.state : ""};
  let sortKey = "name", sortDir = 1, page = 0, size = PAGE_SIZES[0];
  let view = [];
  const selected = new Set();

  const bulkBox = h("div");
  const note = h("div", {class: "selnote hide"});
  const missingNote = h("div", {class: "selnote hide"});
  const thead = h("thead");
  const tbody = h("tbody");
  const pager = h("div", {class: "pager"});
  const count = h("span", {class: "mute"});
  const clearFilters = h("button", {type: "button", class: "linkbtn hide"}, "Clear filters");

  const search = h("input", {type: "text", value: f.q, placeholder: "Search, or paste VM names separated by comma, space or new line\u2026", "aria-label": "Search VMs"});
  const cloudSel = h("select", {"aria-label": "Cloud", value: f.cloud},
    h("option", {value: ""}, "All clouds"), h("option", {value: "azure"}, "Azure"), h("option", {value: "gcp"}, "Google Cloud"));
  const schedSel = h("select", {"aria-label": "Schedule"});
  const stateSel = h("select", {"aria-label": "State", value: f.state}, STATE_OPTIONS.map(([v, l]) => h("option", {value: v}, l)));

  async function load() {
    const [a, b] = await Promise.all([api("vms"), api("schedules")]);
    vms = a.vms;
    for (const r of vms) {
      r._hay = [r.name, r.provider, r.scope, r.scope_name, r.group, r.schedule, r.notes, r.override_reason, r.override_by, r.status].join(" ").toLowerCase();
    }
    schedules = b.schedules;
    schedTz = new Map(schedules.map(s => [s.name, s.tz]));
    schedGrid = new Map(schedules.map(s => [s.name, s.grid]));
    if (f.schedule && f.schedule !== NO_SCHEDULE && !schedTz.has(f.schedule)) f.schedule = ""; // an old link to a deleted schedule
    const known = new Set(vms.map(r => r.name));
    for (const n of [...selected]) if (!known.has(n)) selected.delete(n);
    schedSel.replaceChildren(...[["", "All schedules"], [NO_SCHEDULE, "(no schedule)"]].concat(schedules.map(s => [s.name, s.name]))
      .map(([v, l]) => h("option", {value: v}, l)));
    schedSel.value = f.schedule;
  }

  let missing = [];
  function recompute() {
    const byState = STATE_FILTERS[f.state];
    const tokens = splitNames(f.q);
    const names = new Map(vms.map(r => [r.name.toLowerCase(), r]));
    const exact = tokens.filter(t => names.has(t.toLowerCase())).length;
    const listMode = tokens.length > 1 && (/[,;\n]/.test(f.q) || exact >= 2);
    let match;
    missing = [];
    if (listMode) {
      const want = new Set();
      for (const t of tokens) {
        const lo = t.toLowerCase();
        if (names.has(lo)) { want.add(names.get(lo).name); continue; }
        const part = vms.filter(r => r.name.toLowerCase().includes(lo));
        if (part.length) part.forEach(r => want.add(r.name)); else missing.push(t);
      }
      match = r => want.has(r.name);
    } else {
      const terms = f.q.toLowerCase().split(/\s+/).filter(Boolean);
      match = r => terms.every(t => r._hay.includes(t));
    }
    view = vms.filter(r =>
      (!f.cloud || r.provider === f.cloud) &&
      (!f.schedule || (f.schedule === NO_SCHEDULE ? !r.schedule : r.schedule === f.schedule)) &&
      (!byState || byState(r)) && match(r));
    const cmp = COMPARE[sortKey];
    view.sort((a, b) => sortDir * cmp(a, b));
    page = Math.min(page, Math.max(0, Math.ceil(view.length / size) - 1));
  }
  async function afterChange() {
    await load();
    recompute();
    draw();
  }
  const pageRows = () => view.slice(page * size, (page + 1) * size);
  const filtersOn = () => !!(f.q || f.cloud || f.schedule || f.state);
  function resetSelection() {
    selected.clear();
    page = 0;
  }
  function filtersChanged() {
    resetSelection();
    recompute();
    draw();
  }

  /* ---- drawing ---- */
  function overrideCell(r) {
    if (r.override) {
      return h("div", null, chip("Override", "ovr", r.override_reason ? `${r.override_reason} (set by ${r.override_by})` : ""),
        h("div", {class: "sub nowrap"}, r.override_until ? "until " + fmtDay(r.override_until) : "until cleared"));
    }
    if (r.override_by) return h("div", {class: "sub", title: "Ended " + fmtDate(r.override_until)}, "ended " + inWords(r.override_until));
    return null;
  }
  function rowEl(r) {
    const cb = h("input", {type: "checkbox", checked: selected.has(r.name), "aria-label": "Select " + r.name});
    const tr = h("tr", {class: "click" + (r.override ? " ovr" : "") + (selected.has(r.name) ? " sel" : "")},
      h("td", {class: "cb"}, cb),
      h("td", null, h("div", {class: "nowrap"}, h("b", null, r.name), " ", cloudChip(r.provider), r.enabled ? null : [" ", chip("Disabled", "stop", "Mini PMC is not managing this VM")])),
      h("td", null, h("div", null, r.group), h("div", {class: "sub", title: r.scope}, r.scope_name || r.scope)),
      h("td", null, r.schedule
        ? h("div", {class: "sched-cell"}, strip(schedGrid.get(r.schedule)), h("span", null, r.schedule))
        : chip("Unmanaged", "unm", "No schedule: Mini PMC never starts or stops this VM")),
      h("td", {class: "nowrap"}, statusChip(r), r.pending_action ? [" ", chip(r.pending_action === "START" ? "Starting\u2026" : "Stopping\u2026", "ovr", "Mini PMC has sent or queued this action")] : null,
        " ", refreshBtn(r.name),
        mismatch(r) ? h("div", {class: "sub"}, "differs from schedule") : null),
      h("td", {class: "nowrap"}, stateChip(r.says),
        r.local ? h("div", {class: "sub", title: "Time now in the schedule's time zone: " + (schedTz.get(r.schedule) || "")}, r.local + (gmtLabel(schedTz.get(r.schedule)) ? " " + gmtLabel(schedTz.get(r.schedule)) : "")) : null),
      h("td", null, overrideCell(r)));
    cb.addEventListener("change", () => {
      if (cb.checked) selected.add(r.name); else selected.delete(r.name);
      tr.classList.toggle("sel", cb.checked);
      drawChrome();
    });
    tr.addEventListener("click", e => {
      if (e.target.closest("input, a, button")) return;
      openDrawer(r.name, afterChange);
    });
    return tr;
  }
  function emptyRow() {
    const none = !vms.length;
    return h("tr", null, h("td", {class: "empty-cell", colspan: 7},
      none ? "There are no VMs yet." : "No VMs match your search or filters.",
      !none && filtersOn() ? [" ", h("button", {type: "button", class: "linkbtn", onclick: resetFilters}, "Clear filters")] : null));
  }
  function headRow(rows) {
    const all = rows.length > 0 && rows.every(r => selected.has(r.name));
    const cb = h("input", {type: "checkbox", checked: all, "aria-label": "Select all on this page"});
    cb.indeterminate = !all && rows.some(r => selected.has(r.name));
    cb.addEventListener("change", () => {
      for (const r of rows) { if (cb.checked) selected.add(r.name); else selected.delete(r.name); }
      draw();
    });
    const th = (label, key) => key
      ? h("th", {class: "sortable", onclick: () => {
        if (sortKey === key) sortDir = -sortDir; else { sortKey = key; sortDir = 1; }
        recompute();
        draw();
      }}, label, sortKey === key ? h("span", {class: "sortmark"}, icon(sortDir > 0 ? "arrowUp" : "arrowDown", 12)) : null)
      : h("th", null, label);
    return h("tr", null, h("th", {class: "cb"}, cb), th("Name", "name"), th("Location", "group"), th("Schedule", "schedule"),
      th("Current status", "status"), th("Schedule says", "says"), th("Override", "override"));
  }
  function drawNote(rows) {
    note.replaceChildren();
    const pageAll = rows.length > 0 && rows.every(r => selected.has(r.name));
    note.classList.toggle("hide", !(pageAll && view.length > rows.length));
    if (note.classList.contains("hide")) return;
    if (view.every(r => selected.has(r.name))) {
      note.append(`All ${fmtNum(view.length)} matching VMs are selected. `,
        h("button", {type: "button", class: "linkbtn", onclick: () => { selected.clear(); draw(); }}, "Clear selection"));
    } else {
      note.append(`All ${fmtNum(rows.length)} VMs on this page are selected. `,
        h("button", {type: "button", class: "linkbtn", onclick: () => { for (const r of view) selected.add(r.name); draw(); }},
          `Select all ${fmtNum(view.length)} matching VMs`));
    }
  }
  function drawBulk() {
    bulkBox.replaceChildren();
    if (!selected.size) return;
    const rows = vms.filter(r => selected.has(r.name));
    const withOverride = rows.filter(r => r.override || r.override_by);
    bulkBox.appendChild(h("div", {class: "bulk"},
      h("b", null, `${fmtNum(selected.size)} selected`),
      btn("Refresh status", {sm: true, icon: "refresh", onclick: () => refreshStatus([...selected])}),
      can("admin") ? btn("Override\u2026", {sm: true, icon: "shield", onclick: () => overrideDialog({names: [...selected], onDone: afterChange})}) : null,
      can("admin") ? btn("Clear override", {sm: true, disabled: !withOverride.length,
        title: withOverride.length ? "" : "None of the selected VMs has an override", onclick: () => clearOverrides(withOverride)}) : null,
      can("admin") ? btn("Attach schedule\u2026", {sm: true, icon: "calendar", onclick: attachSchedule}) : null,
      can("admin") ? btn("Enable", {sm: true, onclick: () => setEnabled(true)}) : null,
      can("admin") ? btn("Disable", {sm: true, onclick: () => setEnabled(false)}) : null,
      can("admin") ? btn("Remove", {sm: true, kind: "outline-danger", icon: "trash", onclick: removeVms}) : null,
      h("div", {class: "sp"}),
      btn("Clear selection", {sm: true, onclick: () => { selected.clear(); draw(); }})));
  }
  function drawPager() {
    const pages = Math.max(1, Math.ceil(view.length / size));
    const from = view.length ? page * size + 1 : 0;
    const to = Math.min(view.length, (page + 1) * size);
    const sizeSel = h("select", {"aria-label": "Rows per page", value: size}, PAGE_SIZES.map(n => h("option", {value: n}, n)));
    sizeSel.addEventListener("change", () => { size = Number(sizeSel.value); page = 0; draw(); });
    pager.replaceChildren(
      h("span", null, "Rows per page"), sizeSel, h("span", null, `${fmtNum(from)}\u2013${fmtNum(to)} of ${fmtNum(view.length)}`),
      h("div", {class: "sp"}),
      btn("Previous", {sm: true, disabled: page === 0, onclick: () => { page--; draw(); window.scrollTo(0, 0); }}),
      h("span", null, `Page ${page + 1} of ${pages}`),
      btn("Next", {sm: true, disabled: page >= pages - 1, onclick: () => { page++; draw(); window.scrollTo(0, 0); }}));
  }
  function drawChrome() {
    const rows = pageRows();
    thead.replaceChildren(headRow(rows));
    drawNote(rows);
    drawBulk();
    drawPager();
    count.textContent = `${fmtNum(view.length)} of ${fmtNum(vms.length)} VMs`;
    missingNote.replaceChildren();
    missingNote.classList.toggle("hide", !missing.length);
    if (missing.length) {
      missingNote.append(`${plural(missing.length, "name")} not found: `, h("b", null, missing.slice(0, 15).join(", ") + (missing.length > 15 ? " \u2026" : "")), " ",
        h("button", {type: "button", class: "linkbtn", onclick: () => navigator.clipboard && navigator.clipboard.writeText(missing.join("\n")).then(() => toast("Copied."))}, "Copy"));
    }
    clearFilters.classList.toggle("hide", !filtersOn());
  }
  function draw() {
    const rows = pageRows();
    tbody.replaceChildren(...(rows.length ? rows.map(rowEl) : [emptyRow()]));
    drawChrome();
  }
  function resetFilters() {
    f.q = f.cloud = f.schedule = f.state = "";
    search.value = "";
    cloudSel.value = schedSel.value = stateSel.value = "";
    filtersChanged();
  }

  /* ---- status refresh ---- */
  let refreshing = false;
  function refreshBtn(name) {
    return h("button", {type: "button", class: "icon-btn", title: "Refresh this VM's status", "aria-label": "Refresh status of " + name,
      onclick: e => { e.stopPropagation(); refreshStatus([name]); }}, icon("refresh", 14));
  }
  async function refreshStatus(names) {
    if (refreshing) { toast("A refresh is already running.", "err"); return; }
    if (!names.length) { toast("Nothing to refresh."); return; }
    refreshing = true;
    let done = 0;
    const errors = [];
    try {
      for (let i = 0; i < names.length; i += STATUS_BATCH) {
        if (names.length > STATUS_BATCH) toast(`Refreshing status\u2026 ${fmtNum(Math.min(i, names.length))} of ${fmtNum(names.length)}`);
        const r = await api("vm/refresh", {names: names.slice(i, i + STATUS_BATCH)});
        done += r.done || 0;
        errors.push(...(r.errors || []));
      }
    } catch (e) {
      toast(e.message, "err");
    } finally {
      refreshing = false;
    }
    await afterChange();
    if (errors.length) showResults("Refresh status", `${plural(done, "VM")} refreshed, ${errors.length} failed:`, errors);
    else toast(`Status refreshed for ${plural(done, "VM")}.`);
  }
  async function exportXlsx() {
    try {
      await downloadPost("export/resources", selected.size ? {names: [...selected]} : {}, "minipmc-resources.xlsx");
    } catch (e) {
      toast(e.message, "err");
    }
  }

  /* ---- bulk actions ---- */
  function clearOverrides(rows) {
    confirmDialog({
      title: "Clear override", confirm: "Clear override",
      message: `Clear the override on ${plural(rows.length, "VM")}? The schedule takes control of them again.`,
      run: async () => {
        reportBulk("Override cleared", await api("override/clear", {names: rows.map(r => r.name)}));
        await afterChange();
      },
    });
  }
  function attachSchedule() {
    const names = [...selected];
    formDialog({
      title: "Attach schedule", submit: "Attach",
      intro: `Choose the schedule for ${plural(names.length, "VM")}. Pick "(none)" to stop managing them.`,
      fields: [{name: "schedule", label: "Schedule", type: "select", options: scheduleOptions(schedules), value: ""}],
      run: async v => {
        reportBulk("Schedule attached", await api("vm/update", {names, schedule: v.schedule || null}));
        await afterChange();
      },
    });
  }
  function setEnabled(on) {
    const names = [...selected];
    confirmDialog({
      title: on ? "Enable VMs" : "Disable VMs", confirm: on ? "Enable" : "Disable", kind: on ? "primary" : "danger",
      message: on ? `Let Mini PMC manage ${plural(names.length, "VM")} again?`
        : `Mini PMC will stop managing ${plural(names.length, "VM")}: no start, no stop and no signals. Continue?`,
      run: async () => {
        reportBulk(on ? "Enabled" : "Disabled", await api("vm/update", {names, enabled: on}));
        await afterChange();
      },
    });
  }
  function removeVms() {
    const names = [...selected];
    confirmDialog({
      title: "Remove VMs", confirm: "Remove", kind: "danger",
      message: `Remove ${plural(names.length, "VM")} from Mini PMC? The VMs themselves are not touched in the cloud. The removal is recorded in the audit log.`,
      run: async () => {
        reportBulk("Removed", await api("vm/remove", {names}));
        selected.clear();
        await afterChange();
      },
    });
  }

  /* ---- wiring ---- */
  const searchSoon = debounce(() => { f.q = search.value; filtersChanged(); }, 160);
  search.addEventListener("input", searchSoon);
  cloudSel.addEventListener("change", () => { f.cloud = cloudSel.value; filtersChanged(); });
  schedSel.addEventListener("change", () => { f.schedule = schedSel.value; filtersChanged(); });
  stateSel.addEventListener("change", () => { f.state = stateSel.value; filtersChanged(); });
  clearFilters.addEventListener("click", resetFilters);

  await load();
  recompute();
  draw();

  return h("div", null,
    pageHead("Resources",
      btn("Refresh", {icon: "refresh", onclick: () => afterChange().then(() => toast("Refreshed."), e => toast(e.message, "err"))}),
      btn("Refresh status", {icon: "refresh", title: "Read the live power state from the cloud for the VMs in the current view (or the selected ones)",
        onclick: () => refreshStatus(selected.size ? [...selected] : view.map(r => r.name))}),
      btn("Export Excel", {icon: "file", title: "Selected VMs, or everything if none are selected", onclick: exportXlsx}),
      can("admin") ? btn("Add resources", {kind: "primary", icon: "plus", onclick: () => addResourcesDialog(schedules, afterChange)}) : null),
    h("div", {class: "toolbar"},
      h("div", {class: "search"}, icon("search", 16), search), cloudSel, schedSel, stateSel, count, clearFilters),
    missingNote,
    bulkBox,
    h("div", {class: "panel"}, note,
      h("div", {class: "table-wrap"}, h("table", {class: "t"}, thead, tbody)), pager));
}

/* ------------------------------------------------------------------ details drawer */
const drawerState = {nodes: null};
function closeDrawer() {
  if (drawerState.nodes) drawerState.nodes.forEach(n => n.remove());
  drawerState.nodes = null;
}
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && drawerState.nodes && !document.querySelector("dialog[open]")) closeDrawer();
});

async function testConnection(name) {
  const body = h("div", null, spinner(`Checking the cloud connections for ${name}\u2026 this can take a few seconds.`));
  modal("Test connection", body, [{label: "Close", kind: "primary"}]);
  try {
    body.replaceChildren(checksList((await api("connections/test", {vm: name})).results));
  } catch (e) {
    body.replaceChildren(errorPanel(e));
  }
}

function drawerSections(d, ctl) {
  const r = d.vm;
  const azure = r.provider === "azure";
  const out = [];

  out.push(h("div", {class: "dsec"}, h("h4", null, "Details"), kv([
    ["Cloud", cloudChip(r.provider)],
    [azure ? "Subscription" : "Project", h("code", null, r.scope)],
    [azure ? "Resource group" : "Zone", r.group],
    ["Schedule", r.schedule
      ? h("a", {href: "#/schedules/edit/" + encodeURIComponent(r.schedule), onclick: ctl.close}, r.schedule)
      : chip("Unmanaged", "unm")],
    ["Managed by Mini PMC", r.enabled ? chip("Yes", "run") : chip("Disabled", "stop")],
    r.notes ? ["Notes", r.notes] : null,
  ])));

  let ovr;
  if (r.override) {
    ovr = h("div", {class: "callout warn"}, icon("shield"), h("div", null,
      h("b", null, "Override active"),
      h("div", null, "Mini PMC will not start or stop this VM. ",
        r.override_until ? `It ends ${fmtDate(r.override_until)} (${inWords(r.override_until)}).` : "It stays until someone clears it."),
      h("div", null, `Set by ${r.override_by}. Reason: ${r.override_reason || "(none given)"}`),
      can("admin") ? h("div", {class: "btns"},
        btn("Change", {sm: true, onclick: () => overrideDialog({names: [r.name], onDone: ctl.refresh})}),
        btn("Clear override", {sm: true, onclick: () => confirmDialog({
          title: "Clear override", confirm: "Clear override",
          message: `Clear the override on ${r.name}? The schedule takes control again.`,
          run: async () => { await api("override/clear", {name: r.name}); toast("Override cleared."); await ctl.refresh(); },
        })})) : null));
  } else {
    ovr = h("div", {class: "callout"}, icon("info"), h("div", null,
      r.override_by ? `An earlier override by ${r.override_by} ended ${inWords(r.override_until)}.` : "No override. The schedule is in control.",
      can("admin") ? h("div", {class: "btns"}, btn("Set override\u2026", {sm: true, kind: "primary", onclick: () => overrideDialog({names: [r.name], onDone: ctl.refresh})})) : null));
  }
  out.push(h("div", {class: "dsec"}, h("h4", null, "Override"), ovr));

  if (d.grid) {
    out.push(h("div", {class: "dsec"}, h("h4", null, "Schedule"),
      strip(d.grid, true),
      h("p", {class: "sub"}, `Time zone ${d.tz}. Local time now: ${r.local || "n/a"}.`),
      h("div", {class: "windows"}, d.windows.map(w => chip(w))),
      h("p", null, "The schedule says: ", stateChip(r.says), " right now.")));
    if (d.time_check) {
      out.push(h("div", {class: "dsec"}, h("h4", null, "Next changes"),
        r.override ? h("p", {class: "sub"}, "An override is active, so none of these will be acted on until it ends.") : null,
        timeCheckView(d.time_check)));
    }
  } else {
    out.push(h("div", {class: "dsec"}, h("h4", null, "Schedule"),
      h("div", {class: "callout"}, icon("info"), "This VM has no schedule, so Mini PMC never starts or stops it.")));
  }

  out.push(h("div", {class: "dsec"}, h("h4", null, "Recent activity"),
    d.events.length ? h("ul", {class: "events"}, d.events.map(e => h("li", null,
      h("div", null, actionChip(e.action), " ", h("span", {class: "mute"}, `${fmtStamp(e.ts)} UTC \u00b7 ${e.actor}`)),
      e.detail ? h("div", {class: "sub"}, detailText(e.detail)) : null)))
      : h("p", {class: "mute"}, "Nothing has been recorded for this VM yet.")));
  return out;
}

function drawerButtons(d, ctl) {
  const r = d.vm;
  const out = [];
  if (can("admin")) out.push(btn("Test connection", {icon: "zap", onclick: () => testConnection(r.name)}));
  if (can("admin")) {
    out.push(btn("Change schedule\u2026", {icon: "calendar", onclick: async () => {
      let list;
      try { list = (await api("schedules")).schedules; } catch (e) { toast(e.message, "err"); return; }
      formDialog({
        title: "Change schedule", submit: "Save",
        fields: [
          {name: "schedule", label: "Schedule", type: "select", options: scheduleOptions(list), value: r.schedule || ""},
          {name: "enabled", label: "Managed by Mini PMC (untick to switch it off)", type: "checkbox", value: r.enabled},
        ],
        run: async v => {
          await api("vm/update", {name: r.name, schedule: v.schedule || null, enabled: v.enabled});
          toast("Saved.");
          await ctl.refresh();
        },
      });
    }}));
    out.push(btn("Remove", {kind: "outline-danger", icon: "trash", onclick: () => confirmDialog({
      title: "Remove VM", confirm: "Remove", kind: "danger",
      message: `Remove ${r.name} from Mini PMC? The VM itself is not touched in the cloud.`,
      run: async () => {
        await api("vm/remove", {name: r.name});
        toast(`${r.name} removed.`);
        ctl.close();
        if (ctl.changed) await ctl.changed();
      },
    })}));
  }
  return out;
}

/* openDrawer(name, onChange) - a side panel with everything about one VM. onChange runs after any change. */
async function openDrawer(name, onChange) {
  closeDrawer();
  const body = h("div", {class: "drawer-body"}, spinner());
  const foot = h("div", {class: "drawer-foot hide"});
  const panel = h("aside", {class: "drawer", role: "dialog", "aria-label": "Details of " + name},
    h("div", {class: "drawer-head"}, icon("server", 18), h("h3", null, name), h("div", {class: "sp"}), iconBtn("x", "Close", closeDrawer)),
    body, foot);
  const overlay = h("div", {class: "overlay", onclick: closeDrawer});
  drawerState.nodes = [overlay, panel];
  document.body.append(overlay, panel);

  const ctl = {close: closeDrawer, changed: onChange, refresh: null};
  async function show() {
    try {
      const d = await api("vm?name=" + encodeURIComponent(name));
      if (!drawerState.nodes || drawerState.nodes[1] !== panel) return; // closed, or another VM was opened meanwhile
      body.replaceChildren(...drawerSections(d, ctl));
      foot.replaceChildren(...drawerButtons(d, ctl));
      foot.classList.toggle("hide", !foot.childNodes.length);
    } catch (e) {
      body.replaceChildren(errorPanel(e, show));
    }
  }
  ctl.refresh = async () => {
    await show();
    if (onChange) await onChange();
  };
  await show();
}
