"""All data operations (VMs, schedules, holds, settings, audit). Every change is written to the audit log."""
import csv
import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from . import db, grid as g, importer, reconcile as rc
from .logs import utcnow_iso, write_audit
from .tz import get_tz

log = logging.getLogger("minipmc.service")

LIBRARY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config",
                            "schedule_library.csv")
PROVIDERS = ("azure", "gcp")


class UserError(Exception):
    """A mistake by the person using the tool; shown without a traceback."""


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_utc(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_until(text: str) -> datetime:
    """'2026-12-31' means the end of that day (UTC); a full ISO timestamp is taken as given (UTC if no zone)."""
    text = text.strip()
    try:
        if len(text) == 10:
            d = date.fromisoformat(text)
            return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise UserError(f"cannot read date/time '{text}' (use 2026-12-31 or 2026-12-31T18:00:00Z)")
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class Store:
    def __init__(self, home: str, actor: str = "cli"):
        self.home = home
        self.actor = actor

    # ------------------------------------------------------------ audit
    def audit(self, con, action: str, target: str = "", **detail) -> None:
        rec = {"ts": utcnow_iso(), "actor": self.actor, "action": action, "target": target, "detail": detail}
        con.execute("INSERT INTO events (ts, actor, action, target, detail) VALUES (?,?,?,?,?)",
                    (rec["ts"], self.actor, action, target, json.dumps(detail, ensure_ascii=True)))
        write_audit(self.home, rec)
        log.info("AUDIT %s target=%s %s", action, target, json.dumps(detail, ensure_ascii=True))

    # ------------------------------------------------------------ setup
    def init(self) -> dict:
        created = db.init(self.home)
        if created:
            with db.connect(self.home) as con:
                self.audit(con, "DB_INIT", path=db.db_path(self.home))
        added = self.load_library()
        return {"created": created, "schedules_added": added}

    def load_library(self, path: str = LIBRARY_PATH) -> int:
        """Add the built-in schedules and the shipped library (never overwrites existing ones)."""
        rows = [(importer.ALWAYS_UP, "always_up", "UTC", "R" * g.CELLS, "24x7 running"),
                (importer.ALWAYS_PARKED, "always_parked", "UTC", "S" * g.CELLS, "24x7 stopped (parked)")]
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    rows.append((r["name"], r["kind"], r["tz"], r["grid"], r["description"]))
        added = 0
        with db.connect(self.home) as con:
            for name, kind, tz, grid, desc in rows:
                if len(grid) != g.CELLS:
                    raise UserError(f"library schedule '{name}' has a bad grid length")
                cur = con.execute("INSERT OR IGNORE INTO schedules VALUES (?,?,?,?,?,?)",
                                  (name, kind, tz, grid, desc, utcnow_iso()))
                added += cur.rowcount
            if added:
                self.audit(con, "LIBRARY_LOADED", added=added)
        return added

    def export_library(self, cat: "importer.Catalog", path: str) -> int:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        rows = [s for s in cat.schedules.values() if s.kind == "grid"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["name", "kind", "tz", "grid", "description"])
            for s in sorted(rows, key=lambda s: s.name):
                w.writerow([s.name, s.kind, s.tz, s.grid, s.description])
        return len(rows)

    # ------------------------------------------------------------ settings
    def get_setting(self, key: str, default: str = "") -> str:
        with db.connect(self.home) as con:
            r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_setting(self, key: str, value: str) -> None:
        with db.connect(self.home) as con:
            old = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            con.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
            self.audit(con, "SETTING_CHANGED", key, old=old[0] if old else None, new=value)

    @property
    def paused(self) -> bool:
        return self.get_setting("paused", "0") == "1"

    # ------------------------------------------------------------ schedules
    def list_schedules(self) -> List[sqlite3.Row]:
        with db.connect(self.home) as con:
            return con.execute("""SELECT s.*, (SELECT COUNT(*) FROM vms v WHERE v.schedule=s.name) AS vm_count
                                  FROM schedules s ORDER BY s.name""").fetchall()

    def get_schedule(self, name: str) -> sqlite3.Row:
        with db.connect(self.home) as con:
            r = con.execute("SELECT * FROM schedules WHERE name=?", (name,)).fetchone()
        if not r:
            raise UserError(f"schedule not found: '{name}' (see: python -m minipmc schedule list)")
        return r

    def add_schedule_from_text(self, text: str, name: Optional[str] = None) -> str:
        p = importer.parse_schedule(text)
        if p.kind == "unmanaged":
            raise UserError(f"cannot read that schedule text: {p.reason}")
        if p.kind in ("always_up", "always_parked"):
            return importer.ALWAYS_UP if p.kind == "always_up" else importer.ALWAYS_PARKED
        cat = importer.Catalog()
        auto = cat.schedule_for(p)
        grid, tzname, desc = cat.schedules[auto].grid, p.tz, cat.schedules[auto].description
        name = name or auto
        with db.connect(self.home) as con:
            same = con.execute("SELECT name FROM schedules WHERE tz=? AND grid=?", (tzname, grid)).fetchone()
            if same:
                return same[0]  # identical schedule already exists - reuse it
            if con.execute("SELECT 1 FROM schedules WHERE name=?", (name,)).fetchone():
                raise UserError(f"a different schedule is already called '{name}'")
            con.execute("INSERT INTO schedules VALUES (?,?,?,?,?,?)",
                        (name, "grid", tzname, grid, desc, utcnow_iso()))
            self.audit(con, "SCHEDULE_ADDED", name, tz=tzname, text=text, notes=p.notes)
        return name

    def remove_schedule(self, name: str) -> None:
        if name in (importer.ALWAYS_UP, importer.ALWAYS_PARKED):
            raise UserError("built-in schedules cannot be removed")
        with db.connect(self.home) as con:
            self.get_schedule(name)
            n = con.execute("SELECT COUNT(*) FROM vms WHERE schedule=?", (name,)).fetchone()[0]
            if n:
                raise UserError(f"{n} VM(s) still use '{name}'; move them first")
            con.execute("DELETE FROM schedules WHERE name=?", (name,))
            self.audit(con, "SCHEDULE_REMOVED", name)

    # ------------------------------------------------------------ VMs
    def _vm(self, con, name: str) -> sqlite3.Row:
        r = con.execute("SELECT * FROM vms WHERE name=?", (name,)).fetchone()
        if not r:
            raise UserError(f"VM not found: '{name}' (see: python -m minipmc vm list)")
        return r

    def add_vm(self, name: str, provider: str, scope: str, group_or_zone: str,
               schedule: Optional[str] = None, notes: str = "", source_text: str = "") -> None:
        provider = provider.lower()
        if provider not in PROVIDERS:
            raise UserError(f"provider must be one of {PROVIDERS}")
        if not (name.strip() and scope.strip() and group_or_zone.strip()):
            raise UserError("name, scope and group/zone are all required")
        now = utcnow_iso()
        with db.connect(self.home) as con:
            if schedule:
                self.get_schedule(schedule)
            try:
                con.execute("INSERT INTO vms (name, provider, scope, group_or_zone, schedule, enabled, source_text,"
                            " notes, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?,?,?)",
                            (name.strip(), provider, scope.strip(), group_or_zone.strip(), schedule, source_text,
                             notes, now, now))
            except sqlite3.IntegrityError:
                raise UserError(f"VM already exists: '{name}'")
            self.audit(con, "VM_ADDED", name, provider=provider, scope=scope, group_or_zone=group_or_zone,
                       schedule=schedule)

    def remove_vm(self, name: str) -> None:
        with db.connect(self.home) as con:
            vm = self._vm(con, name)
            con.execute("DELETE FROM vms WHERE name=?", (name,))
            self.audit(con, "VM_REMOVED", vm["name"], schedule=vm["schedule"])

    def set_enabled(self, name: str, enabled: bool) -> None:
        with db.connect(self.home) as con:
            vm = self._vm(con, name)
            con.execute("UPDATE vms SET enabled=?, updated_at=? WHERE name=?", (int(enabled), utcnow_iso(), name))
            self.audit(con, "VM_ENABLED" if enabled else "VM_DISABLED", vm["name"])

    def set_schedule(self, name: str, schedule: Optional[str]) -> None:
        with db.connect(self.home) as con:
            vm = self._vm(con, name)
            if schedule:
                self.get_schedule(schedule)
            con.execute("UPDATE vms SET schedule=?, updated_at=? WHERE name=?", (schedule, utcnow_iso(), name))
            self.audit(con, "VM_SCHEDULE_CHANGED", vm["name"], old=vm["schedule"], new=schedule)

    def list_vms(self, text: str = "", unmanaged: bool = False) -> List[sqlite3.Row]:
        q = """SELECT v.*, h.expires_at AS hold_until, h.set_by AS hold_by, h.reason AS hold_reason,
                      (h.vm_name IS NOT NULL) AS held
               FROM vms v LEFT JOIN holds h ON h.vm_name = v.name WHERE 1=1"""
        args: list = []
        if text:
            q += " AND (v.name LIKE ? OR v.schedule LIKE ?)"
            args += [f"%{text}%"] * 2
        if unmanaged:
            q += " AND v.schedule IS NULL"
        with db.connect(self.home) as con:
            return con.execute(q + " ORDER BY v.name", args).fetchall()

    def get_vm(self, name: str):
        with db.connect(self.home) as con:
            vm = self._vm(con, name)
            hold = con.execute("SELECT * FROM holds WHERE vm_name=?", (name,)).fetchone()
            events = con.execute("SELECT ts, actor, action, detail FROM events WHERE target=? ORDER BY id DESC "
                                 "LIMIT 10", (vm["name"],)).fetchall()
        return vm, hold, events

    # ------------------------------------------------------------ holds
    def set_hold(self, name: str, until: Optional[datetime], reason: str) -> None:
        if until is not None and until <= datetime.now(timezone.utc):
            raise UserError("the hold end time is already in the past")
        with db.connect(self.home) as con:
            vm = self._vm(con, name)
            con.execute("INSERT OR REPLACE INTO holds VALUES (?,?,?,?,?)",
                        (vm["name"], iso(until), reason, self.actor, utcnow_iso()))
            self.audit(con, "HOLD_SET", vm["name"], until=iso(until) or "until-cleared", reason=reason)

    def clear_hold(self, name: str) -> None:
        with db.connect(self.home) as con:
            vm = self._vm(con, name)
            cur = con.execute("DELETE FROM holds WHERE vm_name=?", (name,))
            if not cur.rowcount:
                raise UserError(f"'{name}' has no hold")
            self.audit(con, "HOLD_CLEARED", vm["name"])

    def list_holds(self) -> List[sqlite3.Row]:
        with db.connect(self.home) as con:
            return con.execute("SELECT * FROM holds ORDER BY expires_at IS NULL, expires_at, vm_name").fetchall()

    # ------------------------------------------------------------ what would happen
    def explain(self, name: str, at: Optional[datetime] = None, actual: str = "running") -> dict:
        now = at or datetime.now(timezone.utc)
        vm, hold, _ = self.get_vm(name)
        sched = self.get_schedule(vm["schedule"]) if vm["schedule"] else None
        tz = get_tz(sched["tz"]) if sched else None
        override = rc.Override(parse_utc(hold["expires_at"]), hold["reason"], hold["set_by"]) if hold else None
        d = rc.reconcile_vm(enabled=bool(vm["enabled"]), paused=self.paused,
                            schedule_grid=sched["grid"] if sched else None, tz=tz, actual=actual.upper(),
                            status_time=now, override=override, op_in_progress=False, now=now)
        out = {"vm": vm["name"], "at_utc": iso(now), "assumed_state": actual, "action": d.action,
               "reason": d.reason, "schedule_says": d.desired, "schedule": vm["schedule"]}
        if tz:
            out["local_time"] = now.astimezone(tz).strftime("%a %H:%M (%Z)")
        return out

    # ------------------------------------------------------------ import
    def import_catalog(self, cat: "importer.Catalog", apply: bool) -> dict:
        """Add VMs and schedules from an inventory. Existing VMs are never changed."""
        res = {"vms_new": 0, "vms_existing": 0, "schedules_new": 0, "holds_new": 0, "unmanaged": 0}
        with db.connect(self.home) as con:
            for s in cat.schedules.values():
                if s.kind != "grid" or con.execute("SELECT 1 FROM schedules WHERE name=?", (s.name,)).fetchone():
                    continue
                res["schedules_new"] += 1
                if apply:
                    con.execute("INSERT INTO schedules VALUES (?,?,?,?,?,?)",
                                (s.name, s.kind, s.tz, s.grid, s.description, utcnow_iso()))
            now = utcnow_iso()
            for v in cat.vms:
                if con.execute("SELECT 1 FROM vms WHERE name=?", (v.vm_name,)).fetchone():
                    res["vms_existing"] += 1
                    continue
                res["vms_new"] += 1
                res["unmanaged"] += v.schedule is None
                if v.hold:
                    res["holds_new"] += 1
                if apply:
                    con.execute("INSERT INTO vms (name, provider, scope, group_or_zone, schedule, enabled, source_text,"
                                " notes, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?,?,?)",
                                (v.vm_name, v.provider, v.scope, v.group_or_zone, v.schedule, v.source_text,
                                 "", now, now))
                    if v.hold:
                        con.execute("INSERT INTO holds VALUES (?,?,?,?,?)",
                                    (v.vm_name, iso(v.hold_until), v.hold_reason, v.hold_set_by or self.actor, now))
            if apply:
                self.audit(con, "IMPORT_APPLIED", **res)
            else:
                con.rollback()
        return res

    # ------------------------------------------------------------ validate
    def validate(self, now: Optional[datetime] = None) -> List[tuple]:
        """Returns (level, message) pairs; level is ERROR, WARN or INFO."""
        now = now or datetime.now(timezone.utc)
        out: List[tuple] = []
        with db.connect(self.home) as con:
            for s in con.execute("SELECT name, tz FROM schedules WHERE kind='grid'"):
                try:
                    get_tz(s["tz"])
                except Exception:
                    out.append(("ERROR", f"schedule '{s['name']}': time zone '{s['tz']}' cannot be loaded "
                                         f"(pip install tzdata)"))
            for v in con.execute("SELECT name FROM vms WHERE schedule IS NULL AND enabled=1"):
                out.append(("WARN", f"VM {v['name']} has no schedule (unmanaged - nothing will be done)"))
            for v in con.execute("SELECT name FROM vms WHERE enabled=0"):
                out.append(("INFO", f"VM {v['name']} is disabled"))
            for h in con.execute("SELECT * FROM holds"):
                exp = parse_utc(h["expires_at"])
                if exp is None:
                    out.append(("INFO", f"VM {h['vm_name']}: hold has no end (until cleared)"))
                elif exp <= now:
                    out.append(("INFO", f"VM {h['vm_name']}: hold expired {h['expires_at']} (schedule applies)"))
                elif exp - now < timedelta(days=14):
                    out.append(("WARN", f"VM {h['vm_name']}: hold ends {h['expires_at']} - then the schedule applies"))
            if self.paused:
                out.append(("WARN", "the scheduler is PAUSED (python -m minipmc resume)"))
        order = {"ERROR": 0, "WARN": 1, "INFO": 2}
        return sorted(out, key=lambda x: order[x[0]])

    def export_vms_csv(self, path: str) -> int:
        rows = self.list_vms()
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["name", "provider", "scope", "group_or_zone", "schedule", "enabled", "hold_until", "notes"])
            for r in rows:
                w.writerow([r["name"], r["provider"], r["scope"], r["group_or_zone"], r["schedule"] or "",
                            r["enabled"], (r["hold_until"] or "until-cleared") if r["held"] else "", r["notes"]])
        return len(rows)
