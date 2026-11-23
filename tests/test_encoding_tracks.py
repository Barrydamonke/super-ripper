import json

import pytest

from disc_ripper import videofiles
from disc_ripper.encoding import audio_out, pick_tracks
from disc_ripper.presets import SEEDS, PresetSettings
from disc_ripper.videofiles import Stream


def settings(**kw):
    kw = {k: list(v) if isinstance(v, tuple) else v for k, v in kw.items()}
    return PresetSettings.from_dict({**SEEDS[1][1], **kw})


def A(i, lang, ch=6, title="", comment=False):
    return Stream("audio", i, lang, title=title, comment=comment, channels=ch)


def S(i, lang, forced=False):
    return Stream("subtitle", i, lang, forced=forced)


TEXT = json.dumps({"format": {"duration": "60"}, "streams": [
    {"codec_type": "video", "width": 720, "height": 480},
    {"codec_type": "audio", "channels": 6, "codec_name": "ac3", "tags": {"language": "eng"},
     "disposition": {"comment": 0}},
    {"codec_type": "audio", "channels": 2, "tags": {"language": "jpn", "title": "Commentary"},
     "disposition": {"comment": 1}},
    {"codec_type": "subtitle", "tags": {"language": "eng"}, "disposition": {"forced": 1}},
    {"codec_type": "subtitle"},
]})


def test_parse_ffprobe_streams():
    info = videofiles.parse_ffprobe(TEXT)
    a = [x for x in info.streams if x.kind == "audio"]
    s = [x for x in info.streams if x.kind == "subtitle"]
    assert (a[0].index, a[0].language, a[0].channels) == (0, "eng", 6)
    assert a[1].comment and a[1].language == "jpn" and a[1].index == 1
    assert s[0].forced and s[1].language == "und" and s[1].index == 1
    assert info.audio_channels == (6, 2)


def test_first_per_language_in_list_order():
    st = [A(0, "eng"), A(1, "eng", 2), A(2, "jpn"), A(3, "fra")]
    plan = pick_tracks(st, settings(audio_languages=("jpn", "eng")))
    assert [t.index for t in plan.audio] == [2, 0]


def test_all_tracks_and_untagged_last():
    st = [A(0, "und"), A(1, "eng"), A(2, "eng", 2), A(3, "fra")]
    plan = pick_tracks(st, settings(audio_tracks="all"))
    assert [t.index for t in plan.audio] == [1, 2, 0]


def test_untagged_used_when_nothing_tagged_matches():
    plan = pick_tracks([A(0, "und"), A(1, "fra")], settings())
    assert [t.index for t in plan.audio] == [0] and plan.notes == ()


def test_commentary_dropped_or_kept():
    st = [A(0, "eng"), A(1, "eng", 2, title="Director's Commentary"), A(2, "eng", 2, comment=True)]
    assert [t.index for t in pick_tracks(st, settings(audio_tracks="all")).audio] == [0]
    assert [t.index for t in pick_tracks(st, settings(keep_commentary=True)).audio] == [0, 1, 2]


def test_fallback_keeps_first_track_with_note():
    plan = pick_tracks([A(0, "eng"), A(1, "eng", 2)], settings(audio_languages=("jpn",)))
    assert [t.index for t in plan.audio] == [0]
    assert plan.notes == ("No Japanese audio in this rip; kept English.",)


def test_no_audio_at_all():
    assert pick_tracks([S(0, "eng")], settings()).audio == ()


def test_subtitles():
    st = [S(0, "eng"), S(1, "fra"), S(2, "eng", forced=True), S(3, "und")]
    assert [t.index for t in pick_tracks(st, settings()).subtitles] == [0, 2, 3]
    assert [t.index for t in pick_tracks(st, settings(subtitles="forced_only")).subtitles] == [2]
    assert pick_tracks(st, settings(subtitles="none", subtitle_languages=())).subtitles == ()


@pytest.mark.parametrize("mode,ch,expected", [("max51", 8, (6, 640)), ("max51", 2, (2, 224)),
    ("max51", 1, (1, 224)), ("max51", 0, (2, 224)), ("stereo", 6, (2, 224)), ("keep", 8, (8, 640)),
    ("keep", 2, (2, 224))])
def test_audio_out(mode, ch, expected):
    assert audio_out(ch, settings(audio_codec="aac", audio_channels=mode)) == expected


def test_language_aliases_match_either_way():
    st = [A(0, "fre"), A(1, "eng")]
    assert [t.index for t in pick_tracks(st, settings(audio_languages=("fra",))).audio] == [0]
    assert [t.index for t in pick_tracks([A(0, "fra")], settings(audio_languages=("fre",))).audio] == [0]


def test_all_preset_with_both_codes_maps_each_track_once():
    st = [A(0, "fre"), A(1, "eng")]
    picked = pick_tracks(st, settings(audio_tracks="all", audio_languages=("fre", "fra"))).audio
    assert [t.index for t in picked] == [0]


@pytest.mark.parametrize("ch,expected", [(3, 6), (4, 6), (5, 6), (6, 6), (7, 8), (8, 8)])
def test_keep_maps_odd_channel_counts(ch, expected):
    assert audio_out(ch, settings(audio_codec="aac", audio_channels="keep"))[0] == expected
