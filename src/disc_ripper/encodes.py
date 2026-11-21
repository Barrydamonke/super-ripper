"""The encodes table: one global queue of re-encodes of ripped video files."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import encoding

_ACTIVE = "('queued', 'running')"
_SOURCE_FIELDS = ("rip_id", "title", "role", "source", "disc_type", "source_seconds", "source_bytes",
                  "source_audio", "preset")


def _insert_unless_active(conn: sqlite3.Connection, fields: dict, now: str, check=None) -> int | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        if check is not None and not check(conn):  # evaluated inside the write transaction
            conn.rollback()
            return None
        busy = conn.execute(
            f"SELECT 1 FROM encodes WHERE rip_id = ? AND title = ? AND state IN {_ACTIVE}",  # noqa: S608
            (fields["rip_id"], fields["title"])).fetchone()
        if busy:
            conn.rollback()
            return None
        cur = conn.execute(
            """
            INSERT INTO encodes (rip_id, title, role, source, disc_type, source_seconds, source_bytes,
                                 source_audio, preset, state, created_at)
            VALUES (:rip_id, :title, :role, :source, :disc_type, :source_seconds, :source_bytes,
                    :source_audio, :preset, 'queued', :now)
            """,
            {**fields, "now": now})
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:  # the unique index on active files: refused, like the check above
        conn.rollback()
        return None
    except BaseException:
        conn.rollback()
        raise


def create(conn: sqlite3.Connection, *, rip_id: int, title: int, role: str, source: Path, disc_type: str,
           source_seconds: float, source_bytes: int, source_audio: int, preset: str, now: str,
           check=None) -> int | None:
    """Queue an encode; None when that (rip_id, title) already has one queued or running,
    or when check(conn) (run inside the same transaction) is false."""
    return _insert_unless_active(conn, {
        "rip_id": rip_id, "title": title, "role": role, "source": str(source), "disc_type": disc_type,
        "source_seconds": source_seconds, "source_bytes": source_bytes, "source_audio": source_audio,
        "preset": preset}, now, check)


def get(conn: sqlite3.Connection, encode_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM encodes WHERE id = ?", (encode_id,)).fetchone()


def next_queued(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM encodes WHERE state = 'queued' ORDER BY id LIMIT 1").fetchone()


def start(conn: sqlite3.Connection, encode_id: int, now: str) -> bool:
    """Mark a queued encode running. False if it was cancelled meanwhile."""
    with conn:
        cur = conn.execute(
            "UPDATE encodes SET state = 'running', started_at = ?, message = '' WHERE id = ? AND state = 'queued'",
            (now, encode_id))
    return cur.rowcount == 1


def set_progress(conn: sqlite3.Connection, encode_id: int, progress: float, message: str,
                 finish_iso: str | None) -> None:
    with conn:
        conn.execute(
            # Only while running: a late write must not overwrite the final message.
            "UPDATE encodes SET progress = ?, message = ?, estimated_finish = ? WHERE id = ? AND state = 'running'",
            (progress, message, finish_iso, encode_id))


def wait(conn: sqlite3.Connection, encode_id: int, message: str) -> None:
    """Stay queued, with a reason shown."""
    with conn:
        conn.execute("UPDATE encodes SET message = ? WHERE id = ? AND state = 'queued'", (message, encode_id))


def finish(conn: sqlite3.Connection, encode_id: int, state: str, now: str, *, error: str | None = None,
           output: str | None = None, output_bytes: int | None = None, speed: float | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE encodes SET state = ?, finished_at = ?, error = ?, output = ?, output_bytes = ?, speed = ? "
            "WHERE id = ? AND state = 'running'",  # a late finish must not overwrite a row that already ended
            (state, now, error, output, output_bytes, speed, encode_id))


def cancel_queued(conn: sqlite3.Connection, encode_id: int, now: str) -> str:
    """"cancelled", "running" (the caller must stop it), or "gone" (no such row, or already final)."""
    row = get(conn, encode_id)
    if row is None:
        return "gone"
    if row["state"] == "running":
        return "running"
    if row["state"] != "queued":
        return "gone"
    with conn:
        cur = conn.execute(
            "UPDATE encodes SET state = 'cancelled', finished_at = ? WHERE id = ? AND state = 'queued'",
            (now, encode_id))
    # The worker may have started it between the read and the update.
    return "cancelled" if cur.rowcount == 1 else "running"


def retry(conn: sqlite3.Connection, encode_id: int, now: str) -> int | None:
    """A new queued encode with the same source fields; None if refused (like create)."""
    row = get(conn, encode_id)
    if row is None or row["state"] not in ("failed", "cancelled", "interrupted"):
        return None
    return _insert_unless_active(conn, {k: row[k] for k in _SOURCE_FIELDS}, now)


def mark_interrupted(conn: sqlite3.Connection, now: str) -> int:
    with conn:
        cur = conn.execute(
            "UPDATE encodes SET state = 'interrupted', finished_at = ?, "
            "message = 'Interrupted by a restart or shutdown.' WHERE state = 'running'", (now,))
    return cur.rowcount


def requeue_interrupted(conn: sqlite3.Connection, now: str) -> int:
    """Interrupted encodes go back in the queue (the same rows, started afresh).

    One that has an active sibling (a newer encode of the same file) is cancelled instead."""
    n = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in conn.execute("SELECT id, rip_id, title FROM encodes WHERE state = 'interrupted' ORDER BY id").fetchall():
            busy = conn.execute(
                f"SELECT 1 FROM encodes WHERE rip_id = ? AND title = ? AND state IN {_ACTIVE}",  # noqa: S608
                (row["rip_id"], row["title"])).fetchone()
            if busy:
                conn.execute("UPDATE encodes SET state = 'cancelled', message = ? WHERE id = ?",
                             ("Superseded by a newer encode of this file", row["id"]))
                continue
            conn.execute(
                "UPDATE encodes SET state = 'queued', progress = 0, message = '', error = NULL, "
                "estimated_finish = NULL, started_at = NULL, finished_at = NULL WHERE id = ?", (row["id"],))
            n += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return n


def running(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM encodes WHERE state = 'running' ORDER BY id").fetchall()


def queued(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM encodes WHERE state = 'queued' ORDER BY id").fetchall()


def for_rip(conn: sqlite3.Connection, rip_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM encodes WHERE rip_id = ? ORDER BY id", (rip_id,)).fetchall()


def recent_speeds(conn: sqlite3.Connection, preset: str, disc_type: str, n: int = 5) -> list[float]:
    """Speeds (seconds of film per second) of the last n finished encodes of this preset and disc type."""
    rows = conn.execute(
        "SELECT speed FROM encodes WHERE state = 'done' AND speed IS NOT NULL AND preset = ? AND disc_type = ? "
        "ORDER BY finished_at DESC, id DESC LIMIT ?", (preset, disc_type, n)).fetchall()
    return [r[0] for r in rows]


def reserved_bytes(conn: sqlite3.Connection) -> int:
    """Disk the running encodes are expected to still write (estimated output sizes)."""
    return sum(encoding.estimate_size(r["source_bytes"], r["preset"]) for r in running(conn))
