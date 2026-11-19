"""The cd_rips table: what happened to each CD rip after the audio was read."""

from __future__ import annotations

import json
import sqlite3

from .clock import utc_now

_JSON_FIELDS = {"reasons", "candidates", "accuraterip"}
_FIELDS = {
    "state", "reasons", "candidates", "release_id", "medium", "accuraterip",
    "downloads_path", "filed_path", "album_key", "summary",
}


def create(conn: sqlite3.Connection, job_id: int, disc_id: str, now: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO cd_rips (job_id, disc_id, state, updated_at) VALUES (?, ?, 'ripping', ?)",
            (job_id, disc_id, now),
        )


def get(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cd_rips WHERE job_id = ?", (job_id,)).fetchone()


def update(conn: sqlite3.Connection, job_id: int, **fields) -> None:
    unknown = set(fields) - _FIELDS
    if unknown:
        raise KeyError(", ".join(sorted(unknown)))
    values = {k: json.dumps(v) if k in _JSON_FIELDS and not isinstance(v, str) else v for k, v in fields.items()}
    values["updated_at"] = utc_now()
    columns = ", ".join(f"{k} = ?" for k in values)
    with conn:
        conn.execute(f"UPDATE cd_rips SET {columns} WHERE job_id = ?", (*values.values(), job_id))  # noqa: S608


def blocking_rip_for_disc(conn: sqlite3.Connection, disc_id: str) -> sqlite3.Row | None:
    """A rip that means this disc shouldn't auto-start again: waiting, filed, or still queued/running."""
    return conn.execute(
        """
        SELECT cd_rips.*, jobs.state AS job_state FROM cd_rips JOIN jobs ON jobs.id = cd_rips.job_id
        WHERE cd_rips.disc_id = ?
          AND (cd_rips.state IN ('needs_look', 'filed') OR jobs.state IN ('queued', 'waiting_space', 'running'))
        ORDER BY cd_rips.job_id DESC LIMIT 1
        """,
        (disc_id,),
    ).fetchone()


def earlier_unfinished(conn: sqlite3.Connection, disc_id: str, job_id: int) -> list[int]:
    """Earlier rip jobs of this disc that failed, were cancelled or interrupted."""
    rows = conn.execute(
        """
        SELECT cd_rips.job_id FROM cd_rips JOIN jobs ON jobs.id = cd_rips.job_id
        WHERE cd_rips.disc_id = ? AND cd_rips.job_id != ?
          AND jobs.state IN ('failed', 'cancelled', 'interrupted')
        """,
        (disc_id, job_id),
    ).fetchall()
    return [r[0] for r in rows]


def needs_look(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cd_rips WHERE state = 'needs_look' ORDER BY job_id DESC"
    ).fetchall()


def recently_filed(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cd_rips WHERE state = 'filed' ORDER BY updated_at DESC, job_id DESC LIMIT ?", (limit,)
    ).fetchall()
