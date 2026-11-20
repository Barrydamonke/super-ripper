import asyncio
import json
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
    assert info == videofiles.MkvInfo(seconds=6730.04, video=1, audio=2)


def test_attempts_feed_should_eject(config, setup):
    services, rip = scanned(config, setup)
    assert [a[:2] for a in video_rips.attempts(setup, rip)] == [(0, rip)]
    assert not titles.should_eject(video_rips.attempts(setup, rip))
