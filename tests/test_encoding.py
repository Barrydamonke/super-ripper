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
