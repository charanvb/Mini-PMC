"""The status report email (body + Excel attachment): sent by hand from the dashboard, or once a day by the scheduler."""
import logging
from datetime import datetime, timedelta, timezone

from . import db, exports
from .mailer import Mailer, split_addresses
from .service import iso, parse_utc
from .tz import get_tz

log = logging.getLogger("minipmc.reports")


def recipients(store, creds) -> list:
    text = store.get_setting("report_to", "")
    if not text:
        cfg = creds.cfg.get("email_logic_app", {}) or {}
        text = cfg.get("to") or ""
    return split_addresses(text)


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
    body = ("Hi All,<br><br>"
            "Please find attached the latest automated parking schedule report.<br><br>"
            "Regards,<br>"
            "Cloud BAU Team")
    return {"subject": "Cloud VM parking schedules report", "html": body, "health": health,
            "attachment": ("VMs parking schedules.xlsx", exports.daily_xlsx(store))}


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
