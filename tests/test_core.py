from datetime import datetime, timedelta, timezone

import pytest

from minipmc import grid as g
from minipmc.importer import parse_schedule
from minipmc.reconcile import (DEALLOCATED, GLOBAL_PAUSE, IN_DESIRED_STATE, OPERATION_IN_PROGRESS,
                               OVERRIDE_ACTIVE, RUNNING, SCHEDULE_DO_NOTHING, SCHEDULED, SKIP, START,
                               STATUS_UNKNOWN_OR_STALE, STOP, STOPPED, TRANSITIONAL_STATE, UNKNOWN,
                               UNMANAGED_NO_SCHEDULE, VM_DISABLED, Override, reconcile_vm)
from minipmc.tz import get_tz

IST = get_tz("Asia/Kolkata")
UTC = timezone.utc


def _ist(y, mo, d, h, mi):  # 2026-11-16 is a Monday
    return datetime(y, mo, d, h, mi, tzinfo=IST).astimezone(UTC)


def _grid(text):
    p = parse_schedule(text)
    assert p.kind == "grid", (text, p)
    return g.from_string(p.grid), get_tz(p.tz)


# ---------------------------------------------------------------- grid

def test_paint_rounds_to_whole_cells_and_wraps_sunday_into_monday():
    gr = g.new_grid("S")
    g.paint(gr, 6, 22 * 60 + 5, 26 * 60 + 10, "R")  # Sun 22:05 -> Mon 02:10
    (start, length), = g.runs(gr)
    assert g.run_label(start, length) == "Sun 22:00>Mon 02:15"  # start rounded down, end rounded up


def test_paint_rejects_an_empty_range():
    with pytest.raises(ValueError):
        g.paint(g.new_grid(), 0, 600, 600)


def test_from_string_rejects_bad_grids():
    with pytest.raises(ValueError):
        g.from_string("R" * 10)
    with pytest.raises(ValueError):
        g.from_string("X" * g.CELLS)


def test_runs_of_uniform_grids():
    assert g.runs(g.new_grid("S")) == []
    assert g.runs(g.new_grid("R")) == [(0, g.CELLS)]
    assert g.describe(g.new_grid("S")) == "never up"
    assert g.describe(g.new_grid("R")) == "24x7"


# ---------------------------------------------------------------- parsing: single windows

def test_simple_weekday_window():
    gr, tz = _grid("Up 5 days (Mon-Fri) 10 AM to 10 PM IST")
    assert g.desired_at(gr, tz, _ist(2026, 11, 16, 10, 0)) == "R"
    assert g.desired_at(gr, tz, _ist(2026, 11, 16, 21, 59)) == "R"
    assert g.desired_at(gr, tz, _ist(2026, 11, 16, 22, 0)) == "S"
    assert g.desired_at(gr, tz, _ist(2026, 11, 21, 12, 0)) == "S"  # Saturday


def test_overnight_spills_to_next_day():
    gr, tz = _grid("Up 5 days (Mon-Fri) 12:30 PM to 12:30 AM IST")
    assert g.desired_at(gr, tz, _ist(2026, 11, 17, 0, 15)) == "R"  # Tue 00:15 from Mon window
    assert g.desired_at(gr, tz, _ist(2026, 11, 17, 0, 30)) == "S"
    assert g.desired_at(gr, tz, _ist(2026, 11, 21, 0, 15)) == "R"  # Sat 00:15 from Fri window


def test_sunday_wraps_to_monday():
    gr, tz = _grid("Up 1 day (Sun) 10 PM to 2 AM IST")
    assert g.desired_at(gr, tz, _ist(2026, 11, 16, 1, 0)) == "R"


def test_end_of_day_1159_means_midnight():
    gr, tz = _grid("Up 7 days 6 AM to 11:59 PM IST")
    assert g.desired_at(gr, tz, _ist(2026, 11, 18, 23, 50)) == "R"
    assert g.desired_at(gr, tz, _ist(2026, 11, 18, 5, 59)) == "S"


def test_continuous_multiday_window():
    gr, tz = _grid("Up 5 days Mon 7 AM to Fri 9 PM IST")
    assert g.desired_at(gr, tz, _ist(2026, 11, 18, 3, 0)) == "R"
    assert g.desired_at(gr, tz, _ist(2026, 11, 16, 6, 0)) == "S"
    assert g.desired_at(gr, tz, _ist(2026, 11, 21, 0, 0)) == "S"


def test_maintenance_default_sunday_and_thursday_variant():
    gr, tz = _grid("Always Parked (excluding 10hr maintenance period) - IST")
    assert g.desired_at(gr, tz, _ist(2026, 11, 22, 5, 0)) == "R"
    assert g.desired_at(gr, tz, _ist(2026, 11, 22, 10, 0)) == "S"
    assert g.desired_at(gr, tz, _ist(2026, 11, 19, 5, 0)) == "S"  # Thursday is not the IST window
    gr, tz = _grid("Always Parked (excluding 10hr maintenance period) - Thursday/GMT")
    assert g.desired_at(gr, tz, datetime(2026, 11, 19, 5, 0, tzinfo=UTC)) == "R"
    assert g.desired_at(gr, tz, datetime(2026, 11, 22, 5, 0, tzinfo=UTC)) == "S"


def test_schedule_follows_local_clock_across_daylight_saving():
    try:
        get_tz("Europe/London")
    except ValueError:
        pytest.skip("tz data not installed (pip install tzdata)")
    gr, tz = _grid("Always Parked (excluding 10hr maintenance period) - Europe/London")
    assert g.desired_at(gr, tz, datetime(2026, 7, 4, 23, 30, tzinfo=UTC)) == "R"  # Sun 00:30 BST
    assert g.desired_at(gr, tz, datetime(2026, 7, 5, 9, 30, tzinfo=UTC)) == "S"  # Sun 10:30 BST
    assert g.desired_at(gr, tz, datetime(2026, 11, 22, 9, 30, tzinfo=UTC)) == "R"  # Sun 09:30 GMT


def test_always_up_always_parked_and_unmanaged_kinds():
    assert parse_schedule("24*7 Always Parked - On Demand").kind == "always_parked"
    assert parse_schedule("24X7 Always Up").kind == "always_up"
    for text in ("Dev Sche", "IL QA backup 3", "UP 4 days Mon 11AM-4PM &Thu 5:30AM IST"):
        p = parse_schedule(text)
        assert p.kind == "unmanaged" and p.reason, text


# ---------------------------------------------------------------- reconcile

def _rc(**kw):
    now = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)  # Monday 11:30 IST, inside the 10:00-22:00 window
    gr, tz = _grid("Up 5 days (Mon-Fri) 10 AM to 10 PM IST")
    base = dict(enabled=True, paused=False, schedule_grid=gr, tz=tz, actual=STOPPED,
                status_time=now - timedelta(minutes=2), override=None,
                op_in_progress=False, now=now)
    base.update(kw)
    return reconcile_vm(**base)


NIGHT = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)  # Monday 23:30 IST, outside the window


def test_start_when_scheduled_up_and_down():
    d = _rc()
    assert (d.action, d.reason, d.desired) == (START, SCHEDULED, "R")
    assert _rc(actual=DEALLOCATED).action == START


def test_stop_when_scheduled_down_and_running():
    d = _rc(actual=RUNNING, now=NIGHT, status_time=NIGHT - timedelta(minutes=1))
    assert (d.action, d.reason, d.desired) == (STOP, SCHEDULED, "S")


def test_nothing_to_do_when_already_in_desired_state():
    assert (_rc(actual=RUNNING).action, _rc(actual=RUNNING).reason) == (SKIP, IN_DESIRED_STATE)
    stopped_at_night = _rc(actual=STOPPED, now=NIGHT, status_time=NIGHT - timedelta(minutes=1))
    assert (stopped_at_night.action, stopped_at_night.reason) == (SKIP, IN_DESIRED_STATE)


@pytest.mark.parametrize("kwargs, reason", [
    (dict(paused=True), GLOBAL_PAUSE),
    (dict(enabled=False), VM_DISABLED),
    (dict(schedule_grid=None), UNMANAGED_NO_SCHEDULE),
    (dict(actual=UNKNOWN), STATUS_UNKNOWN_OR_STALE),
    (dict(status_time=None), STATUS_UNKNOWN_OR_STALE),
    (dict(status_time=datetime(2026, 11, 16, 5, 0, tzinfo=UTC)), STATUS_UNKNOWN_OR_STALE),  # 60 min old
    (dict(op_in_progress=True), OPERATION_IN_PROGRESS),
    (dict(actual="STARTING"), TRANSITIONAL_STATE),
])
def test_unsafe_situations_are_skipped(kwargs, reason):
    d = _rc(**kwargs)
    assert (d.action, d.reason) == (SKIP, reason)


def test_active_hold_means_no_start_and_no_stop():
    hold = Override(expires_at=datetime(2026, 11, 17, tzinfo=UTC))  # still active at both instants below
    start_case = _rc(actual=STOPPED, override=hold)  # schedule says run
    assert (start_case.action, start_case.reason, start_case.desired) == (SKIP, OVERRIDE_ACTIVE, "R")
    stop_case = _rc(actual=RUNNING, override=hold, now=NIGHT, status_time=NIGHT - timedelta(minutes=1))
    assert (stop_case.action, stop_case.reason) == (SKIP, OVERRIDE_ACTIVE)  # schedule says stop


def test_hold_without_end_time_lasts_until_cleared():
    far_future = datetime(2030, 1, 1, tzinfo=UTC)
    d = _rc(override=Override(), now=far_future, status_time=far_future - timedelta(minutes=1))
    assert d.reason == OVERRIDE_ACTIVE


def test_expired_hold_returns_the_vm_to_its_schedule():
    now = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)
    assert _rc(override=Override(expires_at=now - timedelta(minutes=1))).action == START
    assert _rc(override=Override(expires_at=now)).action == START  # the end instant itself is already free


def test_do_nothing_cell_is_skipped():
    now = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)
    gr = g.new_grid("R")
    gr[g.cell_index(now.astimezone(IST))] = "N"
    d = _rc(schedule_grid=gr)
    assert (d.action, d.reason, d.desired) == (SKIP, SCHEDULE_DO_NOTHING, "N")
