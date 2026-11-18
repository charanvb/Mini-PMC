"use strict";
/* Mini PMC - the shell: login, side navigation, top bar and the hash router. Loaded last. */

const root = document.getElementById("root");
const NAV_KEY = "minipmc.nav";
const NAV = [
  {key: "dashboard", label: "Dashboard", icon: "dashboard", href: "#/dashboard"},
  {key: "resources", label: "Resources", icon: "server", href: "#/resources"},
  {key: "schedules", label: "Schedules", icon: "calendar", href: "#/schedules"},
  {key: "overrides", label: "Overrides", icon: "shield", href: "#/overrides"},
  {key: "audit", label: "Audit Log", icon: "file", href: "#/audit"},
  {key: "settings", label: "Settings", icon: "sliders", href: "#/settings"},
];
const PAGES = {
  dashboard: dashboardPage, resources: resourcesPage, schedules: schedulesPage,
  overrides: overridesPage, audit: auditPage, settings: settingsPage,
};
const MODE_LABEL = {
  readonly: ["Read-only", "Mini PMC only watches and records. It does not start or stop any VM yet."],
  dryrun: ["Dry run", "Mini PMC logs what it would do, but does not start or stop any VM."],
  pilot: ["Pilot", "Mini PMC acts only on the VMs chosen for the pilot."],
  live: ["Live", "Mini PMC starts and stops VMs on their schedules."],
};

let shell = null; // the pieces of the logged-in layout
let timers = [];
let routeSeq = 0; // a page that finishes after the user has moved on is thrown away
let sessionNotice = false; // so a burst of failing calls shows the "session ended" message once
let closeMenu = () => {};

/* ------------------------------------------------------------------ router */
/* "#/resources?state=run" -> {parts: ["resources"], params: {state: "run"}}. Split on "/" first, then decode each part. */
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const q = raw.indexOf("?");
  const params = {};
  if (q >= 0) for (const [k, v] of new URLSearchParams(raw.slice(q + 1))) params[k] = v;
  let parts;
  try {
    parts = (q < 0 ? raw : raw.slice(0, q)).split("/").filter(Boolean).map(decodeURIComponent);
  } catch (e) {
    parts = ["not-found"]; // a malformed %-escape
  }
  return {parts, params};
}

function resolve(parts) {
  const first = parts[0] || "dashboard";
  if (first === "schedules" && (parts[1] === "new" || parts[1] === "edit")) return {key: "schedules", page: scheduleEditorPage};
  return Object.hasOwn(PAGES, first) ? {key: first, page: PAGES[first]} : null;
}

function setNav(key) {
  if (!shell) return;
  for (const a of shell.links) a.classList.toggle("on", a.dataset.key === key);
}

async function route() {
  if (!state.me || !shell) return;
  const seq = ++routeSeq;
  closeDrawer();
  closeMenu();
  const {parts, params} = parseHash();
  const hit = resolve(parts);
  setNav(hit ? hit.key : "");
  refreshStatus();
  const content = shell.content;
  if (!hit) {
    document.title = "Not found \u00b7 Mini PMC";
    content.replaceChildren(h("div", {class: "panel"},
      emptyState("Page not found", "There is nothing at this address.", h("a", {href: "#/dashboard"}, "Go to the dashboard"))));
    return;
  }
  content.replaceChildren(spinner());
  const ctx = {parts, params, setTitle: t => { if (seq === routeSeq) document.title = t + " \u00b7 Mini PMC"; }};
  try {
    const node = await hit.page(ctx);
    if (seq !== routeSeq) return;
    content.replaceChildren(node);
    window.scrollTo(0, 0);
  } catch (e) {
    if (seq !== routeSeq || !state.me) return;
    content.replaceChildren(errorPanel(e, route));
  }
}
/* Pages call this after a change that affects what they show. */
function refreshPage() {
  return route();
}

/* ------------------------------------------------------------------ status, clock, pause */
async function refreshStatus() {
  try {
    const s = await api("status");
    state.status = s;
    const t = Date.parse(s.now_utc);
    if (!isNaN(t)) state.skew = t - Date.now();
  } catch (e) {
    // keep the last known status; the page itself reports real problems
  }
  drawStatus();
  tickClock();
}

function tickClock() {
  if (!shell) return;
  const now = serverNow();
  shell.clock.textContent = `UTC ${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}  \u00b7  London ${fmtZone(now, "Europe/London")}`;
  shell.clock.title = "The server's clock. Each schedule runs in its own time zone; see the Schedules page.";
}

function drawStatus() {
  if (!shell) return;
  const s = state.status;
  if (!s) {
    shell.status.replaceChildren();
    return;
  }
  const [label, tip] = MODE_LABEL[s.mode] || [s.mode, ""];
  const nodes = [];
  if (s.paused) {
    nodes.push(h("span", {class: "chip mode paused", title: "The scheduler is paused: nothing is started or stopped."}, icon("pause", 12), "Scheduler paused"));
  }
  nodes.push(h("span", {class: "chip mode" + (s.mode === "live" ? " live" : ""), title: tip}, label + " mode"));
  if (can("admin")) {
    nodes.push(btn(s.paused ? "Resume" : "Pause", {sm: true, icon: s.paused ? "play" : "pause", onclick: () => togglePause(s.paused),
      title: s.paused ? "Let the scheduler carry on" : "Stop the scheduler from starting or stopping anything"}));
  }
  shell.status.replaceChildren(...nodes);
}

function togglePause(paused) {
  confirmDialog({
    title: paused ? "Resume the scheduler?" : "Pause the scheduler?",
    message: paused
      ? "Mini PMC will carry on following the schedules."
      : "Nothing will be started or stopped, and no block/unblock signals will be sent, until someone resumes the scheduler. Schedules and overrides are not changed.",
    confirm: paused ? "Resume" : "Pause", kind: paused ? "primary" : "danger",
    run: async () => {
      await api("pause", {paused: !paused});
      toast(paused ? "Scheduler resumed." : "Scheduler paused.");
      await refreshPage();
    },
  });
}

/* ------------------------------------------------------------------ layout */
function userMenu() {
  const me = state.me;
  const wrap = h("div", {class: "user"});
  const avatar = h("button", {type: "button", class: "avatar", "aria-label": "Account menu", "aria-haspopup": "true", "aria-expanded": "false"},
    me.username.slice(0, 1));
  let menu = null;
  closeMenu = () => {
    if (!menu) return;
    menu.remove();
    menu = null;
    avatar.setAttribute("aria-expanded", "false");
  };
  avatar.addEventListener("click", e => {
    e.stopPropagation();
    if (menu) { closeMenu(); return; }
    menu = h("div", {class: "menu", role: "menu"},
      h("div", {class: "who"}, h("b", null, me.username), h("div", {class: "sub"}, me.role)),
      h("button", {type: "button", role: "menuitem", onclick: () => { closeMenu(); location.hash = "#/settings/account"; }}, icon("user", 16), "My account"),
      h("button", {type: "button", role: "menuitem", onclick: logout}, icon("logout", 16), "Log out"));
    menu.addEventListener("click", ev => ev.stopPropagation());
    wrap.appendChild(menu);
    avatar.setAttribute("aria-expanded", "true");
  });
  wrap.appendChild(avatar);
  return wrap;
}

function buildShell() {
  let collapsed = false;
  try { collapsed = localStorage.getItem(NAV_KEY) === "collapsed"; } catch (e) { /* storage is blocked: start open */ }

  const links = NAV.map(n => {
    const a = h("a", {class: "nav", href: n.href, title: n.label, "data-key": n.key}, icon(n.icon, 18), h("span", {class: "lbl"}, n.label));
    a.addEventListener("click", e => {
      if (location.hash === n.href) { e.preventDefault(); refreshPage(); } // same address: no hashchange, so reload by hand
    });
    return a;
  });
  const clock = h("span", {class: "clock"});
  const status = h("div", {class: "statusbox"});
  const content = h("main", {class: "content", id: "content"});

  const find = h("input", {type: "text", placeholder: "Search VMs\u2026", "aria-label": "Search all VMs", autocomplete: "off"});
  find.addEventListener("keydown", e => {
    if (e.key !== "Enter" || !find.value.trim()) return;
    location.hash = "#/resources" + qs({q: find.value.trim()});
    find.value = "";
    find.blur();
  });

  const el = h("div", {class: "shell" + (collapsed ? " collapsed" : "")},
    h("aside", {class: "side"},
      h("div", {class: "brand"}, h("span", {class: "logo"}, icon("power", 18)), h("span", {class: "lbl"}, "Mini PMC")),
      h("nav", {"aria-label": "Main"}, links),
      h("div", {class: "foot"}, "Server times are UTC")),
    h("div", {class: "main"},
      h("header", {class: "top"},
        iconBtn("menu", "Show or hide the menu", () => {
          const c = el.classList.toggle("collapsed");
          try { localStorage.setItem(NAV_KEY, c ? "collapsed" : "open"); } catch (e) { /* ignore */ }
        }),
        h("div", {class: "search"}, icon("search", 16), find),
        h("div", {class: "sp"}), clock, status, userMenu()),
      content));
  root.replaceChildren(el);
  shell = {el, content, clock, status, links};
}

function stopTimers() {
  timers.forEach(clearInterval);
  timers = [];
}

async function startApp() {
  buildShell();
  if (!location.hash || location.hash === "#") history.replaceState(null, "", "#/dashboard");
  stopTimers();
  timers.push(setInterval(tickClock, 15000), setInterval(refreshStatus, 60000));
  drawStatus();
  await route();
}

/* ------------------------------------------------------------------ login / logout */
function showLogin(message, problem) {
  stopTimers();
  shell = null;
  closeMenu = () => {};
  closeDrawer();
  document.title = "Log in \u00b7 Mini PMC";
  const user = h("input", {type: "text", name: "username", autocomplete: "username", autocapitalize: "off", spellcheck: "false", "aria-label": "Username"});
  const pass = h("input", {type: "password", name: "password", autocomplete: "current-password", "aria-label": "Password"});
  const err = h("div", {class: "form-err", role: "alert"}, problem || "");
  const go = h("button", {type: "submit", class: "btn primary"}, "Log in");
  const form = h("form", {class: "login-card"},
    h("div", {class: "logo"}, icon("power", 24)),
    h("h1", null, "Mini PMC"),
    h("p", {class: "mute"}, "Sign in to manage VM schedules."),
    message ? h("div", {class: "notice"}, message) : null,
    field("Username", user), field("Password", pass), err, go);
  form.addEventListener("submit", async e => {
    e.preventDefault();
    err.textContent = "";
    if (!user.value.trim() || !pass.value) {
      err.textContent = "Enter your username and password.";
      return;
    }
    go.disabled = true;
    try {
      state.me = await api("login", {username: user.value.trim(), password: pass.value});
    } catch (ex) {
      err.textContent = ex.message;
      pass.value = "";
      pass.focus();
      go.disabled = false;
      return;
    }
    sessionNotice = false;
    await startApp();
  });
  root.replaceChildren(h("div", {class: "login-wrap"}, form));
  user.focus();
}

async function logout() {
  closeMenu();
  sessionNotice = true; // we are leaving on purpose: no "session ended" message
  try { await api("logout", {}); } catch (e) { /* the session was already gone */ }
  state.me = null;
  state.status = null;
  history.replaceState(null, "", location.pathname);
  showLogin();
}

/* ------------------------------------------------------------------ start */
document.addEventListener("click", () => closeMenu());
document.addEventListener("keydown", e => { if (e.key === "Escape") closeMenu(); });
window.addEventListener("hashchange", () => { if (state.me) route(); });
onSessionLost(() => {
  if (sessionNotice) return;
  sessionNotice = true;
  showLogin("Your session has ended. Please log in again.");
});

(async function boot() {
  try {
    state.me = await api("me");
    sessionNotice = false;
    await startApp();
  } catch (e) {
    state.me = null;
    showLogin(null, e.status === 401 ? "" : e.message);
  }
})();
