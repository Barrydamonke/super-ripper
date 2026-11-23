"""The encodes table: one global queue of re-encodes of ripped video files."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from . import encoding, presets

_ACTIVE = "('queued', 'running')"
_SOURCE_FIELDS = ("rip_id", "title", "role", "source", "disc_type", "source_seconds", "source_bytes",
                  "source_audio", "preset_id", "preset_name")


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
                                 source_audio, preset_id, preset_name, state, created_at)
            VALUES (:rip_id, :title, :role, :source, :disc_type, :source_seconds, :source_bytes,
                    :source_audio, :preset_id, :preset_name, 'queued', :now)
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
           source_seconds: float, source_bytes: int, source_audio: int, preset_id: int, preset_name: str, now: str,
           check=None) -> int | None:
    """Queue an encode; None when that (rip_id, title) already has one queued or running,
    or when check(conn) (run inside the same transaction) is false."""
    return _insert_unless_active(conn, {
        "rip_id": rip_id, "title": title, "role": role, "source": str(source), "disc_type": disc_type,
        "source_seconds": source_seconds, "source_bytes": source_bytes, "source_audio": source_audio,
        "preset_id": preset_id, "preset_name": preset_name}, now, check)


def get(conn: sqlite3.Connection, encode_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM encodes WHERE id = ?", (encode_id,)).fetchone()


def snapshot(conn: sqlite3.Connection, encode_id: int, name: str, settings_json: str, fingerprint: str) -> None:
    """Record the preset an encode actually ran with; from here on edits to the preset don't touch it."""
    with conn:
        conn.execute("UPDATE encodes SET preset_name = ?, preset_settings = ?, preset_fingerprint = ? WHERE id = ?",
                     (name, settings_json, fingerprint, encode_id))


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
           output: str | None = None, output_bytes: int | None = None, speed: float | None = None, message: str | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE encodes SET state = ?, finished_at = ?, error = ?, output = ?, output_bytes = ?, speed = ?, "
            "message = COALESCE(?, message) "
            "WHERE id = ? AND state = 'running'",  # a late finish must not overwrite a row that already ended
            (state, now, error, output, output_bytes, speed, message, encode_id))


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
    live = presets.get_preset(conn, row["preset_id"]) if row["preset_id"] is not None else None
    if live is None:  # the preset was deleted since: the caller says so
        return None
    return _insert_unless_active(conn, {**{k: row[k] for k in _SOURCE_FIELDS}, "preset_name": live.name}, now)


MIN_SPEED_PROGRESS = 0.05  # an interrupted encode this far along has told us how fast the machine is


def _observed_speed(row, now: str) -> float | None:
    """Seconds of film per second so far, or None if it hadn't got far enough (or the times don't parse)."""
    if (row["progress"] or 0) < MIN_SPEED_PROGRESS or not row["started_at"]:
        return None
    try:
        elapsed = (datetime.fromisoformat(now) - datetime.fromisoformat(row["started_at"])).total_seconds()
    except (ValueError, TypeError):
        return None
    return row["source_seconds"] * row["progress"] / elapsed if elapsed > 0 else None


def mark_interrupted(conn: sqlite3.Connection, now: str) -> int:
    """Running encodes become interrupted. One that got at least 5% along keeps its observed speed,
    so the next guard decision isn't made on the optimistic default again."""
    with conn:
        rows = conn.execute("SELECT * FROM encodes WHERE state = 'running'").fetchall()
        for row in rows:
            conn.execute(
                "UPDATE encodes SET state = 'interrupted', finished_at = ?, speed = ?, "
                "message = 'Interrupted by a restart or shutdown.' WHERE id = ? AND state = 'running'",
                (now, _observed_speed(row, now), row["id"]))
    return len(rows)


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
                "estimated_finish = NULL, started_at = NULL, finished_at = NULL, preset_settings = NULL WHERE id = ?",
                (row["id"],))  # a queued encode follows its preset's current settings again
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


def history(conn: sqlite3.Connection, fingerprint: str, disc_type: str, n: int = 5) -> tuple[list[float], list[float]]:
    """(speeds, sizes) of the last n encodes with these settings and disc type.

    Speeds are seconds of film per second, from encodes that finished or were interrupted far enough
    along to measure (still counting once requeued). Sizes are output bytes per second of film, from
    finished film encodes. Non-positive values are left out: a zero speed would divide by zero."""
    speeds = conn.execute(
        "SELECT speed FROM encodes WHERE state IN ('done', 'interrupted', 'queued', 'running') "
        "AND speed > 0 AND preset_fingerprint = ? AND disc_type = ? "
        "ORDER BY COALESCE(finished_at, created_at) DESC, id DESC LIMIT ?", (fingerprint, disc_type, n)).fetchall()
    sizes = conn.execute(
        "SELECT output_bytes * 1.0 / source_seconds FROM encodes WHERE state = 'done' AND kind = 'film' "
        "AND output_bytes > 0 AND source_seconds > 0 AND preset_fingerprint = ? AND disc_type = ? "
        "ORDER BY COALESCE(finished_at, created_at) DESC, id DESC LIMIT ?", (fingerprint, disc_type, n)).fetchall()
    return [r[0] for r in speeds], [r[0] for r in sizes]


def estimate_for(conn: sqlite3.Connection, row) -> encoding.Estimate | None:
    """Time and size for an encode row from its preset (snapshot or live); None if the preset is gone."""
    found = presets.for_encode(conn, row)
    if found is None:
        return None
    _name, s, fingerprint = found
    speeds, sizes = history(conn, fingerprint, row["disc_type"])
    return encoding.estimate(s, row["disc_type"], row["source_seconds"], row["source_bytes"], speeds, sizes)


def reserved_bytes(conn: sqlite3.Connection) -> int:
    """Disk the running encodes are expected to still write (estimated output sizes)."""
    return sum(e.bytes for e in (estimate_for(conn, r) for r in running(conn)) if e is not None)


def same_file(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:  # one of them doesn't exist
        return a == b


def output_intact(row) -> bool:
    """The encode's output is still a file of the size it was recorded with (and not empty)."""
    if not row["output"]:
        return False
    try:
        path = Path(row["output"])
        return path.is_file() and path.stat().st_size > 0 and path.stat().st_size == row["output_bytes"]
    except OSError:
        return False


def good_encodes(rows, raw: Path) -> list:
    """The finished encodes whose output is intact and that were made from this raw file: the only
    ones that let the raw go. (A later re-rip of the same title is a different file.)"""
    return [r for r in rows if r["state"] == "done" and output_intact(r) and same_file(Path(r["source"]), raw)]
