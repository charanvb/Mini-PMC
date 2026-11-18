"""Email through a Logic App ("Send an email (V2)" behind an HTTP trigger).

Body sent: {"Message": html, "Subject": text, "ToEmail": "a@x;b@y", "Attachments": [{"Name", "ContentBytes"}]}.
The Logic App sends one email per attachment, so callers pass at most one. `Attachments` is left out when there is
none: the Logic App tests it for null, and an empty list would send nothing.
"""
import base64
import json
import re

from . import creds as cr
from .signals import LogicApp

SECTION = "email_logic_app"
_ADDRESS = re.compile(r"^[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+$")


def split_addresses(text) -> list:
    """'a@x.com; b@y.com, c@z.com' -> list, de-duplicated, order kept."""
    parts = re.split(r"[;,\s]+", text or "")
    return list(dict.fromkeys(p.strip() for p in parts if p.strip()))


def bad_addresses(text) -> list:
    return [a for a in split_addresses(text) if not _ADDRESS.match(a)]


class Mailer:
    def __init__(self, creds: cr.Credentials, app: LogicApp = None):
        self.app = app or LogicApp(creds, section=SECTION)

    @property
    def configured(self) -> bool:
        return self.app.configured

    def send(self, to, subject: str, html: str, attachments=None) -> dict:
        """attachments: [(file name, bytes)]. Returns {"ok", "status", "attempts", "detail"}; never raises."""
        recipients = split_addresses(to) if isinstance(to, str) else list(to)
        if not recipients:
            return {"ok": False, "status": None, "attempts": 0, "detail": "no recipient is set"}
        body = {"Message": html, "Subject": subject, "ToEmail": ";".join(recipients)}
        if attachments:
            body["Attachments"] = [{"Name": n, "ContentBytes": base64.b64encode(b).decode("ascii")}
                                   for n, b in attachments]
        return self.app.post(json.dumps(body).encode("utf-8"), f"email '{subject[:40]}'")
