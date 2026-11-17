from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import grid as g

# Normalised power states
RUNNING, STOPPED, DEALLOCATED, STARTING, STOPPING, UNKNOWN = (
    "RUNNING", "STOPPED", "DEALLOCATED", "STARTING", "STOPPING", "UNKNOWN")
DOWN_STATES = {STOPPED, DEALLOCATED}
TRANSITIONAL = {STARTING, STOPPING}
KNOWN_STATES = {RUNNING} | DOWN_STATES | TRANSITIONAL

# The scheduler only ever decides one of these three.
START, STOP, SKIP = "START", "STOP", "SKIP"

# Reason codes (also written to the audit log)
SCHEDULED = "SCHEDULED"
GLOBAL_PAUSE = "GLOBAL_PAUSE"
VM_DISABLED = "VM_DISABLED"
UNMANAGED_NO_SCHEDULE = "UNMANAGED_NO_SCHEDULE"
OVERRIDE_ACTIVE = "OVERRIDE_ACTIVE"
SCHEDULE_DO_NOTHING = "SCHEDULE_DO_NOTHING"
STATUS_UNKNOWN_OR_STALE = "STATUS_UNKNOWN_OR_STALE"
OPERATION_IN_PROGRESS = "OPERATION_IN_PROGRESS"
TRANSITIONAL_STATE = "TRANSITIONAL_STATE"
IN_DESIRED_STATE = "IN_DESIRED_STATE"


@dataclass(frozen=True)
class Override:
    """A hold: while it is active the scheduler does nothing for the VM (no start, no stop)."""
    expires_at: Optional[datetime] = None  # UTC; None = until cleared by a person
    reason: str = ""
    set_by: str = ""

    def active(self, now: datetime) -> bool:
        return self.expires_at is None or now < self.expires_at


@dataclass
class Decision:
    action: str  # START | STOP | SKIP
    reason: str
    desired: Optional[str] = None  # R / S / N from the schedule, kept for display even when skipping


def reconcile_vm(*, enabled: bool, paused: bool, schedule_grid, tz, actual: str,
                 status_time: Optional[datetime], override: Optional[Override],
                 op_in_progress: bool, now: datetime,
                 max_status_age: timedelta = timedelta(minutes=15)) -> Decision:
    """Decide what to do for one VM. The check order matters."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if paused:
        return Decision(SKIP, GLOBAL_PAUSE)
    if not enabled:
        return Decision(SKIP, VM_DISABLED)
    if schedule_grid is None:
        return Decision(SKIP, UNMANAGED_NO_SCHEDULE)

    desired = g.desired_at(schedule_grid, tz, now)
    if override is not None and override.active(now):
        return Decision(SKIP, OVERRIDE_ACTIVE, desired)
    if desired == "N":
        return Decision(SKIP, SCHEDULE_DO_NOTHING, desired)
    if actual not in KNOWN_STATES or status_time is None or now - status_time > max_status_age:
        return Decision(SKIP, STATUS_UNKNOWN_OR_STALE, desired)
    if op_in_progress:
        return Decision(SKIP, OPERATION_IN_PROGRESS, desired)
    if actual in TRANSITIONAL:
        return Decision(SKIP, TRANSITIONAL_STATE, desired)

    if desired == "R" and actual in DOWN_STATES:
        return Decision(START, SCHEDULED, desired)
    if desired == "S" and actual == RUNNING:
        return Decision(STOP, SCHEDULED, desired)
    return Decision(SKIP, IN_DESIRED_STATE, desired)
