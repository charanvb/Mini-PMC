"""Single SQLite file. One short-lived connection per operation; WAL so readers never block the writer."""
import json
import os
import sqlite3
from contextlib import contextmanager

from .logs import utcnow_iso, write_audit

SCHEMA_VERSION = 6

# v5: last known power state per VM, cloud start/stop operations, and cached subscription names
_STATUS = """CREATE TABLE IF NOT EXISTS vm_status (
        vm_name TEXT PRIMARY KEY COLLATE NOCASE REFERENCES vms(name) ON DELETE CASCADE,
        state TEXT NOT NULL, raw TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL,
        error TEXT NOT NULL DEFAULT '', fails INTEGER NOT NULL DEFAULT 0)"""
_OPS = """CREATE TABLE IF NOT EXISTS ops (
        id INTEGER PRIMARY KEY AUTOINCREMENT, vm_name TEXT NOT NULL COLLATE NOCASE, action TEXT NOT NULL,
        started_at TEXT NOT NULL, sent_at TEXT, finished_at TEXT, outcome TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')"""
_OPS_INDEX = "CREATE INDEX IF NOT EXISTS ops_vm ON ops(vm_name)"
_SUB_NAMES = """CREATE TABLE IF NOT EXISTS sub_names (
        scope TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', fetched_at TEXT NOT NULL)"""

_USERS = """CREATE TABLE users (
        username TEXT PRIMARY KEY COLLATE NOCASE, pw_hash TEXT NOT NULL, role TEXT NOT NULL,
        disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"""


def _two_roles(con) -> None:
    """v4: only 'admin' (everything) and 'reader' (look only) remain; the old operator role becomes admin."""
    con.execute("UPDATE users SET role='admin' WHERE role='operator'")
    con.execute("UPDATE users SET role='reader' WHERE role='viewer'")


def _rename_holds(con) -> None:
    """v3: the earlier 'holds' table is now 'overrides' (and its audit events use the same word)."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='holds'").fetchone():
        con.execute("ALTER TABLE holds RENAME TO overrides")
    con.execute("UPDATE events SET action='OVERRIDE_SET' WHERE action='HOLD_SET'")
    con.execute("UPDATE events SET action='OVERRIDE_CLEARED' WHERE action='HOLD_CLEARED'")


# version N -> steps that bring it to N+1 (SQL text, or a function taking the connection)
def _add_columns(con) -> None:
    """v6: users must change a one-time password at first login; ops can be manual (a person clicked Start/Stop)."""
    def have(table, col):
        return any(r[1] == col for r in con.execute(f"PRAGMA table_info({table})"))
    if not have("users", "must_change"):
        con.execute("ALTER TABLE users ADD COLUMN must_change INTEGER NOT NULL DEFAULT 0")
    if not have("ops", "manual"):
        con.execute("ALTER TABLE ops ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")


_MIGRATIONS = {1: [_USERS], 2: [_rename_holds], 3: [_two_roles], 4: [_STATUS, _OPS, _OPS_INDEX, _SUB_NAMES],
               5: [_add_columns]}

_SCHEMA = [
    _USERS,
    """CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE schedules (
        name TEXT PRIMARY KEY, kind TEXT NOT NULL, tz TEXT NOT NULL, grid TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""",
    """CREATE TABLE vms (
        name TEXT PRIMARY KEY COLLATE NOCASE, provider TEXT NOT NULL, scope TEXT NOT NULL,
        group_or_zone TEXT NOT NULL, schedule TEXT REFERENCES schedules(name),
        enabled INTEGER NOT NULL DEFAULT 1, source_text TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE overrides (
        vm_name TEXT PRIMARY KEY COLLATE NOCASE REFERENCES vms(name) ON DELETE CASCADE,
        expires_at TEXT, reason TEXT NOT NULL DEFAULT '', set_by TEXT NOT NULL, set_at TEXT NOT NULL)""",
    """CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
        action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '')""",
    """CREATE INDEX events_ts ON events(ts)""",
    """CREATE INDEX events_target ON events(target)""",
    _STATUS, _OPS, _OPS_INDEX, _SUB_NAMES,
]


def db_path(home: str) -> str:
    return os.path.join(home, "data", "minipmc.db")


def exists(home: str) -> bool:
    return os.path.exists(db_path(home))


@contextmanager
def connect(home: str):
    path = db_path(home)
    if not os.path.exists(path):
        raise FileNotFoundError(f"database not found: {path} (run: python -m minipmc init)")
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def init(home: str) -> bool:
    """Create the database if missing. Returns True if newly created."""
    path = db_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with connect(home) as con:
            ver = int(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
        if ver > SCHEMA_VERSION:
            raise RuntimeError(f"database schema is v{ver}, this program only knows up to v{SCHEMA_VERSION}")
        start = ver
        while ver < SCHEMA_VERSION:
            with connect(home) as con:
                for step in _MIGRATIONS[ver]:
                    step(con) if callable(step) else con.execute(step)
                ver += 1
                con.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(ver),))
        if start < SCHEMA_VERSION:
            rec = {"ts": utcnow_iso(), "actor": "system", "action": "SCHEMA_UPGRADED", "target": "",
                   "detail": {"from": start, "to": SCHEMA_VERSION}}
            with connect(home) as con:
                con.execute("INSERT INTO events (ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                            (rec["ts"], rec["actor"], rec["action"], "", json.dumps(rec["detail"])))
            write_audit(home, rec)
        return False
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        for stmt in _SCHEMA:
            con.execute(stmt)
        _add_columns(con)
        con.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        con.commit()
    finally:
        con.close()
    return True
