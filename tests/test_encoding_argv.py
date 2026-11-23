from pathlib import Path

import pytest

from disc_ripper.encoding import (
    TrackPlan, build_argv, expected_dar, ffmpeg_prefix, handbrake_prefix, needs_cropdetect, needs_idet,
    parse_cropdetect, pick_crop)
from disc_ripper.presets import SEEDS, PresetSettings
from disc_ripper.videofiles import Stream

HB = handbrake_prefix()
FF = ffmpeg_prefix()
SRC, OUT = Path("/rips/in.mkv"), Path("/rips/out.mkv")
src, out = str(SRC), str(OUT)


def seed(n):
    return PresetSettings.from_dict(SEEDS[n][1])


def settings_from(n, **kw):
    return PresetSettings.from_dict({**SEEDS[n][1], **kw})


def A(i, lang, ch=6, title="", comment=False):
    return Stream("audio", i, lang, title=title, comment=comment, channels=ch)


def S(i, lang, forced=False):
    return Stream("subtitle", i, lang, forced=forced)


BD_PLAN = TrackPlan(audio=(A(0, "eng", 6),), subtitles=(S(0, "eng"), S(1, "eng", forced=True)))

def test_standard_bluray_exact():
    assert build_argv(seed(1), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080) == [
        *HB, "-i", src, "-o", out, "--format", "av_mkv", "--markers", "--encoder", "x265_10bit",
        "--encoder-preset", "slow", "--quality", "20", "--crop-mode", "none", "--auto-anamorphic",
        "--audio", "1", "--aencoder", "eac3", "--ab", "640", "--mixdown", "5point1",
        "--subtitle", "1,2", "--subtitle-burned=none", "--json"]

def test_standard_dvd_decombs():
    argv = build_argv(seed(1), SRC, OUT, disc_type="dvd", plan=BD_PLAN, source_height=480)
    assert argv[argv.index("--quality") + 1] == "18"
    i = argv.index("--auto-anamorphic")
    assert argv[i + 1:i + 4] == ["--comb-detect", "--decomb", "--detelecine"]

def test_fast_dvd_interlaced_exact():
    assert build_argv(seed(2), SRC, OUT, disc_type="dvd", plan=BD_PLAN, source_height=480, interlaced=True) == [
        *FF, "-hide_banner", "-nostdin", "-y", "-vaapi_device", "/dev/dri/renderD128", "-i", src,
        "-map", "0:v:0", "-map", "0:a:0", "-map", "0:s:0", "-map", "0:s:1", "-map_chapters", "0",
        "-vf", "format=nv12,hwupload,deinterlace_vaapi", "-c:v", "hevc_vaapi", "-rc_mode", "CQP", "-qp", "24",
        "-c:a", "eac3", "-b:a:0", "640k", "-ac:a:0", "6", "-disposition:a:0", "default",
        "-c:s", "copy", "-disposition:s", "-default", "-progress", "pipe:1", "-nostats", out]

def test_fast_anamorphic_sar_and_h264_metadata():
    argv = build_argv(seed(2), SRC, OUT, disc_type="dvd", plan=BD_PLAN, source_height=480, sar=(32, 27))
    assert argv[argv.index("-bsf:v") + 1] == "hevc_metadata=sample_aspect_ratio=32/27"
    h264 = settings_from(2, codec="h264_vaapi")
    argv = build_argv(h264, SRC, OUT, disc_type="dvd", plan=BD_PLAN, source_height=480, sar=(32, 27))
    assert argv[argv.index("-bsf:v") + 1] == "h264_metadata=sample_aspect_ratio=32/27"

def test_copy_exact():
    assert build_argv(seed(3), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080) == [
        *FF, "-hide_banner", "-nostdin", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a:0",
        "-map", "0:s:0", "-map", "0:s:1", "-map_chapters", "0", "-c", "copy",
        "-disposition:a:0", "default", "-disposition:s", "-default", "-progress", "pipe:1", "-nostats", out]

def test_two_audio_tracks_second_not_default():
    plan = TrackPlan(audio=(A(2, "jpn", 6), A(0, "eng", 2)), subtitles=())
    hb = build_argv(seed(1), SRC, OUT, disc_type="bluray", plan=plan, source_height=1080)
    assert hb[hb.index("--audio") + 1:hb.index("--audio") + 8] == ["3,1", "--aencoder", "eac3,eac3", "--ab", "640,224", "--mixdown", "5point1,stereo"]
    assert hb[hb.index("--subtitle") + 1] == "none"
    ff = build_argv(seed(2), SRC, OUT, disc_type="bluray", plan=plan, source_height=1080)
    assert ["-map", "0:a:2", "-map", "0:a:0"] == ff[ff.index("0:v:0") + 1:ff.index("0:v:0") + 5]
    assert "-disposition:a:1" in ff and ff[ff.index("-disposition:a:1") + 1] == "0"
    assert "-sn" in ff

def test_no_audio():
    plan = TrackPlan(audio=(), subtitles=())
    assert "none" == (hb := build_argv(seed(1), SRC, OUT, disc_type="bluray", plan=plan, source_height=1080))[hb.index("--audio") + 1]
    assert "-an" in build_argv(seed(2), SRC, OUT, disc_type="bluray", plan=plan, source_height=1080)

def test_audio_copy_and_aac_opus_names():
    hb = build_argv(settings_from(1, audio_codec="copy"), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080)
    assert hb[hb.index("--aencoder") + 1] == "copy" and "--ab" not in hb and "--mixdown" not in hb
    assert "av_aac" in build_argv(settings_from(1, audio_codec="aac"), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080)
    ff = build_argv(settings_from(2, audio_codec="opus"), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080)
    assert ff[ff.index("-c:a") + 1] == "libopus"

def test_software_ffmpeg_filters_in_order():
    s = settings_from(1, engine="ffmpeg", codec="x265_10bit", max_height=720, crop="auto", deinterlace="always", speed="medium", quality_bluray=22)
    argv = build_argv(s, SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080, interlaced=True, crop=(1920, 800, 0, 140))
    assert argv[argv.index("-vf") + 1] == "bwdif,crop=1920:800:0:140,scale=-2:720"
    i = argv.index("-c:v")
    assert argv[i:i + 9] == ["-c:v", "libx265", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p10le", "-x265-params"]

def test_vaapi_crop_and_scale():
    s = settings_from(2, max_height=720)
    argv = build_argv(s, SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080, crop=(1920, 800, 0, 140))
    assert argv[argv.index("-vf") + 1] == "crop=1920:800:0:140,format=nv12,hwupload,scale_vaapi=w=-2:h=720"

def test_no_scale_when_source_not_taller():
    argv = build_argv(settings_from(2, max_height=720), SRC, OUT, disc_type="dvd", plan=BD_PLAN, source_height=480)
    assert "scale_vaapi" not in argv[argv.index("-vf") + 1]

def test_handbrake_cap_crop_always_deinterlace():
    argv = build_argv(settings_from(1, max_height=720, crop="auto", deinterlace="always"), SRC, OUT,
                      disc_type="bluray", plan=BD_PLAN, source_height=1080)
    assert argv[argv.index("--crop-mode") + 1] == "auto"
    assert argv[argv.index("--maxHeight") + 1] == "720" and "--decomb" in argv and "--comb-detect" not in argv

def test_window():
    hb = build_argv(seed(1), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080, window=(600, 30))
    assert hb[hb.index("--start-at") + 1] == "seconds:600" and hb[hb.index("--stop-at") + 1] == "seconds:30"
    ff = build_argv(seed(2), SRC, OUT, disc_type="bluray", plan=BD_PLAN, source_height=1080, window=(600, 30))
    assert ff[ff.index("-ss") + 1] == "600" and ff[ff.index("-t") + 1] == "30" and ff.index("-ss") < ff.index("-i")

def test_crop_helpers():
    assert parse_cropdetect("[Parsed_cropdetect_0] x1:0 crop=1920:800:0:140\n... crop=1920:804:0:138") == (1920, 804, 0, 138)
    assert pick_crop([(1920, 800, 0, 140)] * 2 + [(1920, 804, 0, 138)], 1920, 1080) == (1920, 800, 0, 140)
    assert pick_crop([(1920, 800, 0, 140), (1920, 1080, 0, 0), None], 1920, 1080) is None
    assert pick_crop([(1920, 1080, 0, 0)] * 3, 1920, 1080) is None
    assert expected_dar(16 / 9, 1920, 1080, (1920, 800, 0, 140)) == pytest.approx(2.4)

def test_detection_rules():
    assert needs_idet(seed(2), "dvd") and not needs_idet(seed(2), "bluray") and not needs_idet(seed(1), "dvd")
    assert needs_cropdetect(settings_from(2, crop="auto")) and not needs_cropdetect(settings_from(1, crop="auto"))
