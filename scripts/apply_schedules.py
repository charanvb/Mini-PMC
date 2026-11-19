"""Create schedules from a CSV and attach them to VMs that are already onboarded.

CSV columns:  Name (VM), Schedule Name, OverrideUntilUTC (ignored), Time Zone

  python scripts\apply_schedules.py --csv schedules.csv --home E:\\Charan\\Mini-PMC            (preview only)
  python scripts\apply_schedules.py --csv schedules.csv --home E:\\Charan\\Mini-PMC --apply    (do it)

- The schedule name is used exactly as written; the time zone comes from the CSV.
- The weekly hours are read from the schedule name with the same reader the dashboard uses.
- Overrides are never touched. An active override keeps working after the schedule is attached.
- Never edits an existing schedule: a same-named schedule with different hours is reported and skipped.
- Names that cannot be read, or that need a human look, are skipped and listed in the report
  (schedule_apply_report.csv, next to the CSV). Use --include-review to create the "REVIEW" ones too.
"""
import argparse
import csv
import os
import sys
from collections import OrderedDict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minipmc import db, grid as g, importer  # noqa: E402
from minipmc.service import Store, UserError  # noqa: E402
from minipmc.tz import get_tz  # noqa: E402


def read_rows(path):
    raw = open(path, "rb").read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    rows = []
    for r in csv.DictReader(text.splitlines()):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        rows.append((r.get("Name", ""), r.get("Schedule Name", ""), r.get("Time Zone", "")))
    return [x for x in rows if x[0]]


def offsets(zone):
    """UTC offsets in winter and summer, so zones that are really the same compare equal."""
    return tuple(datetime(2026, m, 15, 12, tzinfo=timezone.utc).astimezone(get_tz(zone)).utcoffset()
                 for m in (1, 7))


def plan_schedule(name, tz):
    """-> (status, grid or None, detail). status: OK | REVIEW | SKIP"""
    try:
        get_tz(tz)
    except ValueError as e:
        return "SKIP", None, f"time zone not usable: {e}"
    p = importer.parse_schedule(name)
    if p.kind == "unmanaged":
        return "SKIP", None, f"cannot read hours from the name ({p.reason}); create it by hand"
    if p.kind == "always_parked":
        gr = "S" * g.CELLS
    elif p.kind == "always_up":
        gr = p.grid
    else:
        gr = p.grid
    why = list(p.notes) if (p.check or p.notes) else []
    try:
        named = importer.detect_tz(importer._normalise(name))
        if named and offsets(named[0]) != offsets(tz):
            why.append(f"name says {named[1]} but the CSV time zone is {tz}")
    except ValueError:
        pass
    if p.check or why:
        return "REVIEW", gr, "; ".join(why) or "reading was a judgement call"
    return "OK", gr, g.describe(g.from_string(gr))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--home", default=os.environ.get("MINIPMC_HOME", os.getcwd()))
    ap.add_argument("--apply", action="store_true", help="make the changes (default is preview only)")
    ap.add_argument("--include-review", action="store_true", help="also create schedules marked REVIEW")
    ap.add_argument("--report", help="report path (default: schedule_apply_report.csv next to the CSV)")
    a = ap.parse_args()

    home = os.path.abspath(a.home)
    if not os.path.exists(db.db_path(home)):
        sys.exit(f"no database at {db.db_path(home)} - check --home")
    db.init(home)
    store = Store(home, actor="apply_schedules")
    rows = read_rows(a.csv)

    by_name = OrderedDict()
    for vm, sched, tz in rows:
        by_name.setdefault(sched, OrderedDict()).setdefault(tz, []).append(vm)

    existing = {r["name"]: r for r in store.list_schedules()}
    with db.connect(home) as con:
        vm_sched = {r["name"].lower(): r["schedule"] for r in con.execute("SELECT name, schedule FROM vms")}
        overrides = {r[0].lower() for r in con.execute("SELECT vm_name FROM overrides")}

    report, usable = [], {}  # usable: schedule name -> True when VMs may be attached
    created = 0
    for name, tzs in by_name.items():
        if not name:
            for tz, vms in tzs.items():
                report += [("VM", name, v, tz, "SKIP", "blank schedule name") for v in vms]
            continue
        if len(tzs) > 1:
            for tz, vms in tzs.items():
                report += [("VM", name, v, tz, "SKIP", "same schedule name has different time zones in the CSV")
                           for v in vms]
            continue
        tz = next(iter(tzs))
        status, gr, detail = plan_schedule(name, tz)
        if name in existing:
            ex = existing[name]
            if gr is not None and ex["grid"] == gr and ex["tz"] == tz:
                status, detail = "OK", "already exists with the same hours"
            elif name in (importer.ALWAYS_UP, importer.ALWAYS_PARKED):
                status, detail = "OK", "built-in schedule"
            else:
                status, detail = "SKIP", f"a schedule with this name exists with different hours/zone (kept as is); {detail}"
            ok = status == "OK"
        else:
            ok = status == "OK" or (status == "REVIEW" and a.include_review)
            if ok and a.apply:
                try:
                    store._insert_grid_schedule(name, tz, gr, source="apply_schedules")
                    created += 1
                except UserError as e:
                    ok, status, detail = False, "SKIP", str(e)
            elif ok:
                created += 1
        usable[name] = ok
        report.append(("SCHEDULE", name, "", tz, status if ok or status != "OK" else "SKIP", detail))

    attached = unchanged = 0
    for name, tzs in by_name.items():
        for tz, vms in tzs.items():
            if not usable.get(name):
                continue
            for vm in vms:
                key = vm.lower()
                if key not in vm_sched:
                    report.append(("VM", name, vm, tz, "SKIP", "VM is not onboarded"))
                elif vm_sched[key] == name:
                    unchanged += 1
                    report.append(("VM", name, vm, tz, "OK", "already on this schedule"))
                else:
                    note = f"was: {vm_sched[key] or 'none'}" + ("; override kept" if key in overrides else "")
                    if a.apply:
                        store.set_schedule(vm, name)
                    attached += 1
                    report.append(("VM", name, vm, tz, "OK", note))

    for name, tzs in by_name.items():  # VMs of schedules that were not created
        if name and not usable.get(name):
            for tz, vms in tzs.items():
                if len(tzs) == 1:
                    st = next((r[4] for r in report if r[0] == "SCHEDULE" and r[1] == name), "SKIP")
                    report += [("VM", name, v, tz, "SKIP" if st != "REVIEW" else "REVIEW",
                                "not attached: schedule not created") for v in vms]

    out = a.report or os.path.join(os.path.dirname(os.path.abspath(a.csv)), "schedule_apply_report.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Type", "Schedule Name", "VM", "Time Zone", "Status", "Detail"])
        w.writerows(report)

    sched_rows = [r for r in report if r[0] == "SCHEDULE"]
    count = lambda s: sum(1 for r in sched_rows if r[4] == s)
    print(("APPLIED" if a.apply else "PREVIEW (nothing changed - add --apply)"))
    print(f"CSV rows: {len(rows)}   distinct schedules: {len(sched_rows)}")
    print(f"schedules ready: {count('OK')}   need review: {count('REVIEW')}   skipped: {count('SKIP')}")
    print(f"schedules {'created' if a.apply else 'to create'}: {created}")
    print(f"VMs {'attached' if a.apply else 'to attach'}: {attached}   already correct: {unchanged}")
    print(f"VMs not attached: {sum(1 for r in report if r[0] == 'VM' and r[4] != 'OK')}")
    print(f"report: {out}")


if __name__ == "__main__":
    main()
