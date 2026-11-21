import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from disc_ripper import db, encodepool, encodes, jobs, settings, video_rips, videofiles
from disc_ripper.services import Services

from .test_encodepool import FakeSpace  # noqa: F401
from .test_videorip_rip import (  # noqa: F401
    make_services, probe, rip_state, run_until_done, scanned, shortfall)
from .test_videorip_scan import setup  # noqa: F401

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def set_setting(conn, key, value):
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def encodes_of(conn):
    return conn.execute("SELECT * FROM encodes ORDER BY id").fetchall()


# -- queueing after a rip ------------------------------------------------------


def test_rip_completion_queues_default_preset(config, setup):
    services, rip = scanned(config, setup)
    run_until_done(config, setup, services, rip)
    [row] = encodes_of(setup)
    assert (row["rip_id"], row["preset"], row["state"], row["role"]) == (rip, "standard", "queued", "main")
    assert row["disc_type"] == "dvd" and row["source_bytes"] > 0 and row["source_seconds"] > 0
    assert row["source"] == str(config.downloads_dir / str(rip) / "title_t00.mkv")


def test_encode_after_rip_off(config, setup):
    set_setting(setup, "encode_after_rip", "off")
    services, rip = scanned(config, setup)
    run_until_done(config, setup, services, rip)
    assert rip_state(setup, rip)["state"] == "ripped"
    assert encodes_of(setup) == []


def test_encode_refused_while_one_is_active(config, setup):
    services, rip = scanned(config, setup)
    run_until_done(config, setup, services, rip)  # one is queued by the rip
    assert asyncio.run(services.queue_encode(rip, 0, "fast")) is None
    assert len(encodes_of(setup)) == 1


def test_encode_all_queues_each_file(config, setup):
    set_setting(setup, "encode_after_rip", "off")
    services, rip = scanned(config, setup)
    for n, name in ((0, "a.mkv"), (1, "b.mkv")):
        path = config.downloads_dir / str(rip) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
        video_rips.record_file(setup, rip, n, path, "main" if n == 0 else "extra", "t")
    ids = asyncio.run(services.encode_all(rip, "fast"))
    assert len(ids) == 2
    assert [(r["title"], r["role"], r["preset"]) for r in encodes_of(setup)] == [(0, "main", "fast"), (1, "extra", "fast")]
    assert asyncio.run(services.encode_all(rip, "fast")) == []  # both are still queued


def test_encode_refused_after_raw_deleted(config, setup):
    set_setting(setup, "encode_after_rip", "off")
    services, rip = scanned(config, setup)
    run_until_done(config, setup, services, rip)
    video_rips.mark_raw_deleted(setup, rip, 0, "t")
    assert asyncio.run(services.queue_encode(rip, 0, "standard")) is None
    assert asyncio.run(services.queue_encode(rip, 9, "standard")) is None  # unknown file
    assert encodes_of(setup) == []


def test_move_again_queues_encode(config, setup, monkeypatch):
    services, rip = scanned(config, setup)
    monkeypatch.setattr(videofiles, "STATVFS", shortfall(1))
    run_until_done(config, setup, services, rip)
    assert rip_state(setup, rip)["state"] == "move_failed"
    assert encodes_of(setup) == []
    monkeypatch.setattr(videofiles, "STATVFS", shortfall(500))
    assert asyncio.run(services.move_again(rip)) is None
    [row] = encodes_of(setup)
    assert row["rip_id"] == rip and row["state"] == "queued"


# -- raw deletion --------------------------------------------------------------


@pytest.fixture
def filed(config, setup):
    """A rip with one raw file and a done encode whose output exists, finished at NOW - 30h."""
    with setup:
        setup.execute("INSERT INTO video_discs (fingerprint, disc_type, last_seen) VALUES ('fp', 'dvd', 't')")
    jid = jobs.enqueue(setup, "serial:A", "rip_title", {}, uses_drive=True, now="t")
    with setup:
        setup.execute("INSERT INTO video_rips (rip_id, fingerprint, state, created_at, updated_at) "
                      "VALUES (?, 'fp', 'ripped', 't', 't')", (jid,))
    raw = config.downloads_dir / str(jid) / "film.mkv"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"raw")
    video_rips.record_file(setup, jid, 0, raw, "main", "t")
    out = config.downloads_dir / str(jid) / "film-out.mkv"
    out.write_bytes(b"out")
    enc = encodes.create(setup, rip_id=jid, title=0, role="main", source=raw, disc_type="dvd", source_seconds=60,
                         source_bytes=3, source_audio=1, preset="standard", now="t")
    set_enc_done(setup, enc, out, NOW - timedelta(hours=30))
    return Services(config), jid, raw, out, enc


def set_enc_done(conn, enc, out, finished):
    with conn:
        conn.execute("UPDATE encodes SET state = 'done', output = ?, output_bytes = 3, finished_at = ? WHERE id = ?",
                     (str(out) if out else None, finished.isoformat(timespec="seconds"), enc))


def files_of(conn, rip):
    return json.loads(conn.execute("SELECT files FROM video_rips WHERE rip_id = ?", (rip,)).fetchone()[0])


def test_raw_deleted_after_keep_hours(filed, setup):
    services, rip, raw, out, enc = filed
    assert services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=48) is not None
    assert raw.exists()
    assert services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=24) is None
    assert not raw.exists() and out.exists()  # only the raw goes; never the output or the folder
    assert raw.parent.is_dir()
    assert files_of(setup, rip)["0"]["raw_deleted_at"]
    assert "raw rip deleted" in (services.config.config_dir / "logs" / f"encode-{enc}.log").read_text()
    assert services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=24) is not None  # already gone


def test_raw_kept_while_another_encode_queued(filed, setup):
    services, rip, raw, out, enc = filed
    assert encodes.create(setup, rip_id=rip, title=0, role="main", source=raw, disc_type="dvd", source_seconds=60,
                          source_bytes=3, source_audio=1, preset="fast", now="t") is not None
    assert "waiting or running" in services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=0)
    assert raw.exists()


def test_raw_kept_when_output_missing(filed, setup):
    services, rip, raw, out, enc = filed
    out.unlink()
    assert "kept until" in services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=0)
    assert raw.exists()


def test_delete_raw_now_needs_done_encode(filed, setup):
    services, rip, raw, out, enc = filed
    with setup:
        setup.execute("UPDATE encodes SET state = 'failed', output = NULL WHERE id = ?", (enc,))
    assert services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=None) is not None
    assert raw.exists()
    set_enc_done(setup, enc, out, NOW)  # just finished: "now" ignores the keep time
    assert services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=None) is None
    assert not raw.exists()


def test_raw_never_deleted_when_it_is_an_output(filed, setup):
    services, rip, raw, out, enc = filed
    set_enc_done(setup, enc, raw, NOW - timedelta(hours=30))  # paranoia: the output is the raw file
    assert "output" in services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=0)
    assert raw.exists()


def test_sweep_deletes_old_raws_only(filed, setup):
    services, rip, raw, out, enc = filed
    set_setting(setup, "raw_keep_hours", 24)
    assert services._sweep_raw(setup) == 1
    assert not raw.exists() and out.exists()
    assert services._sweep_raw(setup) == 0


# -- start-up ------------------------------------------------------------------


def test_start_requeues_interrupted(config, setup, monkeypatch):
    enc = encodes.create(setup, rip_id=1, title=0, role="main", source=config.downloads_dir / "f.mkv",
                         disc_type="dvd", source_seconds=60, source_bytes=3, source_audio=1, preset="standard",
                         now="t")
    assert encodes.start(setup, enc, "t")  # left running by a crash
    seen = []
    monkeypatch.setattr(encodepool.EncodePool, "start",
                        lambda self: seen.append(setup.execute("SELECT state FROM encodes").fetchone()[0]))

    async def main():
        services = Services(config)
        await services.start()
        await services.stop()

    asyncio.run(main())
    assert seen == ["queued"]
    assert encodes_of(setup)[0]["state"] == "queued"


# -- review fixes ----------------------------------------------------------------


def test_queue_refused_without_scan_length(config, setup, filed):
    services, rip, raw, out, enc = filed
    with setup:
        setup.execute("UPDATE encodes SET state = 'failed' WHERE id = ?", (enc,))
    # the fixture's disc has an empty scan: no length to check the encode against
    result = asyncio.run(services.queue_encode_result(rip, 0, "standard"))
    assert result == (None, "The rip's scan is missing, so its length can't be checked; can't encode.")
    scan = {"titles": [{"number": 0, "seconds": 0, "audio": 1}]}
    with setup:
        setup.execute("UPDATE video_discs SET scan = ?", (json.dumps(scan),))
    assert asyncio.run(services.queue_encode(rip, 0, "standard")) is None  # zero seconds
    with setup:
        setup.execute("DELETE FROM video_discs")
    assert asyncio.run(services.queue_encode(rip, 0, "standard")) is None  # no disc row
    assert [r["state"] for r in encodes_of(setup)] == ["failed"]


def test_queue_between_flag_and_unlink_is_refused(filed, setup, monkeypatch):
    services, rip, raw, out, enc = filed
    scan = {"titles": [{"number": 0, "seconds": 60, "audio": 1}]}
    with setup:
        setup.execute("UPDATE video_discs SET scan = ?", (json.dumps(scan),))
    seen = []
    real = Services._unlink_raw

    def racing_unlink(path):
        seen.append(services._db(services._queue_encode, rip, 0, "fast"))  # a queue lands mid-delete
        real(path)

    monkeypatch.setattr(Services, "_unlink_raw", staticmethod(racing_unlink))
    assert services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=0) is None
    assert seen[0][0] is None
    assert [r["state"] for r in encodes_of(setup)] == ["done"]
    assert not raw.exists()


def test_unlink_failure_rolls_the_flag_back(filed, setup, monkeypatch):
    services, rip, raw, out, enc = filed

    def broken(path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Services, "_unlink_raw", staticmethod(broken))
    error = services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=0)
    assert "Couldn't delete" in error and raw.exists()
    assert "raw_deleted_at" not in files_of(setup, rip)["0"]


def test_truncated_output_never_lets_raw_go(filed, setup):
    services, rip, raw, out, enc = filed
    out.write_bytes(b"")  # emptied
    assert services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=0) is not None
    out.write_bytes(b"ab")  # replaced by something of another size
    assert services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=0) is not None
    assert raw.exists()


def test_no_finish_time_refuses_instead_of_raising(filed, setup):
    services, rip, raw, out, enc = filed
    with setup:
        setup.execute("UPDATE encodes SET finished_at = NULL WHERE id = ?", (enc,))
    assert "isn't recorded" in services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=24)
    assert raw.exists()


# -- what a notification publishes ------------------------------------------------


def _published(config, setup, **kw):
    services = Services(config)
    seen = []
    services.broker.publish = lambda name, html: seen.append(name)
    asyncio.run(services.encodes_changed(**kw))
    return seen


def test_state_change_publishes_films_and_encodes(config, setup):
    assert _published(config, setup) == ["films", "encodes"]


def test_progress_tick_publishes_only_the_panel(config, setup):
    assert _published(config, setup, progress_only=True) == ["encodes"]


def test_encode_of_another_rip_of_the_file_does_not_free_the_raw(filed, setup):
    services, rip, raw, out, enc = filed
    with setup:  # the good encode was made from some earlier file at that path, not this raw
        setup.execute("UPDATE encodes SET source = ? WHERE id = ?", (str(raw.parent / "earlier.mkv"), enc))
    assert services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=0) is not None
    assert raw.exists()
