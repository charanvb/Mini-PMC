"use strict";
/* Mini PMC - schedules: the weekly strip, the 15-minute grid editor, and the Schedules pages.
   A schedule is a week of 672 quarter-hour cells (Monday 00:00 first), each R (run) or S (stop), in its own time zone. */

const CELLS = 96; // quarter-hours in a day
const WEEK = 7 * CELLS;
const stripCache = new Map();

/* A tiny picture of the week: one row per day (Mon on top), green where the VM runs. Cached per distinct grid. */
function stripBg(grid) {
  let url = stripCache.get(grid);
  if (url) return url;
  let rects = "";
  for (let d = 0; d < 7; d++) {
    let start = -1;
    for (let i = 0; i <= CELLS; i++) {
      const on = i < CELLS && grid[d * CELLS + i] === "R";
      if (on && start < 0) start = i;
      if (!on && start >= 0) {
        rects += `<rect x='${start}' y='${d}' width='${i - start}' height='.78'/>`;
        start = -1;
      }
    }
  }
  const svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 96 7' preserveAspectRatio='none' shape-rendering='crispEdges'>" +
    `<rect width='96' height='7' fill='#e5e9ef'/><g fill='#2fa84f'>${rects}</g></svg>`;
  url = `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
  stripCache.set(grid, url);
  return url;
}

function strip(grid, big) {
  const g = typeof grid === "string" && grid.length === WEEK ? grid : "S".repeat(WEEK);
  return h("div", {class: "strip" + (big ? " big" : ""), style: {backgroundImage: stripBg(g)},
    title: "Monday at the top, Sunday at the bottom. Green = running."});
}

function runHours(grid) {
  let n = 0;
  for (let i = 0; i < grid.length; i++) if (grid[i] === "R") n++;
  return n / 4;
}
function fmtHours(x) {
  return (Number.isInteger(x) ? String(x) : x.toFixed(2).replace(/0+$/, "")) + " h";
}

/* ------------------------------------------------------------------ grid editor */
/* gridEditor(initial, {readonly, onChange}) -> {node, get(), set(str), setAll(v), copyDay(from, [to...])}
   Press a cell and drag to another: the whole block between them is added (or removed). */
function gridEditor(initial, opts) {
  opts = opts || {};
  const ro = !!opts.readonly;
  let g = (typeof initial === "string" && initial.length === WEEK ? initial : "S".repeat(WEEK)).split("");
  const cells = [];
  const root = h("div", {class: "ged" + (ro ? " ro" : "")});
  const hint = ro ? "" : "Press a cell and drag to another to fill a whole block (several days at once too). The first cell decides whether you add or remove run time. Click a day name to fill that day.";
  const statusEl = h("div", {class: "ged-status"}, hint);

  root.appendChild(h("div"));
  for (let hr = 0; hr < 24; hr++) root.appendChild(h("div", {class: "ged-hour", style: {gridColumn: "span 4"}}, pad(hr)));
  for (let d = 0; d < 7; d++) {
    root.appendChild(h("div", {class: "ged-day", "data-day": d, title: ro ? null : "Click to fill or clear this whole day"}, DAYS[d]));
    for (let i = 0; i < CELLS; i++) {
      const idx = d * CELLS + i;
      const c = h("div", {class: "gc" + (i % 4 === 0 ? " q" : "") + (g[idx] === "R" ? " R" : ""), "data-i": idx});
      cells.push(c);
      root.appendChild(c);
    }
  }

  const redraw = () => { for (let i = 0; i < WEEK; i++) cells[i].classList.toggle("R", g[i] === "R"); };
  const changed = () => { if (opts.onChange) opts.onChange(g.join("")); };
  const clock = n => pad(Math.floor(n / 4)) + ":" + pad((n % 4) * 15);
  const showStatus = idx => {
    if (idx < 0) { statusEl.textContent = hint; return; }
    const q = idx % CELLS;
    statusEl.textContent = `${DAYS[Math.floor(idx / CELLS)]} ${clock(q)}-${clock(q + 1)} \u00b7 ${g[idx] === "R" ? "running" : "stopped"}`;
  };
  const cellOf = el => {
    const c = el && el.closest ? el.closest(".gc") : null;
    return c && root.contains(c) ? Number(c.dataset.i) : -1;
  };

  /* Press on one cell and drag to another: the whole rectangle (days x times) between them is painted at once.
     Pointer position is mapped to a cell by geometry, so dragging past the edge or over a gap still works. */
  let brush = null, anchor = -1, base = null, dirty = false;
  const cellAt = (x, y) => {
    const a = cells[0].getBoundingClientRect(), b = cells[CELLS].getBoundingClientRect(), z = cells[CELLS - 1].getBoundingClientRect();
    const w = (z.right - a.left) / CELLS, rowH = b.top - a.top;
    const col = Math.min(CELLS - 1, Math.max(0, Math.floor((x - a.left) / w)));
    const row = Math.min(6, Math.max(0, Math.floor((y - a.top) / rowH)));
    return row * CELLS + col;
  };
  const apply = idx => {
    const d0 = Math.min(Math.floor(anchor / CELLS), Math.floor(idx / CELLS)), d1 = Math.max(Math.floor(anchor / CELLS), Math.floor(idx / CELLS));
    const c0 = Math.min(anchor % CELLS, idx % CELLS), c1 = Math.max(anchor % CELLS, idx % CELLS);
    for (let i = 0; i < WEEK; i++) {
      const d = Math.floor(i / CELLS), c = i % CELLS;
      const v = d >= d0 && d <= d1 && c >= c0 && c <= c1 ? brush : base[i];
      if (g[i] !== v) { g[i] = v; cells[i].classList.toggle("R", v === "R"); }
    }
    dirty = true;
    showStatus(idx);
  };
  const end = () => {
    if (brush === null) return;
    brush = null;
    base = null;
    if (dirty) changed();
    dirty = false;
  };
  const toggleDay = d => {
    const full = g.slice(d * CELLS, (d + 1) * CELLS).every(c => c === "R");
    for (let i = 0; i < CELLS; i++) g[d * CELLS + i] = full ? "S" : "R";
    redraw();
    changed();
  };

  if (!ro) {
    root.addEventListener("pointerdown", e => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      const dayEl = e.target.closest ? e.target.closest(".ged-day") : null;
      if (dayEl) { toggleDay(Number(dayEl.dataset.day)); return; }
      const idx = cellOf(e.target);
      if (idx < 0) return;
      e.preventDefault();
      brush = g[idx] === "R" ? "S" : "R";
      anchor = idx;
      base = g.slice();
      dirty = false;
      root.setPointerCapture(e.pointerId);
      apply(idx);
    });
    root.addEventListener("pointerup", end);
    root.addEventListener("pointercancel", end);
    root.addEventListener("lostpointercapture", end);
  }
  root.addEventListener("pointermove", e => {
    if (brush === null) showStatus(cellOf(e.target));
    else apply(cellAt(e.clientX, e.clientY));
  });
  root.addEventListener("pointerleave", () => { if (brush === null) showStatus(-1); });

  const node = h("div", null,
    h("div", {style: {marginBottom: "8px"}},
      h("span", {class: "legend"}, h("span", {class: "sw R"}), "Running"),
      h("span", {class: "legend"}, h("span", {class: "sw"}), "Stopped"),
      h("span", {class: "mute"}, "15-minute steps")),
    h("div", {class: "ged-scroll"}, root),
    statusEl);

  return {
    node,
    get: () => g.join(""),
    set(str) {
      if (typeof str !== "string" || str.length !== WEEK) throw new Error(`A week must be ${WEEK} cells`);
      g = str.split("");
      redraw();
    },
    setAll(v) { g.fill(v); redraw(); changed(); },
    copyDay(from, tos) {
      for (const d of tos) for (let i = 0; i < CELLS; i++) g[d * CELLS + i] = g[from * CELLS + i];
      redraw();
      changed();
    },
  };
}

/* ------------------------------------------------------------------ time zones */
const COMMON_TZ = ["UTC", "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Amsterdam", "Europe/Madrid", "Europe/Rome",
  "Europe/Moscow", "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Asia/Shanghai", "Asia/Tokyo", "Australia/Sydney",
  "America/New_York", "America/Chicago", "America/Los_Angeles", "America/Sao_Paulo", "Etc/GMT-2"];
function tzDatalist(id, inUse) {
  return h("datalist", {id}, [...new Set(COMMON_TZ.concat(inUse || []))].sort().map(z => h("option", {value: z, label: `${z}  (${gmtLabel(z) || "?"})`}, gmtLabel(z))));
}

/* The "Time check": what the schedule says now, and the next switches in UTC, London and the schedule's own zone. */
function timeCheckView(tc) {
  const now = tc.now || {};
  const sw = tc.switches || [];
  return h("div", null,
    h("div", {class: "callout"}, icon("clock"), h("div", null,
      h("div", null, "Right now it is ", h("b", null, now.schedule || "?"), " in ", h("b", null, tc.tz),
        ` (UTC ${now.utc || "?"}, London ${now.london || "?"}).`),
      h("div", null, "The schedule says this should be ", stateChip(tc.now_state), " now."))),
    sw.length ? h("div", {class: "table-wrap", style: {marginTop: "12px"}}, h("table", {class: "t"},
      h("thead", null, h("tr", null, h("th", null, "Next change"), h("th", null, "UTC"), h("th", null, "London"), h("th", null, tc.tz))),
      h("tbody", null, sw.map(s => h("tr", null,
        h("td", null, s.to === "R" ? chip("Start", "run") : chip("Stop", "stop")),
        h("td", {class: "nowrap"}, s.utc), h("td", {class: "nowrap"}, s.london), h("td", {class: "nowrap"}, s.schedule))))))
      : h("p", {class: "mute"}, "This schedule never changes state."));
}

/* ------------------------------------------------------------------ Schedules list */
async function schedulesPage(ctx) {
  ctx.setTitle("Schedules");
  const list = (await api("schedules")).schedules;
  let query = "";
  const tbody = h("tbody");
  const count = h("span", {class: "mute"});
  const canDelete = can("admin");
  const selected = new Set();
  const deletable = s => s.kind === "grid" && !s.vms; // built-in schedules and schedules with VMs cannot be deleted
  const delBtn = btn("Delete selected", {kind: "outline-danger", icon: "trash", disabled: true, onclick: () => deleteSchedules([...selected])});
  const allBox = h("input", {type: "checkbox", "aria-label": "Select all deletable schedules"});

  function syncSelection() {
    delBtn.disabled = selected.size === 0;
    delBtn.lastChild.textContent = selected.size ? `Delete selected (${selected.size})` : "Delete selected";
  }
  function deleteSchedules(names) {
    if (!names.length) return;
    confirmDialog({
      title: names.length === 1 ? "Delete schedule" : `Delete ${names.length} schedules`, confirm: "Delete", kind: "danger",
      message: names.length === 1 ? `Delete the schedule '${names[0]}'? This cannot be undone.`
        : h("div", null, h("p", {class: "intro"}, "Delete these schedules? This cannot be undone."),
            h("ul", {class: "list-plain"}, names.slice(0, 15).map(n => h("li", null, n)), names.length > 15 ? h("li", null, `\u2026and ${names.length - 15} more`) : null)),
      run: async () => {
        const r = await api("schedule/remove", {names});
        if (r.errors && r.errors.length) showResults("Delete schedules", `${r.done || 0} deleted, ${r.errors.length} not deleted:`, r.errors);
        else toast(names.length === 1 ? `Schedule '${names[0]}' deleted.` : `${r.done} schedules deleted.`);
        route();
      },
    });
  }

  function openRow(name) {
    location.hash = "#/schedules/edit/" + encodeURIComponent(name);
  }
  function row(s) {
    const box = canDelete ? h("td", {class: "check"}, deletable(s) ? (() => {
      const cb = h("input", {type: "checkbox", checked: selected.has(s.name), "aria-label": "Select " + s.name});
      cb.addEventListener("click", e => e.stopPropagation());
      cb.addEventListener("change", () => { cb.checked ? selected.add(s.name) : selected.delete(s.name); syncSelection(); });
      return cb;
    })() : null) : null;
    const del = canDelete ? h("td", {class: "nowrap"}, (() => {
      const b = h("button", {type: "button", class: "icon-btn", "aria-label": "Delete " + s.name, disabled: !deletable(s),
        title: s.kind !== "grid" ? "Built-in schedules cannot be deleted" : s.vms ? `Move its ${s.vms} VM(s) to another schedule first` : "Delete this schedule"}, icon("trash"));
      b.addEventListener("click", e => { e.stopPropagation(); deleteSchedules([s.name]); });
      return b;
    })()) : null;
    const tr = h("tr", {class: "click", tabindex: 0}, box,
      h("td", null, h("b", null, s.name), s.kind !== "grid" ? h("span", {class: "sub"}, " built-in") : null),
      h("td", null, strip(s.grid)),
      h("td", null, s.description),
      h("td", {class: "nowrap"}, s.tz, " ", gmtLabel(s.tz) ? h("span", {class: "mute"}, `(${gmtLabel(s.tz)})`) : null),
      h("td", {class: "num"}, s.vms ? h("a", {href: "#/resources?schedule=" + encodeURIComponent(s.name)}, fmtNum(s.vms)) : "0"), del);
    tr.addEventListener("click", e => { if (!e.target.closest("a")) openRow(s.name); });
    tr.addEventListener("keydown", e => { if (e.key === "Enter" && e.target === tr) openRow(s.name); });
    return tr;
  }
  function draw() {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    const rows = list.filter(s => terms.every(t => `${s.name} ${s.description} ${s.tz}`.toLowerCase().includes(t)));
    count.textContent = `${rows.length} of ${list.length}`;
    allBox.onchange = () => {
      for (const s of rows.filter(deletable)) allBox.checked ? selected.add(s.name) : selected.delete(s.name);
      syncSelection();
      draw();
    };
    tbody.replaceChildren(...(rows.length ? rows.map(row)
      : [h("tr", null, h("td", {class: "empty-cell", colspan: 7}, "No schedules match your search."))]));
  }

  const search = h("input", {type: "text", placeholder: "Search schedules", "aria-label": "Search schedules"});
  search.addEventListener("input", () => { query = search.value; draw(); });
  draw();

  return h("div", null,
    pageHead("Schedules",
      btn("Export Excel", {icon: "file", onclick: () => downloadPost("export/schedules", {}, "minipmc-schedules.xlsx").catch(e => toast(e.message, "err"))}),
      canDelete ? delBtn : null,
      can("cloudops") ? btn("From text\u2026", {onclick: scheduleFromTextDialog, title: "Build a schedule from text such as \u201cUp 5 days (Mon-Fri) 10 AM to 10 PM IST\u201d"}) : null,
      can("cloudops") ? btn("New schedule", {kind: "primary", icon: "plus", onclick: () => { location.hash = "#/schedules/new"; }}) : null),
    h("div", {class: "toolbar"}, h("div", {class: "search"}, icon("search", 16), search), count),
    h("div", {class: "panel"}, h("div", {class: "table-wrap"}, h("table", {class: "t"},
      h("thead", null, h("tr", null, canDelete ? h("th", {class: "check"}, allBox) : null, h("th", null, "Name"), h("th", null, "Week"), h("th", null, "Description"), h("th", null, "Time zone"), h("th", {class: "num"}, "VMs"), canDelete ? h("th", null) : null)),
      tbody))));
}

function scheduleFromTextDialog() {
  formDialog({
    title: "Create a schedule from text",
    intro: "Paste the schedule the way it is written in your inventory. Mini PMC works out the days, the hours and the time zone.",
    fields: [
      {name: "text", label: "Schedule text", type: "textarea", rows: 3, required: true, placeholder: "Up 5 days (Mon-Fri) 10 AM to 10 PM IST"},
      {name: "name", label: "Name (optional)", hint: "Leave it empty to get an automatic name."},
    ],
    submit: "Create",
    run: async v => {
      const r = await api("schedule/add", {text: v.text, name: v.name.trim()});
      toast(`Schedule '${r.name}' is ready`);
      location.hash = "#/schedules/edit/" + encodeURIComponent(r.name);
    },
  });
}

/* ------------------------------------------------------------------ Schedule editor (new / edit) */
async function scheduleEditorPage(ctx) {
  const editing = ctx.parts[1] === "edit";
  const origName = editing ? ctx.parts.slice(2).join("/") : "";
  ctx.setTitle(editing ? "Edit schedule" : "New schedule");

  const all = (await api("schedules")).schedules;
  const meta = editing ? all.find(s => s.name === origName) : null;
  if (editing && !meta) throw new Error(`Schedule '${origName}' was not found.`);
  const builtin = !!meta && meta.kind !== "grid";
  const editable = can("cloudops") && !builtin;
  const vmCount = meta ? meta.vms : 0;

  const nameIn = h("input", {type: "text", value: origName, disabled: editing || !editable, placeholder: "e.g. IST_Mon-Fri_10-22"});
  const tzIn = h("input", {type: "text", value: meta ? meta.tz : "", list: "tz-list", disabled: !editable, placeholder: "e.g. Asia/Kolkata"});
  const tzGmt = h("span", {class: "chip"});
  const showGmt = () => { const g = gmtLabel(tzIn.value.trim()); tzGmt.textContent = g; tzGmt.style.display = g ? "" : "none"; };
  tzIn.addEventListener("input", showGmt);
  showGmt();
  const info = h("div");
  const formErr = h("div", {class: "form-err", role: "alert"});
  let seq = 0;

  async function refresh() {
    const mine = ++seq;
    const grid = ed.get();
    try {
      const p = await api("schedule/preview", {grid});
      if (mine !== seq) return;
      const hours = runHours(grid);
      info.replaceChildren(
        h("p", null, h("b", null, fmtHours(hours)), ` running per week of 168 h (${Math.round(hours / 168 * 100)}%)`),
        h("p", {class: "mute"}, p.description),
        h("div", {class: "windows"}, p.windows.map(w => chip(w))));
    } catch (e) {
      if (mine === seq) info.replaceChildren(h("p", {class: "mute"}, e.message));
    }
  }
  const refreshSoon = debounce(refresh, 250);
  const ed = gridEditor(meta ? meta.grid : "S".repeat(WEEK), {readonly: !editable, onChange: refreshSoon});

  function fillFromWindows() {
    formDialog({
      title: "Fill the week from windows",
      intro: "One window per line. This replaces what is in the grid now; you can fine-tune it afterwards.",
      fields: [{name: "windows", label: "Windows", type: "textarea", rows: 5, required: true,
        placeholder: "Mon-Fri 09:00-17:00\nSat 08:00-12:00\nSun 22:00-02:00", hint: "24-hour clock. A window may run past midnight."}],
      submit: "Fill the grid",
      run: async v => {
        const p = await api("schedule/preview", {windows: v.windows});
        ed.set(p.grid);
        refresh();
      },
    });
  }

  async function save() {
    const name = nameIn.value.trim(), tz = tzIn.value.trim(), grid = ed.get();
    if (!name) throw new Error("Please give the schedule a name.");
    if (!tz) throw new Error("Please choose a time zone.");
    if (editing) {
      const r = await api("schedule/update", {name: origName, tz, grid});
      toast(`Saved. ${plural(r.vms, "VM")} now follow the new times.`);
    } else {
      await api("schedule/add", {name, tz, grid});
      toast(`Schedule '${name}' created.`);
    }
    location.hash = "#/schedules";
  }
  const saveBtn = btn(editing ? "Save changes" : "Create schedule", {kind: "primary", icon: "check", onclick: async () => {
    formErr.textContent = "";
    const doSave = async () => {
      saveBtn.disabled = true;
      try { await save(); } catch (e) { formErr.textContent = e.message; } finally { saveBtn.disabled = false; }
    };
    if (editing && vmCount > 0) {
      confirmDialog({title: "Save this schedule?", confirm: "Save", kind: "primary",
        message: `${plural(vmCount, "VM")} use this schedule. They will follow the new times from now on.`,
        run: async () => { await doSave(); }});
    } else {
      await doSave();
    }
  }});

  const tools = editable ? h("div", {class: "tools"},
    btn("Copy Monday to Tue\u2013Fri", {sm: true, onclick: () => ed.copyDay(0, [1, 2, 3, 4])}),
    btn("Copy Monday to all days", {sm: true, onclick: () => ed.copyDay(0, [1, 2, 3, 4, 5, 6])}),
    btn("All running", {sm: true, onclick: () => ed.setAll("R")}),
    btn("All stopped", {sm: true, onclick: () => ed.setAll("S")}),
    btn("Fill from windows\u2026", {sm: true, onclick: fillFromWindows})) : null;

  const deleteBtn = editing && editable && can("admin") ? btn("Delete schedule", {kind: "outline-danger", icon: "trash", disabled: vmCount > 0,
    title: vmCount > 0 ? "Move its VMs to another schedule first" : "",
    onclick: () => confirmDialog({title: "Delete schedule", confirm: "Delete", kind: "danger",
      message: `Delete '${origName}'? This cannot be undone.`,
      run: async () => {
        await api("schedule/remove", {name: origName});
        toast("Schedule deleted.");
        location.hash = "#/schedules";
      }})}) : null;

  const notices = [];
  if (builtin) notices.push(h("div", {class: "callout", style: {marginBottom: "12px"}}, icon("info"), "This is a built-in schedule. It cannot be changed or removed."));
  else if (!can("cloudops")) notices.push(h("div", {class: "callout", style: {marginBottom: "12px"}}, icon("info"), "Only admins can change schedules. You can look at this one."));
  if (editing && vmCount > 0 && editable) notices.push(h("div", {class: "callout warn", style: {marginBottom: "12px"}}, icon("alert"),
    `${plural(vmCount, "VM")} use this schedule. Changing it changes when all of them start and stop.`));

  const nameField = field("Name", nameIn, editing ? "The name cannot be changed." : "Letters, numbers, dashes and underscores work best.");
  nameField.classList.add("full"); // a row of its own, so a long name is readable in full

  const view = h("div", null,
    h("a", {class: "back", href: "#/schedules"}, icon("left", 14), "Schedules"),
    pageHead(editing ? origName : "New schedule"),
    tzDatalist("tz-list", all.map(s => s.tz)),
    h("div", {class: "panel"}, h("div", {class: "panel-body"},
      notices,
      h("div", {class: "form-row"},
        nameField,
        field("Time zone", h("div", {class: "nowrap"}, tzIn, " ", tzGmt), "The grid below is in this zone. Summer/winter clock changes are handled automatically.")),
      tools,
      ed.node,
      h("div", {style: {marginTop: "14px"}}, info),
      formErr,
      h("div", {class: "tools", style: {marginTop: "14px"}},
        editable ? saveBtn : null,
        btn(editable ? "Cancel" : "Back", {onclick: () => { location.hash = "#/schedules"; }}),
        h("div", {class: "sp"}), deleteBtn))));

  refresh();

  if (editing) {
    const slot = h("div", {class: "panel-body"}, spinner("Checking the switch times\u2026"));
    view.appendChild(h("div", {class: "panel"}, h("div", {class: "panel-head"}, icon("clock", 16), "Time check"), slot));
    api("schedule/test?name=" + encodeURIComponent(origName)).then(
      tc => slot.replaceChildren(timeCheckView(tc)),
      e => slot.replaceChildren(h("p", {class: "mute"}, e.message)));
  }
  return view;
}
