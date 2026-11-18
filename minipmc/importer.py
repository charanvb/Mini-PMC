"""Turn the free-text schedule strings of vms_all.csv into real 7x24 (15-minute) schedules.

Everything the parser understands becomes a grid. Anything else comes back as
kind='unmanaged' with a reason, so those VMs stay untouched and are highlighted.
"""
import csv
import hashlib
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from . import grid as g

ALWAYS_UP = "ALWAYS_UP"
ALWAYS_PARKED = "ALWAYS_PARKED"
MAINTENANCE_MINUTES = 600  # the 10-hour maintenance window: 00:00-10:00 local
MAX_NAME = 44

# word-boundary tokens -> IANA zone. Order matters (specific first).
TZ_TOKENS = [
    ("asia/dhaka", "Asia/Dhaka"), ("dhaka", "Asia/Dhaka"),
    ("europe/london", "Europe/London"), ("asia/jakarta", "Asia/Jakarta"),
    ("asia/manila", "Asia/Manila"), ("africa/casablanca", "Africa/Casablanca"),
    ("casablanca", "Africa/Casablanca"), ("algiers", "Africa/Algiers"),
    ("brazil/east", "America/Sao_Paulo"), ("brazil", "America/Sao_Paulo"),
    ("brt", "America/Sao_Paulo"), ("cst6cdt", "America/Chicago"),
    ("turkey", "Europe/Istanbul"), ("israel", "Asia/Jerusalem"),
    ("il", "Asia/Jerusalem"), ("egypt", "Africa/Cairo"),
    ("portugal", "Europe/Lisbon"), ("wet", "Europe/Lisbon"),
    ("jkt", "Asia/Jakarta"), ("pkt", "Asia/Karachi"), ("mmt", "Asia/Yangon"),
    ("aest", "Australia/Sydney"), ("est", "America/New_York"),
    ("pst", "America/Los_Angeles"), ("cet", "Europe/Paris"),
    ("eet", "Europe/Athens"), ("ist", "Asia/Kolkata"),
    ("bst", "Europe/London"), ("gmt", "UTC"), ("utc", "UTC"),
]
_TZ_RES = [(tok, zone, re.compile(r"(?<![a-z])" + re.escape(tok) + r"(?![a-z])"))
           for tok, zone in TZ_TOKENS]
TZ_LABELS = {"Asia/Kolkata": "IST", "UTC": "UTC", "Europe/London": "UK"}

OFFSET_RE = re.compile(r"(?:gmt|utc)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", re.I)
PAREN_OFFSET_RE = re.compile(r"\(\s*[+-]\d{1,2}(?::?\d{2})?\s*\)")
ZONE_WORDS_RE = re.compile(r"\b(?:time|asia|africa|europe|america|australia)\b")

DAY_WORDS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
ALLDAY_RE = re.compile(r"\b(\d+)\s*[*x]\s*(\d+)\b|\b24\s*hours?\b")
TOKEN_RE = re.compile(
    r"(?P<day>(?<![a-z])(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*)"
    r"|(?<![\d:.])(?P<h>\d{1,2})(?:[:.](?P<m>\d{2})(?::\d{2})?)?\s*(?P<ap>am|pm)?(?![a-z\d])"
    r"|(?P<sep>\bto\b|-)"
    r"|(?P<down>\bdown\b)"
)
FILLER_WORDS = {"to", "and", "down", "day", "days", "up", "from", "until", "till", "only", "hrs"}


@dataclass
class Parsed:
    kind: str  # always_up | always_parked | grid | unmanaged
    tz: str = "UTC"
    tz_token: str = ""
    grid: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    check: bool = False  # the reading was a judgement call; a person should confirm it
    reason: str = ""  # why it is unmanaged


class _Reject(Exception):
    pass


@dataclass
class _Window:
    day: int  # day the window starts on (0=Mon)
    start: int  # minutes from that day's 00:00
    end: int  # minutes from that day's 00:00; may run past midnight
    value: str  # R or S


@dataclass
class _Piece:
    days: List[int] = field(default_factory=list)
    from_range: bool = False  # days came from a single "Mon-Fri" style range
    times: List[int] = field(default_factory=list)
    cont: Optional[Tuple[int, int, int, int]] = None  # day1, minute1, day2, minute2
    allday: bool = False
    typo: bool = False
    down: bool = False
    leftovers: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def has_time(self) -> bool:
        return bool(self.times) or self.cont is not None or self.allday


def _normalise(text: str) -> str:
    t = text.lower()
    for ch in "\u2013\u2014\u2212":
        t = t.replace(ch, "-")
    return re.sub(r"\s+", " ", t.replace("|", " ")).strip()


def detect_tz(t: str) -> Optional[Tuple[str, str]]:
    """(IANA zone, token as written) or None. An explicit UTC/GMT offset wins over a name."""
    m = OFFSET_RE.search(t)
    if m:
        sign, hrs, mins = m.group(1), int(m.group(2)), m.group(3)
        if mins and int(mins):
            raise ValueError("UTC offset with minutes is not supported")
        token = f"UTC{sign}{hrs}"
        if hrs == 0:
            return "UTC", token
        return f"Etc/GMT{'-' if sign == '+' else '+'}{hrs}", token  # Etc/GMT signs are inverted
    for tok, zone, rx in _TZ_RES:
        if rx.search(t):
            return zone, tok.upper()
    return None


def _strip_tz(t: str) -> str:
    t = PAREN_OFFSET_RE.sub(" ", OFFSET_RE.sub(" ", t))
    for _, _, rx in _TZ_RES:
        t = rx.sub(" ", t)
    return ZONE_WORDS_RE.sub(" ", t)


def tz_label(zone: str) -> str:
    if zone in TZ_LABELS:
        return TZ_LABELS[zone]
    m = re.match(r"Etc/GMT([+-])(\d+)$", zone)
    if m:
        return f"GMT{'-' if m.group(1) == '+' else '+'}{m.group(2)}"
    return zone.split("/")[-1].replace("_", " ")


def _minutes(h: int, m: int, ap: Optional[str]) -> Optional[int]:
    if m > 59 or h > 24 or (h == 24 and m):
        return None
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return h * 60 + m


def _day_list(a: int, b: int) -> List[int]:
    out, i = [a], a
    while i != b:
        i = (i + 1) % 7
        out.append(i)
    return out


def _analyse(text: str) -> _Piece:
    """Pull days, times and flags out of one clause."""
    p = _Piece()

    def whole_day(m):
        if m.group(1) is None:  # "24 hours"
            p.allday = True
            return " "
        a, b = int(m.group(1)), int(m.group(2))
        if a in (24, 25) or b == 24:  # 24*5, 24X5, 1*24 ... (25*5 is a typo for 24*5)
            p.allday = True
            p.typo = p.typo or a == 25
            return " "
        return m.group(0)

    text = ALLDAY_RE.sub(whole_day, text)

    tokens = []
    for m in TOKEN_RE.finditer(text):
        if m.group("day"):
            idx = DAY_WORDS.get(m.group("day"))
            if idx is None:
                p.error = f"unrecognised day word '{m.group('day')}'"
                return p
            tokens.append(("day", idx))
        elif m.group("h") is not None:
            if m.group("m") is None and m.group("ap") is None:
                continue  # a bare number is not a time
            minute = _minutes(int(m.group("h")), int(m.group("m") or 0), m.group("ap"))
            if minute is None:
                p.error = f"invalid time '{m.group(0).strip()}'"
                return p
            tokens.append(("time", minute))
        elif m.group("sep"):
            tokens.append(("sep", 0))
        else:
            p.down = True

    items, i = [], 0  # ("days", [..], is_range) | ("time", minute)
    while i < len(tokens):
        kind, val = tokens[i]
        if kind == "day":
            if i + 2 < len(tokens) and tokens[i + 1][0] == "sep" and tokens[i + 2][0] == "day":
                items.append(("days", _day_list(val, tokens[i + 2][1]), True))
                i += 3
                continue
            items.append(("days", [val], False))
        elif kind == "time":
            items.append(("time", val))
        i += 1

    if "".join("D" if it[0] == "days" else "T" for it in items) == "DTDT":
        (_, d1, r1), (_, t1), (_, d2, r2), (_, t2) = items
        if r1 or r2:
            p.error = "unsupported day/time combination"
        else:
            p.cont = (d1[0], t1, d2[0], t2)  # e.g. "Sat 8:30 PM to Sun 2:30 AM"
    else:
        day_items = [it for it in items if it[0] == "days"]
        for it in day_items:
            p.days.extend(d for d in it[1] if d not in p.days)
        p.from_range = len(day_items) == 1 and day_items[0][2]
        p.times = [it[1] for it in items if it[0] == "time"]
        if len(p.times) not in (0, 2):
            p.error = f"expected a start and an end time, found {len(p.times)} times"
        elif p.times and p.allday:
            p.error = "both a whole-day marker and times"

    rest = TOKEN_RE.sub(" ", text)
    p.leftovers = [w for w in re.findall(r"[a-z]+", rest) if w not in FILLER_WORDS]
    return p


def _pieces(text: str) -> List[_Piece]:
    """Split on '&' / 'and'. A part that has days but no times ("Tue&Wed") joins the next part."""
    parts = [s.strip() for s in re.split(r"&|\band\b", text) if s.strip()]
    out, carry = [], ""
    for i, part in enumerate(parts):
        part = (carry + " " + part).strip()
        piece = _analyse(part)
        if piece.days and not piece.has_time and not piece.error and i < len(parts) - 1:
            carry = part
            continue
        carry = ""
        out.append(piece)
    return out


def _default_days(n_days: Optional[int], notes: List[str]) -> Tuple[List[int], bool]:
    if n_days is None:
        notes.append("no days given; assumed every day")
        return list(range(7)), True
    if n_days >= 7:
        return list(range(7)), False
    days = list(range(n_days))
    notes.append(f"no days named; assumed {g.days_label(days)} ({n_days} days)")
    return days, True


def _check_day_count(windows: List[_Window], n_days: int, notes: List[str]) -> bool:
    """With several Up windows, 'N days' should equal the days they start on (or touch)."""
    up = [w for w in windows if w.value == "R"]
    starts = {w.day for w in up}
    touched = {(w.day + k) % 7 for w in up for k in range((w.end - 1) // 1440 + 1)}
    if n_days in (len(starts), len(touched)):
        return False
    notes.append(f"'{n_days} days' stated but the windows start on {len(starts)} days "
                 f"({g.days_label(starts)}); the windows as written were used")
    return True


def _to_windows(pieces, n_days, notes, whole_day_ok, n_rule) -> Tuple[List[_Window], bool]:
    windows, check, prev_days = [], False, None
    single_up = n_rule and sum(1 for p in pieces if not p.down) == 1
    for p in pieces:
        if p.error:
            raise _Reject(p.error)
        if p.leftovers:
            notes.append("ignored words: " + " ".join(p.leftovers))
            check = True
        value = "S" if p.down else "R"

        if p.cont:
            d1, t1, d2, t2 = p.cont
            start, end = d1 * 1440 + t1, d2 * 1440 + t2
            if end <= start:
                end += 7 * 1440
            windows.append(_Window(d1, t1, end - d1 * 1440, value))
            prev_days = [d1]
            continue

        days = list(p.days)
        if not days:
            if prev_days:
                days = list(prev_days)
                notes.append("days not repeated; taken from the previous window")
            else:
                days, flagged = _default_days(n_days, notes)
                check = check or flagged
        elif single_up and n_days and not p.down and len(days) != n_days:
            overnight = len(p.times) == 2 and p.times[1] <= p.times[0]
            if len(days) == n_days + 1 and overnight and p.from_range:
                notes.append(f"'{n_days} days' but {len(days)} named: the last day "
                             f"({g.DAY_LABELS[days[-1]]}) only holds the overnight spill, "
                             f"so windows start on {g.days_label(days[:-1])}")
                days = days[:-1]
            else:
                notes.append(f"'{n_days} days' but {len(days)} days named; the named days were used")
            check = True

        if p.allday or (not p.has_time and whole_day_ok):
            if p.typo:
                notes.append("'25*N' read as 24*N (typo)")
                check = True
            windows.extend(_Window(d, 0, 1440, value) for d in days)
        elif not p.has_time:
            raise _Reject("no start/stop times found")
        else:
            s, e = p.times
            if s == e:
                raise _Reject("start time equals stop time")
            if e <= s:
                e += 1440  # overnight: ends the next day
            windows.extend(_Window(d, s, e, value) for d in days)
        prev_days = days
    if n_rule and n_days and not single_up:
        check = _check_day_count(windows, n_days, notes) or check
    return windows, check


def _parse_weekly(t: str, zone: str, token: str) -> Parsed:
    body = re.sub(r"[(),]", " ", _strip_tz(t))
    body = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", body)
    counts = {int(x) for x in re.findall(r"\b(\d+)\s*days?\b", body)}
    n_days = counts.pop() if len(counts) == 1 else None  # two different "Up N days" parts: each names its own days
    body = re.sub(r"\b\d+\s*days?\b|\bup\b", " ", body)

    m = re.search(r"\bexcept\b", body)
    base_txt, exc_txt = (body[:m.start()], body[m.end():]) if m else (body, None)

    notes: List[str] = []
    try:
        if exc_txt is None:
            windows, check = _to_windows(_pieces(base_txt), n_days, notes, False, True)
        else:
            # "Up 7 days (Mon-Sun) except Down Sat&Sun 10 AM to 10 PM": whole-day base, then the exception.
            windows, check = _to_windows(_pieces(base_txt) or [_Piece()], n_days, notes, True, False)
            exc = _pieces(exc_txt)
            if any(p.down for p in exc):
                for p in exc:
                    p.down = True
            more, flagged = _to_windows(exc, n_days, notes, False, False)
            windows += more
            check = check or flagged
    except _Reject as e:
        return Parsed("unmanaged", zone, token, reason=str(e))

    gr = g.new_grid("S")
    for w in windows:
        if w.value == "R":
            g.paint(gr, w.day, w.start, w.end, "R")
    up_before = gr.count("R")
    for w in windows:
        if w.value == "S":
            g.paint(gr, w.day, w.start, w.end, "S")
    if any(w.value == "S" for w in windows) and gr.count("R") == up_before:
        notes.append("the 'Down' part is already stopped by the Up windows; kept as written")
        check = True

    if "R" not in gr:
        return Parsed("unmanaged", zone, token, reason="schedule never runs")
    if "S" not in gr:
        return Parsed("always_up", grid=g.to_string(gr), notes=notes + ["covers the whole week"])
    return Parsed("grid", zone, token, g.to_string(gr), notes, check)


def parse_schedule(text: str) -> Parsed:
    t = _normalise(text)
    if "always up" in t:
        return Parsed("always_up", grid="R" * g.CELLS)
    if "always parked" in t and "maintenance" not in t:
        return Parsed("always_parked", grid="S" * g.CELLS)
    try:
        hit = detect_tz(t)
    except ValueError as e:
        return Parsed("unmanaged", reason=str(e))

    if "always parked" in t:  # parked except a weekly maintenance window
        if hit is None:
            return Parsed("unmanaged", reason="maintenance schedule without a time zone")
        day = 3 if "thursday" in t else 6
        gr = g.new_grid("S")
        g.paint(gr, day, 0, MAINTENANCE_MINUTES, "R")
        note = ("maintenance window Thu 00:00-10:00" if day == 3 else
                "maintenance window Sun 00:00-10:00 (no day stated, Sunday assumed)")
        return Parsed("grid", hit[0], hit[1], g.to_string(gr), [note])

    if hit is None:
        return Parsed("unmanaged", reason="no time zone found")
    return _parse_weekly(t, hit[0], hit[1])


# ---------------------------------------------------------------- catalog

@dataclass
class Conversion:
    """One distinct source text and what it became."""
    text: str
    parsed: Parsed
    schedule: Optional[str]
    vms: int = 0


@dataclass
class Schedule:
    name: str
    kind: str
    tz: str
    grid: str
    description: str
    vms: int = 0

    @property
    def up_hours(self) -> float:
        return g.hours(g.from_string(self.grid))["R"]


@dataclass
class VmRow:
    vm_name: str
    provider: str
    scope: str  # Azure subscription id / GCP project
    group_or_zone: str  # Azure resource group / GCP zone
    source_text: str
    schedule: Optional[str]  # None = unmanaged
    override: bool = False
    override_until: Optional[datetime] = None
    override_reason: str = ""
    override_set_by: str = ""
    schedule_forced: bool = False


@dataclass
class OverrideRow:
    vm_name: str
    override_until: Optional[datetime]
    reason: str
    force_schedule: Optional[str]
    set_by: str = "initial-import"


class Catalog:
    def __init__(self):
        self.schedules: "OrderedDict[str, Schedule]" = OrderedDict()
        self.conversions: "OrderedDict[str, Conversion]" = OrderedDict()
        self.vms: List[VmRow] = []
        self.tz_usage: "OrderedDict[Tuple[str, str], dict]" = OrderedDict()
        self.override_errors: List[str] = []
        self.duplicate_names: List[str] = []
        self._by_key: Dict[Tuple[str, str], str] = {}
        self.schedules[ALWAYS_UP] = Schedule(ALWAYS_UP, "always_up", "UTC", "R" * g.CELLS, "24x7 running")
        self.schedules[ALWAYS_PARKED] = Schedule(ALWAYS_PARKED, "always_parked", "UTC", "S" * g.CELLS,
                                                 "24x7 stopped (parked)")

    def schedule_for(self, p: Parsed) -> str:
        if p.kind == "always_up":
            return ALWAYS_UP
        if p.kind == "always_parked":
            return ALWAYS_PARKED
        key = (p.tz, p.grid)
        if key in self._by_key:
            return self._by_key[key]
        label, desc = tz_label(p.tz), g.describe(g.from_string(p.grid))
        digest = hashlib.md5((p.tz + p.grid).encode()).hexdigest()[:5]
        name = f"{label} {desc}"
        if len(name) > MAX_NAME:
            name = f"{label} Custom {digest}"
        if name in self.schedules:
            name = f"{name} #{digest}"
        self.schedules[name] = Schedule(name, "grid", p.tz, p.grid, desc)
        self._by_key[key] = name
        return name

    def finalize(self):
        self.tz_usage.clear()
        counts = Counter(v.schedule for v in self.vms if v.schedule)
        for name, s in self.schedules.items():
            s.vms = counts.get(name, 0)
        for conv in self.conversions.values():
            conv.vms = sum(1 for v in self.vms if v.source_text == conv.text)
        for v in self.vms:
            p = self.conversions[v.source_text].parsed
            if p.kind == "grid" and p.tz_token:
                e = self.tz_usage.setdefault((p.tz_token, p.tz), {"vms": 0, "example": v.source_text})
                e["vms"] += 1

    @property
    def unmanaged(self) -> List[VmRow]:
        return [v for v in self.vms if v.schedule is None]


def build_catalog(csv_path: str) -> Catalog:
    cat = Catalog()
    seen: Dict[str, int] = Counter()
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            text = re.sub(r"\s+", " ", row["Schedule"]).strip()
            conv = cat.conversions.get(text)
            if conv is None:
                p = parse_schedule(text)
                conv = Conversion(text, p, None if p.kind == "unmanaged" else cat.schedule_for(p))
                cat.conversions[text] = conv
            name = row["VMName"].strip()
            seen[name.lower()] += 1
            cat.vms.append(VmRow(
                vm_name=name, provider=row["Provider"].strip().lower(),
                scope=row["ProjectOrSubscription"].strip(), group_or_zone=row["ZoneOrResourceGroup"].strip(),
                source_text=text, schedule=conv.schedule))
    cat.duplicate_names = sorted(n for n, c in seen.items() if c > 1)
    cat.finalize()
    return cat


# ---------------------------------------------------------------- overrides

def _parse_utc(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def load_overrides(path: str) -> List[OverrideRow]:
    """Rows: vm_name, override_until_utc (blank = until cleared), reason, force_schedule (optional)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    if reader.fieldnames and "override_until_utc" not in reader.fieldnames:
        raise ValueError(f"{path}: the header must be vm_name,override_until_utc,reason,force_schedule")
    out = []
    for row in reader:
        name = (row.get("vm_name") or "").strip()
        if name:
            out.append(OverrideRow(name, _parse_utc(row.get("override_until_utc")),
                                   (row.get("reason") or "").strip(), (row.get("force_schedule") or "").strip() or None))
    return out


def apply_overrides(cat: Catalog, rows: List[OverrideRow]) -> List[str]:
    """Mark VMs as overridden (and optionally force a schedule). Returns problems found."""
    by_name: Dict[str, List[VmRow]] = {}
    for v in cat.vms:
        by_name.setdefault(v.vm_name.lower(), []).append(v)
    errors = []
    for r in rows:
        vms = by_name.get(r.vm_name.lower())
        if not vms:
            errors.append(f"override VM not found in inventory: {r.vm_name}")
            continue
        if r.force_schedule and r.force_schedule not in cat.schedules:
            errors.append(f"{r.vm_name}: unknown schedule '{r.force_schedule}'")
            continue
        for v in vms:
            v.override, v.override_until, v.override_reason, v.override_set_by = True, r.override_until, r.reason, r.set_by
            if r.force_schedule:
                v.schedule, v.schedule_forced = r.force_schedule, True
    cat.override_errors = errors
    cat.finalize()
    return errors
