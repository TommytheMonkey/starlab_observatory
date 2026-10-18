"""UTC helpers. Everything internal is timezone-aware UTC."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc


def utc_now() -> dt.datetime:
    return dt.datetime.now(tz=UTC)


def ensure_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_utc(value: str) -> dt.datetime:
    return ensure_utc(dt.datetime.fromisoformat(value.replace("Z", "+00:00")))


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def local(value: dt.datetime, tz: str = "America/New_York") -> dt.datetime:
    return ensure_utc(value).astimezone(ZoneInfo(tz))


# Times are shown on a 12-hour clock in the station's display zone (US Eastern unless configured).
DISPLAY_TZ = "America/New_York"
CLOCK = "%-I:%M %p"
CLOCK_S = "%-I:%M:%S %p"


def fmt_local(value: dt.datetime | None, tz: str = DISPLAY_TZ, pattern: str = f"%a %Y-%m-%d {CLOCK_S} %Z") -> str:
    if value is None:
        return "-"
    return local(value, tz).strftime(pattern)


def fmt_clock(value: dt.datetime | str | None, tz: str | None = None, seconds: bool = False, zone: bool = True,
          day: bool = False) -> str:
    """A user-facing time such as "2:05 PM EDT" (or "Sat Oct 17 2:05 PM EDT" with day=True)."""
    if value is None or value == "":
        return "-"
    if isinstance(value, str):
        value = parse_utc(value)
    pattern = ("%a %b %d " if day else "") + (CLOCK_S if seconds else CLOCK) + (" %Z" if zone else "")
    return local(value, tz or DISPLAY_TZ).strftime(pattern)
