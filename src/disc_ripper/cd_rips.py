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


def ensure(conn: sqlite3.Connection, job_id: int, disc_id: str, now: str) -> None:
    """Make sure a rip job has its row (a retried job is a new job without one)."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO cd_rips (job_id, disc_id, state, updated_at) VALUES (?, ?, 'ripping', ?)",
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
          AND (cd_rips.state IN ('needs_look', 'filing', 'filed')
               OR jobs.state IN ('queued', 'waiting_space', 'running'))
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


def reconcile(conn: sqlite3.Connection, work_dir) -> int:
    """After a hard stop: put rips caught mid-way back on the Music page. Run before workers start."""
    from pathlib import Path

    changed = 0
    with conn:
        cur = conn.execute(
            "UPDATE cd_rips SET state = 'needs_look', reasons = ? WHERE state = 'filing'",
            (json.dumps(["filing was interrupted — check the album's library folder, then try again"]),),
        )
        changed += cur.rowcount
        rows = conn.execute(
            """
            SELECT cd_rips.job_id, cd_rips.downloads_path FROM cd_rips JOIN jobs ON jobs.id = cd_rips.job_id
            WHERE cd_rips.state = 'ripping' AND jobs.state NOT IN ('queued', 'waiting_space', 'running')
            """
        ).fetchall()
        for row in rows:
            path = row["downloads_path"]
            work_rip = Path(work_dir) / str(row["job_id"]) / "rip"
            if not path and any(work_rip.glob("*.flac")):
                path = str(work_rip)
            if not path or not any(Path(path).glob("*.flac")):
                continue  # the rip itself never finished: the job's Retry covers it
            conn.execute(
                "UPDATE cd_rips SET state = 'needs_look', downloads_path = ?, reasons = ? WHERE job_id = ?",
                (path, json.dumps(["identification was interrupted — choose the release or type the details in"]),
                 row["job_id"]),
            )
            changed += 1
    return changed


def needs_look(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cd_rips WHERE state = 'needs_look' ORDER BY job_id DESC"
    ).fetchall()


def recently_filed(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cd_rips WHERE state = 'filed' ORDER BY updated_at DESC, job_id DESC LIMIT ?", (limit,)
    ).fetchall()


def remember_disc(conn: sqlite3.Connection, disc_id: str, toc_json: str, track_count: int, drive_id: str,
                  now: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO discs (disc_id, toc, track_count, drive_id, last_seen) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(disc_id) DO UPDATE SET toc = excluded.toc, track_count = excluded.track_count,
                drive_id = excluded.drive_id, last_seen = excluded.last_seen
            """,
            (disc_id, toc_json, track_count, drive_id, now),
        )


def disc_in_drive(conn: sqlite3.Connection, drive_id: str) -> sqlite3.Row | None:
    """The disc most recently seen in this drive."""
    return conn.execute(
        "SELECT * FROM discs WHERE drive_id = ? ORDER BY last_seen DESC LIMIT 1", (drive_id,)
    ).fetchone()
