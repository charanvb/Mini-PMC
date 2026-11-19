"""Alert emails for things that need a person: failed or stuck actions, an unreadable cloud, a stopped scheduler.

Each alert has a key; the same key is sent at most once per throttle window, so a problem that lasts hours does not
fill the inbox. The send time is kept in the settings table without an audit event of its own (the send is audited).
"""
import html
import logging
import threading
from datetime import datetime, timedelta, timezone

from . import db
from .logs import utcnow_iso
from .mailer import Mailer, split_addresses
from .service import parse_utc

log = logging.getLogger("minipmc.alerts")
THROTTLE = timedelta(hours=1)


def recipients(store, creds=None) -> list:
    """Alert recipients: the dashboard setting, else email_logic_app.alert_to / .to from settings.json."""
    text = store.get_setting("alert_to", "")
    if not text and creds is not None:
        cfg = creds.cfg.get("email_logic_app", {}) or {}
        text = cfg.get("alert_to") or cfg.get("to") or ""
    return split_addresses(text)


def wrap(title: str, lines) -> str:
    items = "".join(f"<li>{html.escape(str(x))}</li>" for x in lines)
    return ("<div style='font-family:Segoe UI,Arial,sans-serif;font-size:14px'>"
            "<div style='background:#b00020;color:#fff;padding:10px 14px;font-weight:600'>"
            f"{html.escape(title)}</div><ul>{items}</ul>"
            "<p style='color:#666;font-size:12px'>Mini PMC alert. Open the dashboard (Events and Resources) for details."
            "</p></div>")


_lock = threading.Lock()  # one alert decision at a time, so two workers cannot both pass the throttle check


def notify(store, creds, key: str, subject: str, lines, throttle: timedelta = THROTTLE, mailer: Mailer = None,
           now=None) -> bool:
    with _lock:
        return _notify(store, creds, key, subject, lines, throttle, mailer, now)


def _notify(store, creds, key: str, subject: str, lines, throttle, mailer, now) -> bool:
    """Send an alert unless the same key was sent within `throttle`. Returns True if an email went out."""
    now = now or datetime.now(timezone.utc)
    lines = list(lines)[:60]
    with db.connect(store.home) as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", ("alert:" + key,)).fetchone()
    if row and now - parse_utc(row[0]) < throttle:
        return False
    to = recipients(store, creds)
    mailer = mailer or Mailer(creds)
    if not to or not mailer.configured:
        log.warning("alert '%s' not sent: no recipient or email Logic App is not configured", key)
        store.log_event("ALERT_NOT_SENT", key, reason="alerting is not configured", subject=subject)
        return False
    result = mailer.send(to, "[Mini PMC ALERT] " + subject, wrap(subject, lines))
    if result["ok"]:
        with db.connect(store.home) as con:  # only a delivered alert starts the quiet period
            con.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", ("alert:" + key, utcnow_iso()))
        store.log_event("ALERT_SENT", key, subject=subject, to=len(to))
        return True
    store.log_event("ALERT_FAILED", key, subject=subject, error=result["detail"])
    return False
