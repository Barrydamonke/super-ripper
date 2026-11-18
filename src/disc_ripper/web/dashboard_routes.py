"""Dashboard: live drive cards, the schedule panel, actions, /events and /api/busy."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .. import jobs, schedule_source
from ..services import drive_key
from .deps import templates


def render_drive_card(conn: sqlite3.Connection, drive_row: sqlite3.Row, now: datetime) -> str:
    return templates.get_template("_drive_card.html").render(
        drive=drive_row,
        key=drive_key(drive_row["id"]),
        jobs=jobs.drive_jobs(conn, drive_row["id"]),
        guard=schedule_source.current_guard(conn, now),
    )


def render_guard(conn: sqlite3.Connection, now: datetime) -> str:
    return templates.get_template("_guard.html").render(guard=schedule_source.current_guard(conn, now))
