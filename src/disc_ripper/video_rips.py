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
