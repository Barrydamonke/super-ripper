from pathlib import PurePosixPath

import pytest

from disc_ripper import library


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AC/DC", "AC_DC"),
        ("Sigur Rós", "Sigur Rós"),
        ("Live...", "Live"),
        ("  ", "_"),
        ('What?: "Yes"', "What__ _Yes_"),
        ("tab\there", "tab_here"),
        ("  lead and trail  ", "lead and trail"),
        ("a   b", "a b"),
        ("back\\slash|pipe", "back_slash_pipe"),
    ],
)
def test_safe_component_cases(raw, expected):
    assert library.safe_component(raw) == expected


def test_safe_component_length_cap():
    assert len(library.safe_component("a" * 300)) == 120
    # a cut that lands on a trailing dot or space is re-stripped
    assert not library.safe_component("a" * 119 + ". tail").endswith((".", " "))


def test_album_folder():
    assert library.album_folder("Pink Floyd", "The Dark Side of the Moon", "1973", compilation=False) == PurePosixPath(
        "Pink Floyd/The Dark Side of the Moon (1973)"
    )
    assert library.album_folder("Pink Floyd", "The Dark Side of the Moon", "", compilation=False) == PurePosixPath(
        "Pink Floyd/The Dark Side of the Moon"
    )
    assert library.album_folder("Various Artists", "Now 44", "1999", compilation=True) == PurePosixPath(
        "Various Artists/Now 44 (1999)"
    )
    assert library.album_folder("Ignored", "Now 44", "1999", compilation=True).parts[0] == "Various Artists"


def test_track_filename():
    assert library.track_filename(1, "Speak to Me", disc=None, track_artist=None) == "01 - Speak to Me.flac"
    assert library.track_filename(1, "X", disc=2, track_artist=None) == "2-01 - X.flac"
    assert (
        library.track_filename(3, "I Have a Dream", disc=1, track_artist="Westlife")
        == "1-03 - Westlife - I Have a Dream.flac"
    )
    assert library.track_filename(100, "Long", disc=None, track_artist=None) == "100 - Long.flac"
    assert library.track_filename(4, "AC/DC?", disc=None, track_artist=None) == "04 - AC_DC_.flac"
