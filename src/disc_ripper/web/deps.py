"""Shared FastAPI dependencies and the template renderer."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .. import db
from ..config import AppConfig

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


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
