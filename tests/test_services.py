import re
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from disc_ripper import db, drives, jobs, services
from disc_ripper.app import create_app


def _prepare(config, add_sg):
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=b"\x05\x80\x00\x01A")
    conn = db.connect(config.db_path)
    db.migrate(conn)
    drives.sync(conn, drives.discover(config.sysfs_root), "t0")
    return conn


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_lifespan_shutdown_marks_running_interrupted(config, add_sg):
    conn = _prepare(config, add_sg)
    j = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 600}, now="t")
    cfg = replace(config, run_background=True)
    with TestClient(create_app(cfg)):
        _wait(lambda: jobs.get(conn, j)["state"] == "running")
    assert jobs.get(conn, j)["state"] == "interrupted"
    conn.close()


def test_startup_marks_leftover_running_interrupted(config, add_sg):
    conn = _prepare(config, add_sg)
    j = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 600}, now="t")
    jobs.start(conn, j, "t")
    cfg = replace(config, run_background=True)
    with TestClient(create_app(cfg)) as client:
        _wait(lambda: jobs.get(conn, j)["state"] == "interrupted")
        assert client.app.state.services.runner is not None
    conn.close()


def test_no_background_without_flag(config, add_sg):
    conn = _prepare(config, add_sg)
    j = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 1}, now="t")
    with TestClient(create_app(config)):
        time.sleep(0.2)
        assert jobs.get(conn, j)["state"] == "queued"
    conn.close()


def test_drive_key_stable():
    key = services.drive_key("serial:3524728 2M8751502226")
    assert key == services.drive_key("serial:3524728 2M8751502226")
    assert re.fullmatch(r"[0-9a-f]{10}", key)
    assert key != services.drive_key("serial:K9ECARB4719")


def test_drive_changed_publishes_card(config, add_sg):
    import asyncio

    conn = _prepare(config, add_sg)
    svc = services.Services(config)

    async def main():
        with svc.broker.subscribe() as sub:
            await svc.drive_changed("serial:A")
            await svc.guard_changed()
            name, html = await sub.queue.get()
            assert name == f"drive-{services.drive_key('serial:A')}"
            assert "BD-RE WH16NS40" in html
            name, html = await sub.queue.get()
            assert name == "guard"

    asyncio.run(main())
    conn.close()


def test_guard_loop_survives_render_error(config, add_sg, monkeypatch):
    import asyncio

    _prepare(config, add_sg).close()
    monkeypatch.setattr(services, "GUARD_TICK_SECONDS", 0.01)
    svc = services.Services(config)
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("render failed")

    svc.guard_changed = flaky

    async def main():
        task = asyncio.create_task(svc._guard_loop())
        await asyncio.sleep(0.2)
        assert not task.done()
        task.cancel()

    asyncio.run(main())
    assert len(calls) > 2
