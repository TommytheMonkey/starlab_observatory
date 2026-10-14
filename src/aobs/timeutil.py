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


def fmt_local(value: dt.datetime | None, tz: str = "America/New_York", pattern: str = "%a %Y-%m-%d %H:%M:%S %Z") -> str:
    if value is None:
        return "-"
    return local(value, tz).strftime(pattern)
