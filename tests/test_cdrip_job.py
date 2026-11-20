import asyncio
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from disc_ripper import cd_rips, cdrip, db, drives, jobs, runner
from disc_ripper.cdtoc import Toc, musicbrainz_disc_id

FAKE = Path(__file__).parent / "fixtures" / "cyanrip" / "fake_cyanrip.py"
TOC = Toc(first=1, last=3, offsets=(0, 15750, 31500), leadout=47250, data_tracks=frozenset())
DISC_ID = musicbrainz_disc_id(TOC)
OTHER = Toc(first=1, last=2, offsets=(0, 20000), leadout=40000, data_tracks=frozenset())


class FakeDrive:
    def __init__(self, toc):
        self.toc = toc
        self.ejected = 0

    def read_toc(self):
        return self.toc

    def eject(self):
        self.ejected += 1


class FakeMonitor:
    def __init__(self):
        self._locks = {}

    def drive_lock(self, drive_id):
        return self._locks.setdefault(drive_id, threading.Lock())


class FakeServices:
    def __init__(self, toc=TOC):
        self.drive = FakeDrive(toc)
        self.monitor = FakeMonitor()
        self.after = []

    def drive_factory(self, path):
        return self.drive


class Notifier:
    async def drive_changed(self, drive_id):
        pass

    async def guard_changed(self):
        pass


class Space:
    def check(self, *args):
        return None

    def invalidate(self):
        pass


@pytest.fixture
def setup(config, add_sg, monkeypatch):
    monkeypatch.setenv("CYANRIP_BIN", json.dumps([sys.executable, str(FAKE)]))
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=b"\x05\x80\x00\x01A")
    conn = db.connect(config.db_path)
    db.migrate(conn)
    drives.sync(conn, drives.discover(config.sysfs_root), "t0")
    yield conn
    conn.close()


def enqueue(conn, toc=TOC):
    job_id = jobs.enqueue(
        conn, "serial:A", "rip_cd",
        {"disc_id": musicbrainz_disc_id(toc), "toc": toc.to_json(), "offset": 667, "sr_name": "sr0"},
        uses_drive=True, now="t",
    )
    cd_rips.create(conn, job_id, musicbrainz_disc_id(toc), "t")
    return job_id


def run(config, services, until, *, timeout=15.0, after_start=None):
    async def main():
        r = runner.JobRunner(config, Notifier(), Space(), services=services)
        r.sync_workers({"serial:A"})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if after_start:
            await after_start(r)
        while not until():
            assert loop.time() < deadline, "timed out"
            await asyncio.sleep(0.05)
        await r.stop()

    asyncio.run(main())


def test_rip_job_ok(config, setup, monkeypatch):
    seen = []

    async def fake_after(ctx, **kw):
        seen.append(kw)
        await asyncio.to_thread(cd_rips.update, setup, ctx.job_id, state="needs_look", reasons=["test"])

    monkeypatch.setattr(cdrip, "after_rip", fake_after)
    services = FakeServices()
    j = enqueue(setup)
    run(config, services, lambda: jobs.get(setup, j)["state"] in ("done", "failed"))
    row = jobs.get(setup, j)
    assert row["state"] == "done", row["error"]
    assert services.drive.ejected == 1
    [kw] = seen
    assert kw["disc_id"] == DISC_ID
    assert [c.status for c in kw["checks"]] == ["verified", "mismatch", "verified"]
    assert sorted(p.name for p in kw["rip_dir"].glob("*.flac")) == ["1.flac", "2.flac", "3.flac"]
    argv = (config.work_dir / str(j) / "argv.txt").read_text().split("\n")
    assert argv[argv.index("-s") + 1] == "667"
    assert argv[argv.index("-d") + 1] == (config.dev_root / "sr0").as_posix()
    assert json.loads(cd_rips.get(setup, j)["accuraterip"])[1]["status"] == "mismatch"


def test_after_rip_error_goes_to_look_not_failed(config, setup, monkeypatch):
    async def broken(ctx, **kw):
        raise RuntimeError("MusicBrainz exploded")

    monkeypatch.setattr(cdrip, "after_rip", broken)
    j = enqueue(setup)
    run(config, FakeServices(), lambda: jobs.get(setup, j)["state"] in ("done", "failed"))
    assert jobs.get(setup, j)["state"] == "done"
    rip = cd_rips.get(setup, j)
    assert rip["state"] == "needs_look"
    assert "MusicBrainz exploded" in json.loads(rip["reasons"])[0]


def test_rip_job_fail_keeps_disc_in(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_CYANRIP_MODE", "fail")
    services = FakeServices()
    j = enqueue(setup)
    run(config, services, lambda: jobs.get(setup, j)["state"] in ("done", "failed"))
    row = jobs.get(setup, j)
    assert row["state"] == "failed"
    assert "Error ripping: Input/output error" in row["error"]
    assert services.drive.ejected == 0


def test_rip_job_cancel_kills_process(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_CYANRIP_MODE", "hang")
    j = enqueue(setup)

    async def cancel_when_running(r):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while not (config.work_dir / str(j) / "argv.txt").exists():
            assert loop.time() < deadline
            await asyncio.sleep(0.05)
        start = loop.time()
        assert await r.cancel(j) is True
        assert loop.time() - start < 6

    run(config, FakeServices(), lambda: jobs.get(setup, j)["state"] == "cancelled", after_start=cancel_when_running)
    assert cdrip.running_process_count() == 0


def test_retry_refuses_different_disc(config, setup):
    j = enqueue(setup)  # queued for TOC, but OTHER is in the drive now
    run(config, FakeServices(toc=OTHER), lambda: jobs.get(setup, j)["state"] in ("done", "failed"))
    row = jobs.get(setup, j)
    assert row["state"] == "failed"
    assert row["error"] == "A different disc is in the drive."


def test_retry_uses_fresh_work_folder(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_CYANRIP_MODE", "fail")
    first = enqueue(setup)
    run(config, FakeServices(), lambda: jobs.get(setup, first)["state"] == "failed")
    assert (config.work_dir / str(first)).is_dir()  # kept for diagnosis

    monkeypatch.setenv("FAKE_CYANRIP_MODE", "ok")
    monkeypatch.setattr(cdrip, "after_rip", lambda ctx, **kw: asyncio.sleep(0))
    second = jobs.retry(setup, first, "t2")
    cd_rips.create(setup, second, DISC_ID, "t2")
    run(config, FakeServices(), lambda: jobs.get(setup, second)["state"] in ("done", "failed"))
    assert not (config.work_dir / str(first)).exists()


def test_sweep_work_removes_old_finished(config, setup):
    old = enqueue(setup)
    jobs.start(setup, old, "t")
    jobs.finish(setup, old, "failed", (datetime.now(timezone.utc) - timedelta(days=8)).isoformat())
    recent = enqueue(setup)
    jobs.start(setup, recent, "t")
    jobs.finish(setup, recent, "failed", datetime.now(timezone.utc).isoformat())
    for j in (old, recent, 999):
        (config.work_dir / str(j)).mkdir()
    cdrip.sweep_work(setup, config.work_dir, datetime.now(timezone.utc))
    assert not (config.work_dir / str(old)).exists()
    assert (config.work_dir / str(recent)).exists()
    assert (config.work_dir / "999").exists()  # unknown folders are left alone


@pytest.mark.skipif(os.name == "nt", reason="process-group kill is POSIX")
def test_cancel_leaves_no_orphan(config, setup, monkeypatch):
    test_rip_job_cancel_kills_process(config, setup, monkeypatch)


def test_retry_without_cd_rips_row_is_tracked(config, setup, monkeypatch):
    async def fake_after(ctx, **kw):
        await asyncio.to_thread(cd_rips.update, setup, ctx.job_id, state="needs_look", reasons=["x"])

    monkeypatch.setattr(cdrip, "after_rip", fake_after)
    j = jobs.enqueue(setup, "serial:A", "rip_cd",
                     {"disc_id": DISC_ID, "toc": TOC.to_json(), "offset": 0, "sr_name": "sr0"},
                     uses_drive=True, now="t")  # e.g. created by jobs.retry: no cd_rips row
    run(config, FakeServices(), lambda: jobs.get(setup, j)["state"] in ("done", "failed"))
    assert cd_rips.get(setup, j)["state"] == "needs_look"


def test_cancel_during_identification_keeps_the_rip(config, setup, monkeypatch):
    started = asyncio.Event() if False else None

    async def slow_after(ctx, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(cdrip, "_identify", slow_after)
    j = enqueue(setup)

    async def cancel_when_identifying(r):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while not (config.downloads_dir / str(j)).exists():
            assert loop.time() < deadline
            await asyncio.sleep(0.05)
        await r.cancel(j)

    run(config, FakeServices(), lambda: jobs.get(setup, j)["state"] not in ("queued", "running"),
        after_start=cancel_when_identifying)
    rip = cd_rips.get(setup, j)
    assert rip["state"] == "needs_look"
    assert "interrupted" in json.loads(rip["reasons"])[0]
    assert len(list((config.downloads_dir / str(j)).glob("*.flac"))) == 3


def test_sweep_never_touches_done_jobs(config, setup):
    j = enqueue(setup)
    jobs.start(setup, j, "t")
    jobs.finish(setup, j, "done", (datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
    (config.work_dir / str(j)).mkdir()
    cdrip.sweep_work(setup, config.work_dir, datetime.now(timezone.utc))
    assert (config.work_dir / str(j)).exists()


def test_process_killed_when_line_handler_fails(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_CYANRIP_MODE", "hang")
    seen = []

    async def main():
        r = runner.JobRunner(config, Notifier(), Space())
        ctx = runner.JobContext(r, 1, "serial:A", {})
        work = config.work_dir / "x"
        work.mkdir()

        async def on_line(line):
            seen.extend(runner.LIVE_PROCESSES)
            raise RuntimeError("database is locked")

        with pytest.raises(RuntimeError):
            await ctx.run_process(json.loads(os.environ["CYANRIP_BIN"]), cwd=work, on_line=on_line)

    asyncio.run(main())
    assert seen and all(p.returncode is not None for p in seen)
