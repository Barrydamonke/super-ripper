"""Folder and file names in the music library.

    <Album Artist>/<Album> (<Year>)/01 - <Title>.flac
    <Album Artist>/<Album> (<Year>)/1-01 - <Title>.flac          (more than one disc)
    Various Artists/<Album> (<Year>)/01 - <Artist> - <Title>.flac (compilations)

Names come from MusicBrainz or were typed in, so every component is sanitised.
Jellyfin reads the tags, which keep the original text.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

VARIOUS_ARTISTS_ID = "89ad4ac3-39f7-470e-963a-56509c546377"
VARIOUS_ARTISTS = "Various Artists"
COMPONENT_MAX = 120  # characters, for readability
# Bytes: Linux filesystems allow 255 per name; leave room for a " (2)" suffix,
# the track number and ".flac".
COMPONENT_MAX_BYTES = 200

_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
_SPACES = re.compile(r" {2,}")


def _cap(text: str, max_chars: int, max_bytes: int) -> str:
    text = text[:max_chars]
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        # Cut on a character boundary, never through a multi-byte character.
        text = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return text


def safe_component(text: str, *, max_chars: int = COMPONENT_MAX, max_bytes: int = COMPONENT_MAX_BYTES) -> str:
    """A name valid on Linux and Windows (SMB): never empty, never hidden (no leading
    dot), no trailing dot or space, at most max_chars characters and max_bytes bytes."""
    cleaned = _SPACES.sub(" ", _UNSAFE.sub("_", text)).strip().lstrip(". ")
    cleaned = _cap(cleaned, max_chars, max_bytes).rstrip(". ")
    return cleaned or "_"


def album_folder(album_artist: str, album: str, year: str, *, compilation: bool) -> PurePosixPath:
    artist = VARIOUS_ARTISTS if compilation else album_artist
    if year:
        suffix = f" ({year})"
        # Cap the title first so the year always survives: it tells editions apart.
        name = safe_component(
            album, max_chars=COMPONENT_MAX - len(suffix), max_bytes=COMPONENT_MAX_BYTES - len(suffix.encode())
        ) + suffix
    else:
        name = safe_component(album)
    return PurePosixPath(safe_component(artist), name)


def track_filename(track: int, title: str, *, disc: int | None, track_artist: str | None) -> str:
    number = f"{track:02d}" if disc is None else f"{disc}-{track:02d}"
    name = f"{track_artist} - {title}" if track_artist else title
    return f"{number} - {safe_component(name)}.flac"
