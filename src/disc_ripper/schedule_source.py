"""Fetch the power schedule, keep the last good copy, and work out the guard state.

unraid-backend's /status carries the schedule it polls from the Pi; the Pi's own
GET /schedule is the fallback. The last valid copy is stored in the database so
the guard keeps working through a Pi outage or a container restart.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import db, settings
from .clock import utc_now
from .schedule import DayEntry, GuardState, guard_state, parse_schedule

CACHE_KEY = "internal.schedule"
TIMEOUT = 5.0

Fetcher = Callable[[str, float], bytes]


@dataclass(frozen=True)
class CachedSchedule:
    entries: list[DayEntry]
    source: str
    fetched_at: str


def http_get(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 (URL is an admin setting)
        if response.status != 200:
            raise OSError(f"HTTP {response.status}")
        return response.read(1_000_000)


def _try(url: str, fetch: Fetcher, timeout: float, pick: Callable[[object], object]) -> tuple[list | None, str]:
    try:
        data = pick(json.loads(fetch(url, timeout)))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return None, f"{url}: {exc}"
    if parse_schedule(data) is None:
        return None, f"{url}: not a valid 7-day schedule"
    return data, ""


def fetch_schedule(
    status_url: str, pi_url: str, fetch: Fetcher = http_get, timeout: float = TIMEOUT
) -> tuple[list | None, str]:
    """Return (raw schedule, source name), or (None, error text)."""
    if not status_url and not pi_url:
        return None, "No schedule URL set"
    errors = []
    if status_url:
        raw, error = _try(status_url, fetch, timeout, lambda body: body["schedule"])
        if raw is not None:
            return raw, "unraid-backend"
        errors.append(error)
    if pi_url:
        raw, error = _try(pi_url, fetch, timeout, lambda body: body)
        if raw is not None:
            return raw, "pi"
        errors.append(error)
    return None, "; ".join(errors)


def save_cached(conn: sqlite3.Connection, raw: list, source: str, fetched_at: str) -> None:
    settings.set_internal(conn, CACHE_KEY, {"raw": raw, "source": source, "fetched_at": fetched_at})


def load_cached(conn: sqlite3.Connection) -> CachedSchedule | None:
    stored = settings.get_internal(conn, CACHE_KEY)
    if not isinstance(stored, dict):
        return None
    entries = parse_schedule(stored.get("raw"))
    if entries is None:
        return None
    return CachedSchedule(entries, str(stored.get("source", "")), str(stored.get("fetched_at", "")))


def current_guard(conn: sqlite3.Connection, now: datetime) -> GuardState:
    cached = load_cached(conn)
    grace = timedelta(hours=settings.get(conn, "shutdown_grace_hours"))
    return guard_state(cached.entries if cached else None, now, grace)


def _refresh_sync(db_path: Path, fetch: Fetcher) -> str | None:
    conn = db.connect(db_path)
    try:
        raw, detail = fetch_schedule(
            settings.get(conn, "schedule_status_url"), settings.get(conn, "schedule_pi_url"), fetch
        )
        if raw is None:
            return detail
        save_cached(conn, raw, detail, utc_now())
        return None
    finally:
        conn.close()


async def refresh(db_path: Path, fetch: Fetcher = http_get) -> str | None:
    """Fetch and store the schedule. Returns the error text, or None on success."""
    return await asyncio.to_thread(_refresh_sync, db_path, fetch)
