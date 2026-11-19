"""Shared FastAPI dependencies and the template renderer."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .. import db
from ..config import AppConfig

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _hm(value: str | datetime | None) -> str:
    """HH:MM in the container's local time (TZ) for an ISO string or aware datetime."""
    if not value:
        return ""
    when = datetime.fromisoformat(value) if isinstance(value, str) else value
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().strftime("%H:%M")


templates.env.filters["hm"] = _hm
templates.env.filters["fromjson"] = json.loads


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.config.db_path)
    try:
        yield conn
    finally:
        conn.close()


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"
