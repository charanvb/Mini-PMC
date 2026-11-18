"""The part that acts: compares what each schedule wants with the live state and starts or stops VMs.

Flow, driven by scheduler.py:
  tick()  every 5 min   read every VM's power state, close finished/stuck operations, and - once per 15-minute slot -
                        decide who must start or stop and put those VMs in the queue (table `ops`, outcome QUEUED).
  pump()  about 1/min   take the next wave of queued operations, re-check each VM's live state, send the block/unblock
                        signals in the background (never waited for), then call the cloud to start/stop.
The cloud accepts a start/stop quickly and finishes later, so an accepted operation is PENDING until a later status
read shows the wanted state (SUCCEEDED), or after `stuck_minutes` it becomes STUCK and an alert goes out.

Modes (setting `mode`): readonly = read only; dryrun = only write what would be done to the audit log;
pilot = act only on the VMs listed in setting `pilot_vms`; live = act on everything.
"""
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import alerts, creds as cr, db, providers, reconcile as rc, signals, status
from .logs import utcnow_iso
from .service import UserError, iso, parse_utc
from .tz import get_tz

log = logging.getLogger("minipmc.executor")

MODES = ("readonly", "dryrun", "pilot", "live")
OPEN = ("QUEUED", "PENDING")
RETRY_HTTP = {408, 429, 500, 502, 503, 504}
DEFAULTS = {"wave_size": 100, "wave_seconds": 60, "workers": 10, "stuck_minutes": 30, "abandon_minutes": 90,
            "max_actions_per_slot": 2500, "retries": 3}
SLOT_MINUTES = 15


def slot_key(now: datetime) -> str:
    now = now.astimezone(timezone.utc)
    return now.replace(minute=now.minute - now.minute % SLOT_MINUTES, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%MZ")


def pilot_vms(store) -> set:
    return {x.strip().lower() for x in store.get_setting("pilot_vms", "").replace(";", ",").replace("\n", ",").split(",")
            if x.strip()}


def check_manual(store, vm_name: str, action: str, guard: set = frozenset()) -> None:
    """Raise UserError when a person may not start/stop this VM right now (nothing is changed)."""
    if action not in ("start", "stop"):
        raise UserError("action must be start or stop")
    mode = store.get_setting("mode", "readonly")
    if mode not in ("pilot", "live"):
        raise UserError(f"the scheduler is in {mode} mode; start/stop buttons work in Pilot or Live mode only")
    if mode == "pilot" and vm_name.lower() not in pilot_vms(store):
        raise UserError("pilot mode: this VM is not in the pilot list")
    if action == "stop" and vm_name.lower() in guard:
        raise UserError("this is the machine running Mini PMC; it is never stopped from here")
    with db.connect(store.home) as con:
        if con.execute("SELECT 1 FROM ops WHERE vm_name=? AND outcome IN ('QUEUED','PENDING')", (vm_name,)).fetchone():
            raise UserError("an action is already in progress for this VM")


def queue_manual(store, vm_name: str, action: str, guard: set = frozenset()) -> None:
    """A person pressed Start/Stop. Queued like any action (sent within seconds, signals included) but the
    schedule is not consulted again; the caller sets an override first when the schedule would undo it."""
    check_manual(store, vm_name, action, guard)
    with db.connect(store.home) as con:
        con.execute("INSERT INTO ops (vm_name, action, started_at, outcome, detail, manual) VALUES (?,?,?,'QUEUED','',1)",
                    (vm_name, action, utcnow_iso()))
    store.log_event("MANUAL_" + action.upper(), vm_name)


class Engine:
    def __init__(self, store, creds_factory=None, sleep=time.sleep, background=True, clock=None):
        self.store, self.sleep, self.background = store, sleep, background
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.new_creds = creds_factory or (lambda: cr.Credentials(cr.load_settings(store.home)))
        self._last_wave, self._ws = None, DEFAULTS["wave_seconds"]
        self._lock = threading.Lock()  # tick and pump both change ops; keep their decisions apart

    # ------------------------------------------------------------ helpers
    def cfg(self, creds) -> dict:
        got = creds.cfg.get("executor", {}) or {}
        return {k: type(v)(got.get(k, v)) for k, v in DEFAULTS.items()}

    def mode(self) -> str:
        m = self.store.get_setting("mode", "readonly")
        return m if m in MODES else "readonly"

    def guarded(self, creds) -> set:
        """VMs that must never be stopped: the machine this app runs on."""
        names = {socket.gethostname().lower()}
        host = (creds.cfg.get("host_vm") or "").strip().lower()
        return names | ({host} if host else set())

    def _open_ops(self) -> dict:
        with db.connect(self.store.home) as con:
            return {r["vm_name"]: dict(r) for r in con.execute("SELECT * FROM ops WHERE outcome IN ('QUEUED','PENDING')")}

    def _close(self, op_id: int, outcome: str, detail: str = "") -> None:
        with db.connect(self.store.home) as con:
            con.execute("UPDATE ops SET outcome=?, detail=?, finished_at=? WHERE id=?", (outcome, detail[:300], utcnow_iso(), op_id))

    def _decide(self, vm, sched, state, checked, override, open_op, now, paused) -> rc.Decision:
        try:
            tz = get_tz(sched[1]) if sched else None
        except ValueError:  # the time zone cannot be loaded
            return rc.Decision(rc.SKIP, "TZ_UNAVAILABLE")
        return rc.reconcile_vm(
            enabled=bool(vm["enabled"]), paused=paused, schedule_grid=sched[0] if sched else None,
            tz=tz, actual=state, status_time=checked, override=override,
            op_in_progress=open_op, now=now)

    def _context(self):
        scheds = {s["name"]: (s["grid"], s["tz"]) for s in self.store.list_schedules()}
        return scheds, self.store.paused

    # ------------------------------------------------------------ tick
    def tick(self, now: datetime = None) -> dict:
        """One 5-minute pass. Returns a small summary (used by `python -m minipmc cycle` and tests)."""
        now = now or self.clock()
        creds = self.new_creds()
        with self._lock:
            res = status.refresh(self.store, creds)
            self.finalize(creds, now)
            planned = self.plan(creds, now)
        self.alert_unreadable(creds)
        status.beat(self.store, "tick")
        return {"mode": self.mode(), "read": res["done"], "unreadable": len(res["errors"]), **planned}

    def finalize(self, creds, now: datetime) -> None:
        cfg = self.cfg(creds)
        cur = status.all_status(self.store)
        stuck = []
        with db.connect(self.store.home) as con:
            ops = [dict(r) for r in con.execute("SELECT * FROM ops WHERE outcome IN ('QUEUED','PENDING')")]
        for op in ops:
            started = parse_utc(op["started_at"])
            age = now - started
            if op["outcome"] == "QUEUED":
                if age > timedelta(minutes=cfg["abandon_minutes"]):
                    self._close(op["id"], "FAILED", "never sent (the queue did not reach it in time)")
                    self.store.log_event("ACTION_FAILED", op["vm_name"], op=op["action"], error="abandoned in the queue")
                continue
            st = cur.get(op["vm_name"].lower())
            fresh = st and parse_utc(st["checked_at"]) >= parse_utc(op.get("sent_at") or op["started_at"])
            want = {rc.RUNNING} if op["action"] == "start" else rc.DOWN_STATES
            if fresh and st["state"] in want:
                self._close(op["id"], "SUCCEEDED", st["raw"])
                self.store.log_event("ACTION_CONFIRMED", op["vm_name"], op=op["action"], state=st["state"],
                                     minutes=round(age.total_seconds() / 60))
            elif age > timedelta(minutes=cfg["stuck_minutes"]):
                why = (f"still {st['state']}" if st and st["state"] else "state unknown") + \
                      (f" ({st['error']})" if st and st["error"] else "")
                self._close(op["id"], "STUCK", f"no effect after {cfg['stuck_minutes']} min; {why}")
                self.store.log_event("ACTION_STUCK", op["vm_name"], op=op["action"], detail=why)
                stuck.append(f"{op['vm_name']}: {op['action']} requested {op['started_at']}, {why}")
        if stuck:
            alerts.notify(self.store, creds, "stuck", f"{len(stuck)} VM action(s) did not take effect", stuck)

    def plan(self, creds, now: datetime) -> dict:
        mode = self.mode()
        if mode == "readonly":
            return {"planned": 0, "slot": None}
        key = slot_key(now)
        if self.store.get_setting("last_slot", "") == key:
            return {"planned": 0, "slot": key}
        cfg = self.cfg(creds)
        scheds, paused = self._context()
        cur, busy, guard = status.all_status(self.store), self._open_ops(), self.guarded(creds)
        pilot = pilot_vms(self.store) if mode == "pilot" else None
        acts, reasons = [], {}
        for vm in self.store.list_vms():
            st = cur.get(vm["name"].lower())
            override = None
            if vm["overridden"]:
                override = rc.Override(parse_utc(vm["override_until"]), vm["override_reason"] or "", vm["override_by"] or "")
            sched = scheds.get(vm["schedule"])
            try:
                d = self._decide(vm, sched, st["state"] if st else "", parse_utc(st["checked_at"]) if st else None,
                                 override, vm["name"] in busy, now, paused)
            except Exception as e:  # one broken schedule must not stop the whole slot
                log.error("could not decide for %s: %s", vm["name"], type(e).__name__)
                d = rc.Decision(rc.SKIP, "DECISION_ERROR")
            reasons[d.reason] = reasons.get(d.reason, 0) + 1
            if d.action == rc.SKIP:
                continue
            if d.action == rc.STOP and vm["name"].lower() in guard:
                self.store.log_event("HOST_GUARD", vm["name"], reason="this is the machine running Mini PMC; not stopped")
                continue
            if pilot is not None and vm["name"].lower() not in pilot:
                reasons["NOT_IN_PILOT"] = reasons.get("NOT_IN_PILOT", 0) + 1
                continue
            acts.append((vm, d.action.lower()))
        self.store.set_setting_quiet("last_slot", key)
        summary = {"slot": key, "planned": len(acts), "reasons": reasons}
        if len(acts) > cfg["max_actions_per_slot"]:
            self.store.log_event("ACTION_CAP_EXCEEDED", "", planned=len(acts), cap=cfg["max_actions_per_slot"])
            alerts.notify(self.store, creds, "cap", "Too many VM actions planned - nothing was sent",
                          [f"{len(acts)} actions planned in one slot, limit is {cfg['max_actions_per_slot']}.",
                           "Check the schedules or the time zone, then raise executor.max_actions_per_slot if intended."])
            return {**summary, "planned": 0, "capped": True}
        if mode == "dryrun":
            for vm, action in acts[:200]:
                self.store.log_event("DRYRUN_" + action.upper(), vm["name"], slot=key)
            self.store.log_event("DRYRUN_SUMMARY", "", **summary)
            return summary
        stamp = utcnow_iso()
        with db.connect(self.store.home) as con:
            con.executemany("INSERT INTO ops (vm_name, action, started_at, outcome, detail) VALUES (?,?,?,'QUEUED','')",
                            [(vm["name"], action, stamp) for vm, action in acts])
        if acts:
            self.store.log_event("ACTIONS_QUEUED", "", **summary)
        return summary

    def alert_unreadable(self, creds) -> None:
        with db.connect(self.store.home) as con:
            bad = [f"{r['vm_name']}: {r['error']}" for r in con.execute(
                "SELECT vm_name, error FROM vm_status WHERE fails >= 3 ORDER BY vm_name")]
        if bad:
            alerts.notify(self.store, creds, "unreadable", f"{len(bad)} VM(s) could not be read for 15+ minutes", bad)

    # ------------------------------------------------------------ pump
    def pump(self, now: datetime = None, force: bool = False) -> int:
        """Send one wave of queued operations. Returns how many were sent to the cloud."""
        now = now or self.clock()
        if not force and self._last_wave and (now - self._last_wave).total_seconds() < self._ws:
            with db.connect(self.store.home) as con:  # a person is waiting: do not hold a button press for a minute
                if not con.execute("SELECT 1 FROM ops WHERE outcome='QUEUED' AND manual=1").fetchone():
                    return 0
        with self._lock:
            return self._wave(now)

    def _wave(self, now: datetime) -> int:
        creds = self.new_creds()
        cfg = self.cfg(creds)
        self._ws = cfg["wave_seconds"]
        with db.connect(self.store.home) as con:
            queued = [dict(r) for r in con.execute("SELECT * FROM ops WHERE outcome='QUEUED' ORDER BY manual DESC, id LIMIT ?",
                                                   (cfg["wave_size"],))]
        if not queued:
            return 0
        self._last_wave = now
        mode = self.mode()
        by_name = {v["name"]: v for v in self.store.list_vms()}
        scheds, paused = self._context()
        pilot = pilot_vms(self.store) if mode == "pilot" else None
        guard = self.guarded(creds)

        def recheck(op):
            """(vm row, error) - read the live state again and make sure the action is still wanted."""
            vm = by_name.get(op["vm_name"])
            if vm is None:
                return None, "VM removed from the list"
            if mode not in ("pilot", "live"):
                return None, f"mode is now {mode}"
            if pilot is not None and vm["name"].lower() not in pilot:
                return None, "VM is not in the pilot list"
            if op["action"] == "stop" and vm["name"].lower() in guard:
                return None, "host guard"
            state, raw, err = status.read_one(creds, vm)
            if err:
                return None, "could not read the live state: " + err
            if op.get("manual"):  # a person asked: only skip if it is already in the wanted state
                if (op["action"] == "start" and state == rc.RUNNING) or (op["action"] == "stop" and state in rc.DOWN_STATES):
                    return None, f"no longer needed (VM is already {state})"
                return vm, ""
            override = None
            if vm["overridden"]:
                override = rc.Override(parse_utc(vm["override_until"]), vm["override_reason"] or "", vm["override_by"] or "")
            d = self._decide(vm, scheds.get(vm["schedule"]), state, now, override, False, now, paused)
            if d.action.lower() != op["action"]:
                return None, f"no longer needed ({d.reason}, VM is {state})"
            return vm, ""

        workers = cfg["workers"]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            checked = list(pool.map(recheck, queued))
            go = []
            for op, (vm, why) in zip(queued, checked):
                if vm is None:
                    outcome = "FAILED" if why.startswith("could not read") else "SKIPPED"
                    self._close(op["id"], outcome, why)
                    self.store.log_event("ACTION_FAILED" if outcome == "FAILED" else "ACTION_SKIPPED", op["vm_name"],
                                         op=op["action"], reason=why)
                else:
                    go.append((op, vm))
            self._signals(creds, [(vm, op["action"]) for op, vm in go])
            results = list(pool.map(lambda ov: self._call(creds, ov[1], ov[0]["action"], cfg["retries"]), go))

        failed = []
        for (op, vm), (ok, detail) in zip(go, results):
            if ok:
                with db.connect(self.store.home) as con:
                    con.execute("UPDATE ops SET outcome='PENDING', detail=?, sent_at=? WHERE id=?",
                                (detail, utcnow_iso(), op["id"]))
                self.store.log_event("START_REQUESTED" if op["action"] == "start" else "STOP_REQUESTED", vm["name"],
                                     detail=detail)
            else:
                self._close(op["id"], "FAILED", detail)
                self.store.log_event("ACTION_FAILED", vm["name"], op=op["action"], error=detail)
                failed.append(f"{vm['name']}: {op['action']} failed - {detail}")
        if failed:
            alerts.notify(self.store, creds, "action-failed", f"{len(failed)} VM action(s) failed", failed)
        status.beat(self.store, "pump")
        return sum(1 for ok, _ in results if ok)

    def _call(self, creds, vm, action: str, retries: int):
        for n in range(1, max(1, retries) + 1):
            try:
                return True, providers.vm_action(creds, vm["provider"], vm["scope"], vm["group_or_zone"], vm["name"], action)
            except providers.ProviderError as e:
                if e.status in RETRY_HTTP and n < retries:
                    self.sleep(min(2 ** n, 20))
                    continue
                return False, str(e)
            except cr.CredentialError as e:
                return False, str(e)
            except Exception as e:  # keep only the type; messages can hold URLs
                return False, f"{type(e).__name__} while calling the cloud"
        return False, "gave up"

    def _signals(self, creds, items) -> None:
        """Block before every stop, unblock before every start. Fire and forget: the cloud call does not wait for it."""
        groups = {"block": [vm for vm, a in items if a == "stop"], "unblock": [vm for vm, a in items if a == "start"]}
        for action, vms in groups.items():
            if not vms:
                continue
            job = threading.Thread(target=self._send_signal, args=(creds, action, vms), daemon=True,
                                   name="signal-" + action)
            job.start() if self.background else job.run()

    def _send_signal(self, creds, action, vms) -> None:
        try:
            result = signals.send_signal(self.store, action, vms, app=signals.LogicApp(creds), reason="scheduled")
            if not result["ok"]:
                alerts.notify(self.store, creds, "signal-failed", f"{action} signal could not be sent",
                              [f"{len(vms)} VM(s), e.g. {vms[0]['name']}: {result['detail']}",
                               "The VMs were still started/stopped as planned."])
        except Exception as e:  # never let a signal problem reach the action path
            log.error("signal %s crashed: %s", action, type(e).__name__)
