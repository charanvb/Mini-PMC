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
    users.add_user(st, "ops", "password-2", "admin")
    users.add_user(st, "eye", "password-3", "reader")
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
    assert c.call("me")[1] == {"username": "boss", "role": "admin", "must_change": False}
    assert c.call("logout", {})[0] == 200
    assert c.call("me")[0] == 401


def test_post_needs_csrf_header(srv):
    c = Client(srv[0])
    c.login("boss", "password-1")
    code, body = c.call("pause", {"paused": True}, headers={"X-Requested-With": ""})
    assert code == 400


def test_roles_are_enforced(srv):
    reader, admin = Client(srv[0]), Client(srv[0])
    reader.login("eye", "password-3")
    admin.login("ops", "password-2")
    assert reader.call("vms")[0] == 200
    for path, body in [("override/set", {"name": "vm1", "forever": True, "reason": "x"}), ("pause", {"paused": True}),
                       ("vm/remove", {"name": "vm1"}), ("vm/bulk", {"text": "a"}), ("connections/test", {}),
                       ("signal/test", {"name": "vm1", "action": "block"}), ("schedule/add", {})]:
        assert reader.call(path, body)[0] == 403, path
    assert reader.call("users")[0] == 403 and reader.call("connections")[0] == 403
    assert admin.call("override/set", {"name": "vm1", "forever": True, "reason": "x"})[0] == 200
    assert admin.call("users")[0] == 200


def test_bulk_add_with_shared_cloud_details(srv):
    c = Client(srv[0])
    c.login("boss", "password-1")
    body = {"text": "n1\nn2\tALWAYS_UP\nvm1\nn3,azure,s,g\n\nname\tx", "schedule": "ALWAYS_PARKED",
            "provider": "gcp", "scope": "prj", "group": "europe-west4-a"}
    r = c.call("vm/bulk", body)[1]
    assert r["added"] == ["n1", "n2", "n3"]
    assert len(r["errors"]) == 1 and "vm1" in r["errors"][0]  # vm1 already exists
    vms = {v["name"]: v for v in c.call("vms")[1]["vms"]}
    assert vms["n1"]["provider"] == "gcp" and vms["n1"]["schedule"] == "ALWAYS_PARKED"
    assert vms["n2"]["schedule"] == "ALWAYS_UP" and vms["n3"]["provider"] == "azure"


def test_override_flow_shows_in_overview_and_audit(srv):
    c = Client(srv[0])
    c.login("ops", "password-2")
    assert c.call("override/set", {"name": "vm1", "days": 2, "reason": ""})[0] == 400  # reason required
    assert c.call("override/set", {"name": "vm1", "days": 0, "reason": "x"})[0] == 400
    assert c.call("override/set", {"name": "nope", "days": 2, "reason": "x"})[0] == 400
    assert c.call("override/set", {"name": "vm1", "days": 2, "reason": "testing"})[1] == {"done": 1, "errors": []}
    vm = c.call("vms")[1]["vms"][0]
    assert vm["override"] and vm["override_by"] == "ops" and vm["says"] == "R"
    assert c.call("summary")[1]["overrides"] == 1
    listed = c.call("overrides")[1]["overrides"]
    assert [(o["vm_name"], o["active"], o["reason"]) for o in listed] == [("vm1", True, "testing")]
    assert c.call("override/clear", {"name": "vm1"})[0] == 200
    assert c.call("override/clear", {"name": "vm1"})[0] == 400  # nothing to clear
    actions = [e["action"] for e in c.call("events")[1]["events"]]
    assert "OVERRIDE_SET" in actions and "OVERRIDE_CLEARED" in actions and "LOGIN_OK" in actions


def test_bulk_calls_report_each_failure_and_do_the_rest(srv):
    boss = Client(srv[0])
    boss.login("boss", "password-1")
    boss.call("vm/add", {"name": "vm2", "provider": "gcp", "scope": "p", "group": "z", "schedule": ""})
    r = boss.call("override/set", {"names": ["vm1", "vm2", "ghost"], "until": "2099-01-01", "reason": "freeze"})[1]
    assert r["done"] == 2 and len(r["errors"]) == 1 and "ghost" in r["errors"][0]
    vms = {v["name"]: v for v in boss.call("vms")[1]["vms"]}
    assert vms["vm1"]["override"] and vms["vm2"]["override"]
    assert boss.call("vm/update", {"names": ["vm1", "vm2"], "schedule": importer.ALWAYS_PARKED})[1]["done"] == 2
    assert boss.call("vm/update", {"names": ["vm1", "vm2"], "enabled": False})[1]["done"] == 2
    assert boss.call("vm/update", {"names": ["vm1"]})[0] == 400  # nothing to change
    assert boss.call("override/clear", {"names": ["vm1", "vm2"]})[1]["done"] == 2
    assert boss.call("vm/remove", {"names": ["vm1", "vm2"]})[1]["done"] == 2
    assert boss.call("vms")[1]["vms"] == []
    assert boss.call("vm/remove", {"names": []})[0] == 400


def test_vm_detail(srv):
    c = Client(srv[0])
    c.login("eye", "password-3")
    d = c.call("vm?name=VM1")[1]  # names are not case sensitive
    assert d["vm"]["name"] == "vm1" and d["vm"]["schedule"] == importer.ALWAYS_UP and d["tz"] == "UTC"
    assert d["windows"] == ["Mon 00:00>Sun 24:00"]  # always-up is a single run for the whole week
    assert any(e["action"] == "VM_ADDED" for e in d["events"])
    assert c.call("vm?name=ghost")[0] == 400


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
    assert c.call("user/save", {"create": True, "username": "new", "password": "longenough", "role": "reader"})[0] == 200
    assert c.call("user/remove", {"username": "ops"})[0] == 200
    assert c.call("user/remove", {"username": "boss"})[0] == 400  # last admin protected


def test_open_sessions_follow_user_changes(srv):
    boss, ops, eye = Client(srv[0]), Client(srv[0]), Client(srv[0])
    boss.login("boss", "password-1")
    ops.login("ops", "password-2")
    eye.login("eye", "password-3")
    assert boss.call("user/save", {"username": "ops", "role": "reader"})[0] == 200
    assert ops.call("me")[1]["role"] == "reader" and ops.call("pause", {"paused": True})[0] == 403  # demoted at once
    assert boss.call("user/save", {"username": "eye", "disabled": True})[0] == 200
    assert eye.call("me")[0] == 401  # disabled: logged out at once
    assert boss.call("user/remove", {"username": "ops"})[0] == 200
    assert ops.call("me")[0] == 401


def test_password_reset_ends_other_logins(srv):
    a, b = Client(srv[0]), Client(srv[0])
    a.login("eye", "password-3")
    b.login("eye", "password-3")
    assert a.call("change-password", {"old": "password-3", "new": "password-4"})[0] == 200
    assert a.call("me")[0] == 200 and b.call("me")[0] == 401


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


def test_schema_migrates_v2_holds_and_events_to_overrides(tmp_path):
    home = str(tmp_path)
    st = Store(home, actor="tester")
    st.init()
    st.add_vm("vm1", "azure", "sub", "rg", importer.ALWAYS_UP)
    st.set_override("vm1", None, "preserve me")
    with db.connect(home) as con:
        con.execute("UPDATE events SET action='HOLD_SET' WHERE action='OVERRIDE_SET'")
        con.execute("ALTER TABLE overrides RENAME TO holds")
        con.execute("UPDATE meta SET value='2' WHERE key='schema_version'")

    assert db.init(home) is False
    with db.connect(home) as con:
        override = con.execute("SELECT vm_name, reason FROM overrides").fetchone()
        actions = [r["action"] for r in con.execute("SELECT action FROM events ORDER BY id")]
        version = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert (override["vm_name"], override["reason"]) == ("vm1", "preserve me")
    assert "HOLD_SET" not in actions and "OVERRIDE_SET" in actions
    assert actions[-1] == "SCHEMA_UPGRADED" and version == str(db.SCHEMA_VERSION)


def test_schema_v3_converts_old_roles(tmp_path):
    home = str(tmp_path)
    st = Store(home)
    st.init()
    users.add_user(st, "a", "longenough", "admin")
    users.add_user(st, "b", "longenough", "reader")
    with db.connect(home) as con:
        con.execute("UPDATE users SET role='operator' WHERE username='a'")
        con.execute("UPDATE users SET role='viewer' WHERE username='b'")
        con.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    db.init(home)
    assert {u["username"]: u["role"] for u in users.list_users(st)} == {"a": "admin", "b": "reader"}


def test_one_time_password_forces_change(srv):
    base, home = srv
    adm = Client(base)
    adm.login("boss", "password-1")
    assert adm.call("user/save", {"create": True, "username": "newbie", "password": "temp-pass-1", "role": "reader"})[0] == 200
    c = Client(base)
    assert c.login("newbie", "temp-pass-1")[0] == 200
    assert c.call("me")[1]["must_change"] is True
    code, body = c.call("vms")
    assert code == 403 and body["password_change_required"]
    assert c.call("change-password", {"old": "temp-pass-1", "new": "temp-pass-1"})[0] == 400
    assert c.call("change-password", {"old": "temp-pass-1", "new": "my-own-pass-9"})[0] == 200
    assert c.call("vms")[0] == 200 and c.call("me")[1]["must_change"] is False


def test_vm_action_needs_override_when_schedule_disagrees(srv):
    base, home = srv
    from minipmc.service import Store
    c = Client(base)
    c.login("boss", "password-1")
    code, r = c.call("vm/action", {"names": ["vm1"], "action": "start"})  # schedule agrees: no override needed
    assert code == 400 and "readonly" in r["error"]
    Store(home).set_setting("mode", "live")
    code, r = c.call("vm/action", {"names": ["vm1"], "action": "stop"})
    assert code == 400 and "override is required" in r["error"]
    ov = {"hours": 2, "reason": "maintenance"}
    assert c.call("vm/action", {"names": ["vm1"], "action": "stop", "override": ov})[0] == 200
    with db.connect(home) as con:
        assert con.execute("SELECT manual FROM ops WHERE vm_name='vm1'").fetchone()[0] == 1
    assert [o["vm_name"] if "vm_name" in o.keys() else o[0] for o in Store(home).list_overrides()]


def test_bulk_csv_onboarding_format(tmp_path):
    st = Store(str(tmp_path)); st.init()
    r = st.add_vms_bulk('VMName,Provider,ProjectOrSubscription,ZoneOrResource,Schedule\nv1,gcp,prj,eu-a,none\nv2,azure,sub,rg,always_up\nv3,gcp,prj,eu-a,\n"v4",gcp,prj,eu-a,nosuch\n')
    assert r["added"] == ["v1", "v2", "v3"] and len(r["errors"]) == 1 and "nosuch" in r["errors"][0]


def test_https_with_generated_certificate(tmp_path):
    pytest.importorskip("cryptography")
    import ssl
    from minipmc import certs
    Store(str(tmp_path)).init()
    crt, key = certs.make_self_signed(["localhost", "127.0.0.1"], str(tmp_path / "c"))
    server = web.make_server(str(tmp_path), "127.0.0.1", 0, crt, key)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        r = urllib.request.urlopen(f"https://localhost:{server.server_address[1]}/", context=ssl.create_default_context(cafile=crt))
        assert r.status == 200 and r.headers.get("Strict-Transport-Security")
    finally:
        server.shutdown()
        server.server_close()


def test_cloudops_role_limits(srv):
    base, home = srv
    adm = Client(base); adm.login("boss", "password-1")
    adm.call("user/save", {"create": True, "must_change": False, "username": "co", "password": "cloudops-1", "role": "cloudops"})
    c = Client(base); assert c.login("co", "cloudops-1")[0] == 200
    assert c.call("vms")[0] == 200 and c.call("events")[0] == 200
    assert c.call("override/set", {"name": "vm1", "forever": True, "reason": "x"})[0] == 200
    for path, body in [("mode", {"mode": "live"}), ("pause", {}), ("users", None), ("user/save", {"username": "x"}),
                       ("vm/remove", {"names": ["vm1"]}), ("schedule/remove", {"name": "x"}), ("email", None)]:
        assert c.call(path, body)[0] == 403, path
