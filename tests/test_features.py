import base64
import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from openpyxl import load_workbook
from test_web import Client, srv  # noqa: F401  (srv is a fixture)

from minipmc import exports, mailer, reports, tz
from minipmc.service import Store


class StubApp:
    configured = True

    def __init__(self):
        self.bodies = []

    def post(self, body, what):
        self.bodies.append(json.loads(body))
        return {"ok": True, "status": 202, "attempts": 1, "detail": "HTTP 202"}


def test_gmt_labels():
    jan = datetime(2026, 1, 15, tzinfo=timezone.utc)
    assert tz.gmt_label("Asia/Kolkata", jan) == "GMT+5:30"
    assert tz.gmt_label("America/Sao_Paulo", jan) == "GMT-3"
    assert tz.gmt_label("UTC", jan) == "GMT"
    assert tz.gmt_label("Not/AZone", jan) == ""


def test_email_payload_matches_the_logic_app_contract():
    app = StubApp()
    m = mailer.Mailer(None, app=app)
    m.send("a@x.com; b@y.com,a@x.com", "Subj", "<p>hi</p>")
    assert app.bodies[-1] == {"Message": "<p>hi</p>", "Subject": "Subj", "ToEmail": "a@x.com;b@y.com"}  # no Attachments key
    m.send(["a@x.com"], "Subj", "<p>hi</p>", [("r.xlsx", b"\x00\x01data")])
    att = app.bodies[-1]["Attachments"]
    assert att[0]["Name"] == "r.xlsx" and base64.b64decode(att[0]["ContentBytes"]) == b"\x00\x01data"
    assert m.send("", "s", "m")["ok"] is False
    assert mailer.bad_addresses("good@x.com, nope") == ["nope"]


def test_excel_text_is_never_a_formula(srv):
    wb = load_workbook(io.BytesIO(exports.workbook([("T", ["h"], [["=1+1"], ["@SUM(A1)"], ["plain"]])])))
    cells = [c.value for c in wb["T"]["A"][1:]]
    assert cells == ["=1+1", "@SUM(A1)", "plain"] and wb["T"]["A2"].data_type == "s"


def test_report_has_both_sheets(tmp_path):
    st = Store(str(tmp_path), actor="t")
    st.init()
    st.add_vm("vm1", "azure", "sub", "rg", "ALWAYS_UP")
    rep = reports.build(st)
    name, data = rep["attachment"]
    assert name.endswith(".xlsx") and set(load_workbook(io.BytesIO(data)).sheetnames) == {"Resources", "Schedules"}
    assert "vm1" in rep["html"] and rep["subject"].startswith("Mini PMC report [")


def test_daily_report_is_sent_once_a_day(tmp_path, monkeypatch):
    st = Store(str(tmp_path), actor="t")
    st.init()
    for k, v in {"report_auto": "1", "report_time": "08:00", "report_tz": "Asia/Kolkata"}.items():
        st.set_setting(k, v)
    sent = []
    monkeypatch.setattr(reports, "send", lambda *a, **k: sent.append(1) or {"ok": True})
    before = datetime(2026, 3, 2, 2, 0, tzinfo=timezone.utc)  # 07:30 in Kolkata
    after = datetime(2026, 3, 2, 3, 0, tzinfo=timezone.utc)  # 08:30 in Kolkata
    assert reports.send_if_due(st, None, before) is False
    assert reports.send_if_due(st, None, after) is True
    assert reports.send_if_due(st, None, after.replace(hour=9)) is False and len(sent) == 1


def _raw(base, path, cookie, body):
    req = urllib.request.Request(base + "/api/" + path, data=json.dumps(body).encode(),
                                 headers={"X-Requested-With": "minipmc", "Cookie": cookie})
    return urllib.request.urlopen(req)


def test_export_download_and_roles(srv):
    base, _ = srv
    reader, admin = Client(base), Client(base)
    reader.login("eye", "password-3")
    admin.login("boss", "password-1")
    r = _raw(base, "export/resources", reader.cookie, {})
    assert r.headers["Content-Disposition"].startswith("attachment") and "spreadsheet" in r.headers["Content-Type"]
    assert load_workbook(io.BytesIO(r.read()))["Resources"]["A2"].value == "vm1"
    assert reader.call("mode", {"mode": "live", "confirm": "LIVE"})[0] == 403
    assert reader.call("vm/refresh", {"names": ["vm1"]})[0] == 400  # allowed for readers; fails only because no settings.json
    assert reader.call("email")[0] == 403


def test_mode_changes_need_the_right_input(srv):
    base, _ = srv
    c = Client(base)
    c.login("boss", "password-1")
    assert c.call("mode", {"mode": "live"})[0] == 400  # LIVE must be typed
    assert c.call("mode", {"mode": "pilot", "pilot_vms": ["ghost"]})[0] == 400
    code, body = c.call("mode", {"mode": "pilot", "pilot_vms": ["VM1"]})
    assert code == 200 and body["mode"] == "pilot" and body["warnings"]
    assert c.call("status")[1]["mode"] == "pilot"
    assert c.call("mode", {"mode": "live", "confirm": "LIVE"})[1]["mode"] == "live"


def test_email_settings_validate_and_healthz(srv):
    base, _ = srv
    c = Client(base)
    c.login("boss", "password-1")
    assert c.call("email/save", {"alert_to": "not-an-email"})[0] == 400
    assert c.call("email/save", {"report_time": "25:00"})[0] == 400
    code, body = c.call("email/save", {"alert_to": "a@x.com, b@y.com", "report_auto": True, "report_tz": "Asia/Kolkata"})
    assert code == 200 and body["alert_to"] == "a@x.com; b@y.com" and body["report_tz_label"] == "GMT+5:30"
    try:
        urllib.request.urlopen(base + "/healthz")
        assert False, "no scheduler heartbeat should be unhealthy"
    except urllib.error.HTTPError as e:
        assert e.code == 503
