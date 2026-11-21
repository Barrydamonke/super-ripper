import asyncio
import json
import threading
import types
from pathlib import Path

import pytest

from disc_ripper import jobs, runner, titles, video_rips, videofiles, videorip
from disc_ripper.services import Services

from .test_videorip_scan import Notifier, finished, rip_jobs, run, scan_job, setup  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures" / "makemkv"


class FakeDrive:
    def __init__(self):
        self.ejected = 0

    def eject(self):
        self.ejected += 1


def fake_probe(path):
    stub = json.loads(Path(path).read_text(encoding="utf-8"))
    return videofiles.MkvInfo(stub["seconds"], stub["video"], stub["audio"])


@pytest.fixture(autouse=True)
def probe(monkeypatch):
    monkeypatch.setattr(videofiles, "PROBE", fake_probe)


def make_services(config):
    services = Services(config)
    services.drive = FakeDrive()
    services.drive_factory = lambda path: services.drive
    return services


def scanned(config, setup):
    """Run the scan; leave a queued main rip_title for the tests to run."""
    scan_job(setup)
    services = make_services(config)
    run(config, services, lambda: rip_jobs(setup) != [])
    [rip] = rip_jobs(setup)
    return services, rip["id"]


class Space:
    def check(self, *args):
        return None

    def invalidate(self):
        pass


def run_free(config, services, until, timeout=15.0):
    async def main():
        r = runner.JobRunner(config, Notifier(), Space(), services=services)
        r.sync_workers({"serial:A"})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not until():
            assert loop.time() < deadline, "timed out"
            await asyncio.sleep(0.05)
        await r.stop()

    asyncio.run(main())


def run_until_done(config, conn, services, job_id):
    run_free(config, services, finished(conn, job_id))
    return jobs.get(conn, job_id)


def rip_state(conn, rip_id):
    return conn.execute("SELECT * FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()


def queue_extra(conn, rip_id, title=1):
    params = json.loads(jobs.get(conn, rip_id)["params"])
    params.update(title=title, role="extra")
    return jobs.enqueue(conn, "serial:A", "rip_title", params, uses_drive=True, now="t")


def test_rip_main_moves_and_ejects(config, setup):
    services, rip = scanned(config, setup)
    with setup:
        setup.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('video_min_title_seconds', '300')")
    row = run_until_done(config, setup, services, rip)
    assert row["state"] == "done", row["error"]
    assert (config.downloads_dir / str(rip) / "title_t00.mkv").is_file()
    assert not (config.work_dir / str(rip)).exists()
    assert rip_state(setup, rip)["state"] == "ripped"
    assert services.drive.ejected == 1
    assert "--minlength=120" in (config.work_dir / "argv.txt").read_text()


def test_extra_goes_to_extras_and_eject_waits(config, setup):
    services, rip = scanned(config, setup)
    extra = queue_extra(setup, rip)
    run_free(config, services, lambda: jobs.get(setup, extra)["state"] == "done")
    assert (config.downloads_dir / str(rip) / "title_t00.mkv").is_file()
    assert (config.downloads_dir / str(rip) / "extras" / "title_t01.mkv").is_file()
    assert services.drive.ejected == 1
    assert rip_state(setup, rip)["state"] == "ripped"


def test_short_file_fails_and_keeps_disc_in(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", "short")
    services, rip = scanned(config, setup)
    row = run_until_done(config, setup, services, rip)
    assert row["state"] == "failed" and "length" in row["error"]
    assert not (config.work_dir / str(rip)).exists()
    assert services.drive.ejected == 0
    assert rip_state(setup, rip)["state"] == "failed"


def test_no_audio_fails(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", "no_audio")
    services, rip = scanned(config, setup)
    row = run_until_done(config, setup, services, rip)
    assert row["state"] == "failed" and "audio" in row["error"]


def test_rip_refuses_different_disc(config, setup):
    services, rip = scanned(config, setup)
    (config.work_dir / "argv.txt").unlink()  # the scan's; the rip mustn't write one
    with setup:
        setup.execute("UPDATE drives SET disc_label = 'OTHER'")
    row = run_until_done(config, setup, services, rip)
    assert row["state"] == "failed" and "A different disc is in the drive." in row["error"]
    assert not (config.work_dir / "argv.txt").exists()


def test_retry_same_folder_then_eject(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", "short")
    services, rip = scanned(config, setup)
    run_until_done(config, setup, services, rip)
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", "ok")
    retry = jobs.retry(setup, rip, "t")
    run_until_done(config, setup, services, retry)
    assert jobs.get(setup, retry)["state"] == "done"
    assert (config.downloads_dir / str(rip) / "title_t00.mkv").is_file()
    assert services.drive.ejected == 1
    assert rip_state(setup, rip)["state"] == "ripped"


def test_retry_removes_partial_work(config, setup):
    services, rip = scanned(config, setup)
    with setup:
        setup.execute("UPDATE jobs SET state = 'interrupted' WHERE id = ?", (rip,))
    partial = config.work_dir / str(rip) / "title_t00.mkv"
    partial.parent.mkdir()
    partial.write_text("partial")
    retry = jobs.retry(setup, rip, "t")
    run_until_done(config, setup, services, retry)
    assert jobs.get(setup, retry)["state"] == "done"
    assert not partial.exists()


def shortfall(free_gb):
    return lambda path: types.SimpleNamespace(f_bavail=int(free_gb * 1024**3), f_frsize=1)


def test_move_space_shortfall_is_move_failed(config, setup, monkeypatch):
    services, rip = scanned(config, setup)
    monkeypatch.setattr(videofiles, "STATVFS", shortfall(1))
    row = run_until_done(config, setup, services, rip)
    assert row["state"] == "done"
    state = rip_state(setup, rip)
    assert state["state"] == "move_failed" and "Couldn't move to downloads" in state["message"]
    assert (config.work_dir / str(rip) / "title_t00.mkv").is_file()
    monkeypatch.setattr(videofiles, "STATVFS", shortfall(500))
    assert asyncio.run(services.move_again(rip)) is None
    assert rip_state(setup, rip)["state"] == "ripped"
    assert (config.downloads_dir / str(rip) / "title_t00.mkv").is_file()
    assert not (config.work_dir / str(rip)).exists()


def test_move_never_overwrites_and_ignores_part(config, tmp_path):
    src = config.work_dir / "5" / "title_t00.mkv"
    src.parent.mkdir()
    src.write_text("new")
    dest = config.downloads_dir / "7"
    dest.mkdir()
    (dest / "title_t00.mkv").write_text("old")
    (dest / "title_t00.mkv.part").write_text("stale")
    result = videofiles.move_to_downloads(config, 7, "main", src, min_free_gb=0)
    assert result.name == "title_t00 (2).mkv" and result.read_text() == "new"
    assert (dest / "title_t00.mkv").read_text() == "old"
    assert not list(dest.glob("*.part"))
    assert not src.exists()


def test_parse_ffprobe():
    info = videofiles.parse_ffprobe((FIXTURES / "ffprobe_ok.json").read_text())
    assert info == videofiles.MkvInfo(seconds=6730.04, video=1, audio=2, audio_channels=(6, 2))


def test_attempts_feed_should_eject(config, setup):
    services, rip = scanned(config, setup)
    assert [a[:2] for a in video_rips.attempts(setup, rip)] == [(0, rip)]
    assert not titles.should_eject(video_rips.attempts(setup, rip))


def stub_mkv(folder, name, seconds, audio=2):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps({"seconds": seconds, "video": 1, "audio": audio}))


def test_check_output_length_tolerance(tmp_path):
    stub_mkv(tmp_path / "a", "t.mkv", 6730)
    assert videofiles.check_output(tmp_path / "a", 6730).name == "t.mkv"
    stub_mkv(tmp_path / "b", "t.mkv", 6730 * 0.97)  # about 3% short
    with pytest.raises(RuntimeError, match="length"):
        videofiles.check_output(tmp_path / "b", 6730)
    stub_mkv(tmp_path / "c", "t.mkv", 6730 * 1.03)  # and too long
    with pytest.raises(RuntimeError, match="length"):
        videofiles.check_output(tmp_path / "c", 6730)
    stub_mkv(tmp_path / "d", "t.mkv", 100)  # short titles get 5 s
    assert videofiles.check_output(tmp_path / "d", 104)
    with pytest.raises(RuntimeError, match="length"):
        videofiles.check_output(tmp_path / "d", 106)


def test_check_output_needs_exactly_one_top_level_file(tmp_path):
    stub_mkv(tmp_path, "a.mkv", 100)
    stub_mkv(tmp_path, "b.mkv", 100)
    with pytest.raises(RuntimeError, match="more than one file"):
        videofiles.check_output(tmp_path, 100)
    stub_mkv(tmp_path / "n", "a.mkv", 100)
    stub_mkv(tmp_path / "n" / "sub", "b.mkv", 100)  # nested files are not the output
    assert videofiles.check_output(tmp_path / "n", 100).name == "a.mkv"


def test_long_file_fails(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", "long")
    services, rip = scanned(config, setup)
    row = run_until_done(config, setup, services, rip)
    assert row["state"] == "failed" and "length" in row["error"]


def test_two_files_fail_and_nothing_is_deleted_silently(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", "two_files")
    services, rip = scanned(config, setup)
    row = run_until_done(config, setup, services, rip)
    assert row["state"] == "failed" and "more than one file" in row["error"]
    assert not (config.downloads_dir / str(rip)).exists()


def test_cancel_during_move_leaves_nothing_in_downloads(config, tmp_path):
    src = config.work_dir / "5" / "title_t00.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 100)
    stop = threading.Event()
    stop.set()
    with pytest.raises(videofiles.MoveCancelled):
        videofiles.move_to_downloads(config, 7, "main", src, min_free_gb=0, cancel=stop)
    assert src.read_bytes() == b"x" * 100
    assert not list((config.downloads_dir / "7").glob("*"))


def test_cancel_mid_copy_stops_between_chunks(config, monkeypatch):
    monkeypatch.setattr(videofiles, "CHUNK", 10)
    src = config.work_dir / "5" / "title_t00.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 100)
    stop = threading.Event()

    class Reader:
        def __init__(self, f):
            self.f, self.n = f, 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

        def read(self, size):
            self.n += 1
            if self.n == 3:
                stop.set()
            return self.f.read(size)

        def fileno(self):
            return self.f.fileno()

    real_open = Path.open
    monkeypatch.setattr(Path, "open", lambda self, mode="r", *a, **k:
                        Reader(real_open(self, mode, *a, **k)) if self == src else real_open(self, mode, *a, **k))
    with pytest.raises(videofiles.MoveCancelled):
        videofiles.move_to_downloads(config, 7, "main", src, min_free_gb=0, cancel=stop)
    assert src.exists() and not list((config.downloads_dir / "7").glob("*"))


def test_handler_cancel_during_move_keeps_file_in_work(config, setup, monkeypatch):
    services, rip = scanned(config, setup)
    inside = threading.Event()
    real = videofiles.move_to_downloads

    def slow(config_, rip_id, role, src, statvfs=None, min_free_gb=0, cancel=None, **kw):
        inside.set()
        assert cancel.wait(10)  # the handler must set the event on cancel
        return real(config_, rip_id, role, src, min_free_gb=min_free_gb, cancel=cancel, **kw)

    monkeypatch.setattr(videofiles, "move_to_downloads", slow)

    async def go():
        r = runner.JobRunner(config, Notifier(), Space(), services=services)
        r.sync_workers({"serial:A"})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        while not inside.is_set():
            assert loop.time() < deadline
            await asyncio.sleep(0.05)
        await r.cancel(rip)
        await r.stop()

    asyncio.run(go())
    assert jobs.get(setup, rip)["state"] == "cancelled"
    assert (config.work_dir / str(rip) / "title_t00.mkv").is_file()
    assert not list((config.downloads_dir / str(rip)).glob("*"))
    assert rip_state(setup, rip)["files"] == "{}"


# --- settling the rip once nothing of it is pending (review finding #1) ---------------

def fingerprint_of(conn, rip_id):
    return rip_state(conn, rip_id)["fingerprint"]


def blocking(config, conn, rip_id):
    return video_rips.blocking_rip(conn, fingerprint_of(conn, rip_id), config.downloads_dir, config.work_dir)


def test_extra_added_after_main_then_failing_settles_ripped(config, setup, monkeypatch):
    services, rip = scanned(config, setup)
    run_until_done(config, setup, services, rip)
    extra = asyncio.run(services.add_video_extra(rip, 1))
    assert extra is not None
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", "short")
    assert run_until_done(config, setup, services, extra)["state"] == "failed"
    row = rip_state(setup, rip)
    assert row["state"] == "ripped"
    assert "title 1 failed" in row["message"]
    assert blocking(config, setup, rip) is not None
    assert services.drive.ejected == 1  # the extra failed: the disc stays in after it


def test_cancelled_queued_extra_settles_and_ejects(config, setup):
    services, rip = scanned(config, setup)
    extra = queue_extra(setup, rip)
    assert asyncio.run(services.cancel_job(extra))
    run_until_done(config, setup, services, rip)
    row = rip_state(setup, rip)
    assert row["state"] == "ripped" and "title 1 cancelled" in row["message"]
    assert (config.downloads_dir / str(rip) / "title_t00.mkv").is_file()
    assert blocking(config, setup, rip) is not None
    assert services.drive.ejected == 1


class SpaceExcept:
    """Every job fits except ones needing exactly `refuse` bytes."""

    def __init__(self, refuse):
        self.refuse = refuse

    def check(self, needs_bytes, *args):
        return "no space" if needs_bytes == self.refuse else None

    def invalidate(self):
        pass


def test_cancelling_the_last_queued_extra_ejects(config, setup):
    services, rip = scanned(config, setup)
    extra = queue_extra(setup, rip)
    with setup:
        setup.execute("UPDATE jobs SET needs_bytes = 12345 WHERE id = ?", (extra,))

    async def main():
        r = runner.JobRunner(config, Notifier(), SpaceExcept(12345), services=services)
        r.sync_workers({"serial:A"})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        while jobs.get(setup, rip)["state"] != "done":
            assert loop.time() < deadline, "timed out"
            await asyncio.sleep(0.05)
        await r.stop()

    asyncio.run(main())
    assert services.drive.ejected == 0  # the extra is still queued (waiting for space)
    assert rip_state(setup, rip)["state"] == "ripping"
    assert asyncio.run(services.cancel_job(extra))
    assert services.drive.ejected == 1
    assert rip_state(setup, rip)["state"] == "ripped"


def test_cancelled_running_main_settles_failed_and_does_not_block(config, setup, monkeypatch):
    services, rip = scanned(config, setup)
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", "hang")

    async def main():
        r = runner.JobRunner(config, Notifier(), Space(), services=services)
        r.sync_workers({"serial:A"})
        while jobs.get(setup, rip)["state"] != "running" or not runner.LIVE_PROCESSES:
            await asyncio.sleep(0.05)
        await r.cancel(rip)
        await r.stop()

    asyncio.run(main())
    assert jobs.get(setup, rip)["state"] == "cancelled"
    assert rip_state(setup, rip)["state"] == "failed"
    assert blocking(config, setup, rip) is None
    assert services.drive.ejected == 0


def test_stale_ripping_row_with_downloads_blocks_as_ripped(config, setup):
    services, rip = scanned(config, setup)
    with setup:
        setup.execute("UPDATE jobs SET state = 'cancelled' WHERE id = ?", (rip,))
    (config.downloads_dir / str(rip)).mkdir(parents=True)
    found = blocking(config, setup, rip)
    assert found is not None and found["state"] == "ripped" and found["rip_id"] == rip


def run_held_back(config, conn, services, until, held_bytes=12345):
    async def main():
        r = runner.JobRunner(config, Notifier(), SpaceExcept(held_bytes), services=services)
        r.sync_workers({"serial:A"})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15
        while not until():
            assert loop.time() < deadline, "timed out"
            await asyncio.sleep(0.05)
        await r.stop()

    asyncio.run(main())


def test_queued_extra_failing_after_main_settles_ripped(config, setup, monkeypatch):
    services, rip = scanned(config, setup)
    extra = queue_extra(setup, rip)
    with setup:
        setup.execute("UPDATE jobs SET needs_bytes = 12345 WHERE id = ?", (extra,))
    run_held_back(config, setup, services, lambda: jobs.get(setup, rip)["state"] == "done")
    assert rip_state(setup, rip)["state"] == "ripping"
    with setup:
        setup.execute("UPDATE jobs SET needs_bytes = 1 WHERE id = ?", (extra,))
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", "short")
    assert run_until_done(config, setup, services, extra)["state"] == "failed"
    row = rip_state(setup, rip)
    assert row["state"] == "ripped" and "title 1 failed" in row["message"]
    assert blocking(config, setup, rip) is not None
    assert services.drive.ejected == 0


# --- queued jobs don't outlive their disc (review finding #3) --------------------------

def test_disc_leaving_cancels_a_rip_waiting_for_space(config, setup):
    services, rip = scanned(config, setup)
    extra = queue_extra(setup, rip)
    jobs.wait_space(setup, rip, "Waiting for cache space: needs 38.2 GB")
    asyncio.run(services.on_remove("serial:A"))
    assert jobs.get(setup, rip)["state"] == "cancelled"
    assert jobs.get(setup, extra)["state"] == "cancelled"
    assert rip_state(setup, rip)["state"] == "failed"
    assert blocking(config, setup, rip) is None


def test_eject_with_extras_queued_starts_no_extra(config, setup, monkeypatch):
    services, rip = scanned(config, setup)
    extra = queue_extra(setup, rip)
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", "hang")

    async def main():
        r = runner.JobRunner(config, Notifier(), Space(), services=services)
        services.runner = r
        r.sync_workers({"serial:A"})
        while jobs.get(setup, rip)["state"] != "running" or not runner.LIVE_PROCESSES:
            await asyncio.sleep(0.05)
        await services.cancel_for_eject("serial:A")
        await asyncio.sleep(0.5)  # give the worker a chance to (wrongly) start the extra
        await r.stop()

    asyncio.run(main())
    assert jobs.get(setup, rip)["state"] == "cancelled"
    assert jobs.get(setup, extra)["state"] == "cancelled" and jobs.get(setup, extra)["started_at"] is None
    assert rip_state(setup, rip)["state"] == "failed"


# --- the move's space check counts other running rips (review finding #4) --------------

def test_move_space_check_subtracts_other_running_rips(config, setup, monkeypatch):
    from disc_ripper import settings

    services, rip = scanned(config, setup)
    settings.save(setup, {"cache_min_free_gb": 1})
    other = jobs.enqueue(setup, "serial:A", "rip_title", {"rip_id": 999, "title": 0}, now="t",
                         needs_bytes=10 * 1024**3)
    with setup:  # a rip on another drive, admitted by the space check, that has barely started
        setup.execute("UPDATE jobs SET state = 'running' WHERE id = ?", (other,))
    (config.work_dir / str(other)).mkdir()
    (config.work_dir / str(other) / "part.mkv").write_bytes(b"x" * 1024)
    monkeypatch.setattr(videofiles, "STATVFS", shortfall(6))  # 6 GiB free: enough, unless the other rip grows
    row = run_until_done(config, setup, services, rip)
    assert row["state"] == "done"
    state = rip_state(setup, rip)
    assert state["state"] == "move_failed" and "other rips" in state["message"]
    assert (config.work_dir / str(rip) / "title_t00.mkv").is_file()
    with setup:  # the other rip finished
        setup.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (other,))
    assert asyncio.run(services.move_again(rip)) is None
    assert rip_state(setup, rip)["state"] == "ripped"


def test_move_to_downloads_reserves_bytes(config):
    src = config.work_dir / "5" / "t.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 10)
    with pytest.raises(OSError, match="other rips"):
        videofiles.move_to_downloads(config, 7, "main", src, statvfs=shortfall(3), min_free_gb=1,
                                     reserved_bytes=2 * 1024**3)
    assert videofiles.move_to_downloads(config, 7, "main", src, statvfs=shortfall(3), min_free_gb=1,
                                        reserved_bytes=1024**3).is_file()


def test_outstanding_bytes_of_other_running_jobs(config, setup):
    a = jobs.enqueue(setup, "serial:A", "rip_title", {}, needs_bytes=100, now="t")
    b = jobs.enqueue(setup, "serial:A", "rip_title", {}, needs_bytes=50, now="t")
    me = jobs.enqueue(setup, "serial:A", "rip_title", {}, needs_bytes=70, now="t")
    with setup:
        setup.execute("UPDATE jobs SET state = 'running'")
    for job, size in ((a, 30), (b, 80)):  # b already wrote more than it asked for: floored at 0
        (config.work_dir / str(job)).mkdir()
        (config.work_dir / str(job) / "f").write_bytes(b"x" * size)
    assert videorip.outstanding_bytes(setup, config.work_dir, me) == 70
    assert videorip.outstanding_bytes(setup, config.work_dir, None) == 70 + 70


def test_parse_real_ffprobe_silent_title():
    info = videofiles.parse_ffprobe((FIXTURES / "ffprobe_bluray_real.json").read_text())
    assert info == videofiles.MkvInfo(seconds=340.298, video=1, audio=0, dar=pytest.approx(16 / 9))


def test_check_output_silent_title_only_when_disc_says_so(tmp_path):
    stub_mkv(tmp_path, "t.mkv", 340, audio=0)
    assert videofiles.check_output(tmp_path, 340, expected_audio=0).name == "t.mkv"
    with pytest.raises(RuntimeError, match="no audio"):
        videofiles.check_output(tmp_path, 340, expected_audio=3)
    with pytest.raises(RuntimeError, match="no audio"):
        videofiles.check_output(tmp_path, 340)  # unknown: still required
