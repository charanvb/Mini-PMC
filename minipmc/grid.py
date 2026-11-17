"""7x24 schedule grid with 15-minute cells. Row-major: Monday 00:00 is index 0.

Cell values: R = running, S = stopped, N = do nothing.
A grid is stored as a 672-character string.
"""
from datetime import datetime, timezone
from typing import List, Tuple

CELL_MIN = 15
CELLS_PER_DAY = 96
CELLS = 7 * CELLS_PER_DAY
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = [d.capitalize() for d in DAYS]


def new_grid(fill: str = "S") -> list:
    return [fill] * CELLS


def to_string(grid) -> str:
    return "".join(grid)


def from_string(s: str) -> list:
    if len(s) != CELLS or set(s) - set("RSN"):
        raise ValueError("Invalid grid string")
    return list(s)


def paint(grid, day: int, start_min: int, end_min: int, value: str = "R"):
    """Paint [start_min, end_min) counted from `day` 00:00 (0=Mon).

    The start is rounded down and the end rounded up to whole 15-minute cells.
    The range may run past midnight and wraps from Sunday into Monday.
    """
    if end_min <= start_min:
        raise ValueError("end must be after start")
    first = (day * 1440 + start_min) // CELL_MIN
    last = -(-(day * 1440 + end_min) // CELL_MIN)
    for c in range(first, min(last, first + CELLS)):
        grid[c % CELLS] = value


def cell_index(local: datetime) -> int:
    return local.weekday() * CELLS_PER_DAY + (local.hour * 60 + local.minute) // CELL_MIN


def desired_at(grid, tz, now_utc: datetime) -> str:
    """Desired cell value (R/S/N) at the given instant, read in the schedule's zone."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return grid[cell_index(now_utc.astimezone(tz))]


def hours(grid) -> dict:
    return {v: grid.count(v) / 4 for v in "RSN"}


def runs(grid, value: str = "R") -> List[Tuple[int, int]]:
    """Maximal runs of `value` as (start_cell, length_in_cells), sorted by start.

    Runs wrap from Sunday into Monday; a grid that is all `value` is one (0, CELLS) run.
    """
    n = len(grid)
    if value not in grid:
        return []
    if all(c == value for c in grid):
        return [(0, n)]
    # begin the scan right after a cell that is not `value`, so no run straddles the origin
    origin = next(i for i, c in enumerate(grid) if c != value)
    out, i = [], 1
    while i <= n:
        if grid[(origin + i) % n] == value:
            start, length = (origin + i) % n, 0
            while i <= n and grid[(origin + i) % n] == value:
                length += 1
                i += 1
            out.append((start, length))
        else:
            i += 1
    return sorted(out)


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def run_label(start_cell: int, length: int) -> str:
    """One run as text, e.g. 'Mon 11:00-16:00' or 'Sat 20:30>Sun 02:30' (midnight shows as 24:00)."""
    start = start_cell * CELL_MIN
    day, minute = divmod(start, 1440)
    end_day, end_min = divmod(start + length * CELL_MIN, 1440)
    if end_min == 0:
        end_day, end_min = end_day - 1, 1440
    if end_day == day:
        return f"{DAY_LABELS[day]} {_hhmm(minute)}-{_hhmm(end_min)}"
    return f"{DAY_LABELS[day]} {_hhmm(minute)}>{DAY_LABELS[end_day % 7]} {_hhmm(end_min)}"


def days_label(days) -> str:
    """Compact day list: Mon-Fri, Sun-Thu, Sat-Thu, Mon,Wed; all seven days = Daily."""
    ds = set(days)
    if len(ds) == 7:
        return "Daily"
    parts = []
    for d in sorted(ds):
        if (d - 1) % 7 in ds:
            continue
        n = 1
        while (d + n) % 7 in ds:
            n += 1
        if n >= 3:
            parts.append(f"{DAY_LABELS[d]}-{DAY_LABELS[(d + n - 1) % 7]}")
        else:
            parts.extend(DAY_LABELS[(d + i) % 7] for i in range(n))
    return ",".join(parts)


def _window_label(minute: int, length: int) -> str:
    end = minute + length
    if end <= 1440:
        return f"{_hhmm(minute)}-{_hhmm(end)}"
    return f"{_hhmm(minute)}-{_hhmm(end - 1440)}+1"


def _pattern(rs):
    """Label for a list of runs when they follow a simple pattern, else None."""
    if not rs:
        return None
    if all(s % CELLS_PER_DAY == 0 and n % CELLS_PER_DAY == 0 for s, n in rs):
        covered = {(s // CELLS_PER_DAY + i) % 7 for s, n in rs for i in range(n // CELLS_PER_DAY)}
        return f"{days_label(covered)} 24h"
    shapes = {(s % CELLS_PER_DAY, n) for s, n in rs}
    if len(shapes) == 1:
        (cell_in_day, n), = shapes
        if n <= CELLS_PER_DAY:
            days = {s // CELLS_PER_DAY for s, _ in rs}
            return f"{days_label(days)} {_window_label(cell_in_day * CELL_MIN, n * CELL_MIN)}"
    if len(rs) == 1:
        return run_label(*rs[0])
    return None


def describe(grid) -> str:
    """Short human description of the running windows, in the schedule's own time zone."""
    up = runs(grid, "R")
    if not up:
        return "never up"
    if up == [(0, CELLS)]:
        return "24x7"
    down = runs(grid, "S") if "N" not in grid else []
    if len(up) == 1 and len(down) == 1 and down[0][1] < CELLS_PER_DAY:
        return f"24x7 except {run_label(*down[0])}"  # up all week bar one short outage
    text = _pattern(up)
    if text:
        return text
    down_text = _pattern(down)
    if down_text:
        return f"24x7 except {down_text}"
    return "; ".join(run_label(s, n) for s, n in up)
