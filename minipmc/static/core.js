"use strict";
/* CloudOps Scheduler - core helpers: DOM builder, icons, API client, dialogs and small widgets.
   The page is built only with DOM calls (never innerHTML), so nothing from the server can inject markup. */

const SVGNS = "http://www.w3.org/2000/svg";
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const APP_NAME = "CloudOps Scheduler";

/* ------------------------------------------------------------------ DOM */
function append(parent, kids) {
  for (const k of kids) {
    if (k === null || k === undefined || k === false || k === true) continue;
    if (Array.isArray(k)) append(parent, k);
    else parent.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}

/* h("div", {class: "x", onclick: fn}, child, [children], "text") - attrs may be null. */
function h(tag, attrs, ...kids) {
  if (attrs instanceof Node || Array.isArray(attrs) || typeof attrs === "string" || typeof attrs === "number") {
    kids.unshift(attrs);
    attrs = null;
  }
  const el = document.createElement(tag);
  let value = null;
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "value") value = v; // applied after the children exist (a <select> needs its options first)
    else if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "checked" || k === "selected") el[k] = true;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, kids);
  if (value !== null) el.value = value;
  return el;
}

function svgEl(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v !== null && v !== undefined && v !== false) el.setAttribute(k, String(v));
  }
  append(el, kids);
  return el;
}

/* Simple line icons (24x24 grid). */
const ICONS = {
  dashboard: ["M3 3h7v7H3z", "M14 3h7v7h-7z", "M14 14h7v7h-7z", "M3 14h7v7H3z"],
  server: ["M4 2h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z",
    "M4 14h16a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2z", "M6 6h.01", "M6 18h.01"],
  calendar: ["M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z", "M16 2v4", "M8 2v4", "M3 10h18"],
  shield: ["M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"],
  file: ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z", "M14 2v6h6", "M16 13H8", "M16 17H8", "M10 9H8"],
  sliders: ["M4 21v-7", "M4 10V3", "M12 21v-9", "M12 8V3", "M20 21v-5", "M20 12V3", "M1 14h6", "M9 8h6", "M17 16h6"],
  power: ["M18.36 6.64a9 9 0 1 1-12.73 0", "M12 2v10"],
  refresh: ["M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8", "M21 3v5h-5"],
  menu: ["M3 12h18", "M3 6h18", "M3 18h18"],
  search: ["M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z", "M21 21l-4.35-4.35"],
  plus: ["M12 5v14", "M5 12h14"],
  x: ["M18 6L6 18", "M6 6l12 12"],
  check: ["M20 6L9 17l-5-5"],
  down: ["M6 9l6 6 6-6"],
  up: ["M18 15l-6-6-6 6"],
  left: ["M15 18l-6-6 6-6"],
  trash: ["M3 6h18", "M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6", "M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2", "M10 11v6", "M14 11v6"],
  edit: ["M12 20h9", "M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"],
  user: ["M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2", "M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z"],
  lock: ["M5 11h14a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2z", "M7 11V7a5 5 0 0 1 10 0v4"],
  logout: ["M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4", "M16 17l5-5-5-5", "M21 12H9"],
  alert: ["M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z", "M12 9v4", "M12 17h.01"],
  info: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z", "M12 16v-4", "M12 8h.01"],
  clock: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z", "M12 6v6l4 2"],
  play: ["M5 3l14 9-14 9V3z"],
  stop: ["M6 6h12v12H6z"],
  pause: ["M6 4h4v16H6z", "M14 4h4v16h-4z"],
  zap: ["M13 2L3 14h9l-1 8 10-12h-9l1-8z"],
  arrowUp: ["M12 19V5", "M5 12l7-7 7 7"],
  arrowDown: ["M12 5v14", "M19 12l-7 7-7-7"],
};

function icon(name, size) {
  const s = size || 18;
  const svg = svgEl("svg", {viewBox: "0 0 24 24", width: s, height: s, fill: "none", stroke: "currentColor",
    "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round", class: "ic", "aria-hidden": "true"});
  for (const d of ICONS[name] || []) svg.appendChild(svgEl("path", {d}));
  return svg;
}

/* The product mark (also the favicon). */
function brandMark(size) {
  return h("img", {class: "brand-mark", src: "/static/logo.svg", alt: "", width: size, height: size});
}

/* Microsoft Azure and Google Cloud marks (24x24 grid, filled), used only to say which clouds are managed.
   The colour comes from CSS (currentColor), so they sit quietly inside chips and cards. */
const CLOUDS = {
  azure: {name: "Azure", full: "Microsoft Azure", cls: "az",
    d: "M22.379 23.343a1.62 1.62 0 0 0 1.536-2.14v.002L17.35 1.76A1.62 1.62 0 0 0 15.816.657H8.184A1.62 1.62 0 0 0 6.65 1.76L.086 21.204a1.62 1.62 0 0 0 1.536 2.139h4.741a1.62 1.62 0 0 0 1.535-1.103l.977-2.892 4.947 3.675c.28.208.618.32.966.32m-3.084-12.531 3.624 10.739a.54.54 0 0 1-.51.713v-.001h-.03a.54.54 0 0 1-.322-.106l-9.287-6.9h4.853m6.313 7.006c.116-.326.13-.694.007-1.058L9.79 1.76a1.722 1.722 0 0 0-.007-.02h6.034a.54.54 0 0 1 .512.366l6.562 19.445a.54.54 0 0 1-.338.684"},
  gcp: {name: "GCP", full: "Google Cloud", cls: "gcp",
    d: "M12.19 2.38a9.344 9.344 0 0 0-9.234 6.893c.053-.02-.055.013 0 0-3.875 2.551-3.922 8.11-.247 10.941l.006-.007-.007.03a6.717 6.717 0 0 0 4.077 1.356h5.173l.03.03h5.192c6.687.053 9.376-8.605 3.835-12.35a9.365 9.365 0 0 0-2.821-4.552l-.043.043.006-.05A9.344 9.344 0 0 0 12.19 2.38zm-.358 4.146c1.244-.04 2.518.368 3.486 1.15a5.186 5.186 0 0 1 1.862 4.078v.518c3.53-.07 3.53 5.262 0 5.193h-5.193l-.008.009v-.04H6.785a2.59 2.59 0 0 1-1.067-.23h.001a2.597 2.597 0 1 1 3.437-3.437l3.013-3.012A6.747 6.747 0 0 0 8.11 8.24c.018-.01.04-.026.054-.023a5.186 5.186 0 0 1 3.67-1.69z"},
};
function cloudLogo(provider, size) {
  const s = size || 16;
  const svg = svgEl("svg", {viewBox: "0 0 24 24", width: s, height: s, fill: "currentColor", class: "cloud-logo " + CLOUDS[provider].cls, "aria-hidden": "true"});
  svg.appendChild(svgEl("path", {d: CLOUDS[provider].d}));
  return svg;
}

/* ------------------------------------------------------------------ shared state, formatting */
const state = {me: null, status: null, skew: 0};
const ROLE_RANK = {reader: 1, cloudops: 2, admin: 3};
function can(role) {
  return !!state.me && ROLE_RANK[state.me.role] >= ROLE_RANK[role];
}

function pad(n) {
  return String(n).padStart(2, "0");
}
function serverNow() {
  return new Date(Date.now() + state.skew);
}
function toDate(s) {
  const d = s ? new Date(s) : null;
  return d && !isNaN(d) ? d : null;
}
/* The whole UI shows India Standard Time (UTC+05:30, no daylight saving), so a fixed offset is exact. */
const IST_OFFSET_MS = 330 * 60000;
/* A moment's IST clock fields: shift it by the offset, then read the shifted copy's UTC fields. */
function istParts(d) {
  const t = new Date(d.getTime() + IST_OFFSET_MS);
  return {y: t.getUTCFullYear(), mo: t.getUTCMonth(), d: t.getUTCDate(), h: t.getUTCHours(), mi: t.getUTCMinutes(),
    s: t.getUTCSeconds(), dow: DAYS[(t.getUTCDay() + 6) % 7]};
}
/* "31 Dec 2026 23:59 IST" */
function fmtDate(s) {
  const d = toDate(s);
  if (!d) return s || "";
  const p = istParts(d);
  return `${p.d} ${MONTHS[p.mo]} ${p.y} ${pad(p.h)}:${pad(p.mi)} IST`;
}
/* "31 Dec 2026" */
function fmtDay(s) {
  const d = toDate(s);
  if (!d) return s || "";
  const p = istParts(d);
  return `${p.d} ${MONTHS[p.mo]} ${p.y}`;
}
/* "2026-11-23 10:00:12" (IST) - for the audit log */
function fmtStamp(s) {
  const d = toDate(s);
  if (!d) return s || "";
  const p = istParts(d);
  return `${p.y}-${pad(p.mo + 1)}-${pad(p.d)} ${pad(p.h)}:${pad(p.mi)}:${pad(p.s)}`;
}
/* "Mon 14:35" (IST) */
function fmtWhen(s) {
  const d = toDate(s);
  if (!d) return s || "";
  const p = istParts(d);
  return `${p.dow} ${pad(p.h)}:${pad(p.mi)}`;
}
/* "14:35" - the IST clock in the top bar */
function fmtClock(d) {
  const p = istParts(d);
  return `${pad(p.h)}:${pad(p.mi)}`;
}
/* Server messages carry ISO UTC times ("2026-12-31T18:00:00Z"); show each one as an IST date and time. */
function istText(text) {
  return String(text == null ? "" : text).replace(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z/g, fmtDate);
}
/* "in 3 days" / "2 hours ago" */
function inWords(s) {
  const d = toDate(s);
  if (!d) return "";
  const diff = (d.getTime() - serverNow().getTime()) / 1000;
  const abs = Math.abs(diff);
  if (abs < 60) return diff >= 0 ? "in a moment" : "just now";
  let n, unit;
  if (abs < 3600) { n = Math.round(abs / 60); unit = "minute"; }
  else if (abs < 86400) { n = Math.round(abs / 3600); unit = "hour"; }
  else { n = Math.round(abs / 86400); unit = "day"; }
  const t = plural(n, unit);
  return diff >= 0 ? "in " + t : t + " ago";
}
/* "GMT+5:30" for Asia/Kolkata, "GMT" for UTC; "" if the browser does not know the zone. */
function gmtLabel(zone) {
  try {
    const p = new Intl.DateTimeFormat("en", {timeZone: zone, timeZoneName: "longOffset"}).formatToParts(new Date())
      .find(x => x.type === "timeZoneName");
    const m = p && /^GMT([+-])(\d{1,2})(?::(\d{2}))?$/.exec(p.value);
    if (!m) return p && p.value === "GMT" ? "GMT" : "";
    return `GMT${m[1]}${Number(m[2])}${m[3] && m[3] !== "00" ? ":" + m[3] : ""}`;
  } catch (e) {
    return "";
  }
}
/* POST to an export endpoint and save the returned file. */
async function downloadPost(path, body, fallbackName) {
  let res;
  try {
    res = await fetch("/api/" + path, {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-Requested-With": "minipmc"}, body: JSON.stringify(body || {})});
  } catch (e) {
    throw new Error("Cannot reach the CloudOps Scheduler service. Is it running?");
  }
  if (!res.ok) {
    let msg = `The service answered ${res.status}`;
    try { msg = (await res.json()).error || msg; } catch (e) { /* keep default */ }
    throw new Error(msg);
  }
  const cd = res.headers.get("Content-Disposition") || "";
  const m = /filename="?([^";]+)"?/.exec(cd);
  const url = URL.createObjectURL(await res.blob());
  const a = h("a", {href: url, download: m ? m[1] : fallbackName});
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
function fmtNum(n) {
  return Number(n || 0).toLocaleString("en-GB");
}
function plural(n, word) {
  return `${fmtNum(n)} ${word}${n === 1 ? "" : "s"}`;
}
function debounce(fn, ms) {
  let t = null;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}
function qs(params) {
  const p = [];
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") p.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
  return p.length ? "?" + p.join("&") : "";
}

/* ------------------------------------------------------------------ API */
let sessionLostHandler = null;
function onSessionLost(fn) {
  sessionLostHandler = fn;
}

/* api("vms") is a GET; api("override/set", {...}) is a POST (the header is the CSRF guard the server requires). */
async function api(path, body) {
  const opts = {credentials: "same-origin", headers: {}};
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers = {"Content-Type": "application/json", "X-Requested-With": "minipmc"};
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch("/api/" + path, opts);
  } catch (e) {
    throw Object.assign(new Error("Cannot reach the CloudOps Scheduler service. Is it running?"), {status: 0});
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (res.status === 401 && path !== "login" && path !== "me") {
    state.me = null;
    if (sessionLostHandler) sessionLostHandler();
    throw Object.assign(new Error("Your session has ended. Please log in again."), {status: 401});
  }
  if (!res.ok) throw Object.assign(new Error((data && data.error) || `The service answered ${res.status}`), {status: res.status});
  return data;
}

/* ------------------------------------------------------------------ widgets */
function iconBtn(name, title, onclick) {
  return h("button", {type: "button", class: "icon-btn", title, "aria-label": title, onclick}, icon(name));
}
/* btn("Save", {kind: "primary", icon: "check", sm: true, onclick}) */
function btn(label, o) {
  o = o || {};
  const cls = ["btn", o.kind, o.sm ? "sm" : ""].filter(Boolean).join(" ");
  return h("button", {type: "button", class: cls, title: o.title, disabled: o.disabled, onclick: o.onclick},
    o.icon ? icon(o.icon, o.sm ? 14 : 16) : null, label);
}
function chip(text, kind, title) {
  return h("span", {class: "chip" + (kind ? " " + kind : ""), title}, text);
}
function cloudChip(provider) {
  const c = CLOUDS[provider];
  return c ? h("span", {class: "chip " + c.cls, title: c.full}, cloudLogo(provider, 12), c.name) : chip(provider || "?");
}
/* What the schedule says right now: R run, S stop, "" not managed. */
function stateChip(says) {
  if (says === "R") return chip("Run", "run", "The schedule says this VM should be running now");
  if (says === "S") return chip("Stop", "stop", "The schedule says this VM should be stopped now");
  return chip("n/a", "", "No schedule, or the time zone data is not available");
}
/* The audit detail is a JSON string such as {"schedule":"X","by":"y"}; show it as "schedule: X \u00b7 by: y". */
function detailText(detail) {
  if (!detail) return "";
  let o = detail;
  if (typeof detail === "string") {
    try { o = JSON.parse(detail); } catch (e) { return istText(detail); }
  }
  if (o === null || typeof o !== "object") return istText(o);
  return Object.entries(o).map(([k, v]) => istText(`${k}: ${v !== null && typeof v === "object" ? JSON.stringify(v) : v}`)).join(" \u00b7 ");
}
function actionChip(action) {
  const a = String(action || "");
  return chip(a, /FAIL|ERROR/.test(a) ? "bad" : /OVERRIDE/.test(a) ? "ovr" : "");
}
/* The result lines of a connection check: [{level: ok|warn|fail, label, detail}] */
function checksList(results) {
  const word = {ok: "OK", warn: "WARN", fail: "FAIL"};
  return h("ul", {class: "checks"}, results.map(r => h("li", null,
    h("b", {class: r.level}, word[r.level] || r.level),
    h("div", null, h("div", null, r.label), r.detail ? h("div", {class: "mute"}, r.detail) : null))));
}
function spinner(text) {
  return h("div", {class: "loading"}, h("span", {class: "spin"}), text || "Loading...");
}
function emptyState(title, text, actions) {
  return h("div", {class: "empty"}, h("h3", null, title), text ? h("p", null, text) : null, actions || null);
}
function errorPanel(err, retry) {
  return h("div", {class: "panel"}, h("div", {class: "panel-body"},
    h("div", {class: "callout bad"}, icon("alert"), h("div", null,
      h("b", null, "Something went wrong"),
      h("div", null, (err && err.message) || String(err)),
      retry ? h("div", {class: "btns"}, btn("Try again", {sm: true, onclick: retry})) : null))));
}
function pageHead(title, ...actions) {
  return h("div", {class: "page-head"}, h("h2", null, title), h("div", {class: "sp"}), actions);
}
function tabs(items, current) {
  return h("div", {class: "tabs"}, items.map(i => h("a", {href: i.href, class: i.key === current ? "on" : ""}, i.label)));
}
/* kv([["Provider", node], ["Zone", "x"], null]) -> a label/value list; null rows are skipped. */
function kv(pairs) {
  return h("dl", {class: "kv"}, pairs.filter(Boolean).map(([k, v]) => [h("dt", null, k), h("dd", null, v)]));
}

/* ------------------------------------------------------------------ toasts */
function toast(msg, kind) {
  let box = document.getElementById("toasts");
  if (!box) {
    box = h("div", {id: "toasts"});
    document.body.appendChild(box);
  }
  const t = h("div", {class: "toast" + (kind ? " " + kind : ""), role: "status"},
    icon(kind === "err" ? "alert" : "check", 16), h("div", null, msg));
  const kill = () => t.remove();
  t.addEventListener("click", kill);
  box.appendChild(t);
  setTimeout(kill, kind === "err" ? 9000 : 4500);
}

/* ------------------------------------------------------------------ dialogs */
/* modal(title, body, [{label, kind, run}], {wide}). run may be async; if it throws, the message is shown
   inside the dialog and the dialog stays open. Returning false keeps it open too. */
function modal(title, body, actions, opts) {
  opts = opts || {};
  const dlg = h("dialog", {class: "modal" + (opts.wide ? " wide" : "")});
  const err = h("div", {class: "form-err", role: "alert"});
  const buttons = [];
  const close = () => {
    if (dlg.open) dlg.close();
    dlg.remove();
  };
  for (const a of actions || []) {
    const b = h("button", {type: "button", class: "btn " + (a.kind || "")}, a.label);
    b.addEventListener("click", async () => {
      err.textContent = "";
      if (!a.run) return close();
      buttons.forEach(x => { x.disabled = true; });
      const busy = h("span", {class: "spin"});
      b.prepend(busy); // shows that something is happening (bulk changes can take a few seconds)
      try {
        if ((await a.run(dlg)) !== false) close();
      } catch (e) {
        err.textContent = (e && e.message) || String(e);
      } finally {
        busy.remove();
        buttons.forEach(x => { x.disabled = false; });
      }
    });
    buttons.push(b);
  }
  dlg.append(
    h("div", {class: "modal-head"}, h("h3", null, title), iconBtn("x", "Close", close)),
    h("div", {class: "modal-body"}, body, err),
    h("div", {class: "modal-foot"}, buttons));
  dlg.addEventListener("close", () => dlg.remove());
  document.body.appendChild(dlg);
  dlg.showModal();
  const first = dlg.querySelector(".modal-body input:not([type=hidden]):not([type=checkbox]):not([type=radio]), .modal-body select, .modal-body textarea");
  if (first) first.focus();
  return {dlg, close, buttons, error: m => { err.textContent = m; }};
}

function field(label, input, hint) {
  return h("label", {class: "fld"}, h("span", {class: "fld-l"}, label), input, hint ? h("span", {class: "fld-h"}, hint) : null);
}

/* formDialog({title, intro, fields, submit, kind, wide, run(values, dlg)})
   field: {name, label, type: text|password|number|date|textarea|select|checkbox, value, hint, options, required,
           placeholder, rows, list, node + read (a custom control)} */
function formDialog(o) {
  const readers = {};
  const body = h("div", null, o.intro ? h("p", {class: "intro"}, o.intro) : null);
  for (const f of o.fields || []) {
    let node, read;
    if (f.node) {
      node = f.node;
      read = f.read;
    } else if (f.type === "checkbox") {
      const box = h("input", {type: "checkbox", name: f.name, checked: !!f.value});
      node = h("label", {class: "fld chk"}, box, h("span", null, f.label));
      read = () => box.checked;
    } else {
      let input;
      if (f.type === "select") {
        input = h("select", {name: f.name, value: f.value},
          (f.options || []).map(opt => {
            const [v, l] = Array.isArray(opt) ? opt : [opt, opt];
            return h("option", {value: v}, l);
          }));
      } else if (f.type === "textarea") {
        input = h("textarea", {name: f.name, rows: f.rows || 5, placeholder: f.placeholder, spellcheck: "false"});
        input.value = f.value || "";
      } else {
        input = h("input", {type: f.type || "text", name: f.name, value: f.value === undefined || f.value === null ? "" : f.value,
          placeholder: f.placeholder, autocomplete: f.autocomplete || "off", list: f.list, min: f.min, max: f.max,
          maxlength: f.maxlength, disabled: f.disabled});
      }
      if (f.type === "select" && f.search) {
        // typing narrows the list; the current choice always stays listed
        const box = h("input", {type: "search", placeholder: "Type to search...", autocomplete: "off", "aria-label": "Search " + f.label});
        // a closed <select> cuts a long label off, so a long choice is also written out in full underneath
        const full = h("div", {class: "fld-full", hidden: true});
        const showFull = () => {
          const text = input.selectedIndex >= 0 ? input.options[input.selectedIndex].text : "";
          full.hidden = text.length <= 90;
          full.textContent = full.hidden ? "" : "Selected: " + text;
        };
        input.addEventListener("change", showFull);
        box.addEventListener("input", () => {
          const words = box.value.toLowerCase().split(/\s+/).filter(Boolean);
          const hits = [...input.options].map(o => words.every(w => o.text.toLowerCase().includes(w)));
          const idx = hits.indexOf(true);
          if (idx >= 0 && !hits[input.selectedIndex]) input.selectedIndex = idx;
          [...input.options].forEach((o, i) => { o.hidden = o.disabled = !hits[i] && i !== input.selectedIndex; });
          showFull();
        });
        showFull();
        node = h("div", {class: "fld"}, h("span", {class: "fld-l"}, f.label), box, input, full, f.hint ? h("span", {class: "fld-h"}, f.hint) : null);
      } else {
        node = field(f.label, input, f.hint);
      }
      read = () => input.value;
    }
    if (f.name && read) readers[f.name] = read;
    body.appendChild(node);
  }
  const run = async dlg => {
    const vals = {};
    for (const [k, r] of Object.entries(readers)) vals[k] = r();
    for (const f of o.fields || []) {
      if (f.required && f.name && !String(vals[f.name] === undefined || vals[f.name] === null ? "" : vals[f.name]).trim()) {
        throw new Error((f.label || f.name) + " is required");
      }
    }
    return o.run(vals, dlg);
  };
  const m = modal(o.title, body, [{label: "Cancel"}, {label: o.submit || "Save", kind: o.kind || "primary", run}], {wide: o.wide});
  body.addEventListener("keydown", e => {
    if (e.key === "Enter" && e.target.tagName === "INPUT") {
      e.preventDefault();
      m.buttons[m.buttons.length - 1].click();
    }
  });
  return m;
}

/* confirmDialog({title, message (text or node), confirm, kind: primary|danger, run}) */
function confirmDialog(o) {
  const body = typeof o.message === "string" ? h("p", {class: "intro"}, o.message) : o.message;
  return modal(o.title, body, [{label: "Cancel"}, {label: o.confirm || "Confirm", kind: o.kind || "primary", run: o.run}]);
}

/* showResults("Override set", "3 VMs done, 2 failed:", ["name: reason", ...]) */
function showResults(title, summary, errors) {
  const bad = errors || [];
  modal(title, h("div", null,
    h("p", {class: "intro"}, summary),
    bad.length ? h("ul", {class: "list-plain"}, bad.map(e => h("li", null, e))) : null),
  [{label: "Close", kind: "primary"}], {wide: bad.length > 6});
}
