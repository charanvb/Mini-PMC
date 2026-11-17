import os
from datetime import datetime, timezone

import pytest
from openpyxl import load_workbook

from minipmc import grid as g
from minipmc.build_seed import build
from minipmc.importer import (ALWAYS_PARKED, ALWAYS_UP, apply_overrides, build_catalog, load_overrides,
                              parse_schedule)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_CSV = os.path.join(ROOT, "vms_all.csv")
REAL_OVERRIDES = os.path.join(ROOT, "config", "initial_overrides.csv")
needs_inventory = pytest.mark.skipif(not os.path.exists(REAL_CSV), reason="vms_all.csv not present")


def _parse(text):
    p = parse_schedule(text)
    assert p.kind == "grid", (text, p.kind, p.reason)
    return p, g.from_string(p.grid)


def _runs(text):
    _, gr = _parse(text)
    return "; ".join(g.run_label(s, n) for s, n in g.runs(gr))


# ---------------------------------------------------------------- readable names

@pytest.mark.parametrize("text, expected", [
    ("Up 5 days (Mon-Fri) 10 AM to 10 PM IST", "Mon-Fri 10:00-22:00"),
    ("Up 5 days (Mon-Fri) 12:30 PM to 12:30 AM IST", "Mon-Fri 12:30-00:30+1"),
    ("Up 2 days (Tue&Wed) 11AM to 11PM IST", "Tue,Wed 11:00-23:00"),
    ("Up 7 days 6 AM to 11:59 PM IST", "Daily 06:00-24:00"),
    ("Up 1 day (Sun) 12:00 AM to 06:00 AM IST", "Sun 00:00-06:00"),
    ("Up 5 days (Mon-Fri) 24*5 IST", "Mon-Fri 24h"),
    ("Up 6 days (Mon-Sat) 24*6 IST", "Mon-Sat 24h"),
    ("Up 5 days Mon 7 AM to Fri 9 PM IST", "Mon 07:00>Fri 21:00"),
    ("UP 7 days (Mon-Sun) except Down SAT&SUN 10 AM to 10 PM JKT", "24x7 except Sat,Sun 10:00-22:00"),
    ("UP 7 days (Mon-Sun) except Down SUN 12 AM to 7 AM JKT", "24x7 except Sun 00:00-07:00"),
    ("UP 7 days (Mon-Sun) Except Sat & Sun 6 AM to 11 PM Down BST", "24x7 except Sat,Sun 06:00-23:00"),
    ("UP 7 days (Mon-Fri) 25*5 Except Sat & Sun 8 AM to 12AM Asia/Dhaka (+06:00)", "24x7 except Sat,Sun 00:00-08:00"),
])
def test_schedule_description(text, expected):
    _, gr = _parse(text)
    assert g.describe(gr) == expected


# ---------------------------------------------------------------- several windows in one text

@pytest.mark.parametrize("text, expected", [
    ("Up 3 days Mon 11 AM to 4 PM & Sat 8:30 PM to Sun 2:30 AM & Sun 5:30 AM - 5:30 PM IST",
     "Mon 11:00-16:00; Sat 20:30>Sun 02:30; Sun 05:30-17:30"),
    ("UP 4 days Mon 11AM-4PM &Thu 5:30AM-3:30PM&Sat 8:30PM-Sun2:30AM&Sun5:30AM-5:30PM IST",
     "Mon 11:00-16:00; Thu 05:30-15:30; Sat 20:30>Sun 02:30; Sun 05:30-17:30"),
    ("Up 3 days Mon 11 AM to 4 PM & 11:30 PM to 11:59 PM & Sat 8:30 PM to Sun 11:59 PM IST",
     "Mon 11:00-16:00; Mon 23:30-24:00; Sat 20:30>Sun 24:00"),
    ("Up 5 days Mon 7 AM to 11:30 PM & (Tue-Thur 8 AM to 11:30 PM) & Fri 8 AM to 9:30 PM IST",
     "Mon 07:00-23:30; Tue 08:00-23:30; Wed 08:00-23:30; Thu 08:00-23:30; Fri 08:00-21:30"),
    ("Up 5 days Mon-Thu 8:30 AM to 8:30 PM & Fri 8:30 AM to 11:30 PM IST.",
     "Mon 08:30-20:30; Tue 08:30-20:30; Wed 08:30-20:30; Thu 08:30-20:30; Fri 08:30-23:30"),
    ("Up 7 days (Mon- Fri) 9 AM to 3 AM & Sat (12 AM-3 AM) & Sun (11 AM-11:59 PM)IST",
     "Mon 09:00>Tue 03:00; Tue 09:00>Wed 03:00; Wed 09:00>Thu 03:00; Thu 09:00>Fri 03:00; "
     "Fri 09:00>Sat 03:00; Sun 11:00-24:00"),
    ("Up 7 days(Mon-Fri) 7 AM to 7 PM & (Sat-Sun) 7 AM to 4 PM GMT+1",
     "Mon 07:00-19:00; Tue 07:00-19:00; Wed 07:00-19:00; Thu 07:00-19:00; Fri 07:00-19:00; "
     "Sat 07:00-16:00; Sun 07:00-16:00"),
])
def test_multi_window_schedules(text, expected):
    assert _runs(text) == expected


def test_two_up_parts_are_combined():
    p, gr = _parse("Up 7 days(Mon-Sun) 11:45 PM to 12:30 AM GMT & Up 5 days(Mon-Fri) 06:00 AM to 06:00 PM GMT")
    assert not p.check
    at = lambda day, h, m: g.desired_at(gr, timezone.utc, datetime(2026, 11, day, h, m, tzinfo=timezone.utc))
    assert at(18, 7, 0) == "R"  # Wednesday morning, weekday window
    assert at(21, 7, 0) == "S"  # Saturday morning
    assert at(21, 23, 50) == "R" and at(22, 0, 10) == "R"  # nightly 23:45-00:30, across midnight


# ---------------------------------------------------------------- which readings need a human check

@pytest.mark.parametrize("text, check", [
    ("Up 7 days(Mon-Fri) 7 AM to 7 PM & (Sat-Sun) 7 AM to 4 PM GMT+1", False),  # 5 + 2 days = 7
    ("UP 4 days Mon 11AM-4PM &Thu 5:30AM-3:30PM&Sat 8:30PM-Sun2:30AM&Sun5:30AM-5:30PM IST", False),
    ("Up 5 days Mon-Thu 8:30 AM to 8:30 PM & Fri 8:30 AM to 11:30 PM IST.", False),
    ("Up 3 days Mon 11 AM to 4 PM & 11:30 PM to 11:59 PM & Sat 8:30 PM to Sun 11:59 PM IST", False),  # Mon, Sat, Sun = 3
    ("Up 5 days (Mon-Fri) 9 AM to 3 AM IST and Down (Sat - Sun) 3 AM to 11:00 AM IST", True),  # redundant Down
    ("Up 5 days (Mon-Sat)10 AM to 4:30 AM IST", True),
    ("Up 5 days (Sun - Fri) 06:00 AM - 09:00 PM IL", True),  # 5 days but 6 named
    ("Up 5 days 09:00 AM to 08:00 PM IST", True),  # no days named
    ("9am-11pm GMT+6", True),
    ("UP 7 days (Mon-Fri) 25*5 Except Sat & Sun 8 AM to 12AM Asia/Dhaka (+06:00)", True),  # 25*5 is a typo
    ("Up 5 days (Mon-Fri) 10 AM to 10 PM IST", False),
])
def test_only_judgement_calls_are_flagged(text, check):
    p, _ = _parse(text)
    assert p.check is check, p.notes


def test_five_days_with_a_saturday_spill_starts_on_weekdays():
    p, gr = _parse("Up 5 days (Mon-Sat)10 AM to 4:30 AM IST")
    assert g.describe(gr) == "Mon-Fri 10:00-04:30+1"
    assert gr[5 * g.CELLS_PER_DAY + 18] == "S"  # Saturday 04:30 onwards is stopped


def test_israel_five_days_with_six_named_uses_the_named_days():
    _, gr = _parse("Up 5 days (Sun - Fri) 06:00 AM - 09:00 PM IL")
    assert g.describe(gr) == "Sun-Fri 06:00-21:00"


# ---------------------------------------------------------------- time zones

@pytest.mark.parametrize("text, zone", [
    ("Up 7 days(Mon-Sun) 6 AM to 12 AM BST/ Asia Dhaka", "Asia/Dhaka"),  # BST is Dhaka when the text says so
    ("Up 5 day (Mon-Fri) 7:00 AM to 9:30 PM BST", "Europe/London"),
    ("Up 5 days (Mon-Fri) 6 AM to 6 PM GMT", "UTC"),
    ("Up 7 days 7 AM to 11:59 PM Egypt(UTC+2)", "Etc/GMT-2"),  # an explicit offset wins over the name
    ("Up 7 days (Mon-Sun) 7:00 AM to 23:59:59 Africa/Casablanca UTC+1", "Etc/GMT-1"),
    ("Up 7 Days 07:00 AM to 08:00 PM UTC-3", "Etc/GMT+3"),
    ("Up 5 days (Mon-Fri) 8 AM to 6 PM JKT", "Asia/Jakarta"),
    ("Always Parked (excluding 10hr maintenance period) - Turkey", "Europe/Istanbul"),
])
def test_time_zone_detection(text, zone):
    assert parse_schedule(text).tz == zone


# ---------------------------------------------------------------- left unmanaged

@pytest.mark.parametrize("text", [
    "IL QA backup 3",
    "Dev Sche",
    "Testing Schedule",
    "UP 5 days Mon-Fri Sat-Sun 12 PM - 12 AM",  # no time zone
    "Always Parked (excluding 10hr maintenance period)",  # maintenance window but no zone
])
def test_unreadable_schedules_stay_unmanaged_with_a_reason(text):
    p = parse_schedule(text)
    assert p.kind == "unmanaged" and p.reason and p.grid is None


# ---------------------------------------------------------------- catalog and overrides (small synthetic files)

INVENTORY = """VMName,Provider,ProjectOrSubscription,ZoneOrResourceGroup,Action,Schedule
vm-a,Azure,sub-1,rg-1,x,Up 5 days (Mon-Fri) 10 AM to 10 PM IST
vm-b,GCP,proj-1,europe-west3-a,x,Up 5 days (Mon- Fri) 10 AM to 10 PM IST
vm-c,GCP,proj-1,europe-west3-b,x,24X7 Always Up
vm-d,GCP,proj-1,europe-west3-b,x,Dev Sche
vm-e,Azure,sub-1,rg-1,x,Always Parked (excluding 10hr maintenance period) - Thursday/GMT
"""
OVERRIDES = """# comment lines are ignored
vm_name,hold_until_utc,reason,force_schedule
vm-a,2026-12-31T23:59:59Z,until year end,
VM-B,,keep it,ALWAYS_UP
ghost,2026-12-31T23:59:59Z,not in the inventory,
vm-c,,bad schedule,NO_SUCH_SCHEDULE
"""


@pytest.fixture
def files(tmp_path):
    inv, ov = tmp_path / "inv.csv", tmp_path / "ov.csv"
    inv.write_text(INVENTORY, encoding="utf-8")
    ov.write_text(OVERRIDES, encoding="utf-8")
    return str(inv), str(ov), tmp_path


def test_catalog_shares_one_schedule_between_equivalent_texts(files):
    cat = build_catalog(files[0])
    by_vm = {v.vm_name: v for v in cat.vms}
    assert by_vm["vm-a"].schedule == by_vm["vm-b"].schedule == "IST Mon-Fri 10:00-22:00"
    assert cat.schedules["IST Mon-Fri 10:00-22:00"].vms == 2
    assert by_vm["vm-c"].schedule == ALWAYS_UP
    assert by_vm["vm-d"].schedule is None and [v.vm_name for v in cat.unmanaged] == ["vm-d"]
    assert by_vm["vm-e"].schedule == "UTC Thu 00:00-10:00"
    assert cat.schedules[ALWAYS_PARKED].vms == 0


def test_override_file_holds_forces_and_reports_problems(files):
    inv, ov, _ = files
    cat = build_catalog(inv)
    errors = apply_overrides(cat, load_overrides(ov))
    by_vm = {v.vm_name: v for v in cat.vms}

    assert by_vm["vm-a"].hold and by_vm["vm-a"].hold_until == datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert by_vm["vm-a"].schedule == "IST Mon-Fri 10:00-22:00"  # a hold alone does not change the schedule
    assert by_vm["vm-b"].hold and by_vm["vm-b"].hold_until is None  # blank end = until cleared
    assert by_vm["vm-b"].schedule == ALWAYS_UP and by_vm["vm-b"].schedule_forced
    assert not by_vm["vm-c"].hold  # unknown schedule: nothing applied
    assert len(errors) == 2 and any("ghost" in e for e in errors) and any("NO_SUCH_SCHEDULE" in e for e in errors)
    assert cat.schedules["IST Mon-Fri 10:00-22:00"].vms == 1  # counts follow the forced schedule


def test_workbook_has_every_tab_with_unmanaged_and_holds(files):
    inv, ov, tmp = files
    out = str(tmp / "out" / "seed.xlsx")
    build(inv, ov, out)
    wb = load_workbook(out, read_only=True)
    assert wb.sheetnames == ["Summary", "Unmanaged", "Conversions", "Schedules", "VMs", "Overrides", "Timezones"]
    rows = lambda name: list(wb[name].iter_rows(values_only=True))[1:]
    assert [r[0] for r in rows("Unmanaged")] == ["vm-d"]
    assert len(rows("VMs")) == 5
    status = {r[0]: r[8] for r in rows("Overrides")}
    assert status["vm-a"] == "applied" and status["vm-b"] == "applied (schedule forced)"
    assert status["ghost"] == "NOT FOUND in inventory"
    assert all(len(r[7]) == g.CELLS for r in rows("Schedules"))


# ---------------------------------------------------------------- the real inventory

UNMANAGED_TEXTS = {
    "IL QA backup 3", "Dev Sche", "Testing Schedule", "UP 5 days Mon-Fri Sat-Sun 12 PM - 12 AM",
}


@pytest.fixture(scope="module")
def real():
    return build_catalog(REAL_CSV)


@needs_inventory
def test_real_inventory_only_the_known_odd_texts_are_unmanaged(real):
    assert {v.source_text for v in real.unmanaged} == UNMANAGED_TEXTS


@needs_inventory
def test_real_inventory_is_consistent(real):
    assert not real.duplicate_names
    assert all(c.parsed.kind != "grid" or c.parsed.grid for c in real.conversions.values())
    assert len(real.schedules) == len({s.name for s in real.schedules.values()})
    for s in real.schedules.values():
        g.from_string(s.grid)  # 672 cells of R/S/N
        assert len(s.name) <= 60
    managed = [v for v in real.vms if v.schedule]
    assert sum(s.vms for s in real.schedules.values()) == len(managed)
    assert len(managed) + len(real.unmanaged) == len(real.vms)


@needs_inventory
@pytest.mark.skipif(not os.path.exists(REAL_OVERRIDES), reason="config/initial_overrides.csv not present")
def test_real_initial_overrides_are_all_found_and_correct():
    cat = build_catalog(REAL_CSV)
    assert apply_overrides(cat, load_overrides(REAL_OVERRIDES)) == []
    held = {v.vm_name: v for v in cat.vms if v.hold}
    assert len(held) == 17
    year_end = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert sum(1 for v in held.values() if v.hold_until == year_end) == 16
    host = held["bnlwefspap20560"]
    assert host.hold_until is None and host.schedule == ALWAYS_UP and host.schedule_forced
