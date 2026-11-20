"""Drives page: list detected optical drives, rescan, label and enable them."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from .. import drives
from ..clock import utc_now
from ..config import AppConfig
from .deps import get_config, get_conn, templates

router = APIRouter()

NODE_HINTS = {
    "missing": "Not passed into the container – check the template's device rules, then restart the container.",
    "no-access": "No permission – check the template's two --device-cgroup-rule entries, and that the node's group is PGID.",
}
ABSENT_HINT = "Not detected at last scan."


def _drive_items(conn: sqlite3.Connection, dev_root: Path) -> list[dict]:
    items = []
    for row in drives.list_drives(conn):
        nodes = []
        hints = []
        if row["present"]:
            for name in (row["sr_name"], row["sg_name"]):
                status = drives.node_status(dev_root, name)
                nodes.append({"name": name or "(none)", "status": status})
                if status in NODE_HINTS and NODE_HINTS[status] not in hints:
                    hints.append(NODE_HINTS[status])
        else:
            hints.append(ABSENT_HINT)
        items.append({"row": row, "nodes": nodes, "hints": hints})
    return items


def _table(
    request: Request, conn: sqlite3.Connection, config: AppConfig, status_code: int = 200, error: str | None = None
):
    return templates.TemplateResponse(
        request,
        "_drive_table.html",
        {"items": _drive_items(conn, config.dev_root), "error": error},
        status_code=status_code,
    )


@router.get("/drives")
def drives_page(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    config: AppConfig = Depends(get_config),
):
    return templates.TemplateResponse(
        request, "drives.html", {"items": _drive_items(conn, config.dev_root)}
    )


@router.post("/drives/rescan")
def drives_rescan(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    config: AppConfig = Depends(get_config),
):
    drives.sync(conn, drives.discover(config.sysfs_root), utc_now())
    request.app.state.services.refresh_drives()
    return _table(request, conn, config)


@router.post("/drives/update")
def drives_update(
    request: Request,
    drive_id: str = Form(...),
    label: str = Form(""),
    enabled: str | None = Form(None),
    read_offset: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
    config: AppConfig = Depends(get_config),
):
    offset = None
    if read_offset is not None and read_offset.strip():
        try:
            offset = int(read_offset.strip())
        except ValueError:
            offset = None
        if offset is None or not drives.READ_OFFSET_MIN <= offset <= drives.READ_OFFSET_MAX:
            error = (
                f"Read offset must be a whole number between {drives.READ_OFFSET_MIN} "
                f"and {drives.READ_OFFSET_MAX}."
            )
            return _table(request, conn, config, status_code=400, error=error)
    if not drives.update_drive(conn, drive_id, label=label, enabled=enabled is not None, read_offset=offset):
        return HTMLResponse('<p class="error">Unknown drive.</p>', status_code=404)
    request.app.state.services.refresh_drives()
    return _table(request, conn, config)
