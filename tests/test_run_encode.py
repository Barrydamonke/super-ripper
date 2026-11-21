import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from disc_ripper import encodepool, encodes, joblog, runner, settings, videofiles
from disc_ripper.runner import JobContext

from .test_encodepool import conn, pool_for  # noqa: F401

FAKE = Path(__file__).parent / "fixtures" / "encode" / "fake_encoder.py"
DURATION = 6035.0


def fake_probe(path):
    stub = json.loads(Path(path).read_text(encoding="utf-8"))
    return videofiles.MkvInfo(
        stub["seconds"], stub["video"], stub["audio"], tuple(stub.get("audio_channels", ())),
        tuple(stub.get("sar", (1, 1))), stub.get("dar"))


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


def make_raw(config, name="Film.mkv", seconds=DURATION, dar=1.778, audio=1):
    raw = config.downloads_dir / "1" / name
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps({"seconds": seconds, "video": 1, "audio": audio, "audio_channels": [6],
                               "sar": [1, 1], "dar": dar}), encoding="utf-8")
    return raw


def queue(conn, raw, preset="standard", role="main", disc_type="bluray", audio=1):
    eid = encodes.create(conn, rip_id=1, title=1, role=role, source=raw, disc_type=disc_type,
                         source_seconds=DURATION, source_bytes=1000, source_audio=audio, preset=preset,
                         now="2026-01-01T00:00:00+00:00")
    assert encodes.start(conn, eid, "2026-01-01T00:00:01+00:00")
    return encodes.get(conn, eid)


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
    ctx = go(config, conn, queue(conn, raw, preset="fast", disc_type="dvd"))
    runs = recorded(tmp_path)
    assert len(runs) == 4  # three idet runs, then the encode
    assert "deinterlace_vaapi" in " ".join(runs[-1])
    assert "interlaced" in log_text(ctx).lower()


def test_fast_dvd_progressive_does_not_deinterlace(config, conn, tmp_path):
    go(config, conn, queue(conn, make_raw(config), preset="fast", disc_type="dvd"))
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
