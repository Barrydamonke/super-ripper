import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from disc_ripper import db, schedule, schedule_source, settings

STATUS = "http://backend:3001/status"
PI = "http://pi:8090/schedule"


def raw_week(monday=("18:00", "23:00")):
    raw = [{"day": d, "start": "", "end": "", "endNextDay": False} for d in schedule.DAYS]
    raw[0] = {"day": "monday", "start": monday[0], "end": monday[1], "endNextDay": False}
    return raw


def fake_fetch(responses):
    def fetch(url, timeout):
        assert timeout == 5.0
        value = responses[url]
        if isinstance(value, Exception):
            raise value
        return value

    return fetch


@pytest.fixture
def conn(config):
    c = db.connect(config.db_path)
    db.migrate(c)
    yield c
    c.close()


def test_fetch_prefers_status_url():
    fetch = fake_fetch(
        {STATUS: json.dumps({"schedule": raw_week()}).encode(), PI: json.dumps(raw_week(("08:00", "09:00"))).encode()}
    )
    raw, source = schedule_source.fetch_schedule(STATUS, PI, fetch)
    assert source == "unraid-backend"
    assert raw[0]["start"] == "18:00"


@pytest.mark.parametrize(
    "status_body",
    [b"<html>oops</html>", json.dumps({"schedule": None}).encode(), json.dumps({"schedule": raw_week()[:6]}).encode(),
     OSError("connection refused")],
)
def test_fetch_rejects_invalid_and_falls_back(status_body):
    fetch = fake_fetch({STATUS: status_body, PI: json.dumps(raw_week(("08:00", "09:00"))).encode()})
    raw, source = schedule_source.fetch_schedule(STATUS, PI, fetch)
    assert source == "pi"
    assert raw[0]["start"] == "08:00"


def test_fetch_both_fail_names_both():
    fetch = fake_fetch({STATUS: b"[]", PI: OSError("timed out")})
    raw, error = schedule_source.fetch_schedule(STATUS, PI, fetch)
    assert raw is None
    assert STATUS in error and PI in error


def test_fetch_no_urls():
    assert schedule_source.fetch_schedule("", "", fake_fetch({})) == (None, "No schedule URL set")


def test_refresh_keeps_last_good(config, conn):
    settings.save(conn, {"schedule_status_url": STATUS})
    good = fake_fetch({STATUS: json.dumps({"schedule": raw_week()}).encode()})
    assert asyncio.run(schedule_source.refresh(config.db_path, good)) is None
    cached = schedule_source.load_cached(conn)
    assert cached.source == "unraid-backend"
    assert cached.entries[0] == schedule.DayEntry("monday", "18:00", "23:00", False)

    bad = fake_fetch({STATUS: OSError("down")})
    error = asyncio.run(schedule_source.refresh(config.db_path, bad))
    assert error and "down" in error
    assert schedule_source.load_cached(conn).entries == cached.entries
    assert schedule_source.load_cached(conn).fetched_at == cached.fetched_at


def test_load_cached_rejects_corrupt(conn):
    assert schedule_source.load_cached(conn) is None
    settings.set_internal(conn, "internal.schedule", {"raw": [1, 2], "source": "pi", "fetched_at": "t"})
    assert schedule_source.load_cached(conn) is None


def test_current_guard_uses_grace_setting(conn):
    schedule_source.save_cached(conn, raw_week(), "pi", "t")
    settings.save(conn, {"shutdown_grace_hours": 1.5})
    now = datetime(2026, 11, 16, 20, 0, tzinfo=ZoneInfo("Europe/London"))
    guard = schedule_source.current_guard(conn, now)
    assert guard.phase == "in_window"
    assert guard.grace_ends_at == guard.scheduled_end + timedelta(hours=1.5)


def test_current_guard_without_cache(conn):
    now = datetime(2026, 11, 16, 20, 0, tzinfo=ZoneInfo("Europe/London"))
    assert schedule_source.current_guard(conn, now).phase == "no_schedule"
