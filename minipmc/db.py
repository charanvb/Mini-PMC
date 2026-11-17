"""Single SQLite file. One short-lived connection per operation; WAL so readers never block the writer."""
import os
import sqlite3
from contextlib import contextmanager

SCHEMA_VERSION = 2

_USERS = """CREATE TABLE users (
        username TEXT PRIMARY KEY COLLATE NOCASE, pw_hash TEXT NOT NULL, role TEXT NOT NULL,
        disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"""

# version N -> statements that bring it to N+1
_MIGRATIONS = {1: [_USERS]}

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
    """CREATE TABLE holds (
        vm_name TEXT PRIMARY KEY COLLATE NOCASE REFERENCES vms(name) ON DELETE CASCADE,
        expires_at TEXT, reason TEXT NOT NULL DEFAULT '', set_by TEXT NOT NULL, set_at TEXT NOT NULL)""",
    """CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
        action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '')""",
    """CREATE INDEX events_ts ON events(ts)""",
    """CREATE INDEX events_target ON events(target)""",
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
        while ver < SCHEMA_VERSION:
            with connect(home) as con:
                for stmt in _MIGRATIONS[ver]:
                    con.execute(stmt)
                ver += 1
                con.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(ver),))
        return False
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        for stmt in _SCHEMA:
            con.execute(stmt)
        con.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        con.commit()
    finally:
        con.close()
    return True
