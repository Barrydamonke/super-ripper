import asyncio
import errno

import pytest

from disc_ripper import db, drives, jobs, media

SERIAL_A = b"\x05\x80\x00\x01A"
SERIAL_B = b"\x05\x80\x00\x01B"


class FakeDrive:
    def __init__(self, statuses, probe_result=("dvd", "MOVIE")):
        self.statuses = list(statuses)
        self.probe_result = probe_result
        self.status_calls = 0

    def drive_status(self):
        self.status_calls += 1
        value = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(value, Exception):
            raise value
        return value


class RecordingNotifier:
    def __init__(self):
        self.drives = []
        self.guard = 0

    async def drive_changed(self, drive_id):
        self.drives.append(drive_id)

    async def guard_changed(self):
        self.guard += 1


@pytest.fixture
def setup(config, add_sg):
    add_sg(config.sysfs_root, 1, sr=0, vpd_serial=SERIAL_A)
    add_sg(config.sysfs_root, 2, sr=1, vpd_serial=SERIAL_B)
    conn = db.connect(config.db_path)
    db.migrate(conn)
    drives.sync(conn, drives.discover(config.sysfs_root), "t0")
    yield conn
    conn.close()


def monitor(config, fakes, notifier, inserts=None, monkeypatch=None):
    def factory(path):
        return fakes[path.name]

    async def on_insert(drive_id):
        inserts.append(drive_id)

    m = media.MediaMonitor(
        config, notifier, drive_factory=factory, on_insert=on_insert if inserts is not None else None
    )
    m.probe = lambda drive: drive.probe_result  # discprobe.probe is tested on its own
    return m


def row(conn, drive_id):
    return conn.execute("SELECT * FROM drives WHERE id = ?", (drive_id,)).fetchone()


def test_insert_probes_and_notifies(config, setup):
    fakes = {"sr0": FakeDrive(["empty", "disc"]), "sr1": FakeDrive(["empty"])}
    notifier, inserts = RecordingNotifier(), []
    m = monitor(config, fakes, notifier, inserts)

    asyncio.run(m.poll_once(first=True))
    assert row(setup, "serial:A")["media_state"] == "empty"
    asyncio.run(m.poll_once())
    r = row(setup, "serial:A")
    assert (r["media_state"], r["disc_type"], r["disc_label"]) == ("disc", "dvd", "MOVIE")
    assert inserts == ["serial:A"]
    assert notifier.drives == ["serial:A", "serial:B", "serial:A"]


def test_first_poll_does_not_fire_insert(config, setup):
    fakes = {"sr0": FakeDrive(["disc"]), "sr1": FakeDrive(["empty"])}
    inserts = []
    m = monitor(config, fakes, RecordingNotifier(), inserts)
    asyncio.run(m.poll_once(first=True))
    assert row(setup, "serial:A")["disc_type"] == "dvd"
    assert inserts == []


def test_eject_clears_type_and_label(config, setup):
    fakes = {"sr0": FakeDrive(["disc", "empty"]), "sr1": FakeDrive(["empty"])}
    m = monitor(config, fakes, RecordingNotifier())
    asyncio.run(m.poll_once(first=True))
    asyncio.run(m.poll_once())
    r = row(setup, "serial:A")
    assert (r["media_state"], r["disc_type"], r["disc_label"]) == ("empty", None, "")


def test_no_notify_when_unchanged(config, setup):
    fakes = {"sr0": FakeDrive(["empty"]), "sr1": FakeDrive(["empty"])}
    notifier = RecordingNotifier()
    m = monitor(config, fakes, notifier)
    asyncio.run(m.poll_once(first=True))
    asyncio.run(m.poll_once())
    assert notifier.drives == ["serial:A", "serial:B"]


def test_poll_survives_drive_error(config, setup):
    fakes = {"sr0": FakeDrive([OSError(errno.EIO, "I/O error")]), "sr1": FakeDrive(["disc"])}
    m = monitor(config, fakes, RecordingNotifier())
    asyncio.run(m.poll_once(first=True))
    assert row(setup, "serial:A")["media_state"] == "unknown"
    assert row(setup, "serial:B")["media_state"] == "disc"


def test_probe_error_gives_unknown_type(config, setup):
    fakes = {"sr0": FakeDrive(["disc"]), "sr1": FakeDrive(["empty"])}
    m = monitor(config, fakes, RecordingNotifier())

    def broken(drive):
        raise RuntimeError("probe blew up")

    m.probe = broken
    asyncio.run(m.poll_once(first=True))
    r = row(setup, "serial:A")
    assert (r["media_state"], r["disc_type"]) == ("disc", "unknown")


def test_skips_drive_with_running_uses_drive_job(config, setup):
    job_id = jobs.enqueue(setup, "serial:A", "test", {}, uses_drive=True, now="t")
    setup.execute("UPDATE jobs SET state = 'running' WHERE id = ?", (job_id,))
    setup.commit()
    fakes = {"sr0": FakeDrive(["disc"]), "sr1": FakeDrive(["empty"])}
    m = monitor(config, fakes, RecordingNotifier())
    asyncio.run(m.poll_once(first=True))
    assert fakes["sr0"].status_calls == 0
    assert fakes["sr1"].status_calls == 1
    assert jobs.running_uses_drive(setup, "serial:A") is True
    assert jobs.running_uses_drive(setup, "serial:B") is False


def test_disabled_or_absent_drive_not_polled(config, setup):
    drives.update_drive(setup, "serial:A", label="", enabled=False)
    setup.execute("UPDATE drives SET present = 0 WHERE id = 'serial:B'")
    setup.commit()
    fakes = {"sr0": FakeDrive(["disc"]), "sr1": FakeDrive(["disc"])}
    m = monitor(config, fakes, RecordingNotifier())
    asyncio.run(m.poll_once(first=True))
    assert fakes["sr0"].status_calls == 0
    assert fakes["sr1"].status_calls == 0


def test_drive_lock_is_shared(config):
    m = media.MediaMonitor(config, RecordingNotifier())
    assert m.drive_lock("serial:A") is m.drive_lock("serial:A")
    assert m.drive_lock("serial:A") is not m.drive_lock("serial:B")
