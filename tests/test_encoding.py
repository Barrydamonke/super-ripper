import json
import os
from pathlib import Path

import pytest

from disc_ripper import encoding
from disc_ripper.videofiles import parse_ffprobe

SRC = Path("/work/The Lego Movie_t01.mkv")
OUT = Path("/work/The Lego Movie_t01 - Standard.mkv")
S, O = str(SRC), str(OUT)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("HANDBRAKE_BIN", raising=False)
    monkeypatch.delenv("FFMPEG_BIN", raising=False)


def std(**kw):
    args = dict(disc_type="bluray", audio_channels=6, interlaced=False, fast_quality=24)
    args.update(kw)
    return encoding.encode_argv("standard", SRC, OUT, **args)


def fast(**kw):
    args = dict(disc_type="bluray", audio_channels=6, interlaced=False, fast_quality=24)
    args.update(kw)
    return encoding.encode_argv("fast", SRC, OUT, **args)


def test_output_name():
    assert encoding.output_name(Path("/w/The Lego Movie_t01.mkv"), "standard") == "The Lego Movie_t01 - Standard.mkv"
    assert encoding.PRESETS == ("standard", "fast", "copy")
    assert encoding.PRESET_LABEL["fast"] == "Fast"


def test_audio_plan():
    assert encoding.audio_plan(8) == (6, 640)
    assert encoding.audio_plan(6) == (6, 640)
    assert encoding.audio_plan(2) == (2, 224)
    assert encoding.audio_plan(1) == (1, 224)
    assert encoding.audio_plan(0) == (2, 224)
    assert encoding.audio_plan(4) == (6, 640)
    assert encoding.audio_plan(3) == (6, 640)


@pytest.mark.parametrize("channels,ab,mix,ffac", [(0, "224", "stereo", "2"), (4, "640", "5point1", "6")])
def test_odd_channel_counts_agree_between_presets(channels, ab, mix, ffac):
    s = std(audio_channels=channels)
    assert s[s.index("--ab"):s.index("--ab") + 4] == ["--ab", ab, "--mixdown", mix]
    f = fast(audio_channels=channels)
    assert f[f.index("-b:a"):f.index("-b:a") + 4] == ["-b:a", ab + "k", "-ac", ffac]


def test_env_overrides(monkeypatch):
    assert encoding.handbrake_prefix() == ["HandBrakeCLI"]
    assert encoding.ffmpeg_prefix() == ["ffmpeg"]
    monkeypatch.setenv("HANDBRAKE_BIN", "/x/hb")
    monkeypatch.setenv("FFMPEG_BIN", json.dumps(["python", "fake.py"]))
    assert encoding.handbrake_prefix() == ["/x/hb"]
    assert encoding.ffmpeg_prefix() == ["python", "fake.py"]


BLURAY_TAIL = ["--format", "av_mkv", "--markers", "--encoder", "x265_10bit", "--encoder-preset", "slow"]


def test_standard_bluray_argv_exact():
    assert std() == [
        "HandBrakeCLI", "-i", S, "-o", O, "--format", "av_mkv", "--markers", "--encoder", "x265_10bit",
        "--encoder-preset", "slow", "--quality", "20", "--crop-mode", "none", "--auto-anamorphic",
        "--audio", "1", "--aencoder", "eac3", "--ab", "640", "--mixdown", "5point1",
        "--all-subtitles", "--subtitle-burned=none", "--json"]


def test_standard_dvd_argv_exact():
    argv = std(disc_type="dvd")
    assert argv == [
        "HandBrakeCLI", "-i", S, "-o", O, "--format", "av_mkv", "--markers", "--encoder", "x265_10bit",
        "--encoder-preset", "slow", "--quality", "18", "--crop-mode", "none", "--auto-anamorphic",
        "--comb-detect", "--decomb", "--detelecine",
        "--audio", "1", "--aencoder", "eac3", "--ab", "640", "--mixdown", "5point1",
        "--all-subtitles", "--subtitle-burned=none", "--json"]


def test_standard_stereo_and_mono():
    stereo = std(audio_channels=2)
    assert stereo[stereo.index("--ab"):stereo.index("--ab") + 4] == ["--ab", "224", "--mixdown", "stereo"]
    mono = std(audio_channels=1)
    assert mono[mono.index("--mixdown") + 1] == "mono"


def test_standard_no_audio():
    argv = std(audio_channels=None)
    assert "--audio" in argv and argv[argv.index("--audio") + 1] == "none"
    for flag in ("--aencoder", "--ab", "--mixdown"):
        assert flag not in argv
    assert argv[argv.index("--audio"):argv.index("--audio") + 3] == ["--audio", "none", "--all-subtitles"]


FAST_HEAD = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-vaapi_device", "/dev/dri/renderD128", "-i", S]


def test_fast_argv_exact():
    assert fast() == FAST_HEAD + [
        "-map", "0:v:0", "-map", "0:a:0?", "-map", "0:s?", "-map_chapters", "0",
        "-vf", "format=nv12,hwupload", "-c:v", "hevc_vaapi", "-rc_mode", "CQP", "-qp", "24",
        "-c:a", "eac3", "-b:a", "640k", "-ac", "6", "-c:s", "copy",
        "-progress", "pipe:1", "-nostats", O]


def test_fast_interlaced_and_stereo():
    argv = fast(interlaced=True, audio_channels=2, fast_quality=28)
    assert argv[argv.index("-vf") + 1] == "format=nv12,hwupload,deinterlace_vaapi"
    assert argv[argv.index("-qp") + 1] == "28"
    assert argv[argv.index("-b:a"):argv.index("-b:a") + 4] == ["-b:a", "224k", "-ac", "2"]


def test_fast_no_audio():
    argv = fast(audio_channels=None)
    assert "0:a:0?" not in argv
    for flag in ("-c:a", "-b:a", "-ac"):
        assert flag not in argv
    assert "-an" in argv


def test_fast_anamorphic_sets_sar():
    argv = fast(sar=(64, 45))
    i = argv.index("-bsf:v")
    assert argv[i + 1] == "hevc_metadata=sample_aspect_ratio=64/45"
    assert argv[i + 2] == "-progress"
    assert "-bsf:v" not in fast(sar=(1, 1))


def test_copy_argv_exact():
    assert encoding.encode_argv("copy", SRC, OUT, disc_type="dvd", audio_channels=2, interlaced=False,
                                fast_quality=24) == [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", S, "-map", "0", "-c", "copy",
        "-progress", "pipe:1", "-nostats", O]


def test_idet_argv():
    assert encoding.idet_argv(SRC, 300.4) == [
        "ffmpeg", "-hide_banner", "-nostdin", "-ss", "300", "-t", "20", "-i", S,
        "-map", "0:v:0", "-vf", "idet", "-f", "null", "-"]


def test_priority_prefix_posix_only(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert encoding.priority_prefix() == ["nice", "-n", "19", "ionice", "-c", "3"]
    monkeypatch.setattr(os, "name", "nt")
    assert encoding.priority_prefix() == []


def test_parse_ffprobe_audio_channels_and_aspect():
    text = json.dumps({"format": {"duration": "10"}, "streams": [
        {"codec_type": "video", "sample_aspect_ratio": "64:45", "display_aspect_ratio": "16:9"},
        {"codec_type": "audio", "channels": 6},
        {"codec_type": "audio", "channels": 2}]})
    info = parse_ffprobe(text)
    assert info.audio_channels == (6, 2)
    assert info.sar == (64, 45)
    assert info.dar == pytest.approx(1.778, abs=0.001)
    for sar in ("0:1", "N/A", None):
        video = {"codec_type": "video"}
        if sar:
            video["sample_aspect_ratio"] = sar
        info = parse_ffprobe(json.dumps({"streams": [video]}))
        assert info.sar == (1, 1)
        assert info.dar is None


FIX = Path(__file__).parent / "fixtures" / "encode"


def _feed_all(parser, name):
    return [v for line in (FIX / name).read_text().splitlines() if (v := parser.feed(line)) is not None]


def test_handbrake_progress_fixture():
    values = _feed_all(encoding.HandBrakeProgress(), "handbrake_progress.txt")
    assert values == sorted(values) and len(set(values)) == len(values)
    assert values[0] == 0.0 and 0.41231 in values and values[-1] == 1.0
    assert 0.5 not in values and 0.1 not in values  # scan blocks don't count


def test_handbrake_progress_progress_before_state():
    p = encoding.HandBrakeProgress()
    lines = ['Progress: {', '    "Working": {', '        "Progress": 0.3,', '    },', '    "State": "WORKING"', '}']
    assert [p.feed(x) for x in lines] == [None, None, None, None, 0.3, None]


def test_ffmpeg_progress_fixture():
    values = _feed_all(encoding.FfmpegProgress(10.0), "ffmpeg_progress.txt")
    assert values == sorted(values) and values[-1] == 1.0
    assert values[:3] == pytest.approx([0.05, 0.1, 0.2])


def test_ffmpeg_progress_ignores_na_jumps_and_going_back():
    p = encoding.FfmpegProgress(100.0)
    assert p.feed("out_time_us=N/A") is None
    assert p.feed("out_time_us=-5") is None
    assert p.feed("out_time_us=10000000") == pytest.approx(0.10)
    assert p.feed("out_time_us=5000000") is None
    assert p.feed("out_time_us=80000000") == pytest.approx(0.11)  # jump capped at +1%
    assert p.feed("progress=end") == 1.0


def test_progress_noise_filter():
    noise = encoding.ProgressNoise()
    for line in ("frame=12", "fps=24.0", "stream_0_0_q=-0.0", "bitrate=N/A", "total_size=9", "out_time_us=5",
                 "out_time=00:00:01.0", "dup_frames=0", "drop_frames=0", "speed=1.5x", "progress=continue"):
        assert noise(line), line
    for line in ("Progress: {", '    "State": "WORKING",', '    "Working": {', '        "Progress": 0.4,',
                 "    }", "}"):
        assert noise(line), line
    for line in ("progress=end", "x265 [info]: frame I:1", "Error: something failed", "[error] bad"):
        assert not noise(line), line
    assert encoding.is_progress_noise("speed=2x") and not encoding.is_progress_noise("progress=end")


IDET_I = "[Parsed_idet_0 @ 0x1] Multi frame detection: TFF:   400 BFF:    10 Progressive:    50 Undetermined:     0"
IDET_P = "[Parsed_idet_0 @ 0x1] Multi frame detection: TFF:     2 BFF:     1 Progressive:   500 Undetermined:    10"


def test_parse_idet():
    assert encoding.parse_idet("junk\n" + IDET_I + "\n") is True
    assert encoding.parse_idet(IDET_I + "\n" + IDET_P) is False
    assert encoding.parse_idet("nothing here") is False


def test_estimate_defaults_and_history():
    assert encoding.estimate_seconds(6035, "standard", "bluray", []) == (15087.5, True)
    assert encoding.estimate_seconds(6035, "standard", "bluray", [0.5, 0.5]) == (12070.0, False)
    assert encoding.estimate_seconds(600, "fast", "dvd", [])[0] == 100.0
    assert encoding.estimate_seconds(600, "copy", "dvd", [])[0] == 20.0


def test_estimate_size():
    assert encoding.estimate_size(1000, "standard") == 350
    assert encoding.estimate_size(1000, "fast") == 500
    assert encoding.estimate_size(1000, "copy") == 1000


def test_guard_decision():
    from datetime import datetime, timezone
    from disc_ripper.schedule import GuardState
    utc = timezone.utc
    start = datetime(2026, 11, 21, 18, 0, tzinfo=utc)
    end = datetime(2026, 11, 21, 23, 0, tzinfo=utc)
    grace = datetime(2026, 11, 22, 1, 0, tzinfo=utc)
    now = datetime(2026, 11, 21, 20, 0, tzinfo=utc)
    inw = GuardState("in_window", end, grace, start)
    assert encoding.guard_decision(inw, now, 3600, False) == ("start", "")
    assert encoding.guard_decision(inw, now, 6 * 3600, False)[0] == "wait"
    assert encoding.guard_decision(GuardState("draining", end, grace, start), now, 60, False)[0] == "wait"
    assert encoding.guard_decision(GuardState("off_hours", None, None), now, 99 * 3600, False)[0] == "start"
    d, msg = encoding.guard_decision(inw, now, 8 * 3600, False)
    assert d == "too_long" and "about 8 h" in msg and "use Fast" in msg
    assert encoding.guard_decision(inw, now, 4 * 3600, True)[0] == "start"  # 5 h rough, fits exactly
    assert encoding.guard_decision(inw, now, 4 * 3600 + 600, True)[0] == "wait"
    assert encoding.guard_decision(inw, now, 4 * 3600 + 600, False)[0] == "start"


def test_guard_decision_wait_message():
    from datetime import datetime, timezone
    from disc_ripper.schedule import GuardState
    utc = timezone.utc
    inw = GuardState("in_window", datetime(2026, 11, 21, 23, 0, tzinfo=utc), datetime(2026, 11, 22, 1, 0, tzinfo=utc),
                     datetime(2026, 11, 21, 18, 0, tzinfo=utc))
    now = datetime(2026, 11, 22, 0, 0, tzinfo=utc)
    assert encoding.guard_decision(inw, now, 12000, False) == (
        "wait", "Waiting for the next session: needs about 3 h 20 min, the grace period ends at 01:00")
