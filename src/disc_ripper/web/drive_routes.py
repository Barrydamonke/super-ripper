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
    "no-access": "No permission – the node's group must be PGID.",
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


def _table(request: Request, conn: sqlite3.Connection, config: AppConfig, status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "_drive_table.html",
        {"items": _drive_items(conn, config.dev_root)},
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
    return _table(request, conn, config)


@router.post("/drives/update")
def drives_update(
    request: Request,
    drive_id: str = Form(...),
    label: str = Form(""),
    enabled: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_conn),
    config: AppConfig = Depends(get_config),
):
    if not drives.update_drive(conn, drive_id, label=label, enabled=enabled is not None):
        return HTMLResponse('<p class="error">Unknown drive.</p>', status_code=404)
    return _table(request, conn, config)
