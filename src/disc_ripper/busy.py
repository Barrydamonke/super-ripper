"""The report behind GET /api/busy, which unraid-backend polls before powering off."""

from __future__ import annotations

import sqlite3

from . import jobs
from .schedule import GuardState


def busy_report(conn: sqlite3.Connection, guard: GuardState) -> dict:
    running = jobs.running_jobs(conn)
    return {
        # Past the grace period nothing may hold shutdown, whatever is running.
        "busy": bool(running) and guard.holds_shutdown,
        "phase": guard.phase,
        "scheduledEnd": guard.scheduled_end.isoformat() if guard.scheduled_end else None,
        "graceEndsAt": guard.grace_ends_at.isoformat() if guard.grace_ends_at else None,
        "jobs": [
            {
                "id": row["id"],
                "drive": row["drive_label"] or row["drive_model"],
                "kind": row["kind"],
                "estimatedFinish": row["estimated_finish"],
            }
            for row in running
        ],
    }
