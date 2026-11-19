"""The report behind GET /api/busy, which unraid-backend polls before powering off."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from . import jobs
from .schedule import GuardState, local_zone


def _local(value: datetime | str | None) -> str | None:
    """ISO time in the container's zone, with its offset: unambiguous for machines,
    and the same clock time the dashboard shows for people."""
    if not value:
        return None
    when = datetime.fromisoformat(value) if isinstance(value, str) else value
    return when.astimezone(local_zone()).isoformat()


def busy_report(conn: sqlite3.Connection, guard: GuardState) -> dict:
    running = jobs.running_jobs(conn)
    return {
        # Past the grace period nothing may hold shutdown, whatever is running.
        "busy": bool(running) and guard.holds_shutdown,
        "phase": guard.phase,
        "scheduledEnd": _local(guard.scheduled_end),
        "graceEndsAt": _local(guard.grace_ends_at),
        "jobs": [
            {
                "id": row["id"],
                "drive": row["drive_label"] or row["drive_model"],
                "kind": row["kind"],
                "estimatedFinish": _local(row["estimated_finish"]),
            }
            for row in running
        ],
    }
