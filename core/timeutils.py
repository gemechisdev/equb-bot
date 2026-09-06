"""Frequency parsing and draw-time helpers. Pure functions — no Telegram,
DB or bot imports — so they're trivially unit-testable.

All datetimes are handled in UTC; `format_draw_time` converts to the
configured display timezone (default: Africa/Addis_Ababa) only at render
time.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import core.config as config

# Fixed-length intervals keep every announced draw time exact and
# predictable: "monthly" means every 30 days, not a calendar month.
FREQUENCY_DAYS = {"weekly": 7, "biweekly": 14, "monthly": 30}

_N_DAYS_RE = re.compile(r"^(\d+)d$")


def parse_frequency_interval(raw: str) -> int:
    """'weekly' -> 7, 'biweekly' -> 14, 'monthly' -> 30, '3d' -> 3.

    Raises ValueError for anything else (including 0d / negative)."""
    token = (raw or "").strip().lower()
    if token in FREQUENCY_DAYS:
        return FREQUENCY_DAYS[token]
    match = _N_DAYS_RE.match(token)
    if match:
        days = int(match.group(1))
        if days >= 1:
            return days
    raise ValueError(f"invalid frequency: {raw!r}")


def next_draw_at(from_dt: datetime, interval_days: int) -> datetime:
    """The draw time for a round that starts at `from_dt`."""
    return from_dt + timedelta(days=interval_days)


@lru_cache(maxsize=8)
def _display_tz(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def display_tz():
    return _display_tz(config.TIMEZONE)


def format_draw_time(dt_utc: datetime) -> str:
    """Render a UTC datetime in the display timezone, e.g.
    'Fri, 12 Sep 2026, 06:00 PM (EAT)'."""
    local = dt_utc.astimezone(display_tz())
    stamp = local.strftime("%a, %d %b %Y, %I:%M %p")
    zone_label = local.tzname() or f"UTC{local.strftime('%z')}"
    return f"{stamp} ({zone_label})"


def format_countdown(dt_utc: datetime, now: datetime | None = None) -> str:
    """Relative time until `dt_utc`, e.g. 'in 2d 5h' / 'in 3h 20m'.
    Returns 'now' once the moment has passed."""
    now = now or datetime.now(timezone.utc)
    total = int((dt_utc - now).total_seconds())
    if total <= 0:
        return "now"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {minutes}m"
    return "in <1m" if minutes == 0 else f"in {minutes}m"
