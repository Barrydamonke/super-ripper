import asyncio
import json

import pytest

from disc_ripper import cd_rips, db, drives, jobs, media, services, settings
from disc_ripper.cdtoc import Toc, musicbrainz_disc_id

TOC = Toc(first=1, last=3, offsets=(0, 15750, 31500), leadout=47250, data_tracks=frozenset())
HOUR = Toc(first=1, last=1, offsets=(0,), leadout=3600 * 75, data_tracks=frozenset())
DISC = musicbrainz_disc_id(TOC)


class FakeDrive:
    def __init__(self, toc):
        self.toc = toc

    def read_toc(self):
        return self.toc


@pytest.fixture
def svc(config, add_sg):
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=b"\x05\x80\x00\x01A")
    conn = db.connect(config.db_path)
    db.migrate(conn)
    drives.sync(conn, drives.discover(config.sysfs_root), "t0")
    drives.set_media(conn, "serial:A", "disc", "audio", "", "t0")
    s = services.Services(config, drive_factory=lambda path: FakeDrive(TOC))
    s.conn = conn
    yield s
    conn.close()


def rip_jobs(conn):
    return conn.execute("SELECT * FROM jobs WHERE kind = 'rip_cd' ORDER BY id").fetchall()


def test_insert_autostarts_rip(svc):
    asyncio.run(svc.on_insert("serial:A"))
    [job] = rip_jobs(svc.conn)
    params = json.loads(job["params"])
    assert params["disc_id"] == DISC
    assert params["sr_name"] == "sr0"
    assert params["offset"] == 0
    assert Toc.from_json(params["toc"]) == TOC
    assert (job["uses_drive"], job["state"]) == (1, "queued")
    assert cd_rips.get(svc.conn, job["id"])["state"] == "ripping"
    disc = svc.conn.execute("SELECT * FROM discs WHERE disc_id = ?", (DISC,)).fetchone()
    assert (disc["track_count"], disc["drive_id"]) == (3, "serial:A")


def test_insert_ignores_non_audio(svc):
    drives.set_media(svc.conn, "serial:A", "disc", "dvd", "MOVIE", "t1")
    asyncio.run(svc.on_insert("serial:A"))
    assert rip_jobs(svc.conn) == []


def _finished_rip(conn, state, cd_state="ripping"):
    j = jobs.enqueue(conn, "serial:A", "rip_cd", {"disc_id": DISC}, uses_drive=True, now="t")
    cd_rips.ensure(conn, j, DISC, "t")
    jobs.start(conn, j, "t")
    jobs.finish(conn, j, state, "t")
    if cd_state != "ripping":
        cd_rips.update(conn, j, state=cd_state, summary="Nirvana — Nevermind (1991)")
    return j


def test_insert_does_not_restart_filed_disc(svc):
    _finished_rip(svc.conn, "done", "filed")
    asyncio.run(svc.on_insert("serial:A"))
    assert len(rip_jobs(svc.conn)) == 1


def test_insert_does_not_restart_disc_waiting_for_a_look(svc):
    _finished_rip(svc.conn, "done", "needs_look")
    asyncio.run(svc.on_insert("serial:A"))
    assert len(rip_jobs(svc.conn)) == 1


def test_insert_after_failed_rip_autostarts(svc):
    _finished_rip(svc.conn, "failed")
    asyncio.run(svc.on_insert("serial:A"))
    assert len(rip_jobs(svc.conn)) == 2


def test_autostart_off(svc):
    settings.save(svc.conn, {"auto_start_rips": False})
    asyncio.run(svc.on_insert("serial:A"))
    assert rip_jobs(svc.conn) == []
    assert svc.conn.execute("SELECT COUNT(*) FROM discs").fetchone()[0] == 1  # still remembered


def test_needs_bytes_estimate(svc):
    job_id = asyncio.run(svc.enqueue_cd_rip("serial:A", HOUR))
    assert jobs.get(svc.conn, job_id)["needs_bytes"] >= 476_000_000


def test_offset_comes_from_drive(svc):
    drives.update_drive(svc.conn, "serial:A", label="", enabled=True, read_offset=667)
    job_id = asyncio.run(svc.enqueue_cd_rip("serial:A", TOC))
    assert json.loads(jobs.get(svc.conn, job_id)["params"])["offset"] == 667


class Notifier:
    def __init__(self):
        self.calls = []

    async def drive_changed(self, drive_id):
        self.calls.append(drive_id)

    async def guard_changed(self):
        pass


class ScriptedDrive:
    def __init__(self, statuses):
        self.statuses = statuses

    def drive_status(self):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


def test_glitch_unknown_to_disc_no_insert(config, svc):
    inserts = []

    async def on_insert(drive_id):
        inserts.append(drive_id)

    fake = ScriptedDrive(["disc", "unknown", "disc", "empty", "empty", "disc"])
    m = media.MediaMonitor(config, Notifier(), drive_factory=lambda p: fake, on_insert=on_insert)
    m.probe = lambda drive: ("audio", "")

    async def polls():
        await m.poll_once(first=True)
        for _ in range(5):
            await m.poll_once()

    asyncio.run(polls())
    assert inserts == ["serial:A"]  # only the real empty → disc change


def test_first_poll_remembers_present_disc(config, svc):
    async def run():
        svc.monitor.probe = lambda drive: ("audio", "")

        class D(FakeDrive):
            def drive_status(self):
                return "disc"

        svc.drive_factory = lambda path: D(TOC)
        await svc.monitor.poll_once(first=True)
        await asyncio.sleep(0.2)

    asyncio.run(run())
    assert cd_rips.disc_in_drive(svc.conn, "serial:A")["disc_id"] == DISC
    assert rip_jobs(svc.conn) == []  # no auto-start for a disc that was already in
