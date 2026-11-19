import re
from datetime import timedelta, timezone, tzinfo

# Used only when the tz database is missing (e.g. a locked-down dev laptop).
# Non-DST zones only; DST zones require real tz data (install `tzdata` on the server).
_FIXED_MINUTES = {
    "UTC": 0, "Asia/Kolkata": 330, "Asia/Dhaka": 360, "Asia/Jakarta": 420,
    "Asia/Karachi": 300, "Asia/Manila": 480, "Asia/Singapore": 480,
    "Asia/Yangon": 390, "Europe/Istanbul": 180, "Africa/Cairo": 120,
    "Africa/Algiers": 60, "Africa/Casablanca": 60, "America/Sao_Paulo": -180,
}
_ETC_GMT = re.compile(r"^Etc/GMT([+-])(\d{1,2})$")

# The one zone the dashboard shows times in. A fixed offset (India has no DST), so it needs no tz database.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def _have_tzdata() -> bool:
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Europe/London")
        return True
    except Exception:
        return False


def get_tz(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        m = _ETC_GMT.match(name)
        if m:  # Etc/GMT+3 means UTC-3 (inverted sign)
            hours = int(m.group(2)) * (-1 if m.group(1) == "+" else 1)
            return timezone(timedelta(hours=hours), name)
        if name in _FIXED_MINUTES:
            return timezone(timedelta(minutes=_FIXED_MINUTES[name]), name)
        if _have_tzdata():
            raise ValueError(f"Unknown time zone '{name}'. Use a name such as Europe/London, Asia/Kolkata or UTC.")
        raise ValueError(f"Time zone data unavailable for '{name}'. Install tzdata.")


def gmt_label(name: str, now=None) -> str:
    """'GMT+5:30' for Asia/Kolkata, 'GMT+1' for London in summer, 'GMT' at zero. Empty if the zone is unknown."""
    from datetime import datetime
    try:
        offset = (now or datetime.now(timezone.utc)).astimezone(get_tz(name)).utcoffset()
    except ValueError:
        return ""
    minutes = int(offset.total_seconds() // 60)
    if minutes == 0:
        return "GMT"
    sign, minutes = ("+" if minutes > 0 else "-"), abs(minutes)
    return f"GMT{sign}{minutes // 60}" + (f":{minutes % 60:02d}" if minutes % 60 else "")
