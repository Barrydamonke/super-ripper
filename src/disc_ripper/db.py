"""SQLite access: connections and schema migrations.

Uses Python's built-in sqlite3 module; the database is a single file in /config.
"""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{4})_.+\.sql$")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # One connection per request: FastAPI may open it in a worker thread and use it
    # in the event-loop thread, but never from two threads at once.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _migrations() -> list[tuple[int, str]]:
    folder = resources.files("disc_ripper") / "migrations"
    found = []
    for entry in folder.iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.read_text(encoding="utf-8")))
    return sorted(found)


BACKUPS_KEPT = 5


def _backup_before_upgrade(conn: sqlite3.Connection, version: int) -> None:
    """Copy the database to <config>/backups/disc-ripper-v<version>.db, keeping the newest few."""
    row = next((r for r in conn.execute("PRAGMA database_list") if r[1] == "main"), None)
    if row is None or not row[2]:  # in-memory: no file
        return
    path = Path(row[2])
    if not path.exists() or path.stat().st_size == 0:
        return
    folder = path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"disc-ripper-v{version}.db"
    target.unlink(missing_ok=True)

    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    old = sorted(folder.glob("disc-ripper-v*.db"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for stale in old[BACKUPS_KEPT:]:
        stale.unlink(missing_ok=True)


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > 0 and any(number > version for number, _sql in _migrations()):
        _backup_before_upgrade(conn, version)
    for number, sql in _migrations():
        if number <= version:
            continue
        # executescript commits any open transaction first; wrap each file explicitly.
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            raise
        version = number
    return version
