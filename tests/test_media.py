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
    fakes = {"sr0": FakeDrive(["disc", "empty", "empty"]), "sr1": FakeDrive(["empty"])}
    m = monitor(config, fakes, RecordingNotifier())
    asyncio.run(m.poll_once(first=True))
    asyncio.run(m.poll_once())
    asyncio.run(m.poll_once())  # empty must repeat before it counts as an eject
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


def test_busy_drive_lock_does_not_block_other_drives(config, setup):
    import threading
    import time

    fakes = {"sr0": FakeDrive(["disc"]), "sr1": FakeDrive(["disc"])}
    m = monitor(config, fakes, RecordingNotifier())
    lock = m.drive_lock("serial:A")
    lock.acquire()  # e.g. a probe thread stuck on a damaged disc
    try:
        start = time.monotonic()
        asyncio.run(m.poll_once(first=True))
        assert time.monotonic() - start < 1.0
    finally:
        lock.release()
    assert fakes["sr0"].status_calls == 0
    assert row(setup, "serial:B")["media_state"] == "disc"
    assert row(setup, "serial:A")["media_state"] == "unknown"  # untouched


def test_hung_status_call_times_out(config, setup):
    import time

    class Hung(FakeDrive):
        def drive_status(self):
            time.sleep(1.0)
            return "disc"

    fakes = {"sr0": Hung(["disc"]), "sr1": FakeDrive(["empty"])}
    m = media.MediaMonitor(config, RecordingNotifier(), drive_factory=lambda p: fakes[p.name], status_timeout=0.2)
    m.probe = lambda drive: drive.probe_result
    async def timed():
        start = time.monotonic()
        await m.poll_once(first=True)
        return time.monotonic() - start  # (asyncio.run then waits for the stuck thread)

    assert asyncio.run(timed()) < 0.9
    assert row(setup, "serial:B")["media_state"] == "empty"


def test_monitor_survives_errors_in_first_poll(config, setup):
    fakes = {"sr0": FakeDrive(["empty", "disc"]), "sr1": FakeDrive(["empty"])}

    class Flaky(RecordingNotifier):
        calls = 0

        async def drive_changed(self, drive_id):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("template blew up")
            await super().drive_changed(drive_id)

    notifier = Flaky()
    m = media.MediaMonitor(config, notifier, drive_factory=lambda p: fakes[p.name], interval=0.01)
    m.probe = lambda drive: drive.probe_result

    async def main():
        task = asyncio.create_task(m.run())
        await asyncio.sleep(0.3)
        assert not task.done()
        task.cancel()

    asyncio.run(main())
    # Drive B was still handled in the first round, and A's insert was seen later.
    assert row(setup, "serial:B")["media_state"] == "empty"
    assert row(setup, "serial:A")["media_state"] == "disc"


def test_disc_leaving_fires_on_remove(config, setup):
    fakes = {"sr0": FakeDrive(["disc", "disc", "empty", "empty"]), "sr1": FakeDrive(["empty"])}
    removed = []

    async def on_remove(drive_id):
        removed.append(drive_id)

    m = media.MediaMonitor(config, RecordingNotifier(), drive_factory=lambda p: fakes[p.name], on_remove=on_remove)
    m.probe = lambda drive: drive.probe_result
    asyncio.run(m.poll_once(first=True))  # sr1 empty at start-up: nothing left
    asyncio.run(m.poll_once())
    assert removed == []
    asyncio.run(m.poll_once())  # sr0's disc came out
    asyncio.run(m.poll_once())
    assert removed == ["serial:A"]


def _removals(config, statuses):
    fakes = {"sr0": FakeDrive(statuses), "sr1": FakeDrive(["empty"])}
    removed = []

    async def on_remove(drive_id):
        removed.append(drive_id)

    m = media.MediaMonitor(config, RecordingNotifier(), drive_factory=lambda p: fakes[p.name], on_remove=on_remove)
    m.probe = lambda drive: drive.probe_result
    asyncio.run(m.poll_once(first=True))
    for _ in range(len(statuses) - 1):
        asyncio.run(m.poll_once())
    return removed


def test_read_glitch_does_not_count_as_removal(config, setup):
    # A drive that briefly can't report (a failed status call) still has the same disc in.
    assert _removals(config, ["disc", "unknown", "disc", "disc"]) == []


def test_glitch_keeps_disc_state_and_no_second_insert(config, setup):
    fakes = {"sr0": FakeDrive(["disc", "unknown", "disc"]), "sr1": FakeDrive(["empty"])}
    inserts = []
    m = monitor(config, fakes, RecordingNotifier(), inserts)
    asyncio.run(m.poll_once(first=True))
    asyncio.run(m.poll_once())
    assert row(setup, "serial:A")["media_state"] == "disc"
    asyncio.run(m.poll_once())
    assert inserts == [] and fakes["sr0"].status_calls == 3


def test_removal_after_glitch_still_fires(config, setup):
    assert _removals(config, ["disc", "unknown", "tray_open", "empty"]) == ["serial:A"]


def test_timeout_does_not_count_as_removal(config, setup):
    assert _removals(config, ["disc", TimeoutError(), "disc"]) == []


def test_one_transient_not_ready_is_not_a_removal(config, setup):
    assert _removals(config, ["disc", "not_ready", "disc", "disc"]) == []
    assert _removals(config, ["disc", "empty", "disc", "disc"]) == []


def test_transient_count_resets_after_disc_seen(config, setup):
    assert _removals(config, ["disc", "not_ready", "disc", "not_ready", "disc"]) == []


def test_two_not_ready_in_a_row_count_as_removal(config, setup):
    assert _removals(config, ["disc", "not_ready", "not_ready"]) == ["serial:A"]
    assert _removals(config, ["disc", "not_ready", "empty"]) == ["serial:A"]


def test_tray_open_removal_is_immediate(config, setup):
    assert _removals(config, ["disc", "tray_open"]) == ["serial:A"]


def _monitor(config, statuses, probes=None):
    """A monitor on sr0 with scripted statuses, a fake clock and removal/insert records."""
    fakes = {"sr0": FakeDrive(statuses), "sr1": FakeDrive(["empty"])}
    events = []

    async def on_remove(drive_id):
        events.append("remove")

    async def on_insert(drive_id):
        events.append("insert")

    m = media.MediaMonitor(config, RecordingNotifier(), drive_factory=lambda p: fakes[p.name],
                           on_remove=on_remove, on_insert=on_insert)
    probes = list(probes or [("dvd", "MOVIE")])
    m.probe = lambda drive: probes.pop(0) if len(probes) > 1 else probes[0]
    m.now = 0.0
    m._clock = lambda: m.now
    return m, events


def _poll(m, first=False, advance=3.0):
    m.now += advance
    asyncio.run(m.poll_once(first=first))


def test_swap_within_debounce_is_remove_and_insert(config, setup):
    m, events = _monitor(config, ["disc", "not_ready", "disc"], [("dvd", "OLD"), ("dvd", "NEW")])
    _poll(m, first=True)
    _poll(m)
    assert events == []
    _poll(m)
    assert events == ["remove", "insert"]
    r = row(setup, "serial:A")
    assert (r["media_state"], r["disc_type"], r["disc_label"]) == ("disc", "dvd", "NEW")


def test_same_disc_back_within_debounce_is_quiet(config, setup):
    m, events = _monitor(config, ["disc", "not_ready", "disc", "disc"], [("dvd", "MOVIE")])
    _poll(m, first=True)
    _poll(m)
    _poll(m)
    _poll(m)
    assert events == []
    assert row(setup, "serial:A")["disc_label"] == "MOVIE"


def test_transients_far_apart_are_not_a_removal(config, setup):
    m, events = _monitor(config, ["disc", "not_ready", "disc", "disc", "not_ready"], [("dvd", "MOVIE")])
    _poll(m, first=True)
    _poll(m)
    _poll(m)
    _poll(m, advance=60)
    _poll(m, advance=60)  # a new first transient, long after the old one
    assert events == []


def test_two_transients_too_far_apart_are_not_a_removal(config, setup):
    m, events = _monitor(config, ["disc", "not_ready", "not_ready"], [("dvd", "MOVIE")])
    _poll(m, first=True)
    _poll(m)
    _poll(m, advance=60)  # more than the window later
    assert events == []
    _poll(m)  # and one more, now within the window of the second
    assert events == ["remove"]


def test_unknown_between_transients_breaks_the_run(config, setup):
    m, events = _monitor(config, ["disc", "not_ready", "unknown", "not_ready"], [("dvd", "MOVIE")])
    _poll(m, first=True)
    _poll(m)
    _poll(m)
    _poll(m)
    assert events == []


def test_skipped_poll_while_a_job_holds_the_drive_breaks_the_run(config, setup):
    m, events = _monitor(config, ["disc", "not_ready", "not_ready"], [("dvd", "MOVIE")])
    _poll(m, first=True)
    _poll(m)  # not_ready before the title job
    j = jobs.enqueue(setup, "serial:A", "rip_title", {}, now="t")
    with setup:
        setup.execute("UPDATE jobs SET state = 'running', uses_drive = 1 WHERE id = ?", (j,))
    _poll(m)  # skipped: the drive is in use
    with setup:
        setup.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (j,))
    _poll(m)  # not_ready after it: a fresh first reading, not a removal
    assert events == []


def test_reprobe_unknown_after_blip_is_same_disc(config, setup):
    m, events = _monitor(config, ["disc", "not_ready", "disc", "disc"], [("dvd", "MOVIE"), ("unknown", "")])
    _poll(m, first=True)
    _poll(m)
    _poll(m)
    _poll(m)
    assert events == []
    assert row(setup, "serial:A")["disc_label"] == "MOVIE"


def test_busy_reprobe_keeps_the_swap_check_for_next_poll(config, setup):
    m, events = _monitor(config, ["disc", "not_ready", "disc", "disc"], [("dvd", "OLD"), ("dvd", "NEW")])
    real = m._locked
    busy = [True]

    def locked(target, fn):
        if busy and fn == m.probe and busy.pop():
            return media._BUSY
        return real(target, fn)

    _poll(m, first=True)
    m._locked = locked
    _poll(m)
    _poll(m)  # probe busy: nothing yet
    assert events == []
    _poll(m)  # probe runs now
    assert events == ["remove", "insert"]
