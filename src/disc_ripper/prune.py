"""Housekeeping: drop old finished jobs and old log files.

A finished job (done, failed, cancelled, interrupted) older than RETAIN_DAYS goes, with its
log files, unless something the app still reads points at it: a rip, a CD rip, an encode or
a film import, or a rip_title job of a rip that still exists (so which attempt is the latest
for a title never changes). Encode and import logs go when they are old and their row is
finished or gone. app.log and the backups are never touched.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import parse_utc
from .joblog import LOG_DIRNAME

log = logging.getLogger(__name__)

RETAIN_DAYS = 30
_FINISHED = ("done", "failed", "cancelled", "interrupted")
_JOB_LOG = re.compile(r"^job-(\d+)(-.+)?\.log$")
_ENCODE_LOG = re.compile(r"^encode-(\d+)\.log$")
_IMPORT_LOG = re.compile(r"^import-(\d+)\.log$")
_ENCODE_FINAL = ("done", "failed", "cancelled")
_IMPORT_BUSY = ("identifying", "needs_identify", "waiting_other", "needs_replace_ok", "importing", "imported")

# Jobs the app still reads, by what points at them.
_REFERENCED = """
    id IN (SELECT rip_id FROM video_rips) OR id IN (SELECT job_id FROM cd_rips)
    OR id IN (SELECT rip_id FROM encodes) OR id IN (SELECT rip_id FROM film_imports)
    OR (kind = 'rip_title' AND (json_extract(params, '$.rip_id') IS NULL
                                OR json_extract(params, '$.rip_id') IN (SELECT rip_id FROM video_rips)))
"""


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError as exc:
        log.warning("prune: can't remove %s: %s", path, exc)
        return False


def _old_file(path: Path, cutoff: datetime) -> bool:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff
    except OSError:
        return False


def prune_old(conn: sqlite3.Connection, config_dir: Path, now: datetime | None = None) -> tuple[int, int]:
    """Returns (jobs removed, log files removed)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETAIN_DAYS)
    logs_dir = Path(config_dir) / LOG_DIRNAME
    removed_jobs = removed_logs = 0

    rows = conn.execute(
        f"SELECT id, finished_at FROM jobs WHERE state IN ({','.join('?' * len(_FINISHED))}) "  # noqa: S608
        f"AND finished_at IS NOT NULL AND NOT ({_REFERENCED})", _FINISHED).fetchall()
    doomed = []
    for row in rows:
        try:
            if parse_utc(row["finished_at"]) < cutoff:
                doomed.append(row["id"])
        except ValueError:
            continue
    if doomed:
        with conn:
            conn.executemany("DELETE FROM jobs WHERE id = ?", [(i,) for i in doomed])
        removed_jobs = len(doomed)
    gone = set(doomed)

    if not logs_dir.is_dir():
        return removed_jobs, removed_logs
    live_jobs = {r[0] for r in conn.execute("SELECT id FROM jobs")}
    encode_state = {r["id"]: r["state"] for r in conn.execute("SELECT id, state FROM encodes")}
    import_state = {r["rip_id"]: r["state"] for r in conn.execute("SELECT rip_id, state FROM film_imports")}
    for path in logs_dir.iterdir():
        if not path.is_file():
            continue
        if m := _JOB_LOG.match(path.name):
            job_id = int(m.group(1))
            drop = job_id in gone or (job_id not in live_jobs and _old_file(path, cutoff))
        elif m := _ENCODE_LOG.match(path.name):
            state = encode_state.get(int(m.group(1)))
            drop = (state is None or state in _ENCODE_FINAL) and _old_file(path, cutoff)
        elif m := _IMPORT_LOG.match(path.name):
            state = import_state.get(int(m.group(1)))
            drop = (state is None or state not in _IMPORT_BUSY) and _old_file(path, cutoff)
        else:
            continue  # app.log and anything else stay
        if drop and _unlink(path):
            removed_logs += 1
    return removed_jobs, removed_logs
