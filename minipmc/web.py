"""Dashboard web server (standard library only). JSON API under /api, one static page at /."""
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

from . import grid as g, users
from .service import Store, UserError, iso, parse_until

log = logging.getLogger("minipmc.web")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SESSION_HOURS = 12
COOKIE = "minipmc_session"
RANK = {"viewer": 0, "operator": 1, "admin": 2}
MAX_FAILS, LOCK_SECONDS = 5, 60


class Forbidden(Exception):
    pass


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

    def _handle(self, method: str):
        path = urlparse(self.path)
        try:
            if method == "GET" and path.path == "/":
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if not path.path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            name = path.path[5:]
            if method == "POST" and self.headers.get("X-Requested-With") != "minipmc":
                return self._json({"error": "missing X-Requested-With header"}, 400)
            if (method, name) == ("POST", "login"):
                return self._login()
            sess = self.app.session(self._token())
            if not sess:
                return self._json({"error": "login required"}, 401)
            route = ROUTES.get((method, name))
            if not route:
                return self._json({"error": "not found"}, 404)
            fn, need = route
            if RANK[sess["role"]] < RANK[need]:
                raise Forbidden()
            self._json(fn(self, self.app.store(sess["username"]), sess, parse_qs(path.query)))
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


def api_logout(h, st, s, q):
    h.app.drop(h._token())
    return {"ok": True}


def api_summary(h, st, s, q):
    rows = st.overview()
    held = [r for r in rows if r["held"]]
    limit = iso(datetime.now(timezone.utc) + timedelta(days=14))
    soon = [r for r in held if r["hold_until"] and r["hold_until"] < limit]
    return {"paused": st.paused, "vms": len(rows), "azure": sum(r["provider"] == "azure" for r in rows),
            "gcp": sum(r["provider"] == "gcp" for r in rows), "held": len(held),
            "unmanaged": sum(r["schedule"] is None for r in rows),
            "disabled": sum(not r["enabled"] for r in rows),
            "should_run": sum(r["says"] == "R" and r["enabled"] and not r["held"] for r in rows),
            "should_stop": sum(r["says"] == "S" and r["enabled"] and not r["held"] for r in rows),
            "holds_ending_soon": [{"name": r["name"], "until": r["hold_until"]} for r in soon],
            "problems": [{"level": a, "message": m} for a, m in st.validate() if a != "INFO"][:50],
            "now_utc": iso(datetime.now(timezone.utc))}


def api_vms(h, st, s, q):
    return {"vms": st.overview()}


def api_schedules(h, st, s, q):
    return {"schedules": [{"name": r["name"], "tz": r["tz"], "description": r["description"],
                           "vms": r["vm_count"]} for r in st.list_schedules()]}


def api_schedule(h, st, s, q):
    r = st.get_schedule((q.get("name") or [""])[0])
    return {"name": r["name"], "tz": r["tz"], "description": r["description"], "grid": r["grid"],
            "windows": [g.run_label(a, n) for a, n in g.runs(g.from_string(r["grid"]))]}


def api_events(h, st, s, q):
    try:
        n = max(1, min(int((q.get("limit") or ["200"])[0]), 1000))
    except ValueError:
        n = 200
    return {"events": [dict(r) for r in st.recent_events(n, (q.get("q") or [""])[0])]}


def api_hold_set(h, st, s, q):
    b = h._body()
    reason = str(b.get("reason", "")).strip()
    if not reason:
        raise UserError("a reason is required")
    if b.get("forever"):
        until = None
    elif b.get("until"):
        until = parse_until(str(b["until"]))
    else:
        try:
            until = datetime.now(timezone.utc) + timedelta(days=float(b.get("days")))
        except (TypeError, ValueError):
            raise UserError("give days, until, or forever")
    st.set_hold(str(b.get("name", "")), until, reason)
    return {"ok": True}


def api_hold_clear(h, st, s, q):
    st.clear_hold(str(h._body().get("name", "")))
    return {"ok": True}


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
    name = str(b.get("name", ""))
    if "schedule" in b:
        st.set_schedule(name, b["schedule"] or None)
    if "enabled" in b:
        st.set_enabled(name, bool(b["enabled"]))
    return {"ok": True}


def api_vm_remove(h, st, s, q):
    st.remove_vm(str(h._body().get("name", "")))
    return {"ok": True}


def api_schedule_add(h, st, s, q):
    b = h._body()
    return {"name": st.add_schedule_from_text(str(b.get("text", "")), b.get("name") or None)}


def api_users(h, st, s, q):
    return {"users": [dict(u) for u in users.list_users(st)]}


def api_user_save(h, st, s, q):
    b = h._body()
    name = str(b.get("username", ""))
    if b.get("create"):
        users.add_user(st, name, str(b.get("password", "")), str(b.get("role", "viewer")))
        return {"ok": True}
    if b.get("password"):
        users.set_password(st, name, str(b["password"]))
    if b.get("role"):
        users.set_role(st, name, str(b["role"]))
    if "disabled" in b:
        users.set_disabled(st, name, bool(b["disabled"]))
    return {"ok": True}


def api_user_remove(h, st, s, q):
    users.remove_user(st, str(h._body().get("username", "")))
    return {"ok": True}


def api_change_password(h, st, s, q):
    b = h._body()
    if not users.authenticate(st, s["username"], str(b.get("old", ""))):
        raise UserError("current password is wrong")
    users.set_password(st, s["username"], str(b.get("new", "")))
    return {"ok": True}


ROUTES = {
    ("GET", "me"): (api_me, "viewer"), ("POST", "logout"): (api_logout, "viewer"),
    ("GET", "summary"): (api_summary, "viewer"), ("GET", "vms"): (api_vms, "viewer"),
    ("GET", "schedules"): (api_schedules, "viewer"), ("GET", "schedule"): (api_schedule, "viewer"),
    ("GET", "events"): (api_events, "viewer"), ("POST", "change-password"): (api_change_password, "viewer"),
    ("POST", "hold/set"): (api_hold_set, "operator"), ("POST", "hold/clear"): (api_hold_clear, "operator"),
    ("POST", "pause"): (api_pause, "operator"),
    ("POST", "vm/add"): (api_vm_add, "admin"), ("POST", "vm/update"): (api_vm_update, "admin"),
    ("POST", "vm/remove"): (api_vm_remove, "admin"), ("POST", "schedule/add"): (api_schedule_add, "admin"),
    ("GET", "users"): (api_users, "admin"), ("POST", "user/save"): (api_user_save, "admin"),
    ("POST", "user/remove"): (api_user_remove, "admin"),
}


def make_server(home: str, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": App(home)})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve(home: str, host: str, port: int) -> None:
    srv = make_server(home, host, port)
    log.info("dashboard listening on http://%s:%s", host, srv.server_address[1])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        log.info("dashboard stopped")
