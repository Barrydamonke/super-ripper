"""MusicBrainz: look up a disc, pick the release, build the tags.

The client is synchronous (callers run it in a thread). MusicBrainz asks for at
most one request per second and a meaningful User-Agent; both are enforced here.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from . import __version__
from .library import VARIOUS_ARTISTS_ID

USER_AGENT = f"super-ripper/{__version__} ( https://github.com/Barrydamonke/super-ripper )"
BASE = "https://musicbrainz.org/ws/2/"
TIMEOUT = 15.0
MIN_INTERVAL = 1.0
BACKOFF = (2, 4, 8)
_RETRY_CODES = {429, 500, 502, 503, 504}
_MBID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_RELEASE_REF = re.compile(rf"^(?:(?:https?://)?(?:[a-z]+\.)?musicbrainz\.org/release/)?({_MBID})(?:[/?#].*)?$")

Fetcher = Callable[[str, dict[str, str], float], bytes]


class MusicBrainzUnavailable(Exception):
    pass


def http_get(url: str, headers: dict[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed hosts)
        return response.read(20_000_000)


class MusicBrainz:
    def __init__(self, fetch: Fetcher = http_get, clock=time.monotonic, sleep=time.sleep):
        self._fetch = fetch
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()  # one request at a time, across threads

    def _get(self, path: str) -> dict | None:
        """JSON for path, None for 404. Retries busy/rate-limited answers, then gives up."""
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        with self._lock:
            for attempt in range(len(BACKOFF) + 1):
                if self._last is not None:
                    wait = MIN_INTERVAL - (self._clock() - self._last)
                    if wait > 0:
                        self._sleep(wait)
                self._last = self._clock()
                try:
                    return json.loads(self._fetch(BASE + path, headers, TIMEOUT))
                except urllib.error.HTTPError as exc:
                    if exc.code == 404:
                        return None
                    if exc.code not in _RETRY_CODES:
                        raise MusicBrainzUnavailable(f"MusicBrainz answered HTTP {exc.code}") from exc
                    error: Exception = exc
                except (OSError, ValueError) as exc:  # network trouble or a garbled answer
                    error = exc
                if attempt < len(BACKOFF):
                    self._sleep(BACKOFF[attempt])  # longer than MIN_INTERVAL, so no extra wait after it
            raise MusicBrainzUnavailable(f"MusicBrainz unreachable: {error}")

    def lookup_discid(self, disc_id: str) -> list[dict]:
        data = self._get(f"discid/{disc_id}?inc=artist-credits+recordings+release-groups+labels&fmt=json")
        return list(data.get("releases", [])) if data else []

    def get_release(self, release_id: str) -> dict:
        data = self._get(
            f"release/{release_id}?inc=artist-credits+recordings+release-groups+labels+discids&fmt=json"
        )
        if data is None:
            raise MusicBrainzUnavailable("No such release on MusicBrainz.")
        return data


def parse_release_ref(text: str) -> str | None:
    """A release MBID from a bare ID or a musicbrainz.org/release/... URL."""
    match = _RELEASE_REF.match(text.strip().lower())
    return match.group(1) if match else None


@dataclass(frozen=True)
class TrackInfo:
    number: int
    title: str
    artist: str
    recording_id: str
    artist_ids: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    release_id: str
    release_group_id: str
    title: str
    album_artist: str
    album_artist_ids: tuple[str, ...]
    date: str
    original_date: str
    country: str
    label: str
    barcode: str
    medium: int
    medium_count: int
    tracks: tuple[TrackInfo, ...]

    @property
    def compilation(self) -> bool:
        return VARIOUS_ARTISTS_ID in self.album_artist_ids

    @property
    def year(self) -> str:
        return self.date[:4]


def _credit(credit: list[dict] | None) -> tuple[str, tuple[str, ...]]:
    credit = credit or []
    text = "".join(c.get("name", "") + c.get("joinphrase", "") for c in credit)
    ids = tuple(c["artist"]["id"] for c in credit if c.get("artist"))
    return text, ids


def _candidate(release: dict, medium: dict) -> Candidate:
    album_artist, album_artist_ids = _credit(release.get("artist-credit"))
    labels = [li["label"]["name"] for li in release.get("label-info") or [] if li.get("label")]
    group = release.get("release-group") or {}
    tracks = []
    for track in medium.get("tracks", []):
        artist, artist_ids = _credit(track.get("artist-credit"))
        tracks.append(
            TrackInfo(
                number=int(track["position"]),
                title=track.get("title", ""),
                artist=artist or album_artist,
                recording_id=(track.get("recording") or {}).get("id", ""),
                artist_ids=artist_ids or album_artist_ids,
            )
        )
    return Candidate(
        release_id=release["id"],
        release_group_id=group.get("id", ""),
        title=release.get("title", ""),
        album_artist=album_artist,
        album_artist_ids=album_artist_ids,
        date=release.get("date") or "",
        original_date=group.get("first-release-date") or "",
        country=release.get("country") or "",
        label=labels[0] if labels else "",
        barcode=release.get("barcode") or "",
        medium=int(medium["position"]),
        medium_count=len(release.get("media", [])),
        tracks=tuple(tracks),
    )


def candidates_for_disc(releases: list[dict], disc_id: str) -> list[Candidate]:
    """One candidate per release, using the medium that carries this disc ID."""
    out = []
    for release in releases:
        for medium in release.get("media", []):
            if any(d.get("id") == disc_id for d in medium.get("discs", [])):
                out.append(_candidate(release, medium))
                break
    return out


def candidate_for_medium(release: dict, position: int) -> Candidate:
    medium = next(m for m in release.get("media", []) if int(m["position"]) == position)
    return _candidate(release, medium)


def _equivalence_key(c: Candidate) -> tuple:
    return (c.title, c.album_artist, tuple(t.title for t in c.tracks))


def group_equivalent(cands: list[Candidate]) -> list[list[Candidate]]:
    """Pressings with the same title, artist and track titles are one match.

    Within a group: earliest year first, a full date before a bare year (the
    original release over a vaguely dated reissue entry), undated last.
    """
    groups: dict[tuple, list[tuple[int, Candidate]]] = {}
    for index, cand in enumerate(cands):
        groups.setdefault(_equivalence_key(cand), []).append((index, cand))

    def order(item: tuple[int, Candidate]):
        index, cand = item
        return (cand.year or "9999", -len(cand.date), index)

    return [[c for _, c in sorted(members, key=order)] for members in groups.values()]


def choose(groups: list[list[Candidate]], *, filed_groups: Mapping[str, int]) -> Candidate | None:
    """The release to use, or None when it's ambiguous.

    filed_groups: release-group ID -> disc count of albums already in the library.
    A pressing of an album already filed with the same number of discs wins, so
    later discs of a set join it.
    """
    for group in groups:
        for cand in group:
            if filed_groups.get(cand.release_group_id) == cand.medium_count:
                return cand
    if len(groups) == 1:
        return groups[0][0]
    return None


def tags_for(c: Candidate, track: TrackInfo, disc_id: str) -> dict[str, str]:
    """Vorbis comments, with the names Picard uses."""
    return {
        "TITLE": track.title,
        "ARTIST": track.artist,
        "ALBUM": c.title,
        "ALBUMARTIST": c.album_artist,
        "TRACKNUMBER": str(track.number),
        "TRACKTOTAL": str(len(c.tracks)),
        "DISCNUMBER": str(c.medium),
        "DISCTOTAL": str(c.medium_count),
        "DATE": c.date,
        "ORIGINALDATE": c.original_date,
        "LABEL": c.label,
        "BARCODE": c.barcode,
        "MUSICBRAINZ_ALBUMID": c.release_id,
        "MUSICBRAINZ_RELEASEGROUPID": c.release_group_id,
        "MUSICBRAINZ_TRACKID": track.recording_id,
        "MUSICBRAINZ_ARTISTID": "; ".join(track.artist_ids),
        "MUSICBRAINZ_ALBUMARTISTID": "; ".join(c.album_artist_ids),
        "MUSICBRAINZ_DISCID": disc_id,
    }
