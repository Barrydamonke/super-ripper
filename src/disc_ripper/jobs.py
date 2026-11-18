"""The job table: one queue per drive, kept in SQLite so it survives restarts."""

from __future__ import annotations

import json
import sqlite3


def enqueue(
    conn: sqlite3.Connection,
    drive_id: str,
    kind: str,
    params: dict,
    *,
    needs_bytes: int = 0,
    uses_drive: bool = False,
    now: str,
) -> int:
    with conn:
        cur = conn.execute(
            """
            INSERT INTO jobs (drive_id, kind, state, params, needs_bytes, uses_drive, created_at)
            VALUES (?, ?, 'queued', ?, ?, ?, ?)
            """,
            (drive_id, kind, json.dumps(params), needs_bytes, 1 if uses_drive else 0, now),
        )
    return cur.lastrowid


def running_uses_drive(conn: sqlite3.Connection, drive_id: str) -> bool:
    """True while a running job holds the physical drive (the poller leaves it alone)."""
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE drive_id = ? AND state = 'running' AND uses_drive = 1 LIMIT 1",
        (drive_id,),
    ).fetchone()
    return row is not None
