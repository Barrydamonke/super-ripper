"""Running cyanrip and reading what it prints.

Formats are taken from cyanrip 0.9.3's source:
- progress (stdout only, rewritten with '\\r'):
  "Ripping and encoding track 4, progress - 63.21%"
- per track, in the log and stdout:
  "Track 4 ripped and encoded successfully!" then lines such as
  "    Accurip v1:  0123ABCD (accurately ripped, confidence 40)",
  "    Accurip v1:  0123ABCD (not found, either a new pressing, or bad rip)",
  "  Accurip:       not found"  (disc not in the AccurateRip database)
- track files are named from {track}, zero-padded only when the disc has more
  than 9 tracks ("1.flac" or "01.flac").
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_PROGRESS = re.compile(r"Ripping (?:and encoding )?track (\d+), progress - (\d+(?:\.\d+)?)%")
_TRACK_DONE = re.compile(r"^Track (\d+) ripped and encoded successfully!")
_CONFIDENCE = re.compile(r"\(accurately ripped, confidence (\d+)\)")


def command_prefix() -> list[str]:
    """cyanrip, or a test stand-in from CYANRIP_BIN (a path, or a JSON argv list)."""
    value = os.environ.get("CYANRIP_BIN", "").strip()
    if not value:
        return ["cyanrip"]
    if value.startswith("["):
        return [str(part) for part in json.loads(value)]
    return [value]


def build_argv(*, device: Path, offset: int, retries: int, mode: str) -> list[str]:
    """Fixed arguments only: nothing here comes from disc or tag text."""
    return [
        *command_prefix(),
        "-d", device.as_posix(),
        "-s", str(int(offset)),
        "-r", str(int(retries)),
        "-P", "3" if mode == "secure" else "0",
        "-N",  # no MusicBrainz lookup: the app identifies the disc itself
        "-U",  # no cover art lookup
        "-G",  # don't embed cover art
        "-o", "flac",
        "-D", "rip",
        "-F", "{track}",
        "-L", "log",
    ]


class LineSplitter:
    """Split a stream into lines on '\\n', '\\r' or '\\r\\n' (progress uses '\\r')."""

    def __init__(self):
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        self._buf += text
        parts = re.split(r"\r\n|\r|\n", self._buf)
        self._buf = parts.pop()
        return parts

    def flush(self) -> list[str]:
        rest, self._buf = self._buf, ""
        return [rest] if rest else []


class ProgressParser:
    def feed(self, line: str) -> tuple[int, float] | None:
        """(track number, fraction of that track done), or None for other lines."""
        match = _PROGRESS.search(line)
        if not match:
            return None
        return int(match.group(1)), min(float(match.group(2)) / 100.0, 1.0)


@dataclass(frozen=True)
class TrackCheck:
    track: int
    status: Literal["verified", "not_found", "mismatch", "unknown"]
    confidence: int = 0


def parse_accuraterip(log_text: str, tracks: list[int]) -> list[TrackCheck]:
    """Per-track AccurateRip result. A track the log doesn't mention is 'unknown'."""
    found: dict[int, TrackCheck] = {}
    current: int | None = None
    for line in log_text.splitlines():
        done = _TRACK_DONE.match(line)
        if done:
            current = int(done.group(1))
            found[current] = TrackCheck(current, "unknown")
            continue
        if current is None:
            continue
        confidence = _CONFIDENCE.search(line)
        if confidence:
            best = max(found[current].confidence, int(confidence.group(1)))
            found[current] = TrackCheck(current, "verified", best)
        elif "Accurip:       not found" in line:
            found[current] = TrackCheck(current, "not_found")
        elif "(not found, either a new pressing, or bad rip)" in line and found[current].status == "unknown":
            found[current] = TrackCheck(current, "mismatch")
    return [found.get(t, TrackCheck(t, "unknown")) for t in tracks]


def track_files(folder: Path) -> dict[int, Path]:
    """Track number -> FLAC file cyanrip wrote ('1.flac', '01.flac', ...)."""
    out = {}
    for path in folder.glob("*.flac"):
        if path.stem.isdigit():
            out[int(path.stem)] = path
    return dict(sorted(out.items()))
