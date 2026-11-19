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
COMPONENT_MAX = 120

_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
_SPACES = re.compile(r" {2,}")


def safe_component(text: str) -> str:
    """A name that is valid on Linux and Windows (SMB), never empty, '.' or '..'."""
    cleaned = _SPACES.sub(" ", _UNSAFE.sub("_", text)).strip()
    cleaned = cleaned[:COMPONENT_MAX].rstrip(". ").lstrip()
    return cleaned or "_"


def album_folder(album_artist: str, album: str, year: str, *, compilation: bool) -> PurePosixPath:
    artist = VARIOUS_ARTISTS if compilation else album_artist
    name = f"{album} ({year})" if year else album
    return PurePosixPath(safe_component(artist), safe_component(name))


def track_filename(track: int, title: str, *, disc: int | None, track_artist: str | None) -> str:
    number = f"{track:02d}" if disc is None else f"{disc}-{track:02d}"
    name = f"{track_artist} - {title}" if track_artist else title
    return f"{number} - {safe_component(name)}.flac"
