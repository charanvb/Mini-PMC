"""The status report email (body + Excel attachment): sent by hand from the dashboard, or once a day by the scheduler."""
import html
import logging
from datetime import datetime, timedelta, timezone

from . import db, exports
from .mailer import Mailer, split_addresses
from .service import iso, parse_utc
from .tz import get_tz

log = logging.getLogger("minipmc.reports")
MAX_ROWS = 150  # VM rows in the email body; the Excel attachment has all of them
BLUE, RED, AMBER, GREEN = "#004a99", "#b00020", "#b26a00", "#1b7f3b"


def recipients(store, creds) -> list:
    text = store.get_setting("report_to", "")
    if not text:
        cfg = creds.cfg.get("email_logic_app", {}) or {}
        text = cfg.get("to") or ""
    return split_addresses(text)


def _td(text, style="") -> str:
    return f"<td style='border:1px solid #ddd;padding:4px 8px;{style}'>{html.escape(str(text))}</td>"


def _table(headers, rows) -> str:
    head = "".join(f"<th style='border:1px solid #ddd;padding:4px 8px;background:#eef3fa;text-align:left'>"
                   f"{html.escape(h)}</th>" for h in headers)
    return ("<table style='border-collapse:collapse;font-size:13px'><tr>" + head + "</tr>"
            + "".join("<tr>" + "".join(r) + "</tr>" for r in rows) + "</table>")


def build(store, now=None) -> dict:
    """{"subject", "html", "attachment": (name, bytes), "health"}."""
    now = now or datetime.now(timezone.utc)
    vms = store.overview(now)
    managed = [v for v in vms if v["enabled"] and v["schedule"] and not v["override"]]
    mismatch = [v for v in managed
                if (v["says"] == "R" and v["status"] in ("STOPPED", "DEALLOCATED"))
                or (v["says"] == "S" and v["status"] == "RUNNING")]
    unknown = [v for v in vms if v["enabled"] and v["status"] in ("", "UNKNOWN")]
    since = iso(now - timedelta(hours=24))
    with db.connect(store.home) as con:
        ops = {r["outcome"]: r["n"] for r in con.execute(
            "SELECT outcome, COUNT(*) n FROM ops WHERE started_at > ? GROUP BY outcome", (since,))}
    bad_ops = ops.get("FAILED", 0) + ops.get("STUCK", 0)
    health = "RED" if bad_ops else ("AMBER" if mismatch or unknown else "GREEN")
    colour = {"RED": RED, "AMBER": AMBER, "GREEN": GREEN}[health]
    count = lambda state: sum(1 for v in vms if v["status"] == state)  # noqa: E731
    mode = store.get_setting("mode", "readonly")

    summary = _table(["Total VMs", "Running", "Stopped", "Unknown / not read", "Differ from schedule",
                      "Actions 24h (ok / failed / stuck)"],
                     [[_td(len(vms)), _td(count("RUNNING")), _td(count("STOPPED") + count("DEALLOCATED")),
                       _td(len(unknown)), _td(len(mismatch), f"color:{AMBER};font-weight:600" if mismatch else ""),
                       _td(f"{ops.get('SUCCEEDED', 0)} / {ops.get('FAILED', 0)} / {ops.get('STUCK', 0)}",
                           f"color:{RED};font-weight:600" if bad_ops else "")]])
    attention = ""
    if mismatch or unknown:
        rows = [[_td(v["name"]), _td(v["scope_name"] or v["scope"]), _td(exports.STATE_LABEL.get(v["status"], v["status"])),
                 _td(exports.SAYS_LABEL.get(v["says"], v["says"])), _td(v["status_error"] or "")]
                for v in (mismatch + unknown)[:50]]
        attention = ("<h3 style='color:" + AMBER + "'>Needs attention</h3>"
                     + _table(["VM", "Subscription", "Current", "Schedule says", "Problem"], rows))
    h, r = exports.schedules_table(store)
    sched = _table(h[:4], [[_td(c) for c in row[:4]] for row in r])
    vm_rows = [[_td(v["name"]), _td(v["scope_name"] or v["scope"]), _td(v["schedule"] or "(none)"),
                _td(exports.STATE_LABEL.get(v["status"], v["status"])), _td(exports.SAYS_LABEL.get(v["says"], v["says"]))]
               for v in vms[:MAX_ROWS]]
    more = f"<p>Showing {MAX_ROWS} of {len(vms)} VMs; the attached Excel file has all of them.</p>" if len(vms) > MAX_ROWS else ""
    body = (f"<div style='font-family:Segoe UI,Arial,sans-serif;font-size:14px'>"
            f"<div style='background:{BLUE};color:#fff;padding:10px 14px;font-size:16px;font-weight:600'>"
            f"Mini PMC report <span style='background:{colour};padding:2px 8px;margin-left:8px'>{health}</span></div>"
            f"<p>{html.escape(now.strftime('%d %b %Y %H:%M'))} UTC &middot; mode: <b>{html.escape(mode)}</b>"
            f"{' &middot; <b>PAUSED</b>' if store.paused else ''}</p>"
            f"{summary}{attention}<h3>Schedules</h3>{sched}<h3>VMs</h3>"
            f"{_table(['VM', 'Subscription', 'Schedule', 'Current', 'Schedule says'], vm_rows)}{more}"
            f"<p style='color:#666;font-size:12px'>Full detail is in the attached Excel file.</p></div>")
    subject = f"Mini PMC report [{health}] {now.strftime('%Y-%m-%d')} - {len(vms)} VMs"
    return {"subject": subject, "html": body, "health": health,
            "attachment": (f"MiniPMC-report-{now.strftime('%Y%m%d-%H%M')}.xlsx", exports.full_xlsx(store))}


def send(store, creds, to=None, mailer: Mailer = None, now=None) -> dict:
    """Build and email the report. Returns {"ok", "detail", "to"}; never raises for delivery problems."""
    to = split_addresses(to) if isinstance(to, str) else (to or recipients(store, creds))
    mailer = mailer or Mailer(creds)
    if not mailer.configured:
        return {"ok": False, "detail": "the email Logic App is not configured (email_logic_app in config\\settings.json)", "to": to}
    if not to:
        return {"ok": False, "detail": "no report recipient is set (Settings > Email & alerts)", "to": to}
    rep = build(store, now)
    r = mailer.send(to, rep["subject"], rep["html"], [rep["attachment"]])
    store.log_event("REPORT_SENT" if r["ok"] else "REPORT_FAILED", "", to=len(to), health=rep["health"],
                    **({} if r["ok"] else {"error": r["detail"]}))
    return {"ok": r["ok"], "detail": r["detail"], "to": to, "subject": rep["subject"]}


def send_if_due(store, creds, now=None) -> bool:
    """Called every minute: send the daily report once, at the configured local time. Returns True if one was sent."""
    if store.get_setting("report_auto", "0") != "1":
        return False
    now = now or datetime.now(timezone.utc)
    try:
        local = now.astimezone(get_tz(store.get_setting("report_tz", "UTC") or "UTC"))
        hh, mm = (int(x) for x in (store.get_setting("report_time", "08:00") or "08:00").split(":"))
    except ValueError:
        return False
    today = local.strftime("%Y-%m-%d")
    if store.get_setting("report_last", "") == today or (local.hour, local.minute) < (hh, mm):
        return False
    tried = parse_utc(store.get_setting("report_try", "") or None)
    if tried and now - tried < timedelta(minutes=15):
        return False
    store.set_setting_quiet("report_try", iso(now))
    result = send(store, creds, now=now)
    if result["ok"]:
        store.set_setting_quiet("report_last", today)
    return result["ok"]
