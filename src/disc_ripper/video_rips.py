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


PARTIAL = ".partial"


def partial_dir(downloads_dir: Path, rip_id: int, job_id: int) -> Path:
    """Where a rip_title job's MakeMKV writes: inside the rip's own downloads folder, so the
    finished file is renamed into place instead of copied (a Blu-ray needed its size twice)."""
    return downloads_dir / str(rip_id) / PARTIAL / str(job_id)


def drop_partial(downloads_dir: Path, rip_id: int, job_id: int) -> None:
    """Remove a job's partial folder, then .partial and the rip folder if that left them empty."""
    import shutil
    folder = partial_dir(downloads_dir, rip_id, job_id)
    shutil.rmtree(folder, ignore_errors=True)
    for empty in (folder.parent, folder.parent.parent):
        try:
            empty.rmdir()
        except OSError:
            break


def has_downloads(folder: Path) -> bool:
    """The rip's downloads folder holds something besides MakeMKV's partial files."""
    try:
        return any(entry.name != PARTIAL for entry in folder.iterdir())
    except OSError:
        return False


def stuck_in_work(conn: sqlite3.Connection, work_dir: Path, rip_id: int, finishing_job: int | None = None,
                  downloads_dir: Path | None = None) -> bool:
    """A finished title's file still sits where MakeMKV wrote it (its move to downloads failed):
    downloads/<rip>/.partial/<job>/, or /work/<job>/ for rips from before that.
    finishing_job counts as finished (its handler is about to return)."""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM jobs WHERE kind = 'rip_title' AND state = 'done' AND json_extract(params, '$.rip_id') = ?",
        (rip_id,))]
    if finishing_job is not None:
        ids.append(finishing_job)
    folders = [work_dir / str(i) for i in ids]
    if downloads_dir is not None:
        folders += [partial_dir(downloads_dir, rip_id, i) for i in ids]
    return any(f.is_dir() and any(f.glob("*.mkv")) for f in folders)


def blocking_rip(conn: sqlite3.Connection, fingerprint: str, downloads_dir: Path,
                 work_dir: Path) -> dict | None:
    """A rip that means this disc shouldn't auto-start again: one in progress, one with a
    folder in downloads, or one whose move failed and still has its file in /work.

    Returns {"rip_id", "state"}. A row still marked 'ripping' with nothing active (it wasn't
    settled, e.g. after a crash) is reported as what it has become, so "Rip again" is offered."""
    active = conn.execute(
        f"""
        SELECT CAST(json_extract(params, '$.rip_id') AS INTEGER) AS rip_id
        FROM jobs WHERE kind = 'rip_title' AND state IN {_ACTIVE}
          AND json_extract(params, '$.fingerprint') = ?
        ORDER BY id DESC LIMIT 1
        """,  # noqa: S608
        (fingerprint,),
    ).fetchone()
    if active is not None:
        return {"rip_id": active["rip_id"], "state": "ripping"}
    for row in conn.execute(
        "SELECT * FROM video_rips WHERE fingerprint = ? AND state != 'failed' ORDER BY rip_id DESC", (fingerprint,)
    ).fetchall():
        stuck = stuck_in_work(conn, work_dir, row["rip_id"], downloads_dir=downloads_dir)
        if stuck or has_downloads(downloads_dir / str(row["rip_id"])):
            state = row["state"]
            if state == "ripping":
                state = "move_failed" if stuck else "ripped"
            return {"rip_id": row["rip_id"], "state": state}
    return None


def record_file(conn: sqlite3.Connection, rip_id: int, title: int, path: Path, role: str, now: str) -> None:
    row = conn.execute("SELECT files FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
    files = json.loads(row["files"]) if row else {}
    files[str(title)] = {"path": str(path), "role": role}
    with conn:
        conn.execute("UPDATE video_rips SET files = ?, updated_at = ? WHERE rip_id = ?",
                     (json.dumps(files), now, rip_id))


def mark_raw_deleted(conn: sqlite3.Connection, rip_id: int, title: int, now: str) -> None:
    row = conn.execute("SELECT files FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
    files = json.loads(row["files"]) if row else {}
    if str(title) not in files:
        return
    files[str(title)]["raw_deleted_at"] = now
    with conn:
        conn.execute("UPDATE video_rips SET files = ?, updated_at = ? WHERE rip_id = ?",
                     (json.dumps(files), now, rip_id))


def raw_deleted(conn: sqlite3.Connection, rip_id: int, title: int) -> bool:
    row = conn.execute("SELECT files FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
    entry = json.loads(row["files"] or "{}").get(str(title)) if row else None
    return bool(entry and entry.get("raw_deleted_at"))


def clear_raw_deleted(conn: sqlite3.Connection, rip_id: int, title: int) -> None:
    row = conn.execute("SELECT files FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
    files = json.loads(row["files"]) if row else {}
    if str(title) in files:
        files[str(title)].pop("raw_deleted_at", None)
        with conn:
            conn.execute("UPDATE video_rips SET files = ? WHERE rip_id = ?", (json.dumps(files), rip_id))


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


_ENDED_WORDS = {"failed": "failed", "cancelled": "cancelled", "interrupted": "was interrupted"}


def settle(conn: sqlite3.Connection, rip_id: int, work_dir: Path, now: str,
           finishing_job: int | None = None, downloads_dir: Path | None = None) -> str | None:
    """Once nothing of a rip is pending, record what became of it. Returns the state.

    A file still in /work after a failed move makes it move_failed (Move again); otherwise any
    file in downloads makes it ripped, naming the titles that weren't; nothing ripped is failed.
    Called whenever a rip_title job ends (or a queued one is cancelled); idempotent."""
    rip = conn.execute("SELECT * FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
    if rip is None:
        return None
    tries = settled(conn, rip_id, finishing_job)
    if any(state in ("queued", "waiting_space", "running") for _t, _j, state in tries):
        return rip["state"]
    latest: dict[int, tuple[int, str]] = {}
    for title, job_id, state in tries:
        if title not in latest or job_id > latest[title][0]:
            latest[title] = (job_id, state)
    files = json.loads(rip["files"] or "{}")
    stuck = stuck_in_work(conn, work_dir, rip_id, finishing_job, downloads_dir)
    notes = []
    for title, (_job, state) in sorted(latest.items()):
        if str(title) in files:
            continue
        if state in _ENDED_WORDS:
            notes.append(f"title {title} {_ENDED_WORDS[state]}")
        elif state == "done" and not stuck:
            notes.append(f"title {title}'s file is no longer in the work area")
    if stuck:
        state, message = "move_failed", rip["message"] or "Moving the rip to downloads failed."
    elif files:
        state, message = "ripped", ("Not ripped: " + "; ".join(notes) + ".") if notes else ""
    else:
        state = "failed"
        if rip["state"] == "failed" and rip["message"]:
            message = rip["message"]
        else:
            message = ("Nothing ripped: " + "; ".join(notes) + ".") if notes else "Nothing ripped."
    if (state, message) != (rip["state"], rip["message"]):
        set_state(conn, rip_id, state, message, now)
    return state


def fail_if_nothing_done(conn: sqlite3.Connection, rip_id: int, failing_job: int, message: str, now: str) -> None:
    """A rip with no title done and none pending has failed (with this job's error as its message)."""
    for _title, job_id, state in attempts(conn, rip_id):
        if job_id != failing_job and state in ("queued", "waiting_space", "running", "done"):
            return
    set_state(conn, rip_id, "failed", message, now)


def unsettled(conn: sqlite3.Connection) -> list[int]:
    """Rips still marked ripping (settled at start-up: a crash can leave them so)."""
    return [r[0] for r in conn.execute("SELECT rip_id FROM video_rips WHERE state = 'ripping'")]


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
