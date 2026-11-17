"""Build the review workbook from the VM inventory (laptop or server, no cloud access needed).

    python -m minipmc.build_seed --vms vms_all.csv --overrides config/initial_overrides.csv --out review/minipmc_seed.xlsx
"""
import argparse
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import grid as g
from .importer import ALWAYS_PARKED, ALWAYS_UP, Catalog, apply_overrides, build_catalog, load_overrides

IST = timezone(timedelta(hours=5, minutes=30))
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF")
RED = PatternFill("solid", fgColor="F8CBAD")  # unmanaged: Mini PMC will not touch it
YELLOW = PatternFill("solid", fgColor="FFF2CC")  # reading is a judgement call: please confirm
BLUE = PatternFill("solid", fgColor="DDEBF7")  # on hold
WRAP = Alignment(wrap_text=True, vertical="top")

DST_ZONES = {"Europe/London", "Europe/Paris", "Europe/Athens", "Europe/Lisbon", "America/New_York",
             "America/Los_Angeles", "America/Chicago", "Australia/Sydney", "Asia/Jerusalem"}
TZ_NOTES = {
    "BST": "Read as the UK clock (Europe/London): BST in summer, GMT in winter.",
    "GMT": "Read as fixed UTC, no daylight saving (as agreed).",
    "EST": "Read as US Eastern (New York) with daylight saving. A fixed UTC-5 would differ in summer - confirm.",
    "PST": "Read as US Pacific (Los Angeles) with daylight saving - confirm.",
    "AEST": "Read as Sydney time with daylight saving - confirm.",
    "UTC+2": "All 'Egypt(UTC+2)'. Read as fixed UTC+2 exactly as written; Egypt itself uses UTC+3 in summer - confirm.",
    "CET": "Read as Central European time (Paris) with daylight saving.",
    "EET": "Read as Eastern European time (Athens) with daylight saving.",
    "WET": "Read as Western European time (Lisbon) with daylight saving.",
    "PORTUGAL": "Read as Lisbon time with daylight saving.",
    "IL": "Read as Israel (Jerusalem) with daylight saving.",
    "ISRAEL": "Read as Israel (Jerusalem) with daylight saving.",
    "CST6CDT": "Read as US Central (Chicago) with daylight saving.",
    "JKT": "Read as Jakarta, UTC+7 all year.",
    "PKT": "Read as Karachi, UTC+5 all year.",
    "MMT": "Read as Myanmar (Yangon), UTC+6:30 all year.",
    "TURKEY": "Read as Istanbul, UTC+3 all year.",
    "BRAZIL": "Read as Sao Paulo, UTC-3 all year.",
    "BRAZIL/EAST": "Read as Sao Paulo, UTC-3 all year.",
    "BRT": "Read as Sao Paulo, UTC-3 all year.",
}


def _table(ws, headers, rows, widths, wrap=()):
    """rows: iterable of (values, fill or None). wrap: 1-based columns to wrap."""
    ws.append(headers)
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
    for values, fill in rows:
        ws.append(values)
        cells = ws[ws.max_row]
        for col in wrap:
            cells[col - 1].alignment = WRAP
        if fill:
            for cell in cells:
                cell.fill = fill
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def _utc(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "until cleared"


def _ist(dt):
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S") if dt else "until cleared"


def _dst(zone):
    if zone in DST_ZONES:
        return "yes"
    return "no (fixed offset)" if zone == "UTC" or zone.startswith("Etc/") else "no"


def _zone(zone):
    """Etc/GMT-2 means UTC+2: show it the way people read it."""
    m = re.match(r"Etc/GMT([+-])(\d+)$", zone)
    return f"Fixed UTC{'-' if m.group(1) == '+' else '+'}{m.group(2)} ({zone})" if m else zone


def build(vms_csv: str, overrides_csv: str, out_path: str) -> Catalog:
    cat = build_catalog(vms_csv)
    rows = load_overrides(overrides_csv) if overrides_csv and os.path.exists(overrides_csv) else []
    if rows:
        apply_overrides(cat, rows)

    unmanaged_texts = [c for c in cat.conversions.values() if c.parsed.kind == "unmanaged"]
    flagged = [c for c in cat.conversions.values() if c.parsed.check]
    held = [v for v in cat.vms if v.hold]
    forced = [v for v in cat.vms if v.schedule_forced]
    managed = [v for v in cat.vms if v.schedule]
    by_schedule = Counter(v.schedule for v in cat.vms if v.schedule)
    flag_vms = Counter()
    first_text = {}
    for c in cat.conversions.values():
        if c.schedule:
            first_text.setdefault(c.schedule, c.text)
            if c.parsed.check:
                flag_vms[c.schedule] += c.vms

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    summary = [
        ("Generated (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")),
        ("Inventory file", os.path.basename(vms_csv)),
        ("VMs in inventory", len(cat.vms)),
        ("   Azure", sum(1 for v in cat.vms if v.provider == "azure")),
        ("   GCP", sum(1 for v in cat.vms if v.provider == "gcp")),
        ("Distinct source schedule texts", len(cat.conversions)),
        ("   converted to a schedule", len(cat.conversions) - len(unmanaged_texts)),
        ("   left unmanaged", len(unmanaged_texts)),
        ("Schedules created (incl. ALWAYS_UP / ALWAYS_PARKED)", len(cat.schedules)),
        ("VMs with a schedule (managed)", len(managed)),
        ("   always up", by_schedule.get(ALWAYS_UP, 0)),
        ("   always parked", by_schedule.get(ALWAYS_PARKED, 0)),
        ("VMs UNMANAGED - Mini PMC will not touch them (see Unmanaged tab)", len(cat.unmanaged)),
        ("Source texts to confirm (yellow in Conversions)", f"{len(flagged)} texts / {sum(c.vms for c in flagged)} VMs"),
        ("VMs on hold (no start, no stop while active)", len(held)),
        ("   hold until a date", sum(1 for v in held if v.hold_until)),
        ("   hold until cleared by a person", sum(1 for v in held if not v.hold_until)),
        ("   schedule forced", len(forced)),
        ("Duplicate VM names in the inventory", ", ".join(cat.duplicate_names) or "none"),
        ("Override problems", "; ".join(cat.override_errors) or "none"),
        ("", ""),
        ("Colours", "red = unmanaged   yellow = reading needs a human check   blue = on hold"),
        ("Times", "Schedules are in their own time zone (see Schedules / Timezones); the server runs in UTC internally."),
    ]
    _table(ws, ["Item", "Value"], [([k, v], None) for k, v in summary], [66, 90])

    _table(wb.create_sheet("Unmanaged"),
           ["VM name", "Provider", "Subscription / Project", "Resource group / Zone", "Source schedule text", "Why unmanaged"],
           [([v.vm_name, v.provider, v.scope, v.group_or_zone, v.source_text,
              cat.conversions[v.source_text].parsed.reason], RED) for v in cat.unmanaged],
           [24, 10, 40, 34, 48, 40], wrap=(5, 6))

    def conv_key(c):
        kind = 0 if c.parsed.kind == "unmanaged" else 1 if c.parsed.check else 2
        return kind, c.schedule or "", c.text

    conv_rows = []
    for c in sorted(cat.conversions.values(), key=conv_key):
        p = c.parsed
        if p.kind == "unmanaged":
            conv_rows.append(([c.text, c.vms, "UNMANAGED", "", "", "", "YES", p.reason], RED))
            continue
        sched = cat.schedules[c.schedule]
        conv_rows.append(([c.text, c.vms, c.schedule, sched.description, _zone(p.tz), p.tz_token,
                           "YES" if p.check else "", "; ".join(p.notes)], YELLOW if p.check else None))
    _table(wb.create_sheet("Conversions"),
           ["Source schedule text", "VMs", "Mini PMC schedule", "Up windows (schedule time zone)", "Time zone",
            "Zone as written", "Needs check", "Notes"],
           conv_rows, [62, 6, 34, 44, 20, 14, 11, 70], wrap=(1, 8))

    sched_rows = []
    for s in cat.schedules.values():
        sched_rows.append(([s.name, _zone(s.tz), s.description, round(s.up_hours, 2), by_schedule.get(s.name, 0),
                            flag_vms.get(s.name, 0) or "", first_text.get(s.name, ""), s.grid],
                           YELLOW if flag_vms.get(s.name) else None))
    _table(wb.create_sheet("Schedules"),
           ["Schedule", "Time zone", "Up windows", "Up hours/week", "VMs", "VMs to confirm", "Example source text",
            "Grid (672 cells x 15 min, Mon 00:00 first; R=run, S=stop)"],
           sched_rows, [36, 20, 44, 13, 7, 14, 54, 60], wrap=(7,))

    vm_rows = []
    for v in cat.vms:
        conv = cat.conversions[v.source_text]
        if not v.schedule:
            fill = RED
        elif v.hold:
            fill = BLUE
        else:
            fill = YELLOW if conv.parsed.check else None
        vm_rows.append(([v.vm_name, v.provider, v.scope, v.group_or_zone, v.schedule or "UNMANAGED",
                         "YES" if v.schedule else "NO", "YES" if v.hold else "",
                         _utc(v.hold_until) if v.hold else "", v.hold_reason, v.source_text], fill))
    _table(wb.create_sheet("VMs"),
           ["VM name", "Provider", "Subscription / Project", "Resource group / Zone", "Mini PMC schedule", "Managed",
            "On hold", "Hold until (UTC)", "Hold reason", "Source schedule text"],
           vm_rows, [24, 10, 40, 34, 34, 9, 8, 20, 40, 62])

    by_name = {v.vm_name.lower(): v for v in cat.vms}
    ov_rows = []
    for r in rows:
        v = by_name.get(r.vm_name.lower())
        if v is None:
            ov_rows.append(([r.vm_name, "", "", "", _utc(r.hold_until), _ist(r.hold_until), r.reason,
                             r.force_schedule or "", "NOT FOUND in inventory"], RED))
            continue
        ov_rows.append(([v.vm_name, v.provider, v.source_text, v.schedule or "UNMANAGED", _utc(r.hold_until),
                         _ist(r.hold_until), r.reason, r.force_schedule or "",
                         "applied (schedule forced)" if v.schedule_forced else "applied"], BLUE))
    _table(wb.create_sheet("Overrides"),
           ["VM name", "Provider", "Original source schedule", "Mini PMC schedule", "Hold until (UTC)",
            "Hold until (IST)", "Reason", "Forced schedule", "Status"],
           ov_rows, [24, 10, 56, 34, 20, 20, 40, 18, 26], wrap=(3,))

    tz_rows = []
    for (token, zone), use in sorted(cat.tz_usage.items(), key=lambda kv: -kv[1]["vms"]):
        note = TZ_NOTES.get(token, "Fixed UTC offset written in the text; no daylight saving." if token.startswith("UTC") else "")
        tz_rows.append(([token, _zone(zone), use["vms"], _dst(zone), use["example"], note],
                        YELLOW if "confirm" in note else None))
    _table(wb.create_sheet("Timezones"),
           ["Zone as written", "Used as", "VMs", "Daylight saving", "Example source text", "Note"],
           tz_rows, [16, 24, 7, 16, 62, 80], wrap=(5, 6))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    wb.save(out_path)
    return cat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the Mini PMC review workbook from the VM inventory.")
    ap.add_argument("--vms", default="vms_all.csv")
    ap.add_argument("--overrides", default=os.path.join("config", "initial_overrides.csv"))
    ap.add_argument("--out", default=os.path.join("review", "minipmc_seed.xlsx"))
    args = ap.parse_args(argv)

    cat = build(args.vms, args.overrides, args.out)
    unmanaged_texts = sum(1 for c in cat.conversions.values() if c.parsed.kind == "unmanaged")
    flagged = [c for c in cat.conversions.values() if c.parsed.check]
    held = [v for v in cat.vms if v.hold]
    print(f"VMs: {len(cat.vms)}  |  schedule texts: {len(cat.conversions)} ({unmanaged_texts} unmanaged)")
    print(f"Schedules: {len(cat.schedules)}  |  managed VMs: {sum(1 for v in cat.vms if v.schedule)}"
          f"  |  unmanaged VMs: {len(cat.unmanaged)}")
    print(f"To confirm: {len(flagged)} texts / {sum(c.vms for c in flagged)} VMs  |  on hold: {len(held)}"
          f" ({sum(1 for v in held if not v.hold_until)} until cleared)")
    for name in cat.duplicate_names:
        print(f"WARNING duplicate VM name: {name}")
    for err in cat.override_errors:
        print(f"ERROR {err}")
    print(f"Wrote {args.out}")
    return 2 if cat.override_errors else 0


if __name__ == "__main__":
    sys.exit(main())
