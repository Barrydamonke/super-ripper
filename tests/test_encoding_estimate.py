import pytest

from disc_ripper.encoding import estimate
from disc_ripper.filmid import quality_for
from tests.test_encoding_argv import seed, settings_from


def test_defaults_match_step6_for_seeds():
    e = estimate(seed(1), "bluray", 7200, 25_000_000_000, [], [])
    assert e.seconds == pytest.approx(7200 / 0.4) and e.bytes == int(25e9 * 0.35) and e.rough_time and e.rough_size
    assert estimate(seed(1), "dvd", 7200, 6e9, [], []).seconds == pytest.approx(7200 / 1.0)
    assert estimate(seed(2), "bluray", 7200, 25e9, [], []).seconds == pytest.approx(7200 / 2.0)
    assert estimate(seed(2), "bluray", 7200, 25e9, [], []).bytes == int(25e9 * 0.5)
    assert estimate(seed(3), "dvd", 7200, 6e9, [], []).seconds == pytest.approx(7200 / 30.0)


def test_defaults_move_with_settings():
    base = estimate(seed(1), "bluray", 7200, 25e9, [], [])
    faster = estimate(settings_from(1, speed="medium"), "bluray", 7200, 25e9, [], [])
    smaller = estimate(settings_from(1, quality_bluray=26), "bluray", 7200, 25e9, [], [])
    capped = estimate(settings_from(1, max_height=720), "bluray", 7200, 25e9, [], [])
    assert faster.seconds < base.seconds and smaller.bytes == pytest.approx(base.bytes / 2, rel=0.01)
    assert capped.bytes < base.bytes and capped.seconds < base.seconds


def test_history_wins():
    e = estimate(seed(1), "bluray", 7200, 25e9, [0.5, 0.3], [400_000.0])
    assert e.seconds == pytest.approx(7200 / 0.4) and e.bytes == int(7200 * 400_000)
    assert not e.rough_time and not e.rough_size


def test_quality_for():
    assert quality_for("dvd", seed(1)) == "DVD"
    assert quality_for("bluray", seed(1)) == "Bluray-1080p"
    assert quality_for("bluray", seed(3)) == "Remux-1080p"
    assert quality_for("bluray", settings_from(1, max_height=720)) == "Bluray-720p"
    assert quality_for("bluray", settings_from(1, max_height=576)) == "Bluray-576p"
    assert quality_for("bluray", settings_from(1, max_height=480)) == "Bluray-480p"
