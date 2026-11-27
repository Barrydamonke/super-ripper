"""Timestamps stored in the database (UTC, ISO 8601)."""

from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: str) -> datetime:
    """A stored timestamp as an aware UTC datetime (a naive one is taken as UTC)."""
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
