import json
import urllib.error
from pathlib import Path

import pytest

from disc_ripper import coverart, library, musicbrainz
from disc_ripper.musicbrainz import MusicBrainz

FIXTURES = Path(__file__).parent / "fixtures" / "mb"
NEVERMIND_DISC = "64e.YDM.N2tZJzmqrN48iWfJ9.4-"
NOW44_DISC2 = "4IlxhiOv_kwgl3u4ry8bDpY4DIg-"
WALL_DISC2 = "uDAm7lKUREWYqiy5SA5BJ.OI6QM-"
WALL_GROUP = "f2026101-945b-3d05-9ef4-aa718fc3feef"


def releases(name):
    return json.loads((FIXTURES / f"discid_{name}.json").read_text(encoding="utf-8"))["releases"]


def test_candidates_for_disc_picks_matching_medium():
    cands = musicbrainz.candidates_for_disc(releases("two_disc_2"), WALL_DISC2)
    wall = next(c for c in cands if c.release_id.startswith("b13b64f6"))
    assert (wall.medium, wall.medium_count) == (2, 2)
    assert wall.title == "The Wall" and wall.album_artist == "Pink Floyd"
    assert len(wall.tracks) == 13
    assert wall.tracks[0].number == 1
    assert wall.release_group_id == WALL_GROUP
    box = next(c for c in cands if c.title == "Oh by the Way")
    assert (box.medium, box.medium_count) == (13, 16)


def test_group_equivalent_pressings():
    groups = musicbrainz.group_equivalent(musicbrainz.candidates_for_disc(releases("pressings"), NEVERMIND_DISC))
    assert len(groups) == 1
    assert len(groups[0]) == 9
    # earliest year first, a full date before a bare year, undated last
    assert groups[0][0].release_id.startswith("8e061dc4")
    assert groups[0][0].date == "1991-09-24"
    assert groups[0][-1].date == ""


def test_choose():
    nevermind = musicbrainz.group_equivalent(musicbrainz.candidates_for_disc(releases("pressings"), NEVERMIND_DISC))
    assert musicbrainz.choose(nevermind, filed_groups={}).release_id.startswith("8e061dc4")

    wall = musicbrainz.group_equivalent(musicbrainz.candidates_for_disc(releases("two_disc_2"), WALL_DISC2))
    assert len(wall) == 2  # The Wall, and the "Oh by the Way" box set
    assert musicbrainz.choose(wall, filed_groups={}) is None
    chosen = musicbrainz.choose(wall, filed_groups={WALL_GROUP: 2})
    assert chosen.title == "The Wall" and chosen.release_group_id == WALL_GROUP
    # filed with a different disc count (another edition) is not a reason to pick it
    assert musicbrainz.choose(wall, filed_groups={WALL_GROUP: 3}) is None


def test_tags_for_compilation():
    [c] = musicbrainz.candidates_for_disc(releases("compilation"), NOW44_DISC2)
    assert c.compilation is True
    assert c.year == "1999"
    tags = [musicbrainz.tags_for(c, t, NOW44_DISC2) for t in c.tracks]
    first = tags[0]
    assert first["ALBUMARTIST"] == "Various Artists"
    assert first["MUSICBRAINZ_ALBUMARTISTID"] == library.VARIOUS_ARTISTS_ID
    assert len({t["ARTIST"] for t in tags}) > 5
    assert (first["TRACKNUMBER"], first["TRACKTOTAL"], first["DISCNUMBER"], first["DISCTOTAL"]) == ("1", "22", "2", "2")
    assert first["MUSICBRAINZ_DISCID"] == NOW44_DISC2
    assert first["MUSICBRAINZ_ALBUMID"] == c.release_id
    assert first["DATE"] == "1999-11-22"
    expected_keys = {
        "TITLE", "ARTIST", "ALBUM", "ALBUMARTIST", "TRACKNUMBER", "TRACKTOTAL", "DISCNUMBER", "DISCTOTAL",
        "DATE", "ORIGINALDATE", "LABEL", "BARCODE", "MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEGROUPID",
        "MUSICBRAINZ_TRACKID", "MUSICBRAINZ_ARTISTID", "MUSICBRAINZ_ALBUMARTISTID", "MUSICBRAINZ_DISCID",
    }
    assert set(first) == expected_keys


def test_parse_release_ref():
    rid = "b13b64f6-85fc-3c1c-8aae-e5adb94d7181"
    assert musicbrainz.parse_release_ref(rid) == rid
    assert musicbrainz.parse_release_ref(f"https://musicbrainz.org/release/{rid}?tab=x") == rid
    assert musicbrainz.parse_release_ref(f"  musicbrainz.org/release/{rid.upper()}/discids ") == rid
    assert musicbrainz.parse_release_ref("https://musicbrainz.org/release-group/" + rid) is None
    assert musicbrainz.parse_release_ref("not a release") is None


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_client_rate_limit_and_user_agent():
    t = FakeTime()
    seen = []

    def fetch(url, headers, timeout):
        seen.append((url, headers))
        return b'{"releases": []}'

    mb = MusicBrainz(fetch=fetch, clock=t.clock, sleep=t.sleep)
    for _ in range(3):
        mb.lookup_discid(NEVERMIND_DISC)
    assert t.sleeps == [1.0, 1.0]
    assert all(h["User-Agent"] == musicbrainz.USER_AGENT for _, h in seen)
    assert "musicbrainz.org/ws/2/discid/" + NEVERMIND_DISC in seen[0][0]
    assert "inc=artist-credits+recordings+release-groups+labels" in seen[0][0]


def _http_error(code):
    return urllib.error.HTTPError("https://musicbrainz.org", code, "x", {}, None)


def test_client_retries_503_then_gives_up():
    t = FakeTime()

    def always_503(url, headers, timeout):
        raise _http_error(503)

    mb = MusicBrainz(fetch=always_503, clock=t.clock, sleep=t.sleep)
    with pytest.raises(musicbrainz.MusicBrainzUnavailable):
        mb.lookup_discid(NEVERMIND_DISC)
    assert t.sleeps == [2, 4, 8]

    answers = [_http_error(429), b'{"releases": [{"id": "x"}]}']

    def flaky(url, headers, timeout):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    mb = MusicBrainz(fetch=flaky, clock=t.clock, sleep=t.sleep)
    assert mb.lookup_discid(NEVERMIND_DISC) == [{"id": "x"}]


def test_client_404_is_no_match():
    def not_found(url, headers, timeout):
        raise _http_error(404)

    mb = MusicBrainz(fetch=not_found, clock=FakeTime().clock, sleep=lambda s: None)
    assert mb.lookup_discid("nope") == []


def test_get_release_fixture():
    body = (FIXTURES / "release_wall.json").read_bytes()
    mb = MusicBrainz(fetch=lambda url, headers, timeout: body, clock=FakeTime().clock, sleep=lambda s: None)
    rel = mb.get_release("b13b64f6-85fc-3c1c-8aae-e5adb94d7181")
    cands = musicbrainz.candidates_for_disc([rel], WALL_DISC2)
    assert cands[0].medium == 2


def test_cover_art():
    seen = []

    def fetch(url, headers, timeout):
        seen.append(url)
        return b"\xff\xd8jpeg"

    assert coverart.fetch_front("abc", fetch=fetch) == b"\xff\xd8jpeg"
    assert seen == ["https://coverartarchive.org/release/abc/front-1200"]

    def missing(url, headers, timeout):
        raise _http_error(404)

    assert coverart.fetch_front("abc", fetch=missing) is None
