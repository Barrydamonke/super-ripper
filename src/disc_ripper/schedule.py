"""The Raspberry Pi's weekly power schedule, and what it means for the shutdown guard.

The schedule is a 7-entry JSON array, Monday to Sunday, in the Pi's local time
(the container's TZ must match). Rules follow schedule_util.is_scheduled_online
on the Pi: a window runs from start to end; with endNextDay the end falls on the
next calendar day, so an overnight window ends under yesterday's entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

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


def _at(day: date, hhmm: str, now: datetime) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo)


def windows_near(entries: list[DayEntry], now: datetime) -> list[Window]:
    """Windows from the entries for today and the two days before, by start."""
    windows = []
    for back in (2, 1, 0):
        day = now.date() - timedelta(days=back)
        entry = entries[day.weekday()]
        if not entry.start:
            continue
        end_day = day + timedelta(days=1) if entry.end_next_day else day
        windows.append(Window(_at(day, entry.start, now), _at(end_day, entry.end, now)))
    return sorted(windows, key=lambda w: w.start)


def guard_state(entries: list[DayEntry] | None, now: datetime, grace: timedelta) -> GuardState:
    if entries is None:
        return GuardState("no_schedule", None, None)
    windows = windows_near(entries, now)
    for window in windows:
        if window.start <= now < window.end:
            return GuardState("in_window", window.end, window.end + grace)
    ended = [w.end for w in windows if w.end <= now]
    if not ended:
        return GuardState("off_hours", None, None)
    end = max(ended)
    phase: Phase = "draining" if now < end + grace else "off_hours"
    return GuardState(phase, end, end + grace)
