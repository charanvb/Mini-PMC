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


def test_override_blocks_action_and_clear_restores_it(store):
    store.add_vm("vm1", "azure", "s", "g", importer.ALWAYS_PARKED)
    assert store.explain("vm1", actual="running")["action"] == "STOP"
    store.set_override("vm1", datetime.now(timezone.utc) + timedelta(days=1), "testing")
    r = store.explain("vm1", actual="running")
    assert (r["action"], r["reason"]) == ("SKIP", "OVERRIDE_ACTIVE")
    store.clear_override("vm1")
    assert store.explain("vm1", actual="running")["action"] == "STOP"
    with pytest.raises(UserError):
        store.clear_override("vm1")


def test_override_in_the_past_rejected_and_expired_override_ignored(store):
    store.add_vm("vm1", "azure", "s", "g", importer.ALWAYS_PARKED)
    with pytest.raises(UserError):
        store.set_override("vm1", datetime.now(timezone.utc) - timedelta(hours=1), "x")
    at = datetime.now(timezone.utc) + timedelta(days=3)
    store.set_override("vm1", datetime.now(timezone.utc) + timedelta(days=1), "x")
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


def test_time_check_lists_switches_in_utc_and_schedule_zone(store):
    name = store.add_schedule_from_text("IST 10AM-6PM Mon-Fri")
    r = store.time_check(name, datetime(2026, 11, 23, 4, 0, tzinfo=timezone.utc))  # Mon 09:30 IST
    assert r["now_state"] == "S" and r["tz"] == "Asia/Kolkata"
    first = r["switches"][0]
    assert first["to"] == "R" and first["utc"] == "Mon 23 Nov 04:30" and "10:00" in first["schedule"]
    assert r["switches"][1]["to"] == "S" and r["switches"][1]["utc"] == "Mon 23 Nov 12:30"  # 18:00 IST


def test_time_check_handles_uk_clock_change(store):
    try:
        from minipmc.tz import get_tz
        get_tz("Europe/London")
    except ValueError:
        pytest.skip("tz data not installed (pip install tzdata)")
    name = store.add_schedule_from_text("Up 5 days (Mon-Fri) 9 AM to 5 PM Europe/London")
    # Fri 27 Mar 2026 is GMT, Mon 30 Mar is BST (clocks changed 29 Mar): 09:00 local = 09:00 then 08:00 UTC
    r = store.time_check(name, datetime(2026, 3, 27, 18, 0, tzinfo=timezone.utc), days=4)
    starts = [w for w in r["switches"] if w["to"] == "R"]
    assert starts[0]["utc"] == "Mon 30 Mar 08:00" and "09:00 BST" in starts[0]["london"]


def test_custom_schedule_and_bulk_vms(store):
    name = store.add_schedule_custom("Office", "Asia/Kolkata", "Mon-Fri 09:00-17:00\nSat 22:00-02:00")
    r = store.time_check(name, datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc))  # Mon 08:30 IST
    assert r["switches"][0]["to"] == "R" and r["switches"][0]["utc"] == "Mon 23 Nov 03:30"
    # Saturday 22:00 window runs through to Sunday 02:00
    sat = store.get_schedule(name)["grid"]
    assert sat[5 * 96 + 22 * 4] == "R" and sat[6 * 96 + 1 * 4] == "R" and sat[6 * 96 + 2 * 4] == "S"
    for bad in ("Mon-Fri 9-5", "Funday 09:00-17:00", ""):
        with pytest.raises(UserError):
            store.add_schedule_custom("X" + bad[:3], "Asia/Kolkata", bad)
    res = store.add_vms_bulk("a1,azure,sub,rg\nb1,gcp,proj,zone,Office\nbroken\na1,azure,sub,rg", name)
    assert res["added"] == ["a1", "b1"] and len(res["errors"]) == 2
    assert {v["name"]: v["schedule"] for v in store.list_vms()} == {"a1": "Office", "b1": "Office"}


def test_every_change_is_audited_to_db_and_file(store):
    store.add_vm("vm1", "azure", "s", "g")
    store.set_override("vm1", None, "why")
    with db.connect(store.home) as con:
        actions = [r[0] for r in con.execute("SELECT action FROM events ORDER BY id")]
    assert "VM_ADDED" in actions and "OVERRIDE_SET" in actions
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = open(os.path.join(store.home, "logs", "audit", day + ".jsonl"), encoding="utf-8").read().splitlines()
    recs = [json.loads(x) for x in lines]
    assert any(r["action"] == "OVERRIDE_SET" and r["target"] == "vm1" and r["actor"] == "tester" for r in recs)
    assert os.path.getsize(os.path.join(store.home, "logs", "app.log")) > 0


def test_validate_reports_problems(store):
    store.add_vm("vm1", "azure", "s", "g")
    store.add_vm("vm2", "azure", "s", "g", importer.ALWAYS_UP)
    store.set_override("vm2", datetime.now(timezone.utc) + timedelta(days=2), "soon")
    msgs = [m for _, m in store.validate()]
    assert any("vm1" in m and "no schedule" in m for m in msgs)
    assert any("vm2" in m and "override ends" in m for m in msgs)


def test_remove_vm_removes_override(store):
    store.add_vm("vm1", "azure", "s", "g")
    store.set_override("vm1", None, "x")
    store.remove_vm("vm1")
    assert store.list_overrides() == []


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
    assert run_cli(home, "override", "set", "vm1", "--days", "2", "--reason", "test") == 0
    assert run_cli(home, "override", "list") == 0
    assert "vm1" in capsys.readouterr().out
    assert run_cli(home, "desired", "vm1") == 0
    assert "OVERRIDE_ACTIVE" in capsys.readouterr().out
    assert run_cli(home, "override", "clear", "vm1") == 0
    assert run_cli(home, "vm", "add", "vm1", "--provider", "azure", "--scope", "s", "--group", "g") == 1
    assert run_cli(home, "vm", "show", "nope") == 1
    assert run_cli(home, "validate") in (0, 1)  # 1 only if this machine lacks tzdata
    assert run_cli(str(tmp_path / "empty"), "vm", "list") == 1
