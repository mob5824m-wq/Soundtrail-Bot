"""Quiet hours: a daily window in which scheduled posts wait.

A scheduled or repeating announcement that falls due inside the window is
moved to the moment the window ends. Announcements a staff member posts by
hand are not held back, because that person has chosen to post now.

Times are 24-hour ``HH:MM`` in the server's own timezone. A window may cross
midnight, for example ``22:00`` to ``08:00``. Setting only one of the two
times is refused, and so is a window that starts and ends at the same time.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_CLOCK = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_CLOCK_HELP = "Quiet hours use 24-hour times such as 22:00 and 08:00."


def parse_clock(text: str) -> str:
    """``'7:05'`` -> ``'07:05'``. Raises :class:`ValueError` with a message for staff."""
    match = _CLOCK.match(text or "")
    if match is None:
        raise ValueError(_CLOCK_HELP)
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(_CLOCK_HELP)
    return f"{hour:02d}:{minute:02d}"


def _minutes(clock: str) -> int:
    hour, minute = clock.split(":")
    return int(hour) * 60 + int(minute)


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def validate_pair(start: Optional[str], end: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Check the two ends of a window together. Both empty means quiet hours are off."""
    if not start and not end:
        return None, None
    if not start or not end:
        raise ValueError("Set both the start and the end of quiet hours, or clear both.")
    start, end = parse_clock(start), parse_clock(end)
    if start == end:
        raise ValueError("Quiet hours need different start and end times.")
    return start, end


def validate_support_pair(start: Optional[str], end: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Validate server-local opening and closing times; both empty disables support hours."""
    if not start and not end:
        return None, None
    if not start or not end:
        raise ValueError("Set both support-hours times, or clear both.")
    start, end = parse_clock(start), parse_clock(end)
    if start == end:
        raise ValueError("Support hours need different opening and closing times.")
    return start, end


def support_is_open(now: datetime, tz_name: str, start: Optional[str], end: Optional[str]) -> bool:
    """Whether support is currently open in the server's local timezone."""
    if not start or not end:
        return True
    local = now.astimezone(_zone(tz_name))
    minute = local.hour * 60 + local.minute
    opening, closing = _minutes(start), _minutes(end)
    if opening < closing:
        return opening <= minute < closing
    return minute >= opening or minute < closing


def window_end(
    now: datetime, tz_name: str, start: Optional[str], end: Optional[str]
) -> Optional[datetime]:
    """When ``now`` is inside the quiet window, the UTC moment it ends. Otherwise ``None``."""
    if not start or not end or start == end:
        return None
    zone = _zone(tz_name)
    local = now.astimezone(zone)
    moment = local.hour * 60 + local.minute
    s, e = _minutes(start), _minutes(end)
    if s < e:
        if not s <= moment < e:
            return None
        day = local.date()
    elif moment >= s:  # overnight, and we are in the evening part
        day = local.date() + timedelta(days=1)
    elif moment < e:  # overnight, and we are in the morning part
        day = local.date()
    else:
        return None
    ends_local = datetime.combine(day, time(e // 60, e % 60), tzinfo=zone)
    return ends_local.astimezone(timezone.utc)
