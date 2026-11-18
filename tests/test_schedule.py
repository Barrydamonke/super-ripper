from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from disc_ripper import schedule

LONDON = ZoneInfo("Europe/London")
GRACE = timedelta(hours=3)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """2026-11-16 is a Monday; day 16..22 = Monday..Sunday, 23 = next Monday."""
    return datetime(2026, 11, day, hour, minute, tzinfo=LONDON)


def week(**days) -> list[schedule.DayEntry]:
    raw = []
    for name in schedule.DAYS:
        start, end, *flag = days.get(name, ("", ""))
        raw.append({"day": name, "start": start, "end": end, "endNextDay": bool(flag and flag[0])})
    parsed = schedule.parse_schedule(raw)
    assert parsed is not None
    return parsed


def test_parse_schedule_accepts_example():
    raw = [{"day": d, "start": "", "end": ""} for d in schedule.DAYS]
    raw[0] = {"day": "monday", "start": "18:00", "end": "23:00", "endNextDay": False}
    raw[4] = {"day": "friday", "start": "18:00", "end": "02:00", "endNextDay": True}
    parsed = schedule.parse_schedule(raw)
    assert parsed[0] == schedule.DayEntry("monday", "18:00", "23:00", False)
    assert parsed[1] == schedule.DayEntry("tuesday", "", "", False)
    assert parsed[4].end_next_day is True


def _valid():
    return [{"day": d, "start": "", "end": "", "endNextDay": False} for d in schedule.DAYS]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s[:6],
        lambda s: [s[1], s[0], *s[2:]],
        lambda s: [{**s[0], "start": "25:00", "end": "23:00"}, *s[1:]],
        lambda s: [{**s[0], "start": "9:00", "end": "23:00"}, *s[1:]],
        lambda s: [{**s[0], "start": "18:00"}, *s[1:]],
        lambda s: [{**s[0], "endNextDay": "yes"}, *s[1:]],
        lambda s: [{**s[0], "day": "Monday"}, *s[1:]],
        lambda s: "not a list",
        lambda s: None,
        lambda s: [*s[:6], "sunday"],
    ],
)
def test_parse_schedule_rejects(mutate):
    assert schedule.parse_schedule(mutate(_valid())) is None


def test_in_window():
    s = week(monday=("18:00", "23:00"))
    g = schedule.guard_state(s, at(16, 20), GRACE)
    assert g.phase == "in_window"
    assert g.scheduled_end == at(16, 23)
    assert g.grace_ends_at == at(17, 2)
    assert g.holds_shutdown


def test_draining_then_off_hours():
    s = week(monday=("18:00", "23:00"))
    g = schedule.guard_state(s, at(16, 23, 30), GRACE)
    assert g.phase == "draining"
    assert g.grace_ends_at == at(17, 2)
    g = schedule.guard_state(s, at(17, 2), GRACE)
    assert g.phase == "off_hours"
    assert g.scheduled_end == at(16, 23)
    assert not g.holds_shutdown


def test_overnight_sunday_into_monday():
    s = week(sunday=("22:00", "02:00", True))
    g = schedule.guard_state(s, at(23, 1), GRACE)
    assert g.phase == "in_window"
    assert g.scheduled_end == at(23, 2)


def test_overnight_under_yesterday_then_draining():
    s = week(friday=("18:00", "02:00", True))
    g = schedule.guard_state(s, at(21, 3), GRACE)
    assert g.phase == "draining"
    assert g.scheduled_end == at(21, 2)
    assert g.grace_ends_at == at(21, 5)


def test_blank_day_off_hours():
    g = schedule.guard_state(week(), at(18, 12), GRACE)
    assert g.phase == "off_hours"
    assert g.scheduled_end is None
    assert g.grace_ends_at is None


def test_no_schedule():
    g = schedule.guard_state(None, at(18, 12), GRACE)
    assert g.phase == "no_schedule"
    assert g.holds_shutdown


def test_start_equals_now_and_end_exclusive():
    s = week(monday=("18:00", "23:00"))
    assert schedule.guard_state(s, at(16, 18), GRACE).phase == "in_window"
    assert schedule.guard_state(s, at(16, 23), GRACE).phase == "draining"


def test_latest_window_wins():
    # Monday's window ended long ago; Tuesday's is current.
    s = week(monday=("08:00", "09:00"), tuesday=("18:00", "23:00"))
    g = schedule.guard_state(s, at(17, 19), GRACE)
    assert g.phase == "in_window"
    assert g.scheduled_end == at(17, 23)
