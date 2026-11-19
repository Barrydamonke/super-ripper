"""Read and write FLAC tags (Vorbis comments) with mutagen.

cyanrip's ReplayGain tags are kept through every rewrite: the step-8 CD burner
levels volume with them.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from mutagen.flac import FLAC

_KEEP_PREFIX = "REPLAYGAIN_"


def write_tags(path: Path, tags: Mapping[str, str]) -> None:
    """Replace every tag except REPLAYGAIN_*. Empty values are left out."""
    audio = FLAC(path)
    kept = {k.upper(): v for k, v in (audio.tags or {}).items() if k.upper().startswith(_KEEP_PREFIX)}
    audio.delete()
    audio = FLAC(path)
    if audio.tags is None:
        audio.add_tags()
    for key, values in kept.items():
        audio[key] = values
    for key, value in tags.items():
        if value:
            audio[key.upper()] = value
    audio.save()


def update_tags(path: Path, tags: Mapping[str, str]) -> None:
    """Change only the given tags (an empty value removes that tag)."""
    audio = FLAC(path)
    if audio.tags is None:
        audio.add_tags()
    for key, value in tags.items():
        if value:
            audio[key.upper()] = value
        elif key.upper() in audio:
            del audio[key.upper()]
    audio.save()


def read_tags(path: Path) -> dict[str, str]:
    audio = FLAC(path)
    return {k.upper(): v[0] if v else "" for k, v in (audio.tags or {}).items()}


def duration_seconds(path: Path) -> float:
    return FLAC(path).info.length
