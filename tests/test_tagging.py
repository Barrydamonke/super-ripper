from mutagen.flac import FLAC

from disc_ripper import tagging

from .conftest import make_flac


def test_write_tags_keeps_replaygain(tmp_path):
    path = make_flac(tmp_path / "01.flac")
    f = FLAC(path)
    f["REPLAYGAIN_TRACK_GAIN"] = "-3.20 dB"
    f["REPLAYGAIN_ALBUM_PEAK"] = "0.98"
    f["COMMENT"] = "cyanrip"
    f.save()

    tagging.write_tags(path, {"TITLE": "Breathe", "ARTIST": "Pink Floyd", "LABEL": ""})
    tags = tagging.read_tags(path)
    assert tags == {
        "TITLE": "Breathe",
        "ARTIST": "Pink Floyd",
        "REPLAYGAIN_TRACK_GAIN": "-3.20 dB",
        "REPLAYGAIN_ALBUM_PEAK": "0.98",
    }  # COMMENT replaced; empty values not written


def test_unicode_tags_round_trip(tmp_path):
    path = make_flac(tmp_path / "01.flac")
    tagging.write_tags(path, {"ARTIST": "Sigur Rós", "TITLE": "Ágætis byrjun"})
    assert tagging.read_tags(path)["TITLE"] == "Ágætis byrjun"


def test_duration(tmp_path):
    assert tagging.duration_seconds(make_flac(tmp_path / "a.flac", 2.5)) == 2.5
