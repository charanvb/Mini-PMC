"""Logging: rotating app/error logs plus a per-day audit file (one JSON object per line). ASCII only."""
import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone

_FMT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"


def setup_logging(home: str, console: bool = False, level: int = logging.INFO) -> None:
    logdir = os.path.join(home, "logs")
    os.makedirs(os.path.join(logdir, "audit"), exist_ok=True)
    root = logging.getLogger("minipmc")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    fmt = logging.Formatter(_FMT)
    for fname, lvl in (("app.log", level), ("error.log", logging.WARNING)):
        h = logging.handlers.RotatingFileHandler(os.path.join(logdir, fname), maxBytes=5_000_000,
                                                 backupCount=10, encoding="utf-8")
        h.setLevel(lvl)
        h.setFormatter(fmt)
        root.addHandler(h)
    if console:
        c = logging.StreamHandler()
        c.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root.addHandler(c)
    root.propagate = False


def write_audit(home: str, record: dict) -> None:
    """Append one audit record to logs/audit/YYYY-MM-DD.jsonl (UTC day)."""
    day = record["ts"][:10]
    path = os.path.join(home, "logs", "audit", f"{day}.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=True) + "\n")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
