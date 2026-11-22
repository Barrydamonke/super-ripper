"""The film_imports table: where each ripped film is in being identified and imported into Radarr."""

from __future__ import annotations

import json
import sqlite3

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
