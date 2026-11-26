"""The film_imports table: where each ripped film is in being identified and imported into Radarr."""

from __future__ import annotations

import json
import sqlite3

from . import settings

_COLUMNS = {
    "state", "step", "candidates", "tmdb_id", "movie_id", "title", "year", "root_folder", "encode_id", "replace",
    "old_path", "old_folder", "recycled_path", "new_path", "extras", "message", "error", "imported_at",
    "added_by_us",
}
_JSON_COLUMNS = {"candidates", "replace", "extras"}


def get(conn: sqlite3.Connection, rip_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM film_imports WHERE rip_id = ?", (rip_id,)).fetchone()


def ensure(conn: sqlite3.Connection, rip_id: int, now: str) -> sqlite3.Row:
    """The rip's row, inserted as 'identifying' when it has none."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO film_imports (rip_id, state, created_at, updated_at) "
            "VALUES (?, 'identifying', ?, ?)", (rip_id, now, now))
    return get(conn, rip_id)


def update(conn: sqlite3.Connection, rip_id: int, now: str, **fields) -> None:
    unknown = set(fields) - _COLUMNS
    if unknown:
        raise KeyError(sorted(unknown)[0])
    values = {k: json.dumps(v) if k in _JSON_COLUMNS else v for k, v in fields.items()}
    # Column names come from the whitelist above; values are bound.
    assignments = "".join(f"{k} = :{k}, " for k in values)
    with conn:
        conn.execute(
            f"UPDATE film_imports SET {assignments}updated_at = :now WHERE rip_id = :rip_id",  # noqa: S608
            {**values, "now": now, "rip_id": rip_id})


def claim(conn: sqlite3.Connection, rip_id: int, tmdb_id: int, now: str) -> bool:
    """Start importing this film. False when another rip of the same film is already importing."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        busy = conn.execute(
            "SELECT 1 FROM film_imports WHERE tmdb_id = ? AND state = 'importing' AND rip_id != ?",
            (tmdb_id, rip_id)).fetchone()
        if busy:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE film_imports SET state = 'importing', tmdb_id = ?, updated_at = ? WHERE rip_id = ?",
            (tmdb_id, now, rip_id))
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def recent(conn: sqlite3.Connection, n: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM film_imports WHERE state IN ('imported', 'in_jellyfin') "
        "ORDER BY imported_at DESC, rip_id DESC LIMIT ?", (n,)).fetchall()


def waiting_for(conn: sqlite3.Connection, tmdb_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM film_imports WHERE state = 'waiting_other' AND tmdb_id = ? ORDER BY rip_id",
        (tmdb_id,)).fetchall()


def active_rip_ids(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute(
        "SELECT rip_id FROM film_imports WHERE state IN ('importing', 'imported')")}


def raw_waits_for_jellyfin(conn: sqlite3.Connection, rip_id: int) -> bool:
    """Raws are kept until the film is in Jellyfin, and this rip is a film that will get there:
    it has a main title, Radarr is set up, and the import wasn't skipped as TV."""
    if settings.get(conn, "raw_keep_until") != "jellyfin":
        return False
    if not (settings.get(conn, "radarr_url") and settings.get(conn, "radarr_api_key")):
        return False
    rip = conn.execute("SELECT main_title FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
    if rip is None or rip["main_title"] is None:
        return False
    row = get(conn, rip_id)
    return row is None or row["state"] != "tv_skipped"


def fail_interrupted(conn: sqlite3.Connection, now: str) -> int:
    """After a restart: imports (or identifications) that were running can't be (their flow died); they fail, and Retry resumes."""
    with conn:
        return conn.execute(
            "UPDATE film_imports SET state = 'failed', error = 'Interrupted by a restart; press Retry.', "
            "updated_at = ? WHERE state IN ('importing', 'identifying')", (now,)).rowcount


def resumable_waiting(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rips waiting for another rip of their film, when no rip of that film is importing now."""
    return conn.execute(
        "SELECT * FROM film_imports w WHERE w.state = 'waiting_other' AND w.tmdb_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM film_imports i WHERE i.tmdb_id = w.tmdb_id AND i.state = 'importing') "
        "ORDER BY w.rip_id").fetchall()
