"""ONE-TIME: create the schedules below (name kept exactly) and attach them to the already-onboarded VMs.

Run on the server, no arguments:      python apply_schedules_once.py
Safe to re-run (a second run changes nothing).

- Hours and time zone are read from each schedule name with the dashboard's own reader (minipmc.importer),
  except the few names in SPECIAL, which are fixed by hand.
- A schedule that already exists with different hours/zone is UPDATED (VMs already on it get the new hours).
- Overrides are never touched. VMs that are not onboarded are skipped and listed.
- Writes a report to <home>\\logs\\apply_schedules_once_report.csv
"""
import csv
import os
import sys

DRY_RUN = False  # True = only print what would happen

HOME = os.environ.get("MINIPMC_HOME") or next(
    (p for p in (r"E:\Charan\Mini-PMC", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) if os.path.isdir(os.path.join(p, "minipmc"))), "")
if not HOME:
    sys.exit("Cannot find the CloudOps Scheduler folder. Set MINIPMC_HOME first.")
sys.path.insert(0, HOME)

from minipmc import db, grid as g, importer  # noqa: E402
from minipmc.service import Store, UserError  # noqa: E402
from minipmc.tz import get_tz  # noqa: E402

ALL_UP = "R" * g.CELLS
ALL_PARKED = "S" * g.CELLS

# name -> (time zone, 672-char grid)
SPECIAL = {
    "Dev Sche": ("Asia/Kolkata", ALL_UP),
    "Testing Schedule": ("Asia/Kolkata", ALL_UP),
    "IL QA backup 3": ("Asia/Kolkata", ALL_UP),
    "24*7 always parked (Use Only on Servers - Marked for Decomission or Capability)": ("Asia/Kolkata", ALL_PARKED),
    "24x7 always parked (Use Only on Servers - Marked for Decomission or Capability)": ("Asia/Kolkata", ALL_PARKED),
    "UP 5 days Mon-Fri Sat-Sun 12 PM - 12 AM": ("Asia/Kolkata", None),  # built below
}
_gr, _ = Store.grid_from_windows("Mon-Fri 00:00-24:00\nSat-Sun 12:00-24:00")
SPECIAL["UP 5 days Mon-Fri Sat-Sun 12 PM - 12 AM"] = ("Asia/Kolkata", g.to_string(_gr))

DATA = [
    ("Up 5 days (Mon-Fri) 10 AM to 10 PM IST", "bieafpw200011 biefsqw200002 bienwuw200001 biespds200001 biespds200006 biespds200007 biespdsx00099 biespdsx00100 biespdsx00101 biespdsx00106 biespdsx00107 biespdsx00108 biespqsx00015 biespqsx00016 biespqsx00062 biespqsx00063 biespqsx00101 biespqsx00102 biespusx00139 biespusx00142 biespusx00143 biespusx00147 biespusx00151 biespusx00153 biespusx00156 bnlafdw201033 bnlafdw201034 bnlafdw201035 bnlafdw201036 bnlafdw201037 bnlafdw201038 bnlafdw201039 bnlafdw201040 bnlafdw201042 bnlafdw201046 bnlafdw201047 bnlafdw201048 bnlafdw201049 bnlafdw201050 bnlafpw200901 bnlafpw200902 bnlafqw200282 bnlesql200012 bnlspds200001 bnlspds200002 bnlspds200003 bnlspds200013 bnlspdsx00041 bnlspdsx00042 bnlspdsx00061 bnlspdsx00062 bnlspdsx00075 bnlspdsx00076 bnlspdsx00092 bnlspdsx00093 bnlspqs200012 bnlspqs200013 bnlspqsx00076 bnlspqsx00077 bnlspqsx00078 bnlspqsx00151 bnlspqsx00155 bnlspqsx00158 bnlspqsx00170 bnlspqsx00171 bnlspqsx00189 bnlwedauap20012 bnlwesmpap20002 bnlwespdap20001 bnlwesppap20504 gfrspqlx00024 gnlgfpw200025 gnlgfpw200026 gnlgfpw200027"),
    ("Always Parked (excluding 10hr maintenance period) - Turkey", "biedmpw200010"),
    ("Up 5 days (Mon-Sat)10 AM to 4:30 AM IST", "bieesdw200002 bieesdw200003 bieesdw200004 bieesdw200006 bieesqw200007 bieesqw200008 bieesqw200009 bieesqw200010"),
    ("Up 2 days (Tue-Wed) 11:00 AM to 11:00 PM IST", "biefspl200021 biefspl200022 biefspl200024 biefspl200031 biefspl200032 biefspl200033"),
    ("Up 1 day (Sun) 12:00 AM to 06:00 AM IST", "biefsplx00076 bnlfsplx00070 bnlfsplx00071 bpufsplx00034 bpufsplx00035 bpufsplx00036 bsgfsplx00028 bsgfsplx00029"),
    ("Up 5 days (Mon-Fri) 11:30 AM to 02:30 AM IST", "biefspx200024"),
    ("Up 5 days (Mon-Fri) 09:00 AM to 06:00 PM IST", "biefsuwm00001 bnlfsuwm00001"),
    ("Up 3 days Mon 11 AM to 4 PM & 11:30 PM to 11:59 PM & Sat 8:30 PM to Sun 11:59 PM IST", "bienodmpap20010"),
    ("24*7 always parked (Use Only on Servers - Marked for Decomission or Capability)", "bienofspap20130 bnlwefspap20679"),
    ("Up 5 days (Mon-Fri) 10 AM to 6 PM IST", "biespqsx00079 biespqsx00080 bnldadw200058 bnldadw200059 bnldadw200060 bnldaqw200013 gfrmbqw200002 gfrmbqw200004 gfrmbqw200007 gfrspdlx00028 gfrspdlx00029"),
    ("Up 5 days (Mon- Fri) 10 AM to 10 PM IST", "biespqsx00103 bnlafqw200339 bnlafqw200345 bnlafqw200346 bnlafqw200348 bnlafqw200349 bnlafqw200351 bnlafqw200352 bnlafqw200353 bnlafqw200354 bnlnwuw200001 bnlnwuw200002 bnlspdsx00102 bnlspdsx00103 bnlspqsx00221 gfrspdlx00045 gfrspdlx00046 gfrspdlx00061 gfrspdlx00062 gfrspdw200006 gfrspdw200007 gfrsppw200001 gfrspslx00015 gfrspslx00016 gfrspslx00017 gfrspslx00018 gfrspslx00019 gfrspslx00020 gfrspulx00003 gfrspulx00004 gfrspulx00005 gfrspuw200001"),
    ("Up 5 Days (Mon-Fri) 6 AM to 6 PM Brazil/East", "bnlafdw200985"),
    ("Always Parked (excluding 10hr maintenance period) - CST6CDT", "bnlafdw200992 bnlgsqw200010 bnlgsqw200011 gfrgspw200006 gfrgspw200008"),
    ("Up 7 Days 05:00 AM IST to 12:30 AM IST", "bnlafdw201028"),
    ("Up 5 days (Mon-Fri) 10:00 AM to 10:00 PM IST", "bnlafdw201051 bnlafdw201052 bnlafdw201053 bnlafdw201054 bnlafdw201062 bnlafdw201063 bnlafdw201064 bnlafdw201065 bnlafdw201066 bnlafdw201067 bnlfspx200001 gfrspdlx00034 gfrspdlx00035"),
    ("Up 5 Days (Mon-Fri) 06:30 AM to 10:30 PM IST", "bnlafdw201055 bnlafdw201057"),
    ("Up 5 days (Sun-Thu) 5 AM to 5 PM Israel", "bnlcspw200006"),
    ("Up 5 days (Mon- Fri) 9 AM to 6 PM GMT", "bnldeqw200002 bnldeqw200003"),
    ("Up 5 days (Mon- Fri) 6:15 AM to 10 PM Turkey", "bnldmql200001"),
    ("Up 5 days (Mon- Fri) 6 AM to 10 PM Turkey", "bnldmql200002 bnldmqw200001 bnldmqw200002"),
    ("Always Parked (excluding 10hr maintenance period) - Thursday/GMT", "bnldmqw200014 bnldmqw200015 bnldmqw200016 bnldmuw200026 bnldmuw200027 bnldmuw200028 bnlgfdw200005 bnlgfqw200004"),
    ("Always Parked (excluding 10hr maintenance period) - IST", "bnldsdw200001 gfrdadlx00004 gfrdaqlx00004 gfresnw200015 gfresnw200017 gfresnw200018 gfrgfqw200025"),
    ("Up 5 days (Mon - Fri) 06:00 AM \u2013 18:00 PM GMT", "bnlesdw200001"),
    ("Up 5 days (Mon-Fri) 2 PM to 11 PM IST", "bnlesdw200018 bnlweesddb20001 bnlweesqap20044"),
    ("Up 5 days (Mon-Fri) 10.30 AM to 10.30 PM IST", "bnlesdw200023"),
    ("Up 5 days (Mon- Fri) 6 AM to 10 PM GMT", "bnlespw200019 bnlespw200020 gnlespw200028"),
    ("Up 7 days(Mon-Sun) 11:45 PM to 12:30 AM GMT & Up 5 days(Mon-Fri) 06:00 AM to 06:00 PM GMT", "bnlesqw200001"),
    ("Up 5 days (Mon-Fri) 10:30 AM to 10:30 PM IST", "bnlesqw200028 bnlesqw200038 bnlspqsx00013 gfresqw200056 gfresqw200057 gfressw200013 gfrhrpw200010 gfrspdlx00066 gfrspdlx00067"),
    ("Up 5 days (Mon-Fri) 10:30 AM to 11:59 PM IST", "bnlesqw200040 gfrfsplx00022 gfrfsplx00023 gfrfsplx00101 gfrfsplx00105 gfrfsplx00108 gfrntqw200001 gnlfsplx00020 gnlfsplx00024 gnlfsplx00173 gnlfsplx00177"),
    ("Up 5 days (Mon-Fri) 6 AM to 6 PM Europe/London", "bnlesqw200044 bnlesqw200045 bnlfrqlx00001 bnlfrqlx00004 gfrccdw200583"),
    ("Up 5 days (Mon - Fri)  08:00 AM \u2013 18:00 PM GMT", "bnlfrqw200001"),
    ("UP 5 days(Mon-Fri) 6 AM to 8 PM GMT-3", "bnlfrqw200019 bnlfrqw200022 bnlfrqw200029 bnlfrqw200030 gnlgspw200095 gnlhrpw200039 gnlhrpw200041"),
    ("Up 5 days (Mon-Fri) 09:00 AM to 09:00 PM IST", "bnlfsdlx00005 bnlfsdlx00006 bnlfsdw200589 bnlwegspap20549 bnlwegspap20550 gfrdmdlx00019 gfrdmdlx00020 gnlfsplx00175"),
    ("Up 7 days 04:00 PM to 12:00 AM UTC", "bnlfspw200010"),
    ("Up Mon 08:00 AM to Fri 08:00 PM IST", "bnlfspw200161 bnlfspx200020"),
    ("Up 5 days (Mon-Fri) 07:00 AM to 07:00 PM CET", "bnlfspw200185"),
    ("Up 6 days (Mon-Sat) 06:00 AM to 11:00 PM IST", "bnlfspw200188"),
    ("Up 5 days (Mon-Fri) 06:00 AM to 06:00 PM UTC", "bnlfsql200004 bnlfsqw200012 bnlfsqw200013 bnlfsqw200014 bnlfsqw200015"),
    ("Up 5 days (Mon-Fri) 11:00 AM to 11:00 PM IST", "bnlfsul200004 bnlfsul200005 gfrspdlx00047 gfrspdlx00048"),
    ("Up 5 days (Mon-Fri) 10:00 AM to 06:00 PM IST", "bnlfsuw200008"),
    ("Up 7 days 9 AM to 12 AM IST", "bnlfsuw500007"),
    ("Up 5 days (Mon-Fri) 8AM to 6PM BRT", "bnlgfdw200004"),
    ("Up 5 Days (Mon\u2013Fri) 5:30 PM to 2:30 AM IST", "bnlgsdlx00011"),
    ("UP 5 days (Mon -Fri) 5 AM to 5 PM GMT+3", "bnlgspw200004"),
    ("Up 6 days (Mon to Sat) 6 AM to 11:00 PM UTC +8", "bnlgspw200045 bnlgspw200046"),
    ("Up 5 days (Mon-Fri) 8 AM to 6 PM EST", "bnlnapw200005"),
    ("Up 5 day (Mon-Fri) 7:00 AM to 9:30 PM BST", "bnlspdsx00172 bnlspdsx00173 bnlspdsx00174 bnlspdsx00175 bnlspdsx00176 bnlspdsx00177 bnlspqsx00222 bnlspqsx00223 bnlspqsx00226 bnlspqsx00227 bnlspqsx00228 bnlspqsx00229"),
    ("Up 5 days (Mon- Fri) 9 AM to 9 PM IST", "bnlspqs200001 bnlspqs200002 bnlspqs200003 bnlspqs200004 bnlspqs200005 bnlspqs200007 bnlspqs200008 bnlspqs200009 bnlspqs200010 bnlspqs200011 bnlwespqs3099 bnlwespqs3100"),
    ("Up 6 Days (Mon-Sat) 09:00 AM to 01:00 AM IST", "bnlspqsx00192 bnlspqsx00193"),
    ("Up 5 days (Mon-Fri) 24*5 Brazil/East", "bnlweafnlil0009"),
    ("Up 5 days Mon 6 AM to Fri 10 PM GMT", "bnlwegsqap20506"),
    ("Always Parked (excluding 10hr maintenance period) - Europe/London", "gfrcduw200001 gfrcduw200002 gfrcduw200003 gfrcduw200004 gfrcduw200005 gfrcduw200006 gfrcduw200007 gfrcduw200008 gfrcduw200010 gfrcduw200012 gfrcduw200013 gfrcduw200014 gfrcduw200015 gfrcduw200016"),
    ("24x7 always parked (Use Only on Servers - Marked for Decomission or Capability)", "gfrcduw200011"),
    ("Up 5 days (Sun - Fri) 06:00 AM \u2013 09:00 PM IL", "gfrcepw200009 gfrcepw200010 gfrcepw200011 gfrcepw200012 gnlcepw200009"),
    ("UP 5 days Mon-Fri Sat-Sun 12 PM - 12 AM", "gfrcmdw200005"),
    ("9am-11pm GMT+6", "gfrerqw200002"),
    ("Up 5 days Sun 10 PM to Sat 12:30 AM GMT", "gfresew200005 gfresew200006 gfresew200007"),
    ("Up 5 days (Mon-Fri) 10 AM to 10:30 PM IST", "gfresqlx00011"),
    ("Always Parked (excluding 10hr maintenance period) - GMT", "gfresqw200036"),
    ("IL QA backup 3", "gfrfrdw200003 gfrfrdw200006 gfrntqw200002 gfrntqw200003"),
    ("Up 1 day (Sat) 12:00 AM to 11:59 PM IST", "gfrfsqlx00017"),
    ("Up 1 day (Sun) 11:30 AM to 5:30 PM GMT", "gfrgfdw200008 gfrgfpw200009"),
    ("Up 5 days (Mon-Fri) 9 AM to 6 PM Portugal", "gfrgfqw200020 gfrgfqw200022"),
    ("Up 5 days Mon 12:15 AM to Sat 12:15 AM CET", "gfrgfqw200021"),
    ("UP 7 days (Mon-Sun) 7:00 AM to 6 PM GMT", "gfrgsdw200023 gfrgsdw200024"),
    ("Up 1 day (Mon) 5 PM to 8 PM GMT", "gfrgslw200003 gfrhrqw100005"),
    ("Up 5 days (Mon-Fri) 7 AM to 5 PM EST", "gfrgsqlx00007"),
    ("Dev Sche", "gfrhrdlx00001"),
    ("Testing Schedule", "gfrhrqw200002"),
    ("Up 5 days (Mon-Fri) 7 AM to 7 PM AEST", "gfrhrqw200010 gfrhrqw200011"),
    ("Up 5 days (Mon-Fri) 9 AM to 3 AM IST and Down (Sat - Sun) 3 AM to 11:00 AM IST", "gfrhrslx00012 gfrhrslx00013"),
    ("Up 5 days(Mon-Fri) 7 AM to 5 PM CET", "gfrntpw200001 gfrntpw200006"),
    ("Up 5 days (Mon-Fri) 8 AM to 6 PM CET", "gfrntqlx00001"),
    ("Up 5 days 09:00 AM to 08:00 PM IST", "gfrnwdw200001"),
    ("Up 7 days (Mon-Sun) 9:30 AM to 12:30 AM IST", "gfrspdlx00036 gfrspdlx00040"),
    ("up 5 days (Mon-Fri) 7:30 AM to 10:30 PM IST", "gfrspqlx00002 gfrspqlx00004"),
    ("Up 7 days 7 AM to 11:59 PM Egypt(UTC+2)", "gnldmplx00009 gnldmplx00010 gnldmplx00016 gnldmplx00019 gnlgsplx00020 gnlgsplx00022"),
    ("Up 5 days (Mon-Fri) 9 AM to 6 PM CET", "gnldmpw200015"),
    ("Up 5 days (Mon-Fri) 8 AM to 6 PM MMT", "gnldmpw200018"),
    ("Up 5 days (Mon-Fri) 24*5 GMT", "gnlerpw200001"),
    ("UP 7 Days 11:30 AM to 9:30 AM GMT", "gnlfrpw200002"),
    ("Up 7 days 07:00 AM to 07:00 PM IST", "gnlfspw200085"),
    ("Up 7 days 07:00 AM to 08:00 AM IST", "gnlfspx200016 gnlfspx200017"),
    ("Up 7 Days (Mon - Sun ) 5Am to 1Am WET", "gnlgfpw200033 gnlgfpw200035"),
    ("Up 5 days (Mon-Fri) 9:30 AM to 6:30 PM PST", "gnlgfpw200040 gnlgfpw200041"),
    ("Up 5 days(Sun-Thur) 10:00 AM to 11:59 PM Egypt(UTC+2)", "gnlgsplx00021"),
    ("Up 7 days (Mon-Sun) 8 AM to 2 AM IST", "gnlgspw100009"),
    ("Up 5 Days (Mon to Fri) 7AM to 6PM EET", "gnlgspw200046 gnlgspw200047 gnlgspw200048"),
    ("UP 6 days (Mon-Sat) 8 AM to 9 PM PKT", "gnlgspw200098"),
    ("UP 7 days (Mon-Sun) except Down SUN 12 AM to 7 AM JKT", "gnlgspw200100 gnlgspw200102"),
    ("UP 7 days (Mon-Fri) 25*5 Except Sat & Sun 8 AM to 12AM Asia/Dhaka (+06:00)", "gnlhrpw100013 gnlhrpw100014"),
    ("UP 5 days(Mon-Fri) 8 AM to 6 PM GMT+3", "gnlhrpw200002"),
    ("Up 7 days (Mon-Sun) 6 AM to 10 PM CET", "gnlhrpw200004"),
    ("Up 7 days(Mon-Fri) 7 AM to 7 PM & (Sat-Sun) 7 AM to 4 PM GMT+1", "gnlhrpw200008"),
    ("Up 5 days (Mon-Fri) 08:00 AM to 08:00 PM IST", "gsgcdpw200001"),
    ("Up 5 days (Mon- Fri) 6 AM to 11:59 PM IST", "gsgcdul200017 gsgcdul200018 gsgcdul200019 gsgcdul200020"),
]


def plan(name):
    """-> (tz, grid)"""
    if name in SPECIAL:
        return SPECIAL[name]
    p = importer.parse_schedule(name)
    if p.kind != "grid":
        raise UserError(f"cannot read '{name}' ({p.kind}: {p.reason})")
    get_tz(p.tz)
    return p.tz, p.grid


def main():
    db.init(HOME)
    store = Store(HOME, actor="apply_schedules_once")
    existing = {r["name"]: r for r in store.list_schedules()}
    with db.connect(HOME) as con:
        vms = {r["name"].lower(): (r["name"], r["schedule"]) for r in con.execute("SELECT name, schedule FROM vms")}
        ovr = {r[0].lower() for r in con.execute("SELECT vm_name FROM overrides")}

    plans = {}
    for name, _ in DATA:  # read every name first: if any cannot be read, change nothing
        plans[name] = plan(name)

    report, made, updated, same, attached, kept, missing = [], 0, 0, 0, 0, 0, 0
    for name, vm_text in DATA:
        tz, gr = plans[name]
        ex = existing.get(name)
        if ex is None:
            if not DRY_RUN:
                store._insert_grid_schedule(name, tz, gr, source="apply_schedules_once")
            made += 1
            report.append(("SCHEDULE", name, "", tz, "CREATED", g.describe(g.from_string(gr))))
        elif ex["grid"] != gr or ex["tz"] != tz:
            if not DRY_RUN:
                store.update_schedule(name, tz, gr)
            updated += 1
            report.append(("SCHEDULE", name, "", tz, "UPDATED", f"was {ex['tz']} {ex['description']}"))
        else:
            same += 1
            report.append(("SCHEDULE", name, "", tz, "UNCHANGED", ""))
        for vm in vm_text.split():
            row = vms.get(vm.lower())
            if not row:
                missing += 1
                report.append(("VM", name, vm, tz, "NOT ONBOARDED", "skipped"))
            elif row[1] == name:
                kept += 1
                report.append(("VM", name, vm, tz, "ALREADY ATTACHED", ""))
            else:
                if not DRY_RUN:
                    store.set_schedule(row[0], name)
                attached += 1
                report.append(("VM", name, vm, tz, "ATTACHED",
                               f"was: {row[1] or 'none'}" + ("; override kept" if vm.lower() in ovr else "")))

    out = os.path.join(HOME, "logs", "apply_schedules_once_report.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Type", "Schedule Name", "VM", "Time Zone", "Result", "Detail"])
        w.writerows(report)
    print("DRY RUN - nothing changed" if DRY_RUN else "DONE")
    print(f"schedules: created {made}, updated {updated}, unchanged {same}")
    print(f"VMs: attached {attached}, already attached {kept}, NOT onboarded (skipped) {missing}")
    print(f"report: {out}")


if __name__ == "__main__":
    try:
        main()
    except UserError as e:
        sys.exit(f"STOPPED, nothing changed: {e}")
