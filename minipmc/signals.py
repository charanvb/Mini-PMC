"""Block / unblock signals: one HTTP POST to the Logic App, which forwards them to Omnibus.

Contract (matches the Logic App's request schema):
    POST <Logic App URL>   {"vms": [{"name": ..., "project": ..., "zone": ...}], "action": "block" | "unblock"}
For Azure VMs `project` carries the subscription id and `zone` the resource group.

Mini PMC only checks that the Logic App ACCEPTED the call (any 2xx, normally 202). It does not wait for the
workflow to finish and never polls it. The URL contains a secret (sig=...), so it is never written to a log or
shown in the dashboard - only scheme, host and path are.

Where the URL lives (config\\settings.json, section "logic_app"):
    "url_secret_name": name of an Azure Key Vault secret that holds the URL (recommended)
    "url":             the URL itself (quick tests; the file is git-ignored). If set, it wins over the secret.
"""
import json
import logging
import time
import urllib.parse

from . import creds as cr

log = logging.getLogger("minipmc.signals")

ACTIONS = ("block", "unblock")
RETRY_STATUS = {408, 429, 500, 502, 503, 504}  # worth trying again; other 4xx mean the request itself is wrong
BACKOFF_SECONDS = (2, 5, 10)
URL_CACHE_SECONDS = 300


class SignalError(cr.CredentialError):
    pass


def payload(action: str, vms) -> dict:
    """The JSON body for the Logic App. `vms` are rows/dicts with name, scope and group_or_zone."""
    if action not in ACTIONS:
        raise SignalError(f"action must be one of {ACTIONS}")
    if not vms:
        raise SignalError("no VMs to signal")
    return {"vms": [{"name": v["name"], "project": v["scope"], "zone": v["group_or_zone"]} for v in vms],
            "action": action}


def safe_target(url: str) -> str:
    """scheme://host/path - the query string (which holds the secret) is dropped."""
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.hostname or ''}{p.path}"


class LogicApp:
    """One Logic App HTTP trigger. `section` is the settings.json section that holds its URL (and retries/timeout)."""

    def __init__(self, creds: cr.Credentials, sleep=time.sleep, section: str = "logic_app"):
        self.creds, self.section = creds, section
        self.cfg = creds.cfg.get(section, {}) or {}
        self.sleep, self.clock = sleep, creds.clock
        self._url, self._url_expires = "", 0.0

    @property
    def configured(self) -> bool:
        return bool((self.cfg.get("url") or "").strip() or (self.cfg.get("url_secret_name") or "").strip())

    def url(self) -> str:
        if self._url and self.clock() < self._url_expires:
            return self._url
        direct, secret = (self.cfg.get("url") or "").strip(), (self.cfg.get("url_secret_name") or "").strip()
        if not (direct or secret):
            raise SignalError(f"the Logic App URL is not configured: set {self.section}.url_secret_name (Key Vault "
                              f"secret holding the URL) or {self.section}.url in config\\settings.json")
        url = direct or self.creds.keyvault_secret(secret).strip()
        if not url.lower().startswith("https://") or not urllib.parse.urlsplit(url).hostname:
            raise SignalError("the Logic App URL must be the full 'HTTP POST URL' of the trigger, starting with https://")
        self._url, self._url_expires = url, self.clock() + URL_CACHE_SECONDS
        return url

    def send(self, action: str, vms) -> dict:
        """POST one block/unblock signal. Returns {"ok", "status", "attempts", "detail"}; never raises for URL/HTTP/network failures."""
        body = json.dumps(payload(action, vms)).encode("utf-8")
        return self.post(body, f"signal {action} for {len(vms)} VM(s)")

    def post(self, body: bytes, what: str) -> dict:
        """POST a JSON body, retrying transient failures. `what` only labels the log lines."""
        try:
            url = self.url()
        except cr.CredentialError as e:
            return {"ok": False, "status": None, "attempts": 0, "detail": str(e)}
        attempts = max(1, int(self.cfg.get("retries", 3)))
        timeout = float(self.cfg.get("timeout_seconds", 30))
        for n in range(1, attempts + 1):
            status = None
            try:
                status, resp = self.creds.http("POST", url, {"Content-Type": "application/json"}, body, timeout=timeout)
                resp = resp if isinstance(resp, dict) else {}
                detail = f"HTTP {status}"
                if not 200 <= status < 300:
                    err = resp.get("error")
                    msg = err.get("message") if isinstance(err, dict) else (err or resp.get("raw"))
                    detail += ": " + str(msg or "")[:200]
            except (cr.CredentialError, OSError) as e:  # network trouble or a timeout while reading the answer
                detail = f"network error: {e}"
            except Exception as e:  # e.g. a malformed URL; such messages can contain the URL, so keep only the type
                log.error("%s could not be sent: %s", what, type(e).__name__)
                return {"ok": False, "status": None, "attempts": n,
                        "detail": f"the request could not be made ({type(e).__name__}) - check the Logic App URL"}
            ok = status is not None and 200 <= status < 300
            if ok or (status is not None and status not in RETRY_STATUS) or n == attempts:
                log.info("%s to %s: %s (attempt %d)", what, safe_target(url), detail, n)
                return {"ok": ok, "status": status, "attempts": n, "detail": detail}
            log.warning("%s to %s failed (%s), retrying", what, safe_target(url), detail)
            self.sleep(BACKOFF_SECONDS[min(n - 1, len(BACKOFF_SECONDS) - 1)])
        raise AssertionError("unreachable")  # the loop always returns


def send_signal(store, action: str, vms, *, app: LogicApp = None, reason: str = "") -> dict:
    """Send a block/unblock signal for `vms` and write one audit event per VM (SIGNAL_SENT or SIGNAL_FAILED)."""
    app = app or LogicApp(cr.Credentials(cr.load_settings(store.home)))
    result = app.send(action, vms)
    for v in vms:
        detail = {"signal_action": action, "status": result["status"], "attempts": result["attempts"],
                  "reason": reason}
        if result["ok"]:
            store.log_event("SIGNAL_SENT", v["name"], **detail)
        else:
            store.log_event("SIGNAL_FAILED", v["name"], error=result["detail"], **detail)
    return result
