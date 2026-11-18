import json
import threading
import urllib.error
import urllib.request

import pytest

from minipmc import db, importer, users, web
from minipmc.logs import setup_logging
from minipmc.service import Store


@pytest.fixture
def srv(tmp_path):
    home = str(tmp_path)
    setup_logging(home)
    st = Store(home, actor="setup")
    st.init()
    users.add_user(st, "boss", "password-1", "admin")
    users.add_user(st, "ops", "password-2", "operator")
    users.add_user(st, "eye", "password-3", "viewer")
    st.add_vm("vm1", "azure", "sub", "rg", importer.ALWAYS_UP)
    server = web.make_server(home, "127.0.0.1", 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", home
    server.shutdown()
    server.server_close()


class Client:
    def __init__(self, base):
        self.base, self.cookie = base, None

    def call(self, path, body=None, headers=None):
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("X-Requested-With", "minipmc")
            h["Content-Type"] = "application/json"
        if self.cookie:
            h["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + "/api/" + path, data=data, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                sc = r.headers.get("Set-Cookie")
                if sc:
                    self.cookie = sc.split(";")[0]
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def login(self, u, p):
        return self.call("login", {"username": u, "password": p})


def test_page_served_and_api_requires_login(srv):
    base, _ = srv
    assert b"Mini PMC" in urllib.request.urlopen(base + "/").read()
    assert Client(base).call("vms")[0] == 401


def test_login_and_wrong_password(srv):
    c = Client(srv[0])
    assert c.login("boss", "nope")[0] == 401
    assert c.login("boss", "password-1")[0] == 200
    assert c.call("me")[1] == {"username": "boss", "role": "admin"}
    assert c.call("logout", {})[0] == 200
    assert c.call("me")[0] == 401


def test_post_needs_csrf_header(srv):
    c = Client(srv[0])
    c.login("boss", "password-1")
    code, body = c.call("pause", {"paused": True}, headers={"X-Requested-With": ""})
    assert code == 400


def test_roles_are_enforced(srv):
    viewer, op = Client(srv[0]), Client(srv[0])
    viewer.login("eye", "password-3")
    op.login("ops", "password-2")
    assert viewer.call("vms")[0] == 200
    assert viewer.call("hold/set", {"name": "vm1", "forever": True, "reason": "x"})[0] == 403
    assert op.call("hold/set", {"name": "vm1", "forever": True, "reason": "x"})[0] == 200
    assert op.call("vm/remove", {"name": "vm1"})[0] == 403
    assert op.call("users")[0] == 403


def test_hold_flow_shows_in_overview_and_audit(srv):
    c = Client(srv[0])
    c.login("ops", "password-2")
    assert c.call("hold/set", {"name": "vm1", "days": 2, "reason": ""})[0] == 400  # reason required
    assert c.call("hold/set", {"name": "vm1", "days": 2, "reason": "testing"})[0] == 200
    vm = c.call("vms")[1]["vms"][0]
    assert vm["held"] and vm["hold_by"] == "ops" and vm["says"] == "R"
    assert c.call("summary")[1]["held"] == 1
    assert c.call("hold/clear", {"name": "vm1"})[0] == 200
    actions = [e["action"] for e in c.call("events")[1]["events"]]
    assert "HOLD_SET" in actions and "HOLD_CLEARED" in actions and "LOGIN_OK" in actions


def test_admin_manages_vms_schedules_users(srv):
    c = Client(srv[0])
    c.login("boss", "password-1")
    assert c.call("vm/add", {"name": "vm2", "provider": "gcp", "scope": "p", "group": "z", "schedule": ""})[0] == 200
    assert c.call("vm/update", {"name": "vm2", "schedule": importer.ALWAYS_PARKED, "enabled": False})[0] == 200
    code, body = c.call("schedule/add", {"text": "IST 10AM-6PM Mon-Fri"})
    assert code == 200 and body["name"].startswith("IST")
    assert c.call("schedule?name=" + body["name"].replace(" ", "%20"))[1]["windows"][0].startswith("Mon")
    code, body = c.call("schedule/add", {"name": "Office", "tz": "Asia/Kolkata", "windows": "Mon-Fri 09:00-17:00"})
    assert code == 200 and c.call("schedule/test?name=Office")[1]["switches"][0]["to"] == "R"
    code, body = c.call("vm/bulk", {"text": "z1,azure,s,g\nbad", "schedule": "Office"})
    assert body["added"] == ["z1"] and len(body["errors"]) == 1
    assert c.call("user/save", {"create": True, "username": "new", "password": "longenough", "role": "viewer"})[0] == 200
    assert c.call("user/remove", {"username": "boss"})[0] == 400  # last admin protected


def test_lockout_after_repeated_failures(srv):
    c = Client(srv[0])
    for _ in range(5):
        assert c.login("boss", "bad")[0] == 401
    assert c.login("boss", "password-1")[0] == 429


def test_users_rules(tmp_path):
    st = Store(str(tmp_path))
    st.init()
    with pytest.raises(Exception):
        users.add_user(st, "a", "short", "admin")
    users.add_user(st, "a", "longenough", "admin")
    assert users.authenticate(st, "a", "longenough") and not users.authenticate(st, "a", "wrong")
    users.set_disabled(st, "a", False)
    with pytest.raises(Exception):
        users.set_disabled(st, "a", True)  # last admin


def test_schema_migrates_from_v1(tmp_path):
    import sqlite3
    home = str(tmp_path)
    Store(home).init()
    con = sqlite3.connect(db.db_path(home))
    con.execute("DROP TABLE users")
    con.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    con.commit()
    con.close()
    assert db.init(home) is False
    st = Store(home)
    users.add_user(st, "a", "longenough", "admin")
