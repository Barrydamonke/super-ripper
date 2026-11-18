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
_TRUE_VALUES = {"on", "true", "1"}


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    kind: Literal["int", "float", "bool", "path", "url"]
    default: Any
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    help: str = ""


SETTINGS: tuple[SettingDef, ...] = (
    SettingDef(
        "work_budget_gb", "Work area budget", "int", 60, "GB", 1, 10000,
        "Space the in-progress work area may use on the cache.",
    ),
    SettingDef(
        "cache_min_free_gb", "Minimum free cache space", "int", 20, "GB", 0, 10000,
        "A rip only starts if at least this much stays free on the cache pool.",
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


def get(conn: sqlite3.Connection, key: str) -> Any:
    definition = _BY_KEY[key]  # KeyError for unknown keys
    found, value = _read(conn, key)
    return value if found else definition.default


def get_all(conn: sqlite3.Connection) -> dict[str, Any]:
    return {s.key: get(conn, s.key) for s in SETTINGS}


def _parse_one(definition: SettingDef, raw: str | None) -> Any:
    if definition.kind == "bool":
        return raw is not None and raw.strip().lower() in _TRUE_VALUES
    text = (raw or "").strip()
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
    except ValueError:
        raise ValueError(_URL_ERROR) from None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(_URL_ERROR)
    return text


def parse_form(form: Mapping[str, str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for definition in SETTINGS:
        try:
            values[definition.key] = _parse_one(definition, form.get(definition.key))
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
