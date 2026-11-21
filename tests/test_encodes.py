import sqlite3
from pathlib import Path

import pytest

from disc_ripper import db, encodes


@pytest.fixture
def conn(config):
    c = db.connect(config.db_path)
    db.migrate(c)
    yield c
    c.close()


def _create(conn, rip_id=1, title=1, preset="standard", disc_type="bluray", now="2026-01-01T00:00:00+00:00"):
    return encodes.create(
        conn, rip_id=rip_id, title=title, role="main", source=Path("/d/1/film.mkv"), disc_type=disc_type,
        source_seconds=6035.0, source_bytes=30_000_000_000, source_audio=2, preset=preset, now=now)


def test_create_refuses_second_active_for_same_file(conn):
    first = _create(conn)
    assert first is not None
    assert _create(conn) is None
    assert _create(conn, title=2) is not None  # another file is fine
    assert encodes.start(conn, first, "t")
    assert _create(conn) is None  # running still blocks
    encodes.finish(conn, first, "done", "t")
    assert _create(conn) is not None


def test_retry_copies_source_fields(conn):
    first = _create(conn, preset="fast")
    assert encodes.retry(conn, first, "t") is None  # still queued
    encodes.start(conn, first, "t")
    encodes.finish(conn, first, "failed", "t", error="boom")
    new = encodes.retry(conn, first, "t2")
    assert new is not None and new != first
    a, b = encodes.get(conn, first), encodes.get(conn, new)
    for k in ("rip_id", "title", "role", "source", "disc_type", "source_seconds", "source_bytes",
              "source_audio", "preset"):
        assert a[k] == b[k]
    assert b["state"] == "queued" and b["created_at"] == "t2"
    assert encodes.retry(conn, first, "t3") is None  # refused while the retry is active


def test_requeue_interrupted(conn):
    a, b = _create(conn), _create(conn, title=2)
    encodes.start(conn, a, "t")
    assert encodes.mark_interrupted(conn, "t2") == 1
    assert encodes.get(conn, a)["state"] == "interrupted"
    assert encodes.requeue_interrupted(conn, "t3") == 1
    row = encodes.get(conn, a)
    assert row["state"] == "queued" and row["id"] == a
    assert [r["id"] for r in encodes.queued(conn)] == [a, b]
    assert encodes.next_queued(conn)["id"] == a


def test_recent_speeds_last_five_by_preset_and_type(conn):
    ids = []
    for i in range(7):
        eid = _create(conn, rip_id=10 + i)
        encodes.start(conn, eid, "t")
        encodes.finish(conn, eid, "done", "t", speed=float(i + 1))
        ids.append(eid)
    other = _create(conn, rip_id=50, preset="fast")
    encodes.start(conn, other, "t")
    encodes.finish(conn, other, "done", "t", speed=99.0)
    dvd = _create(conn, rip_id=51, disc_type="dvd")
    encodes.start(conn, dvd, "t")
    encodes.finish(conn, dvd, "done", "t", speed=98.0)
    failed = _create(conn, rip_id=52)
    encodes.start(conn, failed, "t")
    encodes.finish(conn, failed, "failed", "t", speed=97.0)
    assert sorted(encodes.recent_speeds(conn, "standard", "bluray")) == [3.0, 4.0, 5.0, 6.0, 7.0]
    assert encodes.recent_speeds(conn, "fast", "bluray") == [99.0]
    assert encodes.recent_speeds(conn, "copy", "bluray") == []


def test_cancel_queued_and_progress(conn):
    a = _create(conn)
    encodes.wait(conn, a, "Waiting for the next session")
    row = encodes.get(conn, a)
    assert (row["state"], row["message"]) == ("queued", "Waiting for the next session")
    assert encodes.start(conn, a, "t") and not encodes.start(conn, a, "t")
    encodes.set_progress(conn, a, 0.5, "50%", "2026-01-01T01:00:00+00:00")
    assert encodes.get(conn, a)["progress"] == 0.5
    assert [r["id"] for r in encodes.running(conn)] == [a]
    assert encodes.cancel_queued(conn, a, "t") == "running"
    encodes.finish(conn, a, "done", "t")
    assert encodes.cancel_queued(conn, a, "t") == "gone"
    assert encodes.cancel_queued(conn, 999, "t") == "gone"
    b = _create(conn, title=2)
    assert encodes.cancel_queued(conn, b, "t") == "cancelled"
    assert encodes.get(conn, b)["state"] == "cancelled"
    assert [r["id"] for r in encodes.for_rip(conn, 1)] == [a, b]


def test_requeue_interrupted_cancels_superseded(conn):
    a = _create(conn)
    encodes.start(conn, a, "t")
    encodes.mark_interrupted(conn, "t2")
    newer = _create(conn)  # an interrupted row is not active, so this is allowed
    assert newer is not None
    assert encodes.requeue_interrupted(conn, "t3") == 0
    row = encodes.get(conn, a)
    assert row["state"] == "cancelled" and row["message"] == "Superseded by a newer encode of this file"
    assert [r["id"] for r in encodes.queued(conn)] == [newer]


def test_retry_refused_unless_ended_badly(conn):
    a = _create(conn)
    encodes.start(conn, a, "t")
    encodes.finish(conn, a, "done", "t")
    assert encodes.retry(conn, a, "t") is None
    assert encodes.retry(conn, 999, "t") is None


def test_unique_index_is_a_backstop(conn):
    _create(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO encodes (rip_id, title, role, source, disc_type, source_seconds, source_bytes, "
            "preset, state, created_at) VALUES (1, 1, 'main', 'x', 'bluray', 1, 1, 'fast', 'running', 't')")


def _running(conn, progress, rip_id=1, preset="standard", started="2026-01-01T00:00:00+00:00"):
    eid = _create(conn, rip_id=rip_id, preset=preset)
    assert encodes.start(conn, eid, started)
    encodes.set_progress(conn, eid, progress, "x", None)
    return eid


def test_interrupted_encode_records_observed_speed(conn):
    eid = _running(conn, 0.2)  # 20% of 6035 s of film in 1000 s
    assert encodes.mark_interrupted(conn, "2026-01-01T00:16:40+00:00") == 1
    row = encodes.get(conn, eid)
    assert row["state"] == "interrupted"
    assert row["speed"] == pytest.approx(6035.0 * 0.2 / 1000)


def test_interrupted_below_five_percent_records_no_speed(conn):
    eid = _running(conn, 0.04)
    encodes.mark_interrupted(conn, "2026-01-01T00:16:40+00:00")
    assert encodes.get(conn, eid)["speed"] is None


def test_interrupted_with_unreadable_times_records_no_speed(conn):
    eid = _running(conn, 0.5, started="t")
    encodes.mark_interrupted(conn, "t2")
    assert encodes.get(conn, eid)["speed"] is None


def test_recent_speeds_include_interrupted_and_requeued(conn):
    done = _create(conn, rip_id=1)
    encodes.start(conn, done, "2026-01-01T00:00:00+00:00")
    encodes.finish(conn, done, "done", "2026-01-01T01:00:00+00:00", speed=7.0)
    eid = _running(conn, 0.2, rip_id=2, started="2026-01-02T00:00:00+00:00")
    encodes.mark_interrupted(conn, "2026-01-02T00:16:40+00:00")
    assert sorted(encodes.recent_speeds(conn, "standard", "bluray")) == pytest.approx([6035.0 * 0.2 / 1000, 7.0])
    encodes.requeue_interrupted(conn, "t3")  # a restart puts it back in the queue; its speed still counts
    assert encodes.get(conn, eid)["state"] == "queued"
    assert len(encodes.recent_speeds(conn, "standard", "bluray")) == 2
