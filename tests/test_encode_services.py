import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from disc_ripper import db, encodepool, encodes, jobs, presets, settings, video_rips, videofiles
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
    assert (row["rip_id"], row["preset_id"], row["preset_name"], row["state"], row["role"]) == (
        rip, 1, "Standard", "queued", "main")
    assert row["disc_type"] == "dvd" and row["source_bytes"] > 0 and row["source_seconds"] > 0
    assert row["source"] == str(config.downloads_dir / str(rip) / "title_t00.mkv")


def test_encode_after_rip_off(config, setup):
    presets.set_default_choice(setup, "off")
    services, rip = scanned(config, setup)
    run_until_done(config, setup, services, rip)
    assert rip_state(setup, rip)["state"] == "ripped"
    assert encodes_of(setup) == []


def test_encode_refused_while_one_is_active(config, setup):
    services, rip = scanned(config, setup)
    run_until_done(config, setup, services, rip)  # one is queued by the rip
    assert asyncio.run(services.queue_encode(rip, 0, "2")) is None
    assert len(encodes_of(setup)) == 1


def test_encode_all_queues_each_file(config, setup):
    presets.set_default_choice(setup, "off")
    services, rip = scanned(config, setup)
    for n, name in ((0, "a.mkv"), (1, "b.mkv")):
        path = config.downloads_dir / str(rip) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
        video_rips.record_file(setup, rip, n, path, "main" if n == 0 else "extra", "t")
    ids = asyncio.run(services.encode_all(rip, "2"))
    assert len(ids) == 2
    assert [(r["title"], r["role"], r["preset_id"], r["preset_name"]) for r in encodes_of(setup)] == [
        (0, "main", 2, "Fast"), (1, "extra", 2, "Fast")]
    assert asyncio.run(services.encode_all(rip, "2")) == []  # both are still queued


def test_encode_refused_after_raw_deleted(config, setup):
    presets.set_default_choice(setup, "off")
    services, rip = scanned(config, setup)
    run_until_done(config, setup, services, rip)
    video_rips.mark_raw_deleted(setup, rip, 0, "t")
    assert asyncio.run(services.queue_encode(rip, 0, "1")) is None
    assert asyncio.run(services.queue_encode(rip, 9, "1")) is None  # unknown file
    assert encodes_of(setup) == []


def test_move_again_queues_encode(config, setup, monkeypatch):
    services, rip = scanned(config, setup)
    monkeypatch.setattr(videofiles, "STATVFS", shortfall(1))
    monkeypatch.setattr(videofiles, "place_in_downloads",  # no rename: the copy needs space
                        lambda *a: (_ for _ in ()).throw(OSError(18, "Invalid cross-device link")))
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
                         source_bytes=3, source_audio=1, preset_id=1, preset_name="Standard", now="t")
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
                          source_bytes=3, source_audio=1, preset_id=2, preset_name="Fast", now="t") is not None
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
                         disc_type="dvd", source_seconds=60, source_bytes=3, source_audio=1, preset_id=1,
                         preset_name="Standard", now="t")
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
    assert all(r[0] for r in setup.execute("SELECT fingerprint FROM presets"))  # filled at start-up


# -- review fixes ----------------------------------------------------------------


def test_queue_refused_without_scan_length(config, setup, filed):
    services, rip, raw, out, enc = filed
    with setup:
        setup.execute("UPDATE encodes SET state = 'failed' WHERE id = ?", (enc,))
    # the fixture's disc has an empty scan: no length to check the encode against
    result = asyncio.run(services.queue_encode_result(rip, 0, "1"))
    assert result == (None, "The rip's scan is missing, so its length can't be checked; can't encode.")
    scan = {"titles": [{"number": 0, "seconds": 0, "audio": 1}]}
    with setup:
        setup.execute("UPDATE video_discs SET scan = ?", (json.dumps(scan),))
    assert asyncio.run(services.queue_encode(rip, 0, "1")) is None  # zero seconds
    with setup:
        setup.execute("DELETE FROM video_discs")
    assert asyncio.run(services.queue_encode(rip, 0, "1")) is None  # no disc row
    assert [r["state"] for r in encodes_of(setup)] == ["failed"]


def test_failing_unlink_leaves_the_record_unmarked(filed, setup, monkeypatch):
    services, rip, raw, out, enc = filed
    marked = []
    real = video_rips.mark_raw_deleted
    monkeypatch.setattr(video_rips, "mark_raw_deleted", lambda *a: (marked.append(a), real(*a)))

    def broken(path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Services, "_unlink_raw", staticmethod(broken))
    assert "Couldn't delete" in services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=0)
    assert marked == [] and raw.exists()
    assert not setup.in_transaction


def test_missing_raw_still_marks_deleted(filed, setup):
    services, rip, raw, out, enc = filed
    raw.unlink()
    assert services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=0) is None
    assert "raw_deleted_at" in files_of(setup, rip)["0"]


def test_record_file_and_mark_raw_deleted_hold_the_write_lock_first(filed, setup, tmp_path):
    services, rip, raw, out, enc = filed
    for run in (lambda: video_rips.record_file(setup, rip, 5, tmp_path / "x.mkv", "extra", "t"),
                lambda: video_rips.mark_raw_deleted(setup, rip, 0, "t"),
                lambda: video_rips.clear_raw_deleted(setup, rip, 0)):
        seen = []
        setup.set_trace_callback(seen.append)
        run()
        setup.set_trace_callback(None)
        assert seen[0] == "BEGIN IMMEDIATE"  # read-modify-write is one transaction
        assert not setup.in_transaction


def test_interleaved_updates_both_survive(filed, setup, tmp_path):
    services, rip, raw, out, enc = filed
    other = db.connect(services.config.db_path)
    try:
        video_rips.record_file(setup, rip, 5, tmp_path / "x.mkv", "extra", "t")
        video_rips.mark_raw_deleted(other, rip, 0, "t")
        video_rips.record_file(setup, rip, 6, tmp_path / "y.mkv", "extra", "t")
    finally:
        other.close()
    files = files_of(setup, rip)
    assert "raw_deleted_at" in files["0"] and "5" in files and "6" in files


def test_queue_during_delete_is_locked_out(filed, setup, monkeypatch):
    services, rip, raw, out, enc = filed
    scan = {"titles": [{"number": 0, "seconds": 60, "audio": 1}]}
    with setup:
        setup.execute("UPDATE video_discs SET scan = ?", (json.dumps(scan),))
    seen = []
    real = Services._unlink_raw

    def racing_unlink(path):
        other = db.connect(services.config.db_path)
        other.execute("PRAGMA busy_timeout=0")
        try:
            other.execute("BEGIN IMMEDIATE")  # a queue lands mid-delete: the write lock is held, so it can't
        except sqlite3.OperationalError:
            seen.append("locked out")
        finally:
            other.close()
        real(path)

    monkeypatch.setattr(Services, "_unlink_raw", staticmethod(racing_unlink))
    assert services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=0) is None
    assert seen == ["locked out"]
    assert [r["state"] for r in encodes_of(setup)] == ["done"]
    assert not raw.exists()


def test_unlink_failure_leaves_the_flag_unset(filed, setup, monkeypatch):
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


def test_raw_deleted_as_soon_as_its_encode_is_done(filed, setup):
    """Raw rips are big and can be ripped again: no waiting once an encode passed its check."""
    services, rip, raw, out, enc = filed
    assert settings.get(setup, "raw_keep_hours") == 0
    set_enc_done(setup, enc, out, datetime.now(timezone.utc))
    asyncio.run(services.encode_done(enc))
    assert not raw.exists() and out.exists()


def _make_film(setup, rip, *, radarr=True, import_state=None):
    with setup:
        setup.execute("UPDATE video_rips SET main_title = 0 WHERE rip_id = ?", (rip,))
    if radarr:
        set_setting(setup, "radarr_url", "http://radarr:7878")
        set_setting(setup, "radarr_api_key", "k")
    if import_state:
        with setup:
            setup.execute("INSERT INTO film_imports (rip_id, state, created_at, updated_at) VALUES (?, ?, 't', 't')",
                          (rip, import_state))


def test_jellyfin_mode_keeps_film_raws(filed, setup):
    services, rip, raw, out, enc = filed
    set_setting(setup, "raw_keep_until", "jellyfin")
    set_setting(setup, "raw_keep_hours", 0)
    _make_film(setup, rip)
    assert services._sweep_raw(setup) == 0 and raw.exists()
    assert services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=None) is None  # Delete raw now still works
    assert not raw.exists()


def test_jellyfin_mode_falls_back_to_hours_for_tv(filed, setup):
    services, rip, raw, out, enc = filed
    set_setting(setup, "raw_keep_until", "jellyfin")
    set_setting(setup, "raw_keep_hours", 24)
    assert services._sweep_raw(setup) == 1 and not raw.exists()  # TV-like: no main title


def test_jellyfin_mode_falls_back_without_radarr(filed, setup):
    services, rip, raw, out, enc = filed
    set_setting(setup, "raw_keep_until", "jellyfin")
    set_setting(setup, "raw_keep_hours", 24)
    _make_film(setup, rip, radarr=False)
    assert services._sweep_raw(setup) == 1 and not raw.exists()


def test_jellyfin_mode_falls_back_when_import_tv_skipped(filed, setup):
    services, rip, raw, out, enc = filed
    set_setting(setup, "raw_keep_until", "jellyfin")
    set_setting(setup, "raw_keep_hours", 24)
    _make_film(setup, rip, import_state="tv_skipped")
    assert services._sweep_raw(setup) == 1 and not raw.exists()


def test_subtitle_shortfall_keeps_raw(filed, setup):
    services, rip, raw, out, enc = filed
    set_setting(setup, "raw_keep_hours", 0)
    with setup:
        setup.execute("UPDATE encodes SET message = 'HandBrake kept 1 of 2 subtitle tracks' WHERE id = ?", (enc,))
    assert services._sweep_raw(setup) == 0 and raw.exists()
    assert services.delete_raw_sync(setup, rip, 0, now=NOW, keep_hours=None) is None  # Delete raw now stays
    assert not raw.exists()


def _hold_case(filed, setup, state=None, step="", hours=0):
    services, rip, raw, out, enc = filed
    set_setting(setup, "raw_keep_until", "jellyfin")
    set_setting(setup, "raw_keep_hours", hours)
    _make_film(setup, rip)
    if state:
        with setup:
            setup.execute("INSERT INTO film_imports (rip_id, state, step, created_at, updated_at) "
                          "VALUES (?, ?, ?, 't', 't')", (rip, state, step))
    return services, raw


@pytest.mark.parametrize("state", ["needs_identify", "failed", "imported", "needs_replace_ok", "importing"])
def test_jellyfin_mode_keeps_raw_while_film_is_headed_for_library(filed, setup, state):
    services, raw = _hold_case(filed, setup, state)
    assert services._sweep_raw(setup) == 0 and raw.exists()


def test_jellyfin_mode_keeps_raw_with_no_import_row_and_failed_title(filed, setup):
    services, raw = _hold_case(filed, setup)  # no film_imports row (e.g. waiting on a failed title)
    assert services._sweep_raw(setup) == 0 and raw.exists()


def test_jellyfin_mode_undone_and_skipped_fall_back_to_hours(filed, setup):
    services, raw = _hold_case(filed, setup, "needs_identify", step="undone")
    assert services._sweep_raw(setup) == 1 and not raw.exists()


def test_jellyfin_mode_skipped_falls_back_to_hours(filed, setup):
    services, raw = _hold_case(filed, setup, "skipped")
    assert services._sweep_raw(setup) == 1 and not raw.exists()


def test_retry_with_invalid_preset_says_so(filed, setup):
    svc, rip, raw, out, enc = filed
    with setup:
        setup.execute("UPDATE encodes SET state = 'failed' WHERE id = ?", (enc,))
        setup.execute("UPDATE presets SET settings = ? WHERE id = 1", ('{"engine": "nope"}',))
    new_id, error = asyncio.run(svc.retry_encode_result(enc))
    assert new_id is None
    assert error == "Its preset's settings are no longer valid; choose another preset with Encode ▾."


def test_sweep_partials_keeps_a_done_jobs_mkv_whatever_the_rip_state(config, setup):
    job = jobs.enqueue(setup, "serial:A", "rip_title", {}, uses_drive=True, now="t")
    other = jobs.enqueue(setup, "serial:A", "rip_title", {}, uses_drive=True, now="t")  # the rip's own job id
    with setup:
        setup.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (job,))
        setup.execute("INSERT INTO video_discs (fingerprint, disc_type, last_seen) VALUES ('fp', 'dvd', 't')")
        setup.execute("INSERT INTO video_rips (rip_id, fingerprint, state, created_at, updated_at) "
                      "VALUES (?, 'fp', 'ripping', 't', 't')", (other,))
    folder = video_rips.partial_dir(config.downloads_dir, other, job)
    folder.mkdir(parents=True)
    (folder / "t.mkv").write_bytes(b"x")
    assert video_rips.sweep_partials(setup, config.downloads_dir) == []
    assert (folder / "t.mkv").is_file()
