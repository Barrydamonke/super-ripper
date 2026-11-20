"""The video_discs and video_rips tables: scanned discs and what became of their rips."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_ACTIVE = "('queued', 'waiting_space', 'running')"


def save_scan(conn: sqlite3.Connection, fingerprint: str, disc_type: str, label: str, scan_json,
              minlength: int, held: list[str], drive_id: str | None, now: str) -> None:
    """Remember a scanned disc (an upsert; the as_film choice survives a rescan)."""
    if not isinstance(scan_json, str):
        scan_json = json.dumps(scan_json)
    with conn:
        conn.execute(
            """
            INSERT INTO video_discs (fingerprint, disc_type, label, scan, minlength, held, drive_id, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
              disc_type = excluded.disc_type, label = excluded.label, scan = excluded.scan,
              minlength = excluded.minlength, held = excluded.held, drive_id = excluded.drive_id,
              last_seen = excluded.last_seen
            """,
            (fingerprint, disc_type, label, scan_json, minlength, json.dumps(held), drive_id, now),
        )
        if drive_id is not None:  # one disc per drive
            conn.execute("UPDATE video_discs SET drive_id = NULL WHERE drive_id = ? AND fingerprint != ?",
                         (drive_id, fingerprint))


def set_as_film(conn: sqlite3.Connection, fingerprint: str, value: bool) -> None:
    with conn:
        conn.execute("UPDATE video_discs SET as_film = ? WHERE fingerprint = ?", (int(value), fingerprint))


def disc_in_drive(conn: sqlite3.Connection, drive_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM video_discs WHERE drive_id = ? ORDER BY last_seen DESC LIMIT 1", (drive_id,)
    ).fetchone()


def _work_has_files(conn: sqlite3.Connection, work_dir: Path, rip_id: int) -> bool:
    ids = [rip_id] + [r[0] for r in conn.execute(
        "SELECT id FROM jobs WHERE json_extract(params, '$.rip_id') = ? AND id != ?", (rip_id, rip_id))]
    return any((work_dir / str(i)).is_dir() and any((work_dir / str(i)).rglob("*.mkv")) for i in ids)


def blocking_rip(conn: sqlite3.Connection, fingerprint: str, downloads_dir: Path,
                 work_dir: Path) -> sqlite3.Row | None:
    """A rip that means this disc shouldn't auto-start again: one in progress, one whose
    files sit in downloads, or one whose move failed and still has its files in /work."""
    active = conn.execute(
        f"""
        SELECT CAST(json_extract(params, '$.rip_id') AS INTEGER) AS rip_id, 'ripping' AS state
        FROM jobs WHERE kind = 'rip_title' AND state IN {_ACTIVE}
          AND json_extract(params, '$.fingerprint') = ?
        ORDER BY id DESC LIMIT 1
        """,  # noqa: S608
        (fingerprint,),
    ).fetchone()
    if active is not None:
        return active
    for row in conn.execute(
        "SELECT * FROM video_rips WHERE fingerprint = ? AND state IN ('ripped', 'move_failed') "
        "ORDER BY rip_id DESC", (fingerprint,)
    ).fetchall():
        if row["state"] == "ripped" and (downloads_dir / str(row["rip_id"])).exists():
            return row
        if row["state"] == "move_failed" and _work_has_files(conn, work_dir, row["rip_id"]):
            return row
    return None


def record_file(conn: sqlite3.Connection, rip_id: int, title: int, path: Path, role: str, now: str) -> None:
    row = conn.execute("SELECT files FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
    files = json.loads(row["files"]) if row else {}
    files[str(title)] = {"path": str(path), "role": role}
    with conn:
        conn.execute("UPDATE video_rips SET files = ?, updated_at = ? WHERE rip_id = ?",
                     (json.dumps(files), now, rip_id))


def set_state(conn: sqlite3.Connection, rip_id: int, state: str, message: str, now: str) -> None:
    with conn:
        conn.execute("UPDATE video_rips SET state = ?, message = ?, updated_at = ? WHERE rip_id = ?",
                     (state, message, now, rip_id))


def attempts(conn: sqlite3.Connection, rip_id: int) -> list[tuple[int, int, str]]:
    """(title, job id, job state) of every rip_title job of this rip, for titles.should_eject."""
    rows = conn.execute(
        "SELECT CAST(json_extract(params, '$.title') AS INTEGER) AS title, id, state FROM jobs "
        "WHERE kind = 'rip_title' AND json_extract(params, '$.rip_id') = ? ORDER BY id", (rip_id,))
    return [(r["title"], r["id"], r["state"]) for r in rows]


def settled(conn: sqlite3.Connection, rip_id: int, finishing_job: int | None = None) -> list[tuple[int, int, str]]:
    """attempts(), with the job that is about to finish counted as done."""
    return [(t, j, "done" if j == finishing_job else s) for t, j, s in attempts(conn, rip_id)]


def all_moved(conn: sqlite3.Connection, rip_id: int, finishing_job: int | None = None) -> bool:
    """Nothing pending, every title's latest attempt done, and each one's file in downloads."""
    latest: dict[int, tuple[int, str]] = {}
    for title, job_id, state in settled(conn, rip_id, finishing_job):
        if title not in latest or job_id > latest[title][0]:
            latest[title] = (job_id, state)
    row = conn.execute("SELECT files FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
    files = json.loads(row["files"]) if row else {}
    return bool(latest) and all(s == "done" and str(t) in files for t, (_j, s) in latest.items())


def fail_if_nothing_done(conn: sqlite3.Connection, rip_id: int, failing_job: int, message: str, now: str) -> None:
    """A rip with no title done and none pending has failed."""
    for _title, job_id, state in attempts(conn, rip_id):
        if job_id != failing_job and state in ("queued", "waiting_space", "running", "done"):
            return
    set_state(conn, rip_id, "failed", message, now)


def role_position(conn: sqlite3.Connection, rip_id: int, role: str, title: int) -> tuple[int, int]:
    """(k, m): this title's place among the rip's titles of that role."""
    rows = conn.execute(
        "SELECT DISTINCT CAST(json_extract(params, '$.title') AS INTEGER) AS title FROM jobs "
        "WHERE kind = 'rip_title' AND json_extract(params, '$.rip_id') = ? AND json_extract(params, '$.role') = ? "
        "ORDER BY 1", (rip_id, role))
    numbers = [r["title"] for r in rows]
    return (numbers.index(title) + 1 if title in numbers else 1), max(len(numbers), 1)


def earlier_unfinished_jobs(conn: sqlite3.Connection, rip_id: int, title: int, job_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM jobs WHERE kind = 'rip_title' AND id != ? AND state != 'done' "
        "AND state NOT IN ('queued', 'waiting_space', 'running') "
        "AND json_extract(params, '$.rip_id') = ? AND json_extract(params, '$.title') = ?",
        (job_id, rip_id, title))
    return [r["id"] for r in rows]
