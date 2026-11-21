import asyncio
import functools
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from disc_ripper import db, encodepool, encodes, runner, settings
from disc_ripper.schedule import GuardState


def sync(fn):
    """Run an async test under asyncio.run (the suite has no async plugin)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))
    return wrapper


class FakeServices:
    def __init__(self):
        self.changes = 0

    def encodes_changed(self):
        self.changes += 1


class FakeSpace:
    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.calls = []

    def check(self, needs_bytes, running_bytes, budget_gb, min_free_gb):
        self.calls.append((needs_bytes, running_bytes))
        return self.answers.pop(0) if self.answers else None

    def invalidate(self):
        pass


class Notifier:
    async def drive_changed(self, drive_id):
        pass

    async def guard_changed(self):
        pass


@pytest.fixture
def conn(config):
    c = db.connect(config.db_path)
    db.migrate(c)
    yield c
    c.close()


def add(conn, n=1, preset="standard", source_seconds=600.0, disc_type="bluray"):
    return encodes.create(
        conn, rip_id=n, title=1, role="main", source=Path(f"/d/{n}/film.mkv"), disc_type=disc_type,
        source_seconds=source_seconds, source_bytes=10_000_000_000, source_audio=2, preset=preset,
        now="2026-01-01T00:00:00+00:00")


def pool_for(config, space=None, services=None, **kw):
    return encodepool.EncodePool(config, Notifier(), space or FakeSpace(), services=services,
                                 recheck_seconds=0.05, **kw)


async def wait_until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.005)


def state(conn, eid):
    return encodes.get(conn, eid)["state"]


def patch_guard(monkeypatch, guard):
    monkeypatch.setattr(encodepool.schedule_source, "current_guard", lambda conn, now: guard)


async def noop(ctx, row):
    pass


@sync
async def test_pool_runs_oldest_first_one_slot(config, conn, monkeypatch):
    order, release = [], asyncio.Event()

    async def fake(ctx, row):
        order.append(row["id"])
        await release.wait()

    monkeypatch.setattr(encodepool, "run_encode", fake)
    a, b = add(conn, 1), add(conn, 2)
    pool = pool_for(config)
    pool.start()
    await wait_until(lambda: order == [a])
    await asyncio.sleep(0.05)
    assert order == [a] and state(conn, b) == "queued"
    release.set()
    await wait_until(lambda: state(conn, b) == "done")
    assert order == [a, b] and state(conn, a) == "done"
    await pool.stop()


@sync
async def test_two_slots_run_together(config, conn, monkeypatch):
    settings.save(conn, {"encode_slots": 2})
    running, release = [], asyncio.Event()

    async def fake(ctx, row):
        running.append(row["id"])
        await release.wait()

    monkeypatch.setattr(encodepool, "run_encode", fake)
    a, b, c = add(conn, 1), add(conn, 2), add(conn, 3)
    pool = pool_for(config)
    pool.start()
    await wait_until(lambda: sorted(running) == [a, b])
    await asyncio.sleep(0.05)
    assert state(conn, c) == "queued" and sorted(running) == [a, b]
    release.set()
    await wait_until(lambda: state(conn, c) == "done")
    await pool.stop()


@sync
async def test_guard_wait_sets_message_and_stays_queued(config, conn, monkeypatch):
    now = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)
    patch_guard(monkeypatch, GuardState("in_window", now + timedelta(hours=1), now + timedelta(hours=1, minutes=10),
                                        window_start=now - timedelta(hours=6)))
    started = []

    async def fake(ctx, row):
        started.append(row["id"])

    monkeypatch.setattr(encodepool, "run_encode", fake)
    eid = add(conn, source_seconds=5400.0)  # about 4.7 h rough: won't finish before the grace ends
    svc = FakeServices()
    pool = pool_for(config, now=lambda: now, services=svc)
    pool.start()
    await wait_until(lambda: encodes.get(conn, eid)["message"].startswith("Waiting for the next session"))
    await asyncio.sleep(0.15)
    assert state(conn, eid) == "queued" and not started
    changes = svc.changes
    await asyncio.sleep(0.15)  # rechecks keep waiting without re-announcing the same message
    assert svc.changes == changes
    patch_guard(monkeypatch, GuardState("no_schedule", None, None))  # the way is clear again
    await wait_until(lambda: state(conn, eid) == "done")  # picked up by the periodic recheck
    await pool.stop()


@sync
async def test_too_long_fails_immediately(config, conn, monkeypatch):
    now = datetime(2026, 11, 18, 22, 0, tzinfo=timezone.utc)
    patch_guard(monkeypatch, GuardState("in_window", now + timedelta(hours=1), now + timedelta(hours=2),
                                        window_start=now - timedelta(hours=1)))
    first = add(conn, 1, source_seconds=20 * 3600.0)
    second = add(conn, 2, source_seconds=60.0)
    monkeypatch.setattr(encodepool, "run_encode", noop)
    pool = pool_for(config, now=lambda: now)
    pool.start()
    await wait_until(lambda: state(conn, first) == "failed")
    assert encodes.get(conn, first)["error"].startswith("Too long to finish in one session")
    await wait_until(lambda: state(conn, second) == "done")  # the next one is not held up
    await pool.stop()


@sync
async def test_space_wait(config, conn, monkeypatch):
    monkeypatch.setattr(encodepool, "run_encode", noop)
    eid = add(conn)
    space = FakeSpace(["Waiting for space", "Waiting for space"])
    pool = pool_for(config, space=space)
    pool.start()
    await wait_until(lambda: encodes.get(conn, eid)["message"] == "Waiting for space")
    assert state(conn, eid) in ("queued", "done")
    await wait_until(lambda: state(conn, eid) == "done")  # rechecked, then fits
    assert len(space.calls) >= 3
    assert space.calls[0][0] == 3_500_000_000  # estimate_size of 10 GB standard
    await pool.stop()


@sync
async def test_cancel_running_and_queued(config, conn, monkeypatch):
    async def fake(ctx, row):
        await asyncio.sleep(3600)

    monkeypatch.setattr(encodepool, "run_encode", fake)
    a, b = add(conn, 1), add(conn, 2)
    pool = pool_for(config)
    pool.start()
    await wait_until(lambda: state(conn, a) == "running")
    assert await pool.cancel(b) is True
    assert state(conn, b) == "cancelled"
    assert await pool.cancel(a) is True
    assert state(conn, a) == "cancelled"
    assert await pool.cancel(a) is False
    assert await pool.cancel(999) is False
    await pool.stop()
    assert state(conn, a) == "cancelled"  # stop does not touch final rows


@sync
async def test_failure_is_recorded_and_slot_survives(config, conn, monkeypatch):
    async def fake(ctx, row):
        if row["id"] == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(encodepool, "run_encode", fake)
    a, b = add(conn, 1), add(conn, 2)
    pool = pool_for(config)
    pool.start()
    await wait_until(lambda: state(conn, b) == "done")
    assert state(conn, a) == "failed" and encodes.get(conn, a)["error"] == "boom"
    await pool.stop()


@sync
async def test_guard_exception_does_not_kill_worker(config, conn, monkeypatch):
    calls = []

    def flaky(c, now):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("schedule broke")
        return GuardState("no_schedule", None, None)

    monkeypatch.setattr(encodepool.schedule_source, "current_guard", flaky)
    monkeypatch.setattr(encodepool, "run_encode", noop)
    eid = add(conn)
    pool = pool_for(config)
    pool.start()
    await wait_until(lambda: state(conn, eid) == "done")
    await pool.stop()


@sync
async def test_stop_marks_interrupted_and_start_requeues(config, conn, monkeypatch):
    async def fake(ctx, row):
        await asyncio.sleep(3600)

    monkeypatch.setattr(encodepool, "run_encode", fake)
    eid = add(conn)
    pool = pool_for(config)
    pool.start()
    await wait_until(lambda: state(conn, eid) == "running")
    await pool.stop()
    assert state(conn, eid) == "interrupted"  # requeue_interrupted is Services.start's job (Task 7)
    assert "finished: interrupted" in (config.config_dir / "logs" / f"encode-{eid}.log").read_text()
    assert not pool._running


CHILD = [sys.executable, "-c", "import time; print('hi', flush=True); time.sleep(60)"]


@sync
async def test_stop_terminates_child_process(config, conn, monkeypatch):
    async def fake(ctx, row):
        async def on_line(line):
            pass
        await ctx.run_process(CHILD, cwd=config.work_dir, on_line=on_line)

    monkeypatch.setattr(encodepool, "run_encode", fake)
    eid = add(conn)
    pool = pool_for(config)
    pool.start()
    await wait_until(lambda: runner.LIVE_PROCESSES)
    await pool.stop()
    assert not runner.LIVE_PROCESSES
    assert state(conn, eid) == "interrupted"


@sync
async def test_cancel_terminates_child_process(config, conn, monkeypatch):
    async def fake(ctx, row):
        async def on_line(line):
            pass
        await ctx.run_process(CHILD, cwd=config.work_dir, on_line=on_line)

    monkeypatch.setattr(encodepool, "run_encode", fake)
    eid = add(conn)
    pool = pool_for(config)
    pool.start()
    await wait_until(lambda: runner.LIVE_PROCESSES)
    assert await pool.cancel(eid)
    assert not runner.LIVE_PROCESSES and state(conn, eid) == "cancelled"
    await pool.stop()


@sync
async def test_encode_log_written_to_encode_log_path(config, conn, monkeypatch):
    async def fake(ctx, row):
        ctx.log.write("hello from the encode")
        await ctx.report(0.5, "Encoding 50%")

    monkeypatch.setattr(encodepool, "run_encode", fake)
    eid = add(conn)
    pool = pool_for(config, services=FakeServices())
    pool.start()
    await wait_until(lambda: state(conn, eid) == "done")
    await pool.stop()
    text = (config.config_dir / "logs" / f"encode-{eid}.log").read_text()
    assert "hello from the encode" in text and "finished: done" in text
    assert not (config.config_dir / "logs" / f"job-{eid}.log").exists()


@sync
async def test_progress_goes_through_encodes_and_notifies(config, conn, monkeypatch):
    seen = {}

    async def fake(ctx, row):
        await ctx.report(0.25, "Encoding 25%")
        c = db.connect(config.db_path)
        seen.update(dict(encodes.get(c, row["id"])))
        c.close()

    monkeypatch.setattr(encodepool, "run_encode", fake)
    eid = add(conn)
    svc = FakeServices()
    pool = pool_for(config, services=svc)
    pool.start()
    await wait_until(lambda: state(conn, eid) == "done")
    assert seen["progress"] == 0.25 and seen["message"] == "Encoding 25%"
    assert svc.changes >= 2
    await pool.stop()


@sync
async def test_log_filter_keeps_noise_out_of_log(config, conn, monkeypatch):
    async def fake(ctx, row):
        lines = []

        async def on_line(line):
            lines.append(line)
        code = "print('noise', flush=True); print('keep', flush=True)"
        await ctx.run_process([sys.executable, "-c", code], cwd=config.work_dir, on_line=on_line,
                              log_filter=lambda line: line == "noise")
        assert lines == ["noise", "keep"]

    monkeypatch.setattr(encodepool, "run_encode", fake)
    eid = add(conn)
    pool = pool_for(config)
    pool.start()
    await wait_until(lambda: state(conn, eid) == "done")
    await pool.stop()
    text = (config.config_dir / "logs" / f"encode-{eid}.log").read_text()
    assert "| keep" in text and "| noise" not in text


def test_late_finish_does_not_overwrite(conn):
    eid = add(conn)
    encodes.start(conn, eid, "t")
    encodes.finish(conn, eid, "cancelled", "t1")
    encodes.finish(conn, eid, "done", "t2")
    assert encodes.get(conn, eid)["state"] == "cancelled"


def test_reserved_bytes(conn):
    a, b = add(conn, 1), add(conn, 2, preset="copy")
    assert encodes.reserved_bytes(conn) == 0
    encodes.start(conn, a, "t")
    encodes.start(conn, b, "t")
    assert encodes.reserved_bytes(conn) == 3_500_000_000 + 10_000_000_000


@sync
async def test_run_encode_default_is_a_stub():
    with pytest.raises(NotImplementedError):
        await encodepool.run_encode(None, None)
