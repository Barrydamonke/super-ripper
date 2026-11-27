import hashlib
import json

import pytest

from disc_ripper.presets import (HARDWARE_CODECS, SEEDS, PresetError, PresetSettings, disabled_choices,
                                 disabled_fields, fingerprint, from_form, language_name, validate_name)


def test_seeds_round_trip():
    for pid, (name, d) in SEEDS.items():
        assert PresetSettings.from_dict(d).to_dict() == d
    assert SEEDS[1][0] == "Standard" and SEEDS[2][0] == "Fast" and SEEDS[3][0] == "Copy only"
    std = PresetSettings.from_dict(SEEDS[1][1])
    assert (std.engine, std.mode, std.codec, std.speed) == ("handbrake", "software", "x265_10bit", "slow")
    assert (std.quality_dvd, std.quality_bluray, std.crop, std.deinterlace) == (18, 20, "off", "auto")
    assert (std.audio_codec, std.audio_channels, std.bitrate_surround, std.bitrate_stereo) == ("eac3", "max51", 640, 224)
    assert std.audio_languages == ("eng",) and std.audio_tracks == "first_per_language"
    assert std.subtitles == "all" and std.subtitle_languages == ("eng",) and not std.keep_commentary
    fast = PresetSettings.from_dict(SEEDS[2][1])
    assert (fast.engine, fast.mode, fast.codec, fast.quality_dvd, fast.quality_bluray) == ("ffmpeg", "hardware", "hevc_vaapi", 24, 24)
    copy = PresetSettings.from_dict(SEEDS[3][1])
    assert (copy.engine, copy.mode, copy.audio_codec, copy.audio_tracks) == ("ffmpeg", "copy", "copy", "all")


@pytest.mark.parametrize("change,field", [
    ({"engine": "handbrake", "mode": "hardware"}, "mode"),
    ({"engine": "handbrake", "mode": "copy"}, "mode"),
    ({"mode": "software", "codec": "hevc_vaapi"}, "codec"),
    ({"quality_bluray": 52}, "quality_bluray"),
    ({"speed": "placebo"}, "speed"),
    ({"max_height": 900}, "max_height"),
    ({"audio_codec": "eac3", "audio_channels": "keep"}, "audio_channels"),
    ({"audio_codec": "ac3", "bitrate_surround": 768}, "bitrate_surround"),
    ({"bitrate_stereo": 32}, "bitrate_stereo"),
    ({"audio_codec": "ac3", "bitrate_stereo": 768}, "bitrate_stereo"),
    ({"audio_languages": []}, "audio_languages"),
    ({"audio_languages": ["english"]}, "audio_languages"),
    ({"subtitles": "all", "subtitle_languages": []}, "subtitle_languages"),
    ({"bogus": 1}, "bogus"),
    ({"engine": "ffmpeg", "mode": "hardware", "codec": "hevc_vaapi", "quality_dvd": 0}, "quality_dvd"),
])
def test_bad_combinations_rejected(change, field):
    d = {**SEEDS[1][1], **change}
    with pytest.raises(PresetError) as err:
        PresetSettings.from_dict(d)
    assert err.value.field == field


def test_copy_mode_forces_audio_copy():
    d = {**SEEDS[2][1], "mode": "copy", "audio_codec": "eac3"}
    assert PresetSettings.from_dict(d).audio_codec == "copy"


def test_names():
    assert validate_name("  Anime (JPN)+ ") == "Anime (JPN)+"
    for bad in ("", "a" * 41, "x/y", "a:b"):
        with pytest.raises(PresetError):
            validate_name(bad)


def test_fingerprint_ignores_greyed_fields_and_changes_on_edit():
    copy = PresetSettings.from_dict(SEEDS[3][1])
    other = PresetSettings.from_dict({**SEEDS[3][1], "quality_bluray": 30, "speed": "fast"})
    assert fingerprint(copy) == fingerprint(other) and len(fingerprint(copy)) == 12
    std = PresetSettings.from_dict(SEEDS[1][1])
    assert fingerprint(std) != fingerprint(PresetSettings.from_dict({**SEEDS[1][1], "quality_bluray": 21}))


def test_greying():
    fast = PresetSettings.from_dict(SEEDS[2][1])
    assert "no speed setting" in disabled_fields(fast)["speed"]
    copy = PresetSettings.from_dict(SEEDS[3][1])
    assert {"codec", "quality_dvd", "quality_bluray", "speed", "max_height", "deinterlace", "crop",
            "audio_codec", "audio_channels", "bitrate_surround", "bitrate_stereo"} <= set(disabled_fields(copy))
    std = PresetSettings.from_dict(SEEDS[1][1])
    assert set(disabled_choices(std)["mode"]) == {"hardware", "copy"}
    assert "keep" in disabled_choices(std)["audio_channels"]
    assert set(disabled_choices(std)["codec"]) == set(HARDWARE_CODECS)


def test_from_form_keeps_omitted_disabled_fields():
    base = PresetSettings.from_dict({**SEEDS[2][1], "quality_dvd": 22})
    form = {"name": "Fast 2", "engine": "ffmpeg", "mode": "hardware", "codec": "h264_vaapi",
            "quality_bluray": "26", "audio_codec": "aac", "audio_channels": "keep",
            "bitrate_surround": "384", "bitrate_stereo": "160", "audio_languages": "eng, jpn",
            "audio_tracks": "all", "subtitles": "forced_only", "subtitle_languages": "eng",
            "deinterlace": "auto", "crop": "auto", "max_height": "720"}
    name, s = from_form(form, base)
    assert name == "Fast 2" and s.codec == "h264_vaapi" and s.quality_dvd == 22 and s.speed == base.speed
    assert s.audio_languages == ("eng", "jpn") and s.max_height == 720 and s.keep_commentary is False


def test_language_name():
    assert language_name("jpn") == "Japanese" and language_name("und") == "unlabelled" and language_name("xho") == "xho"


def test_fingerprint_follows_the_rule():
    copy = PresetSettings.from_dict(SEEDS[3][1])
    d = {**PresetSettings.from_dict(SEEDS[1][1]).to_dict(), **{k: v for k, v in copy.to_dict().items()
                                                              if k not in disabled_fields(copy)}}
    expected = hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    assert fingerprint(copy) == expected
    other = PresetSettings.from_dict({**SEEDS[3][1], "quality_bluray": 30, "speed": "fast"})
    assert fingerprint(other) == expected


def test_languages_canonicalised_and_deduplicated():
    from disc_ripper.presets import canonical_language, from_form, language_name
    assert canonical_language("fra") == "fre" and canonical_language("ENG") == "eng" and canonical_language("und") == "und"
    assert language_name("fra") == language_name("fre") == "French"
    s = PresetSettings.from_dict({**SEEDS[1][1], "audio_languages": ["fre", "fra", "eng"]})
    assert s.audio_languages == ("fre", "eng")
    _, s = from_form({"name": "X", "audio_languages": "eng, eng"}, PresetSettings.from_dict(SEEDS[1][1]))
    assert s.audio_languages == ("eng",)
