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
