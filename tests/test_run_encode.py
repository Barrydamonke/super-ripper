import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from disc_ripper import encodepool, encodes, joblog, presets, runner, settings, videofiles
from disc_ripper.runner import JobContext

from .test_encodepool import conn, pool_for  # noqa: F401

FAKE = Path(__file__).parent / "fixtures" / "encode" / "fake_encoder.py"
DURATION = 6035.0


def fake_probe(path):
    stub = json.loads(Path(path).read_text(encoding="utf-8"))
    return videofiles.MkvInfo(
        stub["seconds"], stub["video"], stub["audio"], tuple(stub.get("audio_channels", ())),
        tuple(stub.get("sar", (1, 1))), stub.get("dar"), stub.get("width", 0), stub.get("height", 0),
        streams=tuple(videofiles.Stream(**s) for s in stub.get("streams", ())))


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(videofiles, "PROBE", fake_probe)
    argv = json.dumps([sys.executable, str(FAKE)])
    monkeypatch.setenv("HANDBRAKE_BIN", argv)
    monkeypatch.setenv("FFMPEG_BIN", argv)
    monkeypatch.setenv("FAKE_ARGV_FILE", str(tmp_path / "argv.log"))
    monkeypatch.setattr(encodepool, "STALL_UNIT_SECONDS", 60)


def recorded(tmp_path):
    f = tmp_path / "argv.log"
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def stream(kind, language="eng", index=0, **kw):
    return {"kind": kind, "index": index, "language": language, "title": "", "comment": False, "forced": False,
            "channels": 6 if kind == "audio" else 0, "codec": "", **kw}


def make_raw(config, name="Film.mkv", seconds=DURATION, dar=1.778, audio=1, streams=None, height=1080):
    raw = config.downloads_dir / "1" / name
    raw.parent.mkdir(parents=True, exist_ok=True)
    streams = [stream("audio"), stream("subtitle")] if streams is None else streams
    raw.write_text(json.dumps({"seconds": seconds, "video": 1, "audio": audio, "audio_channels": [6],
                               "sar": [1, 1], "dar": dar, "width": 1920, "height": height,
                               "streams": streams}), encoding="utf-8")
    return raw


def enqueue(conn, raw, preset=1, role="main", disc_type="bluray", audio=1):
    eid = encodes.create(conn, rip_id=1, title=1, role=role, source=raw, disc_type=disc_type,
                         source_seconds=DURATION, source_bytes=1000, source_audio=audio, preset_id=preset,
                         preset_name=presets.get_preset(conn, preset).name, now="2026-01-01T00:00:00+00:00")
    return eid


def queue(conn, raw, preset=1, role="main", disc_type="bluray", audio=1):
    eid = enqueue(conn, raw, preset, role, disc_type, audio)
    assert encodes.start(conn, eid, "2026-01-01T00:00:01+00:00")
    return encodes.get(conn, eid)


def new_preset(conn, name, **changes):
    s = presets.get_preset(conn, 1).settings.to_dict() | changes
    return presets.create_preset(conn, name, presets.PresetSettings.from_dict(s), "2026-01-01T00:00:00+00:00")


def context(config, row):
    pool = pool_for(config)
    return JobContext(pool, row["id"], "", {}, log=joblog.JobLog(pool._log_path(row["id"])))


def go(config, conn, row):
    ctx = context(config, row)
    asyncio.run(encodepool.run_encode(ctx, row))
    return ctx


def log_text(ctx):
    return ctx.log.path.read_text(encoding="utf-8")


def files_in_downloads(config):
    return sorted(p.name for p in config.downloads_dir.rglob("*") if p.is_file())


def test_standard_encode_lands_beside_raw(config, conn):
    raw = make_raw(config)
    row = queue(conn, raw)
    go(config, conn, row)
    out = config.downloads_dir / "1" / "Film - Standard.mkv"
    assert out.exists() and raw.exists()
    assert not (config.work_dir / f"encode-{row['id']}").exists()
    assert not list(config.downloads_dir.rglob("*.part"))
    done = encodes.get(conn, row["id"])
    assert done["state"] == "done" and done["output"] == str(out) and done["output_bytes"] == out.stat().st_size


def test_extra_encode_lands_in_extras(config, conn):
    raw = make_raw(config)
    go(config, conn, queue(conn, raw, role="extra"))
    assert (config.downloads_dir / "1" / "extras" / "Film - Standard.mkv").exists()


def test_fast_dvd_runs_idet_and_deinterlaces(config, conn, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_IDET", "interlaced")
    raw = make_raw(config)
    ctx = go(config, conn, queue(conn, raw, preset=2, disc_type="dvd"))
    runs = recorded(tmp_path)
    assert len(runs) == 4  # three idet runs, then the encode
    assert "deinterlace_vaapi" in " ".join(runs[-1])
    assert "interlaced" in log_text(ctx).lower()


def test_fast_dvd_progressive_does_not_deinterlace(config, conn, tmp_path):
    go(config, conn, queue(conn, make_raw(config), preset=2, disc_type="dvd"))
    assert "deinterlace_vaapi" not in " ".join(recorded(tmp_path)[-1])


def test_encode_failure_keeps_raw_and_cleans_work(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE_MODE", "fail")
    raw = make_raw(config)
    row = queue(conn, raw)
    with pytest.raises(RuntimeError, match="Encode failed") as err:
        go(config, conn, row)
    assert str(err.value).endswith("See the log for the encoder's messages.")
    assert raw.exists()
    assert not (config.work_dir / f"encode-{row['id']}").exists()
    assert files_in_downloads(config) == ["Film.mkv"]


def test_encode_stall_message(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE_MODE", "hang")
    monkeypatch.setattr(encodepool, "STALL_UNIT_SECONDS", 0.3)
    settings.save(conn, {"encode_stall_minutes": 1})
    raw = make_raw(config)
    row = queue(conn, raw)
    with pytest.raises(RuntimeError, match="The encoder stopped responding: no output for 1 minute"):
        go(config, conn, row)
    assert raw.exists() and not (config.work_dir / f"encode-{row['id']}").exists()


def test_short_output_fails_check(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE_MODE", "short")
    raw = make_raw(config)
    with pytest.raises(RuntimeError, match="length") as err:
        go(config, conn, queue(conn, raw))
    message = str(err.value)
    assert message.startswith("The encode failed its check: The encoded file's length is")
    assert "MakeMKV" not in message and "ripped" not in message and "the source says" in message
    assert files_in_downloads(config) == ["Film.mkv"]


def test_squashed_aspect_fails_check(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_DAR", "1.333")
    raw = make_raw(config)
    row = queue(conn, raw)
    with pytest.raises(RuntimeError, match=r"The encode failed its check: the picture shape.*1\.33.*1\.78"):
        go(config, conn, row)
    assert raw.exists() and not (config.work_dir / f"encode-{row['id']}").exists()


def test_encode_log_stays_small(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_BLOCKS", "530")  # about 10,000 progress blocks
    ctx = go(config, conn, queue(conn, make_raw(config)))
    assert ctx.log.path.stat().st_size < 50_000
    text = log_text(ctx)
    assert "$ " in text and "exit code 0" in text


def test_missing_source_fails_cleanly(config, conn):
    row = queue(conn, config.downloads_dir / "1" / "Gone.mkv")
    with pytest.raises(RuntimeError, match="The ripped file is no longer there"):
        go(config, conn, row)
    assert not (config.work_dir / f"encode-{row['id']}").exists()


def test_cancel_during_encode_leaves_nothing(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE_MODE", "hang")
    raw = make_raw(config)
    row = queue(conn, raw)
    ctx = context(config, row)

    async def main():
        task = asyncio.create_task(encodepool.run_encode(ctx, row))
        await asyncio.sleep(1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert files_in_downloads(config) == ["Film.mkv"]
    assert not (config.work_dir / f"encode-{row['id']}").exists()
    assert not runner.LIVE_PROCESSES


def test_cancel_during_move_leaves_nothing(config, conn, monkeypatch):
    raw = make_raw(config)
    row = queue(conn, raw)
    ctx = context(config, row)
    started = []

    def slow_move(config, rip_id, role, src, **kw):
        started.append(1)
        part = config.downloads_dir / "1" / (src.name + ".part")
        part.write_text("x")
        while not kw["cancel"].is_set():
            time.sleep(0.01)
        part.unlink()
        raise videofiles.MoveCancelled()

    monkeypatch.setattr(videofiles, "move_to_downloads", slow_move)

    async def main():
        task = asyncio.create_task(encodepool.run_encode(ctx, row))
        while not started:
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert files_in_downloads(config) == ["Film.mkv"]
    assert not (config.work_dir / f"encode-{row['id']}").exists()


def test_speed_recorded(config, conn):
    row = queue(conn, make_raw(config))
    go(config, conn, row)
    speed = encodes.get(conn, row["id"])["speed"]
    assert speed is not None and speed > 0


def test_finishing_an_encode_wakes_the_pool(config, conn, monkeypatch):
    async def noop(ctx, row):
        pass

    monkeypatch.setattr(encodepool, "run_encode", noop)
    row = queue(conn, make_raw(config))
    pool = pool_for(config)
    pool._wakeups = [asyncio.Event()]
    asyncio.run(pool._run(row))
    assert pool._wakeups[0].is_set()


def test_encode_check_wording_names_the_encoded_file(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE_MODE", "no_audio")
    with pytest.raises(RuntimeError) as err:
        go(config, conn, queue(conn, make_raw(config)))
    assert str(err.value) == "The encode failed its check: The encoded file has no audio."


def test_standard_clears_the_default_subtitle_flag_afterwards(config, conn, tmp_path):
    # HandBrake's own --subtitle-default=none also drops "forced", so a quick ffmpeg copy clears "default" only.
    raw = make_raw(config)
    go(config, conn, queue(conn, raw))
    runs = recorded(tmp_path)
    assert len(runs) == 2  # HandBrake, then the flag fix-up
    assert "--subtitle-default=none" not in runs[0]
    fix = runs[1]
    assert fix[fix.index("-map") + 1] == "0" and fix[fix.index("-c") + 1] == "copy"
    assert fix[fix.index("-disposition:s") + 1] == "-default"
    assert (config.downloads_dir / "1" / "Film - Standard.mkv").exists()
    assert not list((config.downloads_dir / "1").glob("*.tmp"))


def test_fast_and_copy_need_no_fix_up(config, conn, tmp_path):
    raw = make_raw(config)
    go(config, conn, queue(conn, raw, preset=3))
    assert len(recorded(tmp_path)) == 1


def test_snapshot_written_and_used(config, conn, tmp_path):
    raw = make_raw(config)
    eid = enqueue(conn, raw)
    s = presets.get_preset(conn, 1).settings.to_dict() | {"quality_bluray": 22}
    presets.update_preset(conn, 1, "Standard", presets.PresetSettings.from_dict(s), "2026-02-01T00:00:00+00:00")
    assert encodes.start(conn, eid, "2026-01-01T00:00:01+00:00")
    ctx = go(config, conn, encodes.get(conn, eid))
    argv = recorded(tmp_path)[0]
    assert argv[argv.index("--quality") + 1] == "22"
    row = encodes.get(conn, eid)
    assert row["preset_name"] == "Standard" and json.loads(row["preset_settings"])["quality_bluray"] == 22
    assert row["preset_fingerprint"] == presets.get_preset(conn, 1).fingerprint
    assert log_text(ctx).splitlines()[0].split(" ", 2)[2].startswith("preset: Standard {")


def test_track_check_fails_on_mismatch(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE_MODE", "drop_audio")
    with pytest.raises(RuntimeError, match="The encode's tracks don't match the preset: expected audio eng; got none"):
        go(config, conn, queue(conn, make_raw(config)))


def test_no_audio_or_subtitles_source_passes_the_track_check(config, conn, tmp_path):
    raw = make_raw(config, audio=0, streams=[])
    go(config, conn, queue(conn, raw, audio=0))
    argv = recorded(tmp_path)[0]
    assert argv[argv.index("--audio") + 1] == "none" and argv[argv.index("--subtitle") + 1] == "none"


def test_fallback_note_in_message(config, conn):
    pid = new_preset(conn, "Anime", audio_languages=["jpn"])
    row = queue(conn, make_raw(config), preset=pid)
    go(config, conn, row)
    done = encodes.get(conn, row["id"])
    assert done["state"] == "done" and "No Japanese audio in this rip; kept English." in done["message"]


def test_vaapi_crop_cap(config, conn, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CROP", "1920:800:0:140")
    monkeypatch.setenv("FAKE_DAR", "2.4")
    pid = new_preset(conn, "Small", engine="ffmpeg", mode="hardware", codec="h264_vaapi", max_height=720,
                     crop="auto", audio_codec="eac3")
    go(config, conn, queue(conn, make_raw(config), preset=pid))
    runs = recorded(tmp_path)
    assert len(runs) == 4  # three cropdetect runs, then the encode
    vf = runs[-1][runs[-1].index("-vf") + 1]
    assert vf == "crop=1920:800:0:140,format=nv12,hwupload,scale_vaapi=w=-2:h=720"


def test_vaapi_crop_aspect_mismatch_fails(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_CROP", "1920:800:0:140")
    monkeypatch.setenv("FAKE_DAR", "1.778")  # the crop was not applied to the picture's shape
    pid = new_preset(conn, "Small", engine="ffmpeg", mode="hardware", codec="h264_vaapi", max_height=720,
                     crop="auto", audio_codec="eac3")
    with pytest.raises(RuntimeError, match="picture shape"):
        go(config, conn, queue(conn, make_raw(config), preset=pid))


def test_handbrake_auto_crop_skips_the_aspect_check(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_DAR", "2.4")
    pid = new_preset(conn, "HB crop", crop="auto")
    ctx = go(config, conn, queue(conn, make_raw(config), preset=pid))
    assert "aspect check skipped" in log_text(ctx)


def test_deleted_preset_fails_with_the_copy_text(config, conn):
    pid = new_preset(conn, "Temp")
    row = queue(conn, make_raw(config), preset=pid)
    conn.execute("DELETE FROM presets WHERE id = ?", (pid,))  # as if it vanished under a queued encode
    conn.commit()
    with pytest.raises(RuntimeError, match="That preset was deleted; choose another with Encode ▾."):
        go(config, conn, encodes.get(conn, row["id"]))


def test_running_encode_keeps_snapshot_after_edit(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE_MODE", "hang")
    row = queue(conn, make_raw(config))
    ctx = context(config, row)

    async def main():
        task = asyncio.create_task(encodepool.run_encode(ctx, row))
        await asyncio.sleep(1.0)
        s = presets.get_preset(conn, 1).settings.to_dict() | {"quality_bluray": 30}
        presets.update_preset(conn, 1, "Standard", presets.PresetSettings.from_dict(s), "2026-02-01T00:00:00+00:00")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    snap = encodes.get(conn, row["id"])
    assert json.loads(snap["preset_settings"])["quality_bluray"] == 20
    assert presets.get_preset(conn, 1).settings.quality_bluray == 30


def test_track_check_accepts_language_aliases(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_RELABEL", json.dumps({"fre": "fra"}))
    raw = make_raw(config, streams=[stream("audio", "fre"), stream("subtitle", "eng")])
    go(config, conn, queue(conn, raw, preset=new_preset(conn, "French", audio_languages=["fra"])))


def test_track_check_und_matches_anything(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_RELABEL", json.dumps({"eng": "und"}))
    go(config, conn, queue(conn, make_raw(config)))


def test_track_check_audio_language_mismatch_fails(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_RELABEL", json.dumps({"eng": "jpn"}))
    with pytest.raises(RuntimeError, match="expected audio eng; got jpn"):
        go(config, conn, queue(conn, make_raw(config)))


def test_handbrake_subtitle_shortfall_is_noted_not_failed(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_SUB_KEEP", "1")
    raw = make_raw(config, streams=[stream("audio"), stream("subtitle", index=0), stream("subtitle", index=1)])
    row = queue(conn, raw)
    ctx = go(config, conn, row)
    assert "HandBrake kept 1 of 2 subtitle tracks" in log_text(ctx)
    done = encodes.get(conn, row["id"])
    assert done["state"] == "done" and "HandBrake kept 1 of 2 subtitle tracks" in done["message"]


def test_handbrake_audio_count_must_still_match(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE_MODE", "drop_audio")
    with pytest.raises(RuntimeError, match="tracks don't match"):
        go(config, conn, queue(conn, make_raw(config)))


def test_ffmpeg_subtitle_shortfall_still_fails(config, conn, monkeypatch):
    monkeypatch.setenv("FAKE_SUB_KEEP", "1")
    raw = make_raw(config, streams=[stream("audio"), stream("subtitle", index=0), stream("subtitle", index=1)])
    with pytest.raises(RuntimeError, match="expected 2 subtitle tracks; got 1"):
        go(config, conn, queue(conn, raw, preset=2))
