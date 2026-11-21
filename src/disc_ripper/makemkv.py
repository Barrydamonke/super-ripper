"""Running makemkvcon in robot mode (-r) and reading what it prints.

Robot lines are "KEY:v1,v2,...". String values are double-quoted with \\" and
\\ escapes and may contain commas. Pure functions only: no I/O here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_KEY = re.compile(r"^([A-Z]+):(.*)$")
_DURATION = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})$")
_HINT = re.compile(r"too old|expired|registration key", re.IGNORECASE)
UPDATE_HINT = "Update MakeMKV: set a new beta key in Settings or rebuild the image with a newer version."


@dataclass(frozen=True)
class Title:
    number: int
    seconds: int
    size: int
    chapters: int
    source: str
    output_name: str
    segments: str
    name: str
    audio: int = -1  # audio streams the scan listed; -1 when the scan gave no stream list


@dataclass
class Scan:
    titles: list[Title]
    label: str
    messages: list[str] = field(default_factory=list)


def command_prefix() -> list[str]:
    """makemkvcon, or a test stand-in from MAKEMKVCON_BIN (a path, or a JSON argv list)."""
    value = os.environ.get("MAKEMKVCON_BIN", "").strip()
    if not value:
        return ["makemkvcon"]
    if value.startswith("["):
        return [str(part) for part in json.loads(value)]
    return [value]


def info_argv(device: Path, minlength: int) -> list[str]:
    return [*command_prefix(), "-r", "--progress=-same", f"--minlength={int(minlength)}",
            "info", f"dev:{device.as_posix()}"]


def mkv_argv(device: Path, title: int, minlength: int, out_dir: Path) -> list[str]:
    return [*command_prefix(), "-r", "--progress=-same", f"--minlength={int(minlength)}",
            "mkv", f"dev:{device.as_posix()}", str(int(title)), str(out_dir)]


def parse_line(line: str) -> tuple[str, list[str]] | None:
    match = _KEY.match(line.strip("\r\n"))
    if not match:
        return None
    key, rest = match.groups()
    values: list[str] = []
    i, n = 0, len(rest)
    while i <= n:
        if i < n and rest[i] == '"':
            i += 1
            chars = []
            while i < n and rest[i] != '"':
                if rest[i] == "\\" and i + 1 < n:
                    i += 1
                chars.append(rest[i])
                i += 1
            i += 1  # closing quote
            values.append("".join(chars))
            while i < n and rest[i] != ",":  # stray text after the quote
                i += 1
        else:
            end = rest.find(",", i)
            if end < 0:
                end = n
            values.append(rest[i:end])
            i = end
        i += 1  # the comma
    return key, values


def _seconds(text: str) -> int:
    match = _DURATION.match(text.strip())
    if not match:
        return 0
    h, m, s = (int(g) for g in match.groups())
    return h * 3600 + m * 60 + s


def _int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return 0


class RobotParser:
    def __init__(self):
        self._tinfo: dict[int, dict[int, str]] = {}
        self._streams: dict[int, dict[int, str]] = {}  # title -> stream -> type (SINFO id 1)
        self._label = ""
        self._messages: list[str] = []

    def feed(self, line: str) -> float | None:
        parsed = parse_line(line)
        if parsed is None:
            return None
        key, values = parsed
        try:
            if key == "PRGV" and len(values) >= 3:
                total, maximum = int(values[1]), int(values[2])
                if maximum > 0:
                    return max(0.0, min(total / maximum, 1.0))
            elif key == "TINFO" and len(values) >= 4:
                self._tinfo.setdefault(int(values[0]), {})[int(values[1])] = values[3]
            elif key == "SINFO" and len(values) >= 5 and values[2] == "1":
                self._streams.setdefault(int(values[0]), {})[int(values[1])] = values[4]
            elif key == "CINFO" and len(values) >= 3 and values[0] == "2":
                self._label = values[2]
            elif key == "MSG" and len(values) >= 4:
                self._messages.append(values[3])
        except ValueError:
            pass
        return None

    def _audio_count(self, title: int) -> int:
        streams = self._streams.get(title)
        if streams is None:
            return -1
        return sum(1 for kind in streams.values() if kind == "Audio")

    def result(self) -> Scan:
        titles = [
            Title(
                number=number,
                seconds=_seconds(attrs.get(9, "")),
                size=_int(attrs.get(11, "")),
                chapters=_int(attrs.get(8, "")),
                source=attrs.get(16, ""),
                output_name=attrs.get(27, ""),
                segments=attrs.get(26, ""),
                name=attrs.get(2, ""),
                audio=self._audio_count(number),
            )
            for number, attrs in sorted(self._tinfo.items())
        ]
        return Scan(titles, self._label, list(self._messages))


def fingerprint(disc_type: str, label: str, titles: list[Title]) -> str:
    parts = [disc_type, label]
    for t in sorted(titles, key=lambda t: (t.seconds, t.size, t.chapters, t.source)):
        parts.append(f"{t.seconds}|{t.size}|{t.chapters}|{t.source}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def _quote(value: str) -> str:
    value = value.replace("\r", "").replace("\n", "")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def settings_conf(key: str, selection: str) -> str:
    lines = []
    if key:
        lines.append(f"app_Key = {_quote(key)}")
    lines.append(f"app_DefaultSelectionString = {_quote(selection)}")
    lines.append('app_UpdateEnable = "0"')
    return "\n".join(lines) + "\n"


def update_hint(messages: list[str]) -> str | None:
    return UPDATE_HINT if any(_HINT.search(m) for m in messages) else None
