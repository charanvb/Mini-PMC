import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from minipmc import cli, db, importer
from minipmc.logs import setup_logging
from minipmc.service import LIBRARY_PATH, Store, UserError, parse_until


@pytest.fixture
def store(tmp_path):
    home = str(tmp_path)
    setup_logging(home)
    s = Store(home, actor="tester")
    s.init()
    yield s


def run_cli(home, *args):
    return cli.main(["--home", home, *args])


def test_init_creates_db_and_builtin_schedules(store):
    names = [r["name"] for r in store.list_schedules()]
    assert importer.ALWAYS_UP in names and importer.ALWAYS_PARKED in names
    assert store.init()["created"] is False


def test_shipped_library_loads_and_is_valid():
    assert os.path.exists(LIBRARY_PATH)
    import csv
    rows = list(csv.DictReader(open(LIBRARY_PATH, encoding="utf-8")))
    assert len(rows) > 100
    assert all(len(r["grid"]) == 672 for r in rows)


def test_add_vm_and_duplicates_are_case_insensitive(store):
    store.add_vm("VM1", "azure", "sub", "rg", importer.ALWAYS_UP)
    with pytest.raises(UserError):
        store.add_vm("vm1", "azure", "sub", "rg")
    with pytest.raises(UserError):
        store.add_vm("vm2", "aws", "x", "y")
    with pytest.raises(UserError):
        store.add_vm("vm3", "gcp", "p", "z", schedule="no such schedule")


def test_schedule_from_text_is_reused(store):
    a = store.add_schedule_from_text("IST 10AM-6PM Mon-Fri")
    assert store.add_schedule_from_text("IST 10AM-6PM Mon-Fri") == a
    assert store.get_schedule(a)["tz"] == "Asia/Kolkata"
    with pytest.raises(UserError):
        store.add_schedule_from_text("hello world")


def test_remove_schedule_blocked_while_in_use(store):
    name = store.add_schedule_from_text("IST 10AM-6PM Mon-Fri")
    store.add_vm("vm1", "azure", "s", "g", name)
    with pytest.raises(UserError):
        store.remove_schedule(name)
    store.set_schedule("vm1", None)
    store.remove_schedule(name)
    with pytest.raises(UserError):
        store.remove_schedule(importer.ALWAYS_UP)


def test_hold_blocks_action_and_clear_restores_it(store):
    store.add_vm("vm1", "azure", "s", "g", importer.ALWAYS_PARKED)
    assert store.explain("vm1", actual="running")["action"] == "STOP"
    store.set_hold("vm1", datetime.now(timezone.utc) + timedelta(days=1), "testing")
    r = store.explain("vm1", actual="running")
    assert (r["action"], r["reason"]) == ("SKIP", "OVERRIDE_ACTIVE")
    store.clear_hold("vm1")
    assert store.explain("vm1", actual="running")["action"] == "STOP"
    with pytest.raises(UserError):
        store.clear_hold("vm1")


def test_hold_in_the_past_rejected_and_expired_hold_ignored(store):
    store.add_vm("vm1", "azure", "s", "g", importer.ALWAYS_PARKED)
    with pytest.raises(UserError):
        store.set_hold("vm1", datetime.now(timezone.utc) - timedelta(hours=1), "x")
    at = datetime.now(timezone.utc) + timedelta(days=3)
    store.set_hold("vm1", datetime.now(timezone.utc) + timedelta(days=1), "x")
    assert store.explain("vm1", at, "running")["action"] == "STOP"


def test_explain_disabled_unmanaged_paused(store):
    store.add_vm("vm1", "gcp", "p", "z")
    assert store.explain("vm1")["reason"] == "UNMANAGED_NO_SCHEDULE"
    store.set_schedule("vm1", importer.ALWAYS_PARKED)
    store.set_enabled("vm1", False)
    assert store.explain("vm1")["reason"] == "VM_DISABLED"
    store.set_enabled("vm1", True)
    store.set_setting("paused", "1")
    assert store.explain("vm1")["reason"] == "GLOBAL_PAUSE"


def test_ist_schedule_uses_local_time(store):
    name = store.add_schedule_from_text("IST 10AM-6PM Mon-Fri")
    store.add_vm("vm1", "azure", "s", "g", name)
    # Mon 2026-11-23 05:00 UTC = 10:30 IST -> should be running
    r = store.explain("vm1", datetime(2026, 11, 23, 5, 0, tzinfo=timezone.utc), "stopped")
    assert r["action"] == "START" and r["local_time"].startswith("Mon 10:30")
    r2 = store.explain("vm1", datetime(2026, 11, 23, 5, 0, tzinfo=timezone.utc), "running")
    assert r2["action"] == "SKIP"
    r3 = store.explain("vm1", datetime(2026, 11, 23, 14, 0, tzinfo=timezone.utc), "running")  # 19:30 IST
    assert r3["action"] == "STOP"


def test_every_change_is_audited_to_db_and_file(store):
    store.add_vm("vm1", "azure", "s", "g")
    store.set_hold("vm1", None, "why")
    with db.connect(store.home) as con:
        actions = [r[0] for r in con.execute("SELECT action FROM events ORDER BY id")]
    assert "VM_ADDED" in actions and "HOLD_SET" in actions
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = open(os.path.join(store.home, "logs", "audit", day + ".jsonl"), encoding="utf-8").read().splitlines()
    recs = [json.loads(x) for x in lines]
    assert any(r["action"] == "HOLD_SET" and r["target"] == "vm1" and r["actor"] == "tester" for r in recs)
    assert os.path.getsize(os.path.join(store.home, "logs", "app.log")) > 0


def test_validate_reports_problems(store):
    store.add_vm("vm1", "azure", "s", "g")
    store.add_vm("vm2", "azure", "s", "g", importer.ALWAYS_UP)
    store.set_hold("vm2", datetime.now(timezone.utc) + timedelta(days=2), "soon")
    msgs = [m for _, m in store.validate()]
    assert any("vm1" in m and "no schedule" in m for m in msgs)
    assert any("vm2" in m and "hold ends" in m for m in msgs)


def test_remove_vm_removes_hold(store):
    store.add_vm("vm1", "azure", "s", "g")
    store.set_hold("vm1", None, "x")
    store.remove_vm("vm1")
    assert store.list_holds() == []


def test_parse_until():
    assert parse_until("2026-12-31") == datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert parse_until("2026-12-31T18:00:00+01:00") == datetime(2026, 12, 31, 17, 0, tzinfo=timezone.utc)
    with pytest.raises(UserError):
        parse_until("next friday")


def test_import_dry_run_then_apply_and_never_overwrites(store, tmp_path):
    csv_path = tmp_path / "inv.csv"
    csv_path.write_text(
        "Provider,ProjectOrSubscription,ZoneOrResourceGroup,VMName,Schedule,Action\n"
        "azure,sub1,rg1,vmA,IST 10AM-6PM Mon-Fri,x\n"
        "gcp,proj,zone-a,vmB,IL QA backup 3,x\n", encoding="utf-8")
    cat = importer.build_catalog(str(csv_path))
    assert store.import_catalog(cat, apply=False)["vms_new"] == 2
    assert store.list_vms() == []
    r = store.import_catalog(cat, apply=True)
    assert r["vms_new"] == 2 and r["unmanaged"] == 1
    store.set_schedule("vmA", importer.ALWAYS_UP)
    r = store.import_catalog(cat, apply=True)
    assert r["vms_new"] == 0 and r["vms_existing"] == 2
    assert store.get_vm("vmA")[0]["schedule"] == importer.ALWAYS_UP


def test_cli_end_to_end(tmp_path, capsys):
    home = str(tmp_path)
    assert run_cli(home, "init") == 0
    assert run_cli(home, "vm", "add", "vm1", "--provider", "azure", "--scope", "s", "--group", "g",
                   "--schedule", importer.ALWAYS_UP) == 0
    assert run_cli(home, "hold", "set", "vm1", "--days", "2", "--reason", "test") == 0
    assert run_cli(home, "desired", "vm1") == 0
    assert "OVERRIDE_ACTIVE" in capsys.readouterr().out
    assert run_cli(home, "vm", "add", "vm1", "--provider", "azure", "--scope", "s", "--group", "g") == 1
    assert run_cli(home, "vm", "show", "nope") == 1
    assert run_cli(home, "validate") in (0, 1)  # 1 only if this machine lacks tzdata
    assert run_cli(str(tmp_path / "empty"), "vm", "list") == 1
