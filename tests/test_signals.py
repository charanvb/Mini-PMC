import json

import pytest

from minipmc import db, importer, signals
from minipmc.logs import setup_logging
from minipmc.service import Store


class FakeCredentials:
    def __init__(self, settings, responses=(), secret=""):
        self.cfg = settings
        self.responses = list(responses)
        self.secret = secret
        self.secret_names = []
        self.calls = []
        self.clock = lambda: 100

    def keyvault_secret(self, name):
        self.secret_names.append(name)
        return self.secret

    def http(self, method, url, headers=None, data=None, timeout=20):
        self.calls.append((method, url, headers, data, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


VM = {"name": "vm1", "scope": "subscription-or-project", "group_or_zone": "resource-group-or-zone"}


def test_payload_maps_vm_fields_to_logic_app_contract():
    assert signals.payload("block", [VM]) == {
        "vms": [{"name": "vm1", "project": "subscription-or-project", "zone": "resource-group-or-zone"}],
        "action": "block",
    }
    with pytest.raises(signals.SignalError):
        signals.payload("start", [VM])


def test_safe_target_drops_signed_query_and_userinfo():
    url = "https://user:password@example.test/workflow?api-version=1&sig=secret"
    assert signals.safe_target(url) == "https://example.test/workflow"
    assert "secret" not in signals.safe_target(url)


def test_logic_app_accepts_202_and_posts_json():
    creds = FakeCredentials(
        {"logic_app": {"url": "https://example.test/trigger?sig=secret"}},
        [(202, {})],
    )
    result = signals.LogicApp(creds).send("unblock", [VM])
    method, url, headers, body, timeout = creds.calls[0]
    assert result["ok"] and result["status"] == 202 and result["attempts"] == 1
    assert method == "POST" and url.endswith("sig=secret")
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == signals.payload("unblock", [VM])


def test_retryable_status_retries_but_bad_request_does_not():
    creds = FakeCredentials({"logic_app": {"url": "https://example.test/hook", "retries": 2}},
                            [(503, {"raw": "unavailable"}), (202, {})])
    sleeps = []
    result = signals.LogicApp(creds, sleep=sleeps.append).send("block", [VM])
    assert result["ok"] and result["attempts"] == 2 and sleeps == [2]

    creds = FakeCredentials({"logic_app": {"url": "https://example.test/hook", "retries": 3}},
                            [(400, {"raw": "bad request"})])
    sleeps = []
    result = signals.LogicApp(creds, sleep=sleeps.append).send("block", [VM])
    assert not result["ok"] and result["status"] == 400
    assert result["attempts"] == 1 and not sleeps


def test_url_can_come_from_key_vault_and_missing_url_is_reported():
    url = "https://example.test/hook?sig=secret"
    creds = FakeCredentials({"logic_app": {"url_secret_name": "logicapp-url"}}, secret=url)
    app = signals.LogicApp(creds)
    assert app.url() == url
    assert app.url() == url
    assert creds.secret_names == ["logicapp-url"]

    missing = signals.LogicApp(FakeCredentials({}))
    result = missing.send("block", [VM])
    assert not result["ok"] and result["attempts"] == 0
    assert "not configured" in result["detail"]


def test_failure_never_exposes_signed_url_and_is_audited(tmp_path):
    home = str(tmp_path)
    setup_logging(home)
    store = Store(home, actor="tester")
    store.init()
    store.add_vm("vm1", "azure", "sub", "rg", importer.ALWAYS_UP)
    url = "https://example.test/hook?sig=do-not-log"
    creds = FakeCredentials({"logic_app": {"url": url}}, [RuntimeError(url)])
    result = signals.send_signal(store, "block", [VM], app=signals.LogicApp(creds))
    assert not result["ok"] and "do-not-log" not in result["detail"]
    with db.connect(home) as con:
        event = con.execute("SELECT action, detail FROM events WHERE target='vm1' ORDER BY id DESC").fetchone()
    assert event["action"] == "SIGNAL_FAILED"
    assert "do-not-log" not in event["detail"]
