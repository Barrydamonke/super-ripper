"""The job table: one queue per drive, kept in SQLite so it survives restarts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal


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


FinalState = Literal["done", "failed", "cancelled", "interrupted"]
INTERRUPTED_MESSAGE = "Interrupted by a restart or shutdown."


@dataclass
class DriveJobs:
    running: sqlite3.Row | None
    queued: list[sqlite3.Row]
    recent: list[sqlite3.Row]


def get(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def next_job(conn: sqlite3.Connection, drive_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE drive_id = ? AND state IN ('queued', 'waiting_space') ORDER BY id LIMIT 1",
        (drive_id,),
    ).fetchone()


def start(conn: sqlite3.Connection, job_id: int, now: str) -> bool:
    """Mark a queued job running. False if it was cancelled meanwhile."""
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET state = 'running', started_at = ?, message = '' "
            "WHERE id = ? AND state IN ('queued', 'waiting_space')",
            (now, job_id),
        )
    return cur.rowcount == 1


def wait_space(conn: sqlite3.Connection, job_id: int, message: str) -> None:
    with conn:
        conn.execute(
            "UPDATE jobs SET state = 'waiting_space', message = ? "
            "WHERE id = ? AND state IN ('queued', 'waiting_space')",
            (message, job_id),
        )


def finish(
    conn: sqlite3.Connection,
    job_id: int,
    state: FinalState,
    now: str,
    *,
    error: str | None = None,
    message: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "UPDATE jobs SET state = ?, finished_at = ?, error = ?, message = COALESCE(?, message) WHERE id = ?",
            (state, now, error, message, job_id),
        )


def set_progress(
    conn: sqlite3.Connection, job_id: int, progress: float, message: str, estimated_finish: str | None
) -> None:
    with conn:
        conn.execute(
            # Only while running: a write still in flight when the job ends must not
            # overwrite the final message (e.g. "Interrupted by a restart or shutdown.").
            "UPDATE jobs SET progress = ?, message = ?, estimated_finish = ? WHERE id = ? AND state = 'running'",
            (progress, message, estimated_finish, job_id),
        )


def cancel_queued(
    conn: sqlite3.Connection, job_id: int, now: str
) -> Literal["cancelled", "running", "not_found", "finished"]:
    row = get(conn, job_id)
    if row is None:
        return "not_found"
    if row["state"] == "running":
        return "running"
    if row["state"] not in ("queued", "waiting_space"):
        return "finished"
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET state = 'cancelled', finished_at = ? "
            "WHERE id = ? AND state IN ('queued', 'waiting_space')",
            (now, job_id),
        )
    # The worker may have started it between the read and the update.
    return "cancelled" if cur.rowcount == 1 else "running"


def retry(conn: sqlite3.Connection, job_id: int, now: str) -> int | None:
    row = get(conn, job_id)
    if row is None or row["state"] not in ("interrupted", "failed", "cancelled"):
        return None
    return enqueue(
        conn, row["drive_id"], row["kind"], json.loads(row["params"]),
        needs_bytes=row["needs_bytes"], uses_drive=bool(row["uses_drive"]), now=now,
    )


def mark_interrupted(conn: sqlite3.Connection, now: str) -> int:
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET state = 'interrupted', finished_at = ?, message = ? WHERE state = 'running'",
            (now, INTERRUPTED_MESSAGE),
        )
    return cur.rowcount


def running_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT jobs.*, drives.label AS drive_label, drives.model AS drive_model
        FROM jobs JOIN drives ON drives.id = jobs.drive_id
        WHERE jobs.state = 'running' ORDER BY jobs.id
        """
    ).fetchall()


def running_bytes(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(SUM(needs_bytes), 0) FROM jobs WHERE state = 'running'").fetchone()[0]


def drive_jobs(conn: sqlite3.Connection, drive_id: str, recent: int = 5) -> DriveJobs:
    running = conn.execute(
        "SELECT * FROM jobs WHERE drive_id = ? AND state = 'running' ORDER BY id LIMIT 1", (drive_id,)
    ).fetchone()
    queued = conn.execute(
        "SELECT * FROM jobs WHERE drive_id = ? AND state IN ('queued', 'waiting_space') ORDER BY id",
        (drive_id,),
    ).fetchall()
    finished = conn.execute(
        "SELECT * FROM jobs WHERE drive_id = ? AND state IN ('done', 'failed', 'cancelled', 'interrupted') "
        "ORDER BY id DESC LIMIT ?",
        (drive_id, recent),
    ).fetchall()
    return DriveJobs(running, queued, finished)
