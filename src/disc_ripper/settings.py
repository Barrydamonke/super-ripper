"""User-editable settings, stored as JSON values in the settings table.

Keys starting with "internal." are private state (for example the password
hash). They never appear in get_all() and cannot be set through the form.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

INTERNAL_PREFIX = "internal."
DEFAULT_VIDEO_SELECTION = "-sel:all,+sel:video,+sel:audio,+sel:subtitle,-sel:mvcvideo,=100:all,-10:eng"
_TRUE_VALUES = {"on", "true", "1"}


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    kind: Literal["int", "float", "bool", "path", "url", "choice", "secret", "text"]
    default: Any
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    help: str = ""
    # (value, label) pairs for kind "choice"
    choices: tuple[tuple[str, str], ...] = ()
    group: str = ""


SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        "work_budget_gb", "Work area budget", "int", 60, "GB", 1, 10000,
        "Space the in-progress work area may use on the cache. It covers CD rips and encodes, not DVD and Blu-ray rips (those write to downloads).",
    ),
    SettingDef(
        "cache_min_free_gb", "Minimum free cache space", "int", 20, "GB", 0, 10000,
        "A rip only starts if at least this much stays free on the cache pool (or the downloads disk, for DVD and Blu-ray rips).",
    ),
    SettingDef(
        "auto_start_rips", "Auto-start rips", "bool", True,
        help="Start ripping the main title as soon as a disc is scanned, unless it is held.",
    ),
    SettingDef(
        "shutdown_grace_hours", "Shutdown grace period", "float", 3, "hours", 0, 3.5,
        "How long past the scheduled end jobs may delay shutdown. "
        "unraid-backend stops waiting for the ripper 4 hours after a shutdown request, "
        "so this must stay below that.",
    ),
    SettingDef(
        "cd_rip_mode", "CD rip mode", "choice", "secure",
        help="Secure re-reads every sector until it is sure (slower; best for scratched discs).",
        choices=(("secure", "Secure (re-read until sure)"), ("fast", "Fast")),
    ),
    SettingDef(
        "cd_retries", "CD retries per bad sector", "int", 10, "", 0, 50,
        "How often cyanrip re-reads a bad sector before flagging the track.",
    ),
    SettingDef(
        "cd_approval", "CD approval", "choice", "odd_ones",
        help="Only check the odd ones: an exact match with every track verified files itself.",
        choices=(("odd_ones", "Only check the odd ones"), ("always", "Always ask")),
    ),
    SettingDef("cd_cover_art", "Fetch CD cover art", "bool", True, help="Save cover.jpg from the Cover Art Archive."),
    SettingDef(
        "jellyfin_url", "Jellyfin address", "url", "",
        help="For the music duplicate check and library refresh, e.g. http://192.168.68.143:8096.",
    ),
    SettingDef(
        "jellyfin_api_key", "Jellyfin API key", "secret", "",
        help="Jellyfin Dashboard → API Keys. Never shown again once saved.",
    ),
    SettingDef(
        "schedule_status_url", "Schedule source (unraid-backend /status)", "url", "",
        help="Where to read the power schedule, e.g. http://192.168.1.10:3001/status. "
        "Blank: skip.",
    ),
    SettingDef(
        "schedule_pi_url", "Schedule fallback (Pi /schedule)", "url", "",
        help="Used when the source above can't be read, e.g. http://192.168.1.20:8090/schedule.",
    ),
    SettingDef(
        "raw_retention_hours", "Raw rip retention", "int", 24, "hours", 1, 720,
        "Delete an unkept raw rip this long after its encode passed, even if the job is still waiting.",
    ),
    SettingDef(
        "iso_dir", "ISO folder", "path", "/data/ISOs",
        help="Where data-disc ISOs are saved (a path inside the container).",
    ),
    SettingDef(
        "makemkv_key", "MakeMKV key", "secret", "",
        help="Registration key from makemkv.com. Never shown again once saved.",
        group="DVD and Blu-ray",
    ),
    SettingDef(
        "video_min_title_seconds", "Shortest title to list", "int", 120, "seconds", 0, 3600,
        "Titles shorter than this are ignored when scanning a disc.",
        group="DVD and Blu-ray",
    ),
    SettingDef(
        "makemkv_stall_minutes", "Stop a silent MakeMKV after", "int", 10, "minutes", 1, 240,
        "If MakeMKV prints nothing for this long it is stopped and the job fails, so it can be retried. "
        "MakeMKV 2.0 on Linux sometimes stalls with LG drives.",
        group="DVD and Blu-ray",
    ),
    SettingDef(
        "video_track_selection", "Track selection", "text", DEFAULT_VIDEO_SELECTION,
        help="MakeMKV's default selection string (app_DefaultSelectionString): which audio and subtitle tracks "
             "go into the MKV. Leave as is unless you know the syntax.",
        group="DVD and Blu-ray",
    ),
    SettingDef(
        "encode_slots", "Encodes at once", "int", 1, "", 1, 2,
        "How many encodes may run at the same time. A change applies after the container restarts.",
        group="Encoding",
    ),
    SettingDef(
        "raw_keep_until", "Keep raw rips", "choice", "hours",
        help="Films that are imported are cleaned up once Jellyfin has them. TV-like rips, and films that "
             "can't be imported (no Radarr), use the hours below instead. A disc can always be ripped again.",
        choices=(("hours", "for the hours below after a good encode"), ("jellyfin", "until the film is in Jellyfin")),
        group="Encoding",
    ),
    SettingDef(
        "raw_keep_hours", "Keep raw rips for", "int", 0, "hours", 0, 720,
        "Used when Keep raw rips is set to hours. 0 deletes the raw as soon as the encode passes its check.",
        group="Encoding",
    ),
    SettingDef(
        "encode_stall_minutes", "Stop a silent encode after", "int", 10, "minutes", 1, 240,
        "If an encoder prints nothing for this long it is stopped and the encode fails, so it can be retried.",
        group="Encoding",
    ),
    SettingDef(
        "radarr_url", "Radarr address", "url", "",
        help="Where Radarr runs, e.g. http://192.168.1.10:7878. Blank: films are not identified or imported.",
        group="Radarr",
    ),
    SettingDef(
        "radarr_api_key", "Radarr API key", "secret", "",
        help="Radarr Settings → General → API Key. Never shown again once saved.",
        group="Radarr",
    ),
    SettingDef(
        "ask_before_replace", "Ask before replacing", "bool", True,
        help="When a film is already in Radarr, wait for your OK before replacing its file.",
        group="Radarr",
    ),
    SettingDef(
        "tidy_folders_on_replace", "Tidy folders on replace", "bool", True,
        help="When replacing, rename the film's folder to Radarr's own name for it.",
        group="Radarr",
    ),
)

_BY_KEY = {s.key: s for s in SETTINGS}


class SettingsError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _read(conn: sqlite3.Connection, key: str) -> tuple[bool, Any]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return False, None
    return True, json.loads(row[0])


def _write(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def _clamp(definition: SettingDef, value: Any) -> Any:
    """Stored values predate the current limits (step 1 allowed a grace of 11 h)."""
    if definition.kind not in ("int", "float"):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return definition.default
    if definition.minimum is not None and value < definition.minimum:
        value = definition.minimum
    if definition.maximum is not None and value > definition.maximum:
        value = definition.maximum
    return value


def get(conn: sqlite3.Connection, key: str) -> Any:
    definition = _BY_KEY[key]  # KeyError for unknown keys
    found, value = _read(conn, key)
    return _clamp(definition, value) if found else definition.default


def get_all(conn: sqlite3.Connection) -> dict[str, Any]:
    return {s.key: get(conn, s.key) for s in SETTINGS}


def _parse_one(definition: SettingDef, raw: str | None) -> Any:
    if definition.kind == "bool":
        return raw is not None and raw.strip().lower() in _TRUE_VALUES
    text = (raw or "").strip()
    if definition.kind == "secret":
        return text
    if definition.kind == "text":
        if "\n" in text or "\r" in text:
            raise ValueError("Must be a single line.")
        if len(text) > 2000:
            raise ValueError("Must be at most 2000 characters.")
        return text
    if definition.kind == "choice":
        values = [value for value, _label in definition.choices]
        if text not in values:
            raise ValueError(f"Must be one of: {', '.join(values)}.")
        return text
    if definition.kind == "url":
        return _parse_url(text)
    if definition.kind == "path":
        if not text.startswith("/"):
            raise ValueError("Must be an absolute path starting with /.")
        if ".." in text.split("/"):
            raise ValueError("Must not contain '..'.")
        return text.rstrip("/") or "/"
    try:
        value: float = int(text) if definition.kind == "int" else float(text)
    except ValueError:
        kind = "a whole number" if definition.kind == "int" else "a number"
        raise ValueError(f"Must be {kind}.") from None
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Must be a number.")
    if definition.minimum is not None and value < definition.minimum:
        raise ValueError(f"Must be at least {definition.minimum:g}.")
    if definition.maximum is not None and value > definition.maximum:
        raise ValueError(f"Must be at most {definition.maximum:g}.")
    return value


_URL_ERROR = "Must be blank or an http:// or https:// URL."


def _parse_url(text: str) -> str:
    if not text:
        return ""
    if any(c.isspace() for c in text):
        raise ValueError(_URL_ERROR)
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 (raises ValueError for a non-numeric or out-of-range port)
    except ValueError:
        raise ValueError(_URL_ERROR) from None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(_URL_ERROR)
    return text


def parse_form(form: Mapping[str, str], current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Parse the settings form. A blank secret keeps its current value."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for definition in SETTINGS:
        try:
            value = _parse_one(definition, form.get(definition.key))
            if definition.kind == "secret" and value == "" and current is not None:
                value = current.get(definition.key, "")
            values[definition.key] = value
        except ValueError as exc:
            errors[definition.key] = str(exc)
    if errors:
        raise SettingsError(errors)
    return values


def save(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    for key, value in values.items():
        if key not in _BY_KEY:
            raise KeyError(key)
        _write(conn, key, value)
    conn.commit()


def _check_internal(key: str) -> None:
    if not key.startswith(INTERNAL_PREFIX):
        raise ValueError(f"Internal keys must start with {INTERNAL_PREFIX!r}")


def get_internal(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    _check_internal(key)
    found, value = _read(conn, key)
    return value if found else default


def set_internal(conn: sqlite3.Connection, key: str, value: Any) -> None:
    _check_internal(key)
    _write(conn, key, value)
    conn.commit()
