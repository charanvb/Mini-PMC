"""Excel files for the dashboard's Export buttons and the emailed report. Times in them are IST, like the dashboard."""
import io
from datetime import datetime

from . import grid as g
from .service import parse_utc
from .tz import IST, gmt_label

STATE_LABEL = {"RUNNING": "Running", "STOPPED": "Stopped", "DEALLOCATED": "Stopped (deallocated)",
               "STARTING": "Starting", "STOPPING": "Stopping", "UNKNOWN": "Unknown", "": "Not checked yet"}
SAYS_LABEL = {"R": "Running", "S": "Stopped", "N": "Do nothing", "": ""}


def tz_text(name: str) -> str:
    label = gmt_label(name) if name else ""
    return f"{name} ({label})" if label else (name or "")


def ist_text(iso_text) -> str:
    """'2026-12-31T18:00:00Z' -> '2026-12-31 23:30' (India time); blank stays blank."""
    t = parse_utc(iso_text) if iso_text else None
    return t.astimezone(IST).strftime("%Y-%m-%d %H:%M") if t else ""


def override_text(r) -> str:
    """The Override cell: an active override says when it ends, a waiting one when it starts."""
    if not (r["override"] or r["override_from"]):
        return ""
    until = ist_text(r["override_until"]) + " IST" if r["override_until"] else "cleared by a person"
    text = f"Until {until}" if r["override"] else f"From {ist_text(r['override_from'])} IST until {until}"
    return text + (f" - {r['override_reason']}" if r["override_reason"] else "")


def resources_table(store, names=None):
    """(headers, rows) of the Resources page; `names` limits it to those VMs."""
    wanted = {n.lower() for n in names} if names else None
    tz = {s["name"]: s["tz"] for s in store.list_schedules()}
    rows = []
    for r in store.overview():
        if wanted is not None and r["name"].lower() not in wanted:
            continue
        rows.append([
            r["name"], r["provider"].upper(), r["scope_name"] or r["scope"], r["scope"], r["group"],
            r["schedule"] or "(none)", tz_text(tz.get(r["schedule"], "")),
            STATE_LABEL.get(r["status"], r["status"]), ist_text(r["status_checked"]), r["status_error"],
            SAYS_LABEL.get(r["says"], r["says"]),
            "Yes" if r["enabled"] else "No",
            override_text(r),
            r["notes"] or ""])
    headers = ["VM", "Cloud", "Subscription / project", "Subscription / project ID", "Resource group / zone",
               "Schedule", "Schedule time zone", "Current status", "Status checked (IST)", "Status problem",
               "Schedule says now", "Enabled", "Override", "Notes"]
    return headers, rows


def schedules_table(store):
    rows = []
    for s in store.list_schedules():
        try:
            text = g.describe(g.from_string(s["grid"])) if s["grid"] else ""
        except Exception:
            text = ""
        rows.append([s["name"], tz_text(s["tz"]), text, s["vm_count"], s["description"] or ""])
    return ["Schedule", "Time zone", "When the VMs run", "VMs", "Description"], rows


def workbook(sheets) -> bytes:
    """sheets: [(title, headers, rows)] -> .xlsx bytes. Text is always stored as text, so a cell that starts with
    = + - or @ is never run as a formula when the file is opened."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    wb.remove(wb.active)
    for title, headers, rows in sheets:
        ws = wb.create_sheet(title[:31])
        ws.append(headers)
        for row in rows:
            ws.append(row)
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value[:1] in ("=", "+", "-", "@", "\t", "\r"):
                    c.data_type = "s"
        for c in ws[1]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="004A99")
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(headers, 1):
            width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:300]]) + 2
            ws.column_dimensions[ws.cell(1, i).column_letter].width = min(width, 60)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def stamp() -> str:
    return datetime.now(IST).strftime("%Y%m%d-%H%M")


def resources_xlsx(store, names=None) -> bytes:
    h, r = resources_table(store, names)
    return workbook([("Resources", h, r)])


def schedules_xlsx(store) -> bytes:
    h, r = schedules_table(store)
    return workbook([("Schedules", h, r)])


def daily_xlsx(store) -> bytes:
    """The emailed daily report: one sheet, one row per VM."""
    _, rows = resources_table(store)
    # resources_table columns: 0 VM, 1 cloud, 2 scope name, 4 group, 5 schedule, 7 status, 12 override
    out = [[r[0], r[1], r[2], r[4], r[7], r[5], r[12] or "None"] for r in rows]
    return workbook([("VM Report", ["VM Name", "Provider", "Project / Subscription", "Resource Group / Zone",
                                    "Status", "Schedule", "Override"], out)])


def full_xlsx(store) -> bytes:
    rh, rr = resources_table(store)
    sh, sr = schedules_table(store)
    return workbook([("Resources", rh, rr), ("Schedules", sh, sr)])
