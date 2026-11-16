"""Timestamps stored in the database (UTC, ISO 8601)."""

from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
