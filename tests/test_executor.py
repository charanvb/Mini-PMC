from datetime import datetime, timedelta, timezone

import pytest

from minipmc import executor, importer, providers, signals, status
from minipmc import reconcile as rc
from minipmc.service import Store


class FakeCreds:
    cfg = {"host_vm": "hostvm", "executor": {"wave_size": 2}}
    clock = staticmethod(lambda: 0)

    def azure_token(self, resource=None):
        return "t"

    gcp_token = azure_token


@pytest.fixture
def world(tmp_path, monkeypatch):
    st = Store(str(tmp_path), actor="t")
    st.init()
    state = {"a": rc.DEALLOCATED, "b": rc.RUNNING, "hostvm": rc.RUNNING, "c": rc.RUNNING}
    calls, sent = [], []
    monkeypatch.setattr(providers, "vm_status", lambda creds, p, s, g, n: (state[n], state[n]))
    monkeypatch.setattr(providers, "vm_action", lambda creds, p, s, g, n, a: calls.append((n, a)) or "HTTP 202")
    monkeypatch.setattr(status, "resolve_names", lambda *a, **k: None)
    monkeypatch.setattr(signals, "send_signal",
                        lambda store, action, vms, **k: sent.append((action, [v["name"] for v in vms])) or {"ok": True})
    st.add_vm("a", "azure", "sub", "rg", importer.ALWAYS_UP)
    st.add_vm("b", "azure", "sub", "rg", importer.ALWAYS_PARKED)
    st.add_vm("hostvm", "azure", "sub", "rg", importer.ALWAYS_PARKED)
    st.add_vm("c", "azure", "sub", "rg", importer.ALWAYS_PARKED)
    eng = executor.Engine(st, creds_factory=FakeCreds, sleep=lambda s: None, background=False)
    return st, eng, state, calls, sent


def ops(st):
    from minipmc import db
    with db.connect(st.home) as con:
        return {(r["vm_name"], r["action"]): r["outcome"] for r in con.execute("SELECT * FROM ops")}


def test_readonly_and_dryrun_never_call_the_cloud(world):
    st, eng, state, calls, sent = world
    assert eng.tick()["planned"] == 0 and not ops(st)  # default mode is readonly
    st.set_setting("mode", "dryrun")
    assert eng.tick()["planned"] == 3
    eng.pump(force=True)
    assert not calls and not sent and not ops(st)
    assert any(e["action"] == "DRYRUN_START" for e in st.recent_events())


def test_live_queues_sends_signals_then_confirms(world):
    st, eng, state, calls, sent = world
    st.set_setting("mode", "live")
    assert eng.tick()["planned"] == 3  # host VM is guarded, so a start + stops for b and c
    assert ops(st) == {("a", "start"): "QUEUED", ("b", "stop"): "QUEUED", ("c", "stop"): "QUEUED"}
    assert eng.pump(force=True) == 2  # wave_size is 2
    assert sorted(calls) == [("a", "start"), ("b", "stop")]
    assert ("block", ["b"]) in sent and ("unblock", ["a"]) in sent
    assert eng.pump(force=True) == 1 and ("c", "stop") in calls
    assert "PENDING" in ops(st).values()

    state.update(a=rc.RUNNING, b=rc.DEALLOCATED, c=rc.STOPPED)
    eng.tick(datetime.now(timezone.utc) + timedelta(minutes=20))
    assert set(ops(st).values()) == {"SUCCEEDED"}
    assert not any(n == "hostvm" for n, _ in calls)


def test_unconfirmed_action_becomes_stuck_and_alerts(world, monkeypatch):
    st, eng, state, calls, sent = world
    raised = []
    monkeypatch.setattr(executor.alerts, "notify", lambda store, creds, key, subject, lines, **k: raised.append(key))
    st.set_setting("mode", "pilot")
    st.set_setting("pilot_vms", "a")
    eng.tick(), eng.pump(force=True)
    assert ops(st) == {("a", "start"): "PENDING"}
    eng.tick(datetime.now(timezone.utc) + timedelta(minutes=45))  # a is still deallocated
    assert ops(st) == {("a", "start"): "STUCK"} and "stuck" in raised


def test_failed_call_is_recorded_and_alerted(world, monkeypatch):
    st, eng, state, calls, sent = world
    raised = []
    monkeypatch.setattr(executor.alerts, "notify", lambda store, creds, key, subject, lines, **k: raised.append(key))

    def refuse(*a):
        raise providers.ProviderError("start refused (HTTP 403): no permission", 403)
    monkeypatch.setattr(providers, "vm_action", refuse)
    st.set_setting("mode", "pilot")
    st.set_setting("pilot_vms", "a")
    eng.tick(), eng.pump(force=True)
    assert ops(st) == {("a", "start"): "FAILED"} and "action-failed" in raised


def test_action_no_longer_needed_is_skipped(world):
    st, eng, state, calls, sent = world
    st.set_setting("mode", "live")
    eng.tick()
    state["a"] = rc.RUNNING  # someone started it meanwhile
    eng.pump(force=True)
    assert ops(st)[("a", "start")] == "SKIPPED" and ("a", "start") not in calls


def test_action_cap_stops_everything(world, monkeypatch):
    st, eng, state, calls, sent = world
    raised = []
    monkeypatch.setattr(executor.alerts, "notify", lambda store, creds, key, subject, lines, **k: raised.append(key))
    monkeypatch.setattr(FakeCreds, "cfg", {"executor": {"max_actions_per_slot": 1}})
    st.set_setting("mode", "live")
    assert eng.tick()["planned"] == 0 and not ops(st) and "cap" in raised


def test_slot_is_planned_once(world):
    st, eng, state, calls, sent = world
    st.set_setting("mode", "live")
    assert eng.tick()["planned"] == 3
    assert eng.tick()["planned"] == 0
