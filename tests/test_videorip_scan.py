import asyncio
import json
import math
import sys
from pathlib import Path

import pytest

from disc_ripper import db, drives, jobs, runner, settings, video_rips, videorip
from disc_ripper.services import Services

FAKE = Path(__file__).parent / "fixtures" / "makemkv" / "fake_makemkvcon.py"


class Notifier:
    async def drive_changed(self, drive_id):
        pass

    async def guard_changed(self):
        pass


class Space:
    """Rips need space that never comes, so queued rip_title rows stay put."""

    def check(self, needs_bytes, *args):
        return "no space" if needs_bytes else None

    def invalidate(self):
        pass


@pytest.fixture
def setup(config, add_sg, monkeypatch):
    monkeypatch.setenv("MAKEMKVCON_BIN", json.dumps([sys.executable, str(FAKE)]))
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=b"\x05\x80\x00\x01A")
    conn = db.connect(config.db_path)
    db.migrate(conn)
    drives.sync(conn, drives.discover(config.sysfs_root), "t0")
    drives.set_media(conn, "serial:A", "disc", "dvd", "FILM_LABEL", "t1")
    yield conn
    conn.close()


def scan_job(conn, from_insert=True):
    return jobs.enqueue(
        conn, "serial:A", "scan_video",
        {"sr_name": "sr0", "disc_type": "dvd", "label": "FILM_LABEL", "from_insert": from_insert},
        uses_drive=True, now="t",
    )


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


def finished(conn, job_id):
    return lambda: jobs.get(conn, job_id)["state"] in ("done", "failed", "cancelled")


def rip_jobs(conn):
    return conn.execute("SELECT * FROM jobs WHERE kind = 'rip_title'").fetchall()


def test_scan_saves_titles_and_autostarts(config, setup):
    j = scan_job(setup, from_insert=True)
    # auto_start is on by default; the insert path counts as a fresh insert
    run(config, Services(config), finished(setup, j))
    assert jobs.get(setup, j)["state"] == "done", jobs.get(setup, j)["error"]
    disc = video_rips.disc_in_drive(setup, "serial:A")
    assert disc is not None and json.loads(disc["held"]) == []
    [rip] = rip_jobs(setup)
    params = json.loads(rip["params"])
    assert (params["role"], params["title"], params["minlength"]) == ("main", 0, 120)
    assert params["rip_id"] == rip["id"] and params["fingerprint"] == disc["fingerprint"]
    assert rip["needs_bytes"] == math.ceil(7516192768 * 1.02)
    row = setup.execute("SELECT * FROM video_rips WHERE rip_id = ?", (rip["id"],)).fetchone()
    assert row["state"] == "ripping" and row["main_title"] == 0


def test_scan_decoy_is_held(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_MAKEMKV_INFO", "info_bluray_decoy.txt")
    j = scan_job(setup)
    run(config, Services(config), finished(setup, j))
    disc = video_rips.disc_in_drive(setup, "serial:A")
    assert any("same length" in r for r in json.loads(disc["held"]))
    assert rip_jobs(setup) == []


def test_scan_from_present_never_autostarts(config, setup):
    j = scan_job(setup, from_insert=False)
    run(config, Services(config), finished(setup, j))
    disc = video_rips.disc_in_drive(setup, "serial:A")
    assert "disc was in the drive at start-up" in json.loads(disc["held"])
    assert rip_jobs(setup) == []


def test_scan_too_old_shows_update_hint(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", "fail_old")
    j = scan_job(setup)
    run(config, Services(config), finished(setup, j))
    row = jobs.get(setup, j)
    assert row["state"] == "failed"
    assert "too old" in row["error"] and "Update MakeMKV" in row["error"]
    assert rip_jobs(setup) == []


def test_cancel_scan_queues_nothing(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", "hang")
    j = scan_job(setup)

    async def cancel_later(r):
        while jobs.get(setup, j)["state"] != "running" or not runner.LIVE_PROCESSES:
            await asyncio.sleep(0.05)
        await r.cancel(j)

    run(config, Services(config), finished(setup, j), after_start=cancel_later)
    assert jobs.get(setup, j)["state"] == "cancelled"
    assert not runner.LIVE_PROCESSES
    assert rip_jobs(setup) == []


def test_hold_during_scan(config, setup):
    services = Services(config)
    j = scan_job(setup)

    async def hold_first(r):
        await services.hold_video("serial:A")

    run(config, services, finished(setup, j), after_start=hold_first)
    disc = video_rips.disc_in_drive(setup, "serial:A")
    assert "Hold pressed" in json.loads(disc["held"])
    assert rip_jobs(setup) == []


def test_hold_cancels_queued_main_rip(config, setup):
    services = Services(config)
    j = scan_job(setup)
    run(config, services, lambda: rip_jobs(setup) != [] or jobs.get(setup, j)["state"] == "failed")
    asyncio.run(services.hold_video("serial:A"))
    [rip] = rip_jobs(setup)
    assert rip["state"] == "cancelled"
    assert setup.execute("SELECT video_hold FROM drives").fetchone()[0] == 1


def test_enqueue_video_scan_once_at_a_time(config, setup):
    services = Services(config)
    first = asyncio.run(services.enqueue_video_scan("serial:A", from_insert=True))
    assert first is not None
    assert asyncio.run(services.enqueue_video_scan("serial:A", from_insert=False)) is None


def test_settings_conf_written_with_key(config, setup):
    with setup:
        setup.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('makemkv_key', ?)",
                      (json.dumps("T-SECRET"),))
    j = scan_job(setup, from_insert=False)
    run(config, Services(config), finished(setup, j))
    conf = config.config_dir / "makemkv" / ".MakeMKV" / "settings.conf"
    assert "T-SECRET" in conf.read_text()
    assert (config.work_dir / str(j) / "env_home.txt").read_text() == str(config.config_dir / "makemkv")


def test_write_settings_conf_blank_selection_uses_default(tmp_path):
    videorip.write_settings_conf(tmp_path, "", "  ")
    text = (tmp_path / ".MakeMKV" / "settings.conf").read_text()
    assert settings.DEFAULT_VIDEO_SELECTION in text
