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


def test_services_register_cd_rip_job(config):
    from disc_ripper import runner

    svc = services.Services(config)
    assert "rip_cd" in svc.runner.handlers
    assert svc.runner.services is svc
    assert runner.HANDLERS is svc.runner.handlers


def test_services_cd_helpers(config):
    from disc_ripper import db, jellyfin, musicbrainz, settings

    conn = db.connect(config.db_path)
    db.migrate(conn)
    svc = services.Services(config)
    assert isinstance(svc.musicbrainz, musicbrainz.MusicBrainz)
    assert svc.jellyfin() is None  # not configured
    try:
        settings.save(conn, {"jellyfin_url": "http://jf:8096", "jellyfin_api_key": "k"})
    finally:
        conn.close()
    client = svc.jellyfin()
    assert isinstance(client, jellyfin.Jellyfin) and client.base == "http://jf:8096"
    assert isinstance(svc.refresher, jellyfin.RefreshDebouncer)
    assert callable(svc.cover_fetch)


def test_start_sweeps_old_work_folders(config, add_sg, monkeypatch):
    import asyncio

    from disc_ripper import cdrip

    _prepare(config, add_sg).close()
    calls = []
    monkeypatch.setattr(cdrip, "sweep_work", lambda conn, work_dir, now: calls.append(work_dir))
    svc = services.Services(config)

    async def main():
        await svc.start()
        await svc.stop()

    asyncio.run(main())
    assert calls == [config.work_dir]


def test_start_reconciles_interrupted_cd_rips(config, add_sg):
    import asyncio

    from disc_ripper import cd_rips

    from .conftest import make_flac

    conn = _prepare(config, add_sg)
    filing_job = jobs.enqueue(conn, "serial:A", "rip_cd", {}, now="t")
    cd_rips.create(conn, filing_job, "d1", "t")
    cd_rips.update(conn, filing_job, state="filing")
    ident_job = jobs.enqueue(conn, "serial:A", "rip_cd", {}, now="t")
    jobs.start(conn, ident_job, "t")  # killed while identifying
    cd_rips.create(conn, ident_job, "d2", "t")
    dest = config.downloads_dir / str(ident_job)
    make_flac(dest / "01.flac")
    cd_rips.update(conn, ident_job, downloads_path=str(dest))
    assert cd_rips.blocking_rip_for_disc(conn, "d1") is not None  # 'filing' blocks re-rips

    svc = services.Services(config)

    async def main():
        await svc.start()
        await svc.stop()

    asyncio.run(main())
    assert cd_rips.get(conn, filing_job)["state"] == "needs_look"
    row = cd_rips.get(conn, ident_job)
    assert row["state"] == "needs_look" and "interrupted" in row["reasons"]
    conn.close()


def test_startup_sweeps_leftover_partials(config, add_sg):
    import asyncio

    from disc_ripper import video_rips

    conn = _prepare(config, add_sg)
    drives.set_media(conn, "serial:A", "disc", "dvd", "X", "t")
    ids = {}
    for name, state in (("running", "running"), ("queued", "queued"), ("waiting", "waiting_space"),
                        ("failed", "failed"), ("cancelled", "cancelled"), ("done", "done"),
                        ("moved", "done")):
        job = jobs.enqueue(conn, "serial:A", "rip_title", {"rip_id": 50}, uses_drive=True, now="t")
        with conn:
            conn.execute("UPDATE jobs SET state = ? WHERE id = ?", (state, job))
        ids[name] = job
    # "moved" belongs to a move_failed rip of its own, and its file is what Move again needs
    with conn:
        conn.execute("INSERT INTO video_rips (rip_id, fingerprint, state, created_at, updated_at) "
                     "VALUES (?, 'fp', 'move_failed', 't', 't')", (ids["moved"],))
        conn.execute("UPDATE jobs SET params = json_set(params, '$.rip_id', ?) WHERE id = ?",
                     (ids["moved"], ids["moved"]))
    for name, job in ids.items():
        rip = ids["moved"] if name == "moved" else 50
        folder = video_rips.partial_dir(config.downloads_dir, rip, job)
        folder.mkdir(parents=True)
        (folder / ("t.mkv" if name == "moved" else "t.part")).write_bytes(b"x")
    orphan = video_rips.partial_dir(config.downloads_dir, 51, 9999)  # a job that no longer exists
    orphan.mkdir(parents=True)
    svc = services.Services(config)

    async def main():
        await svc.start()
        await svc.stop()

    asyncio.run(main())
    kept = {n for n, j in ids.items()
            if video_rips.partial_dir(config.downloads_dir, ids["moved"] if n == "moved" else 50, j).is_dir()}
    assert kept == {"queued", "waiting", "moved"}  # the running one was interrupted by the restart
    assert not orphan.exists() and not (config.downloads_dir / "51").exists()
    conn.close()


def test_startup_cancels_queued_disc_jobs_without_a_disc(config, add_sg):
    import asyncio

    conn = _prepare(config, add_sg)
    drives.set_media(conn, "serial:A", "empty", None, "", "t")
    rip = jobs.enqueue(conn, "serial:A", "rip_title", {"rip_id": 1, "title": 0}, uses_drive=True, now="t")
    scan = jobs.enqueue(conn, "serial:A", "scan_video", {"label": "X"}, uses_drive=True, now="t")
    other = jobs.enqueue(conn, "serial:A", "test", {"duration_s": 1}, now="t")
    svc = services.Services(config)

    async def main():
        await svc.start()
        await svc.stop()

    asyncio.run(main())
    for job in (rip, scan):
        row = jobs.get(conn, job)
        assert row["state"] == "cancelled" and row["message"] == "The disc was taken out while the app was off."
    assert jobs.get(conn, other)["state"] != "cancelled"
    conn.close()


def test_startup_keeps_queued_jobs_when_the_stored_media_state_is_unclear(config, add_sg):
    import asyncio

    conn = _prepare(config, add_sg)
    rip = jobs.enqueue(conn, "serial:A", "rip_title", {"rip_id": 1, "title": 0}, uses_drive=True, now="t")
    for state in ("unknown", "not_ready"):  # the last reading before the stop says nothing about the disc
        conn.execute("UPDATE drives SET media_state = ?", (state,))
        conn.commit()
        svc = services.Services(config)

        async def main():
            await svc.start()
            await svc.stop()

        asyncio.run(main())
        assert jobs.get(conn, rip)["state"] == "queued"
    conn.close()


def test_startup_survives_a_failing_step(config, add_sg, monkeypatch):
    import asyncio

    from disc_ripper import cd_rips

    _prepare(config, add_sg).close()

    def boom(conn, work_dir):
        raise RuntimeError("reconcile exploded")

    monkeypatch.setattr(cd_rips, "reconcile", boom)
    svc = services.Services(config)

    async def main():
        await svc.start()
        started = len(svc._tasks)
        await svc.stop()
        return started

    assert asyncio.run(main()) >= 4


def test_app_log_file_written(config):
    import logging

    logger = logging.getLogger("disc_ripper")
    try:
        services._configure_logging(config.config_dir)
        logging.getLogger("disc_ripper.test").info("hello app log")
        for h in logger.handlers:
            h.flush()
        text = (config.config_dir / "logs" / "app.log").read_text(encoding="utf-8")
        assert "hello app log" in text
    finally:
        for h in list(logger.handlers):
            if isinstance(h, services.RotatingFileHandler):
                logger.removeHandler(h)
                h.close()


def test_no_websockets_import():
    from pathlib import Path

    src = Path(services.__file__).parent
    for path in src.rglob("*.py"):
        assert "websockets" not in path.read_text(encoding="utf-8"), path
