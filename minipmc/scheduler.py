"""Background loops that keep the app working without anyone looking at it. Started by `serve`.

  tick thread   every 5 minutes (on the clock: :00, :05, ...) -> Engine.tick()  (read status, plan, close operations)
  pump thread   every 10 seconds                              -> Engine.pump()  (sends a wave when its minute is up)
  report thread every minute                                  -> the daily report email, if enabled
  watchdog      every minute: a tick that runs past 25 minutes is hung -> alert, then exit so the Scheduled Task
                restarts the program.

Each loop catches and logs its own errors and keeps going. `GET /healthz` is 200 only while the tick heartbeat is fresh.
"""
import logging
import os
import threading
import time
from datetime import timedelta

from . import alerts, creds as cr, status
from .executor import Engine

log = logging.getLogger("minipmc.scheduler")

TICK_SECONDS = 300
PUMP_SECONDS = 10
HUNG_SECONDS = 25 * 60
STALE_SECONDS = 12 * 60  # no tick for this long = the scheduler is not running


def healthy(store, now=None) -> bool:
    age = status.beat_age(store, "tick", now)
    return age is not None and age < STALE_SECONDS


class Scheduler:
    def __init__(self, store, engine: Engine = None, exit_fn=os._exit):
        self.store = store
        self.engine = engine or Engine(store)
        self.exit_fn = exit_fn
        self.stop = threading.Event()
        self.tick_started = None  # monotonic time while a tick is running
        self.threads = []

    def start(self) -> None:
        self._startup_check()
        for name, fn in (("tick", self._tick_loop), ("pump", self._pump_loop), ("report", self._report_loop),
                         ("watchdog", self._watchdog_loop)):
            t = threading.Thread(target=fn, name="minipmc-" + name, daemon=True)
            t.start()
            self.threads.append(t)

    def _startup_check(self) -> None:
        """If the last tick is long ago the app was down; say so (the catch-up happens by itself on the first tick)."""
        age = status.beat_age(self.store, "tick")
        if age is not None and age > STALE_SECONDS:
            minutes = int(age // 60)
            log.warning("scheduler was not running for about %d minutes", minutes)
            self.store.log_event("SCHEDULER_RESTARTED", "", down_minutes=minutes)
            try:
                alerts.notify(self.store, cr.Credentials(cr.load_settings(self.store.home)), "restarted",
                              "Mini PMC restarted after downtime",
                              [f"No scheduler activity for about {minutes} minutes before this start.",
                               "It now catches up: the current schedule state is applied on the first cycle."],
                              throttle=timedelta(minutes=30))
            except Exception as e:
                log.error("restart alert failed: %s", type(e).__name__)

    def _sleep_until_tick(self) -> None:
        now = time.time()
        self.stop.wait(TICK_SECONDS - now % TICK_SECONDS + 1)

    def _tick_loop(self) -> None:
        while not self.stop.is_set():
            self.tick_started = time.monotonic()
            try:
                log.info("cycle: %s", self.engine.tick())
            except Exception as e:
                log.exception("cycle failed")
                try:
                    alerts.notify(self.store, cr.Credentials(cr.load_settings(self.store.home)), "cycle-error",
                                  "Mini PMC cycle failed", [f"{type(e).__name__}: {str(e)[:200]}"])
                except Exception:
                    pass
            finally:
                self.tick_started = None
            self._sleep_until_tick()

    def _pump_loop(self) -> None:
        while not self.stop.wait(PUMP_SECONDS):
            try:
                self.engine.pump()
            except Exception:
                log.exception("sending queued actions failed")

    def _report_loop(self) -> None:
        from . import reports
        while not self.stop.wait(60):
            try:
                reports.send_if_due(self.store, cr.Credentials(cr.load_settings(self.store.home)))
            except Exception:
                log.exception("daily report failed")

    def _watchdog_loop(self) -> None:
        while not self.stop.wait(60):
            started = self.tick_started
            if started is not None and time.monotonic() - started > HUNG_SECONDS:
                log.error("cycle has been running for over %d minutes; restarting", HUNG_SECONDS // 60)
                try:
                    alerts.notify(self.store, cr.Credentials(cr.load_settings(self.store.home)), "hung",
                                  "Mini PMC cycle is hung - restarting",
                                  [f"A cycle ran for over {HUNG_SECONDS // 60} minutes. The program is being restarted."],
                                  throttle=timedelta(minutes=30))
                except Exception:
                    pass
                self.exit_fn(3)
