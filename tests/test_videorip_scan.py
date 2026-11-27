import asyncio
import json
import math
import os
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


def scan_job(conn, from_insert=True, disc_type="dvd"):
    return jobs.enqueue(
        conn, "serial:A", "scan_video",
        {"sr_name": "sr0", "disc_type": disc_type, "label": "FILM_LABEL", "from_insert": from_insert},
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
    assert params["expected_audio"] == 2  # the scan listed two audio streams for title 0
    assert rip["needs_bytes"] == math.ceil(7516192768 * 1.02)
    row = setup.execute("SELECT * FROM video_rips WHERE rip_id = ?", (rip["id"],)).fetchone()
    assert row["state"] == "ripping" and row["main_title"] == 0


def test_scan_decoy_is_held(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_MAKEMKV_INFO", "info_bluray_decoy.txt")
    j = scan_job(setup, disc_type="bluray")
    services = Services(config)
    services.dvd_decoy_wait = 0.05
    run(config, services, finished(setup, j))
    disc = video_rips.disc_in_drive(setup, "serial:A")
    [reason] = json.loads(disc["held"])
    assert "same length" in reason and "rips at" not in reason
    assert not services._dvd_waits and rip_jobs(setup) == []  # a Blu-ray waits for the owner


def _dvd_decoy(config, setup, monkeypatch, during_wait=None):
    """A DVD whose two long titles are within 3%: held, then the longest rips after the wait."""
    monkeypatch.setenv("FAKE_MAKEMKV_INFO", "info_bluray_decoy.txt")
    j = scan_job(setup, disc_type="dvd")
    services = Services(config)
    services.dvd_decoy_wait = 0.3
    seen = {}

    def until():
        if jobs.get(setup, j)["state"] not in ("done", "failed", "cancelled"):
            return False
        if "disc" not in seen:
            seen["disc"] = video_rips.disc_in_drive(setup, "serial:A")
            assert services._dvd_waits, "no wait was scheduled"
            if during_wait:
                during_wait()
        return not services._dvd_waits

    run(config, services, until)
    disc = seen["disc"]
    [reason] = json.loads(disc["held"])
    assert "same length" in reason and "rips at" in reason
    return disc


def test_dvd_decoy_rips_longest_after_wait(config, setup, monkeypatch):
    disc = _dvd_decoy(config, setup, monkeypatch)
    [rip] = rip_jobs(setup)
    longest = max(json.loads(disc["scan"])["titles"], key=lambda t: (t["seconds"], t["size"]))
    params = json.loads(rip["params"])
    assert (params["role"], params["title"]) == ("main", longest["number"])


def test_dvd_decoy_wait_respects_hold(config, setup, monkeypatch):
    def hold():
        setup.execute("UPDATE drives SET video_hold = 1")
        setup.commit()
    _dvd_decoy(config, setup, monkeypatch, during_wait=hold)
    assert rip_jobs(setup) == []


def test_dvd_decoy_wait_respects_eject(config, setup, monkeypatch):
    def eject():
        drives.set_media(setup, "serial:A", "empty", None, "", "t2")
    _dvd_decoy(config, setup, monkeypatch, during_wait=eject)
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
    assert rip_jobs(setup) == []
    assert setup.execute("SELECT COUNT(*) FROM video_rips").fetchone()[0] == 0
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
    assert (config.work_dir / "env_home.txt").read_text() == str(config.config_dir / "makemkv")


def test_write_settings_conf_blank_selection_uses_default(tmp_path):
    videorip.write_settings_conf(tmp_path, "", "  ")
    text = (tmp_path / ".MakeMKV" / "settings.conf").read_text()
    assert settings.DEFAULT_VIDEO_SELECTION in text


def test_autostart_rechecks_hold_and_cancel(config, setup):
    services = Services(config)
    j = scan_job(setup)
    run(config, services, finished(setup, j), after_start=None)  # scan done; disc saved, rip queued
    fp = video_rips.disc_in_drive(setup, "serial:A")["fingerprint"]
    with setup:
        setup.execute("DELETE FROM jobs WHERE kind = 'rip_title'")
        setup.execute("UPDATE jobs SET state = 'running' WHERE id = ?", (j,))
    # Hold pressed after the scan decided
    with setup:
        setup.execute("UPDATE drives SET video_hold = 1")
    assert services._db(services._start_video_rip, "serial:A", fp, 0, j) is None
    # Scan cancelled after it decided
    with setup:
        setup.execute("UPDATE drives SET video_hold = 0")
        setup.execute("UPDATE jobs SET state = 'cancelled' WHERE id = ?", (j,))
    assert services._db(services._start_video_rip, "serial:A", fp, 0, j) is None
    assert rip_jobs(setup) == []
    # Still running and not held: it queues; a manual start (no scan id) skips the check
    with setup:
        setup.execute("UPDATE jobs SET state = 'running' WHERE id = ?", (j,))
    assert services._db(services._start_video_rip, "serial:A", fp, 0, j) is not None
    with setup:
        setup.execute("DELETE FROM jobs WHERE kind = 'rip_title'")  # the one queued above would block
        setup.execute("UPDATE drives SET video_hold = 1")
    assert services._db(services._start_video_rip, "serial:A", fp, 0) is not None


@pytest.mark.parametrize("mode", ["ok", "fail_old"])
def test_scan_leaves_no_work_folder(config, setup, monkeypatch, mode):
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", mode)
    j = scan_job(setup, from_insert=False)
    run(config, Services(config), finished(setup, j))
    assert jobs.get(setup, j)["state"] == ("done" if mode == "ok" else "failed")
    assert not (config.work_dir / str(j)).exists()


def test_scan_stall_fails_with_a_clear_message(config, setup, monkeypatch):
    monkeypatch.setenv("FAKE_MAKEMKV_MODE", "hang")
    monkeypatch.setattr(videorip, "STALL_UNIT_SECONDS", 0.05)  # 10 "minutes" = 0.5 s
    j = scan_job(setup)
    run(config, Services(config), finished(setup, j))
    row = jobs.get(setup, j)
    assert row["state"] == "failed"
    assert "MakeMKV stopped responding" in row["error"] and "Retry" in row["error"]
    assert not runner.LIVE_PROCESSES
    assert rip_jobs(setup) == []
    log_text = (config.config_dir / "logs" / f"job-{j}.log").read_text(encoding="utf-8")
    assert "makemkvcon" in log_text and "no output for" in log_text


def test_settings_conf_is_written_via_temp_file_and_replaced(tmp_path, monkeypatch):
    path = tmp_path / ".MakeMKV" / "settings.conf"
    videorip.write_settings_conf(tmp_path, "OLD", "")
    seen = []
    real_replace = os.replace

    def replace(src, dst):
        seen.append((Path(src).name, Path(src).read_text(), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(videorip.os, "replace", replace)
    videorip.write_settings_conf(tmp_path, "NEW", "")
    assert len(seen) == 1
    name, content, dst = seen[0]
    assert dst == path and name != path.name and "NEW" in content  # complete before it was swapped in
    assert path.read_text() == content and "OLD" not in content
    assert [p.name for p in path.parent.iterdir()] == ["settings.conf"]  # temp file gone


def test_settings_conf_failed_write_keeps_old_file(tmp_path, monkeypatch):
    videorip.write_settings_conf(tmp_path, "OLD", "")
    path = tmp_path / ".MakeMKV" / "settings.conf"
    monkeypatch.setattr(videorip.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        videorip.write_settings_conf(tmp_path, "NEW", "")
    assert "OLD" in path.read_text()
    assert [p.name for p in path.parent.iterdir()] == ["settings.conf"]


def test_stale_temp_settings_files_are_removed(tmp_path):
    folder = tmp_path / ".MakeMKV"
    folder.mkdir()
    (folder / "settings.conf.123.456.tmp").write_text("junk")
    videorip.write_settings_conf(tmp_path, "K", "")
    assert [p.name for p in folder.iterdir()] == ["settings.conf"]
