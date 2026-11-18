"""Dashboard web server (standard library only). JSON API under /api, the page at / and its files under /static."""
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import alerts, creds as cr, db, exports, executor, grid as g, health, mailer, reports, scheduler, signals, users
from . import status as vmstatus
from .service import Store, UserError, iso, parse_until, parse_utc
from .tz import get_tz, gmt_label

log = logging.getLogger("minipmc.web")
REFRESH_MAX = 200  # VMs per refresh call; the page sends bigger selections in several calls
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml", ".ico": "image/x-icon"}
# the page may load only its own files; nothing inline except styles, nothing from other sites
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
SESSION_HOURS = 12
COOKIE = "minipmc_session"
RANK = {"reader": 0, "admin": 1}
MAX_FAILS, LOCK_SECONDS = 5, 60


class Forbidden(Exception):
    pass


class Download:
    """Returned by an API handler instead of JSON to send a file."""
    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def __init__(self, data: bytes, filename: str, ctype: str = XLSX):
        self.data, self.filename, self.ctype = data, filename, ctype


class App:
    def __init__(self, home: str):
        self.home = home
        self.sessions = {}  # token -> {"username", "role", "expires"}
        self.fails = {}  # ip -> [count, locked_until]
        self.lock = threading.Lock()

    def store(self, actor: str) -> Store:
        return Store(self.home, actor=actor)

    def new_session(self, user: dict) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            self.sessions = {t: s for t, s in self.sessions.items() if s["expires"] > now}
            self.sessions[token] = {**user, "expires": now + SESSION_HOURS * 3600}
        return token

    def session(self, token):
        with self.lock:
            s = self.sessions.get(token or "")
            return s if s and s["expires"] > time.time() else None

    def drop(self, token):
        with self.lock:
            self.sessions.pop(token or "", None)

    def drop_user(self, username: str, keep: str = "") -> None:
        """End every login of one user (after a password reset, disable or removal), except the token in `keep`."""
        with self.lock:
            self.sessions = {t: s for t, s in self.sessions.items()
                             if t == keep or s["username"].lower() != username.lower()}

    def locked(self, ip: str) -> bool:
        with self.lock:
            return self.fails.get(ip, [0, 0])[1] > time.time()

    def note_login(self, ip: str, ok: bool) -> None:
        with self.lock:
            if ok:
                self.fails.pop(ip, None)
                return
            n = self.fails.get(ip, [0, 0])[0] + 1
            self.fails[ip] = [0, time.time() + LOCK_SECONDS] if n >= MAX_FAILS else [n, 0]


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = "MiniPMC"

    def log_message(self, fmt, *args):  # request lines are not logged; failures and logins are
        pass

    # ---------------------------------------------------------- plumbing
    def _send(self, code: int, body: bytes, ctype: str, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200, headers=None):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json", headers)

    def _token(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c[COOKIE].value if COOKIE in c else None

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise UserError("request too large")
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n))
        except ValueError:
            raise UserError("bad JSON")
        if not isinstance(data, dict):
            raise UserError("bad JSON")
        return data

    # ---------------------------------------------------------- dispatch
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _static(self, name: str):
        ctype = "text/html; charset=utf-8" if name == "index.html" else STATIC_TYPES.get(os.path.splitext(name)[1].lower())
        if not ctype or name not in os.listdir(STATIC):  # only files really in static\ (no paths) of a known type
            return self._json({"error": "not found"}, 404)
        with open(os.path.join(STATIC, name), "rb") as f:
            body = f.read()
        self._send(200, body, ctype, {"Content-Security-Policy": CSP} if name == "index.html" else None)

    def _handle(self, method: str):
        path = urlparse(self.path)
        try:
            if method == "GET" and path.path == "/healthz":  # for the watchdog; says nothing but up/down
                age = vmstatus.beat_age(self.app.store("system"), "tick")
                ok = scheduler.healthy(self.app.store("system"))
                return self._json({"ok": ok, "last_cycle_seconds_ago": None if age is None else int(age)}, 200 if ok else 503)
            if method == "GET" and path.path == "/":
                return self._static("index.html")
            if method == "GET" and path.path.startswith("/static/"):
                return self._static(path.path[len("/static/"):])
            if not path.path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            name = path.path[5:]
            if method == "POST" and self.headers.get("X-Requested-With") != "minipmc":
                return self._json({"error": "missing X-Requested-With header"}, 400)
            if (method, name) == ("POST", "login"):
                return self._login()
            sess = self.app.session(self._token())
            if sess:  # a removed, disabled or re-roled user must not keep an old login
                role = users.active_role(self.app.store(sess["username"]), sess["username"])
                if role is None:
                    self.app.drop(self._token())
                    sess = None
                else:
                    sess["role"] = role
            if not sess:
                return self._json({"error": "login required"}, 401)
            route = ROUTES.get((method, name))
            if not route:
                return self._json({"error": "not found"}, 404)
            fn, need = route
            if RANK[sess["role"]] < RANK[need]:
                raise Forbidden()
            result = fn(self, self.app.store(sess["username"]), sess, parse_qs(path.query))
            if isinstance(result, Download):
                return self._send(200, result.data, result.ctype,
                                  {"Content-Disposition": f'attachment; filename="{result.filename}"'})
            self._json(result)
        except Forbidden:
            self._json({"error": "your role is not allowed to do this"}, 403)
        except UserError as e:
            self._json({"error": str(e)}, 400)
        except Exception:
            log.exception("unhandled error in %s %s", method, self.path)
            self._json({"error": "internal error (see logs\\error.log)"}, 500)

    def _login(self):
        ip = self.client_address[0]
        if self.app.locked(ip):
            return self._json({"error": "too many failed logins; wait a minute"}, 429)
        b = self._body()
        name = str(b.get("username", ""))
        user = users.authenticate(self.app.store(name[:64]), name, str(b.get("password", "")))
        self.app.note_login(ip, bool(user))
        if not user:
            log.warning("failed login for %r from %s", name[:64], ip)
            return self._json({"error": "wrong username or password"}, 401)
        token = self.app.new_session(user)
        cookie = f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_HOURS * 3600}"
        self._json(user, headers={"Set-Cookie": cookie})


# -------------------------------------------------------------- API handlers: (handler, store, session, query)

def api_me(h, st, s, q):
    return {"username": s["username"], "role": s["role"]}


def api_status(h, st, s, q):
    """Cheap facts for the top bar (checked on every page change)."""
    age = vmstatus.beat_age(st, "tick")
    with db.connect(st.home) as con:
        queued = con.execute("SELECT COUNT(*) FROM ops WHERE outcome IN ('QUEUED','PENDING')").fetchone()[0]
    return {"paused": st.paused, "mode": st.get_setting("mode", "readonly"), "now_utc": iso(datetime.now(timezone.utc)),
            "scheduler": {"ok": scheduler.healthy(st), "seconds_ago": None if age is None else int(age), "open_ops": queued}}


def api_logout(h, st, s, q):
    h.app.drop(h._token())
    return {"ok": True}


def _name(q) -> str:
    return (q.get("name") or [""])[0]


def _names(b: dict) -> list:
    """One VM ('name') or several ('names') from a request body."""
    names = b.get("names")
    if names is None:
        names = [b.get("name", "")]
    if not isinstance(names, list) or not 0 < len(names) <= 5000:
        raise UserError("choose between 1 and 5000 VMs")
    return [str(n) for n in names]


def _each(names: list, fn) -> dict:
    """Apply `fn` to each VM name. One bad name does not stop the rest (a single bad name is an error)."""
    done, errors = 0, []
    for n in names:
        try:
            fn(n)
            done += 1
        except UserError as e:
            if len(names) == 1:
                raise
            errors.append(f"{n}: {e}")
    return {"done": done, "errors": errors}


def api_summary(h, st, s, q):
    rows = st.overview()
    now = datetime.now(timezone.utc)
    over = [r for r in rows if r["override"]]
    limit = iso(now + timedelta(days=14))
    soon = sorted((r for r in over if r["override_until"] and r["override_until"] < limit),
                  key=lambda r: r["override_until"])
    return {"paused": st.paused, "mode": st.get_setting("mode", "readonly"), "vms": len(rows),
            "azure": sum(r["provider"] == "azure" for r in rows), "gcp": sum(r["provider"] == "gcp" for r in rows),
            "overrides": len(over), "unmanaged": sum(r["schedule"] is None for r in rows),
            "disabled": sum(not r["enabled"] for r in rows),
            "should_run": sum(r["says"] == "R" and r["enabled"] and not r["override"] for r in rows),
            "should_stop": sum(r["says"] == "S" and r["enabled"] and not r["override"] for r in rows),
            "overrides_ending_soon": [{"name": r["name"], "until": r["override_until"], "reason": r["override_reason"]}
                                      for r in soon],
            "upcoming": st.upcoming(24, now),
            "problems": [{"level": a, "message": m} for a, m in st.validate() if a != "INFO"][:50],
            "now_utc": iso(now)}


def api_vms(h, st, s, q):
    return {"vms": st.overview()}


def api_vm(h, st, s, q):
    vm, _, events = st.get_vm(_name(q))
    out = {"vm": next(r for r in st.overview() if r["name"] == vm["name"]), "events": [dict(e) for e in events],
           "windows": [], "tz": None, "grid": None, "time_check": None}
    if vm["schedule"]:
        sc = st.get_schedule(vm["schedule"])
        out["tz"], out["grid"] = sc["tz"], sc["grid"]
        out["windows"] = [g.run_label(a, n) for a, n in g.runs(g.from_string(sc["grid"]))]
        try:
            out["time_check"] = st.time_check(vm["schedule"])
        except ValueError:
            pass  # time zone data is not installed on this machine
    return out


def api_schedules(h, st, s, q):
    return {"schedules": [{"name": r["name"], "kind": r["kind"], "tz": r["tz"], "description": r["description"],
                           "vms": r["vm_count"], "grid": r["grid"]} for r in st.list_schedules()]}


def api_schedule(h, st, s, q):
    r = st.get_schedule(_name(q))
    return {"name": r["name"], "tz": r["tz"], "description": r["description"], "grid": r["grid"],
            "windows": [g.run_label(a, n) for a, n in g.runs(g.from_string(r["grid"]))]}


def api_events(h, st, s, q):
    try:
        n = max(1, min(int((q.get("limit") or ["200"])[0]), 1000))
    except ValueError:
        n = 200
    return {"events": [dict(r) for r in st.recent_events(n, (q.get("q") or [""])[0])]}


def _until(b: dict):
    """When an override ends, from the request: 'forever', a date/time ('until'), or a number of 'days'."""
    if b.get("forever"):
        return None
    if b.get("until"):
        return parse_until(str(b["until"]))
    try:
        days = float(b.get("days"))
    except (TypeError, ValueError):
        raise UserError("give days, until, or forever")
    if not 0 < days <= 3650:
        raise UserError("days must be between 0 and 3650")
    return datetime.now(timezone.utc) + timedelta(days=days)


def api_overrides(h, st, s, q):
    now = datetime.now(timezone.utc)
    rows = []
    for o in st.list_overrides():
        row = dict(o)
        row["active"] = row["expires_at"] is None or parse_utc(row["expires_at"]) > now
        rows.append(row)
    return {"overrides": rows}


def api_override_set(h, st, s, q):
    b = h._body()
    reason = str(b.get("reason", "")).strip()
    if not reason:
        raise UserError("a reason is required")
    until = _until(b)
    return _each(_names(b), lambda n: st.set_override(n, until, reason))


def api_override_clear(h, st, s, q):
    return _each(_names(h._body()), st.clear_override)


def api_pause(h, st, s, q):
    st.set_setting("paused", "1" if h._body().get("paused") else "0")
    return {"ok": True}


def api_vm_add(h, st, s, q):
    b = h._body()
    st.add_vm(str(b.get("name", "")), str(b.get("provider", "")), str(b.get("scope", "")), str(b.get("group", "")),
              b.get("schedule") or None, str(b.get("notes", "")))
    return {"ok": True}


def api_vm_update(h, st, s, q):
    b = h._body()
    if "schedule" not in b and "enabled" not in b:
        raise UserError("nothing to change")

    def apply(name):
        if "schedule" in b:
            st.set_schedule(name, b["schedule"] or None)
        if "enabled" in b:
            st.set_enabled(name, bool(b["enabled"]))
    return _each(_names(b), apply)


def api_vm_remove(h, st, s, q):
    return _each(_names(h._body()), st.remove_vm)


def api_schedule_add(h, st, s, q):
    b = h._body()
    name, tz = str(b.get("name", "")), str(b.get("tz", ""))
    if b.get("grid") is not None:
        return {"name": st.add_schedule_grid(name, tz, str(b["grid"]))}
    if b.get("windows") is not None:
        return {"name": st.add_schedule_custom(name, tz, str(b["windows"]))}
    return {"name": st.add_schedule_from_text(str(b.get("text", "")), name or None)}


def api_schedule_update(h, st, s, q):
    b = h._body()
    return {"vms": st.update_schedule(str(b.get("name", "")), str(b.get("tz", "")), str(b.get("grid", "")))}


def api_schedule_remove(h, st, s, q):
    st.remove_schedule(str(h._body().get("name", "")))
    return {"ok": True}


def api_schedule_preview(h, st, s, q):
    """Turn 'Mon-Fri 09:00-17:00' lines into a grid, and/or describe a grid. Changes nothing."""
    b = h._body()
    if b.get("windows") is not None:
        text = g.to_string(Store.grid_from_windows(str(b["windows"]))[0])
    else:
        text = Store.check_grid(str(b.get("grid", "")))
    grid = g.from_string(text)
    return {"grid": text, "description": g.describe(grid), "windows": [g.run_label(a, n) for a, n in g.runs(grid)]}


def api_vm_bulk(h, st, s, q):
    b = h._body()
    return st.add_vms_bulk(str(b.get("text", "")), b.get("schedule") or None, str(b.get("provider", "")).lower(),
                           str(b.get("scope", "")), str(b.get("group", "")))


def api_schedule_test(h, st, s, q):
    try:
        return st.time_check(_name(q))
    except ValueError as e:  # time zone data not installed
        raise UserError(str(e))


def api_users(h, st, s, q):
    return {"users": [dict(u) for u in users.list_users(st)]}


def api_user_save(h, st, s, q):
    b = h._body()
    name = str(b.get("username", ""))
    if b.get("create"):
        users.add_user(st, name, str(b.get("password", "")), str(b.get("role", "reader")))
        return {"ok": True}
    if b.get("password"):
        users.set_password(st, name, str(b["password"]))
        h.app.drop_user(name, keep=h._token() if name.lower() == s["username"].lower() else "")
    if b.get("role"):
        users.set_role(st, name, str(b["role"]))
    if "disabled" in b:
        users.set_disabled(st, name, bool(b["disabled"]))
    return {"ok": True}


def api_user_remove(h, st, s, q):
    name = str(h._body().get("username", ""))
    users.remove_user(st, name)
    h.app.drop_user(name)
    return {"ok": True}


def api_change_password(h, st, s, q):
    b = h._body()
    if not users.authenticate(st, s["username"], str(b.get("old", ""))):
        raise UserError("current password is wrong")
    users.set_password(st, s["username"], str(b.get("new", "")))
    h.app.drop_user(s["username"], keep=h._token())
    return {"ok": True}


def api_connections(h, st, s, q):
    """What is configured, for the Connections page. Never includes the Logic App URL's secret or any key."""
    try:
        cfg = cr.load_settings(st.home)
    except cr.CredentialError as e:
        return {"configured": False, "error": str(e)}
    az, gcp, la = cfg.get("azure") or {}, cfg.get("gcp") or {}, cfg.get("logic_app") or {}
    direct, secret = (la.get("url") or "").strip(), (la.get("url_secret_name") or "").strip()
    if direct:
        source, target = "settings file (logic_app.url)", signals.safe_target(direct)
    elif secret:
        source, target = f"Key Vault secret '{secret}'", ""
    else:
        source, target = "", ""
    return {"configured": True,
            "azure": {"uami_client_id": az.get("uami_client_id", ""),
                      "key_vault": urlparse(az.get("key_vault_url", "")).hostname or ""},
            "gcp": {"key_secret_name": gcp.get("key_secret_name", "")},
            "logic_app": {"configured": bool(source), "source": source, "target": target,
                          "timeout_seconds": la.get("timeout_seconds", 30), "retries": la.get("retries", 3)}}


def api_connections_test(h, st, s, q):
    vm = str(h._body().get("vm") or "").strip()
    return {"results": health.check_connections(st, vm or None)}


def api_signal_test(h, st, s, q):
    """Really send one block/unblock signal for one VM (admin only; recorded in the audit log)."""
    b = h._body()
    action = str(b.get("action", ""))
    if action not in signals.ACTIONS:
        raise UserError("action must be block or unblock")
    vm = st.get_vm(str(b.get("name", "")))[0]
    try:
        app = signals.LogicApp(cr.Credentials(cr.load_settings(st.home)))
    except cr.CredentialError as e:
        raise UserError(str(e))
    r = signals.send_signal(st, action, [vm], app=app, reason="manual test from the dashboard")
    return {"ok": r["ok"], "detail": r["detail"], "status": r["status"], "attempts": r["attempts"],
            "action": action, "vm": vm["name"]}


def _creds(st) -> cr.Credentials:
    try:
        return cr.Credentials(cr.load_settings(st.home))
    except cr.CredentialError as e:
        raise UserError(str(e))


def api_vm_refresh(h, st, s, q):
    """Read the live power state of the chosen VMs now (read-only on the cloud side)."""
    names = _names(h._body())
    if len(names) > REFRESH_MAX:
        raise UserError(f"refresh at most {REFRESH_MAX} VMs per call")
    return vmstatus.refresh(st, _creds(st), names)


def _name_list(b: dict):
    names = b.get("names")
    if names is None:
        return None
    if not isinstance(names, list) or len(names) > 20000:
        raise UserError("names must be a list")
    return [str(n) for n in names]


def api_export_resources(h, st, s, q):
    names = _name_list(h._body())
    data = exports.resources_xlsx(st, names)
    st.log_event("EXPORT", "", what="resources", vms=len(names) if names is not None else "all")
    return Download(data, f"MiniPMC-resources-{exports.stamp()}.xlsx")


def api_export_schedules(h, st, s, q):
    st.log_event("EXPORT", "", what="schedules")
    return Download(exports.schedules_xlsx(st), f"MiniPMC-schedules-{exports.stamp()}.xlsx")


def api_mode(h, st, s, q):
    """Change what the scheduler may do: readonly | dryrun | pilot | live."""
    b = h._body()
    mode = str(b.get("mode", ""))
    if mode not in executor.MODES:
        raise UserError("mode must be one of: " + ", ".join(executor.MODES))
    warnings = []
    if mode == "pilot":
        raw = b.get("pilot_vms", [])
        raw = raw if isinstance(raw, list) else str(raw).replace(";", ",").replace("\n", ",").split(",")
        known = {v["name"].lower(): v["name"] for v in st.list_vms()}
        names = [str(x).strip() for x in raw if str(x).strip()]
        unknown = [n for n in names if n.lower() not in known]
        if not names or unknown:
            raise UserError("choose at least one VM for the pilot" if not names else "unknown VM(s): " + ", ".join(unknown[:10]))
        st.set_setting("pilot_vms", ",".join(known[n.lower()] for n in names))
    if mode == "live" and str(b.get("confirm", "")) != "LIVE":
        raise UserError("type LIVE to confirm that the scheduler may start and stop every managed VM")
    if mode in ("pilot", "live"):
        try:
            cfg = cr.load_settings(st.home)
        except (cr.CredentialError, ValueError):
            cfg = {}
        if not signals.LogicApp(cr.Credentials(cfg)).configured:
            warnings.append("The block/unblock Logic App is not configured; VMs will still be started/stopped but no signals go out.")
        if not scheduler.healthy(st):
            warnings.append("The scheduler loop is not running (start the Mini PMC task / `serve`); nothing happens until it runs.")
    st.set_setting("mode", mode)
    st.set_setting_quiet("last_slot", "")  # plan again right away under the new mode
    return {"ok": True, "mode": mode, "warnings": warnings}


def _email_state(st) -> dict:
    cfg = {}
    try:
        cfg = cr.load_settings(st.home)
    except cr.CredentialError:
        pass
    section = cfg.get("email_logic_app", {}) or {}
    tz = st.get_setting("report_tz", "UTC") or "UTC"
    return {"configured": bool((section.get("url") or "").strip() or (section.get("url_secret_name") or "").strip()),
            "report_to": st.get_setting("report_to", "") or section.get("to", ""),
            "alert_to": st.get_setting("alert_to", "") or section.get("alert_to", "") or section.get("to", ""),
            "report_auto": st.get_setting("report_auto", "0") == "1",
            "report_time": st.get_setting("report_time", "08:00") or "08:00",
            "report_tz": tz, "report_tz_label": gmt_label(tz), "report_last": st.get_setting("report_last", "")}


def api_email(h, st, s, q):
    return _email_state(st)


def api_email_save(h, st, s, q):
    b = h._body()
    out = {}
    for key in ("report_to", "alert_to"):
        if key in b:
            text = str(b[key]).strip()
            bad = mailer.bad_addresses(text)
            if bad:
                raise UserError("not a valid email address: " + ", ".join(bad[:5]))
            out[key] = "; ".join(mailer.split_addresses(text))
    if "report_time" in b:
        t = str(b["report_time"])
        try:
            hh, mm = (int(x) for x in t.split(":"))
            assert 0 <= hh < 24 and 0 <= mm < 60
        except (ValueError, AssertionError):
            raise UserError("time must look like 08:00")
        out["report_time"] = f"{hh:02d}:{mm:02d}"
    if "report_tz" in b:
        try:
            get_tz(str(b["report_tz"]))
        except ValueError:
            raise UserError(f"unknown time zone '{b['report_tz']}'")
        out["report_tz"] = str(b["report_tz"])
    if "report_auto" in b:
        out["report_auto"] = "1" if b["report_auto"] else "0"
    for k, v in out.items():
        if st.get_setting(k, "\0") != v:
            st.set_setting(k, v)
    return _email_state(st)


def api_report_send(h, st, s, q):
    """Email the report now (to the saved recipients, or to `to` for a one-off)."""
    b = h._body()
    to = str(b.get("to") or "").strip()
    if to and mailer.bad_addresses(to):
        raise UserError("not a valid email address: " + ", ".join(mailer.bad_addresses(to)[:5]))
    r = reports.send(st, _creds(st), to or None)
    return r


def api_email_test(h, st, s, q):
    """A small test email (no attachment) to the alert recipients, to prove the Logic App path works."""
    creds = _creds(st)
    to = alerts.recipients(st, creds)
    app = mailer.Mailer(creds)
    if not app.configured:
        return {"ok": False, "detail": "the email Logic App is not configured (email_logic_app in config\\settings.json)"}
    if not to:
        return {"ok": False, "detail": "no alert recipient is set"}
    r = app.send(to, "[Mini PMC] test email", alerts.wrap("Test email from Mini PMC",
                                                         ["If you can read this, alert emails will arrive."]))
    st.log_event("EMAIL_TEST", "", ok=r["ok"], to=len(to))
    return {"ok": r["ok"], "detail": r["detail"], "to": to}


ROUTES = {
    ("POST", "vm/refresh"): (api_vm_refresh, "reader"),
    ("POST", "export/resources"): (api_export_resources, "reader"), ("POST", "export/schedules"): (api_export_schedules, "reader"),
    ("POST", "mode"): (api_mode, "admin"), ("GET", "email"): (api_email, "admin"), ("POST", "email/save"): (api_email_save, "admin"),
    ("POST", "report/send"): (api_report_send, "admin"), ("POST", "email/test"): (api_email_test, "admin"),
    ("GET", "me"): (api_me, "reader"), ("GET", "status"): (api_status, "reader"), ("POST", "logout"): (api_logout, "reader"),
    ("GET", "summary"): (api_summary, "reader"), ("GET", "vms"): (api_vms, "reader"), ("GET", "vm"): (api_vm, "reader"),
    ("GET", "schedules"): (api_schedules, "reader"), ("GET", "schedule"): (api_schedule, "reader"),
    ("GET", "schedule/test"): (api_schedule_test, "reader"), ("POST", "schedule/preview"): (api_schedule_preview, "reader"),
    ("GET", "events"): (api_events, "reader"), ("POST", "change-password"): (api_change_password, "reader"),
    ("GET", "overrides"): (api_overrides, "reader"),
    ("POST", "override/set"): (api_override_set, "admin"), ("POST", "override/clear"): (api_override_clear, "admin"),
    ("POST", "pause"): (api_pause, "admin"), ("POST", "connections/test"): (api_connections_test, "admin"),
    ("POST", "vm/add"): (api_vm_add, "admin"), ("POST", "vm/bulk"): (api_vm_bulk, "admin"),
    ("POST", "vm/update"): (api_vm_update, "admin"), ("POST", "vm/remove"): (api_vm_remove, "admin"),
    ("POST", "schedule/add"): (api_schedule_add, "admin"), ("POST", "schedule/update"): (api_schedule_update, "admin"),
    ("POST", "schedule/remove"): (api_schedule_remove, "admin"),
    ("GET", "connections"): (api_connections, "admin"), ("POST", "signal/test"): (api_signal_test, "admin"),
    ("GET", "users"): (api_users, "admin"), ("POST", "user/save"): (api_user_save, "admin"),
    ("POST", "user/remove"): (api_user_remove, "admin"),
}


def make_server(home: str, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": App(home)})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve(home: str, host: str, port: int, run_scheduler: bool = True) -> None:
    srv = make_server(home, host, port)
    log.info("dashboard listening on http://%s:%s", host, srv.server_address[1])
    if run_scheduler:
        scheduler.Scheduler(Store(home, actor="scheduler")).start()
        log.info("scheduler started (mode: %s)", Store(home).get_setting("mode", "readonly"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        log.info("dashboard stopped")
