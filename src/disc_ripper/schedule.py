"""The Raspberry Pi's weekly power schedule, and what it means for the shutdown guard.

The schedule is a 7-entry JSON array, Monday to Sunday, in the Pi's local time
(the container's TZ must match). Rules follow schedule_util.is_scheduled_online
on the Pi: a window runs from start to end; with endNextDay the end falls on the
next calendar day, so an overnight window ends under yesterday's entry.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

Phase = Literal["no_schedule", "in_window", "draining", "off_hours"]


@dataclass(frozen=True)
class DayEntry:
    day: str
    start: str
    end: str
    end_next_day: bool


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class GuardState:
    phase: Phase
    scheduled_end: datetime | None
    grace_ends_at: datetime | None
    window_start: datetime | None = None

    @property
    def holds_shutdown(self) -> bool:
        """Whether a running job may still delay shutdown."""
        return self.phase != "off_hours"


def parse_schedule(data: object) -> list[DayEntry] | None:
    """Validate the Pi's schedule array. None if anything about it is wrong."""
    if not isinstance(data, list) or len(data) != len(DAYS):
        return None
    entries = []
    for expected, item in zip(DAYS, data):
        if not isinstance(item, dict) or item.get("day") != expected:
            return None
        start, end = item.get("start"), item.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            return None
        if (start == "") != (end == ""):
            return None
        if start and not (_HHMM.match(start) and _HHMM.match(end)):
            return None
        flag = item.get("endNextDay", False)
        if not isinstance(flag, bool):
            return None
        entries.append(DayEntry(expected, start, end, flag))
    return entries


def local_zone() -> tzinfo:
    """The container's time zone (TZ), which must match the Pi's clock."""
    name = os.environ.get("TZ", "").lstrip(":")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo


def _zone(now: datetime) -> tzinfo:
    """The zone schedule times are in. A fixed offset (datetime.now().astimezone())
    doesn't know about clock changes, so prefer the container's TZ."""
    if isinstance(now.tzinfo, ZoneInfo):
        return now.tzinfo
    if os.environ.get("TZ"):
        return local_zone()
    return now.tzinfo


def _at(day: date, hhmm: str, zone: tzinfo) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    # fold=0: a time repeated when the clocks go back means its first occurrence;
    # a time skipped when they go forward lands an hour later.
    return datetime.combine(day, time(hour, minute), tzinfo=zone).astimezone(timezone.utc)


def windows_near(entries: list[DayEntry], now: datetime) -> list[Window]:
    """Windows (in UTC) from the entries for today and the two days before, by start."""
    zone = _zone(now)
    today = now.astimezone(zone).date()
    windows = []
    for back in (2, 1, 0):
        day = today - timedelta(days=back)
        entry = entries[day.weekday()]
        if not entry.start:
            continue
        end_day = day + timedelta(days=1) if entry.end_next_day else day
        windows.append(Window(_at(day, entry.start, zone), _at(end_day, entry.end, zone)))
    return sorted(windows, key=lambda w: w.start)


def guard_state(entries: list[DayEntry] | None, now: datetime, grace: timedelta) -> GuardState:
    if entries is None:
        return GuardState("no_schedule", None, None)
    windows = windows_near(entries, now)
    # Compare and add grace in UTC: wall-clock arithmetic is off by an hour
    # across a clock change.
    now = now.astimezone(timezone.utc)
    for window in windows:
        if window.start <= now < window.end:
            return GuardState("in_window", window.end, window.end + grace, window.start)
    ended = [w.end for w in windows if w.end <= now]
    if not ended:
        return GuardState("off_hours", None, None)
    end = max(ended)
    phase: Phase = "draining" if now < end + grace else "off_hours"
    return GuardState(phase, end, end + grace)
