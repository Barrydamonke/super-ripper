"""Films page: ripped DVDs and Blu-rays, their files and encodes, with the encode actions."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import db, encodes, encoding, joblog, settings
from .deps import templates

router = APIRouter()


def _hms(seconds) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _gb(size) -> str:
    return f"{(size or 0) / 1e9:.1f} GB"


def _day(value: str | None) -> str:
    if not value:
        return ""
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone()
    return f"{when.day} {when.strftime('%b')}"


templates.env.filters["hms"] = _hms
templates.env.filters["gb"] = _gb
templates.env.filters["day"] = _day

DISC_NAMES = {"dvd": "DVD", "bluray": "Blu-ray"}


def _parse_utc(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _active_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = []
    for row in [*encodes.running(conn), *encodes.queued(conn)]:
        item = dict(row)
        item["name"] = Path(row["source"]).name
        item["preset_label"] = encoding.PRESET_LABEL.get(row["preset"], row["preset"])
        item["percent"] = int(round((row["progress"] or 0) * 100))
        if row["state"] == "queued":
            seconds, rough = encoding.estimate_seconds(
                row["source_seconds"], row["preset"], row["disc_type"],
                encodes.recent_speeds(conn, row["preset"], row["disc_type"]))
            item["estimate"] = encoding._hours_minutes(seconds)
            item["rough"] = rough
        rows.append(item)
    return rows


def _raw_countdown(done: list[sqlite3.Row], keep_hours: float, now: datetime) -> str | None:
    stamps = [_parse_utc(r["finished_at"]) for r in done if r["finished_at"]]
    if not stamps:
        return None
    left = (min(stamps).timestamp() + keep_hours * 3600 - now.timestamp()) / 3600
    return "soon" if left <= 0 else f"in {math.ceil(left)} h"


def _file_view(key: str, entry: dict, scan_titles: dict, rows: list[sqlite3.Row],
               keep_hours: float, now: datetime) -> dict:
    path = Path(entry.get("path", ""))
    deleted = bool(entry.get("raw_deleted_at"))
    size = None
    if not deleted:
        try:
            size = path.stat().st_size
        except OSError:
            size = None
    mine = [dict(r) for r in rows if str(r["title"]) == key]
    for r in mine:
        r["preset_label"] = encoding.PRESET_LABEL.get(r["preset"], r["preset"])
        r["percent"] = int(round((r["progress"] or 0) * 100))
        r["output_name"] = Path(r["output"]).name if r["output"] else ""
    done = [r for r in rows if str(r["title"]) == key and r["state"] == "done"]
    busy = any(r["state"] in ("queued", "running") for r in mine)
    name = f"{path.parent.name}/{path.name}" if path.parent.name == "extras" else path.name
    return {
        "title": int(key), "name": name, "role": entry.get("role", ""),
        "seconds": scan_titles.get(int(key), {}).get("seconds"),
        "size": size, "deleted": deleted, "encodes": mine,
        "countdown": None if deleted or busy or not done else _raw_countdown(done, keep_hours, now),
        "can_delete": bool(done) and not deleted and not busy,
    }


def films_context(conn: sqlite3.Connection, *, now: datetime | None = None, error: str | None = None,
                  notice: str | None = None, oob: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    keep_hours = settings.get(conn, "raw_keep_hours")
    rips = []
    for rip in conn.execute("SELECT * FROM video_rips ORDER BY rip_id DESC").fetchall():
        disc = conn.execute("SELECT disc_type, label, scan FROM video_discs WHERE fingerprint = ?",
                            (rip["fingerprint"],)).fetchone()
        scan_titles = {}
        if disc is not None:
            try:
                scan_titles = {t["number"]: t for t in json.loads(disc["scan"] or "{}").get("titles", [])}
            except (ValueError, KeyError, TypeError, AttributeError):
                scan_titles = {}
        rows = encodes.for_rip(conn, rip["rip_id"])
        files = json.loads(rip["files"] or "{}")
        views = [_file_view(key, files[key], scan_titles, rows, keep_hours, now)
                 for key in sorted(files, key=int)]
        logs = [r[0] for r in conn.execute(
            "SELECT id FROM jobs WHERE kind = 'rip_title' AND json_extract(params, '$.rip_id') = ? ORDER BY id",
            (rip["rip_id"],))]
        rips.append({
            "rip_id": rip["rip_id"], "label": disc["label"] if disc else "",
            "disc_type": DISC_NAMES.get(disc["disc_type"], "") if disc else "",
            "created_at": rip["created_at"], "files": views, "logs": logs,
            "can_encode_all": any(not f["deleted"] for f in views),
        })
    return {"active": _active_rows(conn), "rips": rips, "presets": encoding.PRESET_LABEL,
            "error": error, "notice": notice, "oob": oob}


def render_films(conn: sqlite3.Connection, **kwargs) -> str:
    return templates.get_template("_films_list.html").render(**films_context(conn, **kwargs))


def render_encodes(conn: sqlite3.Connection) -> str:
    return templates.get_template("_encodes_panel.html").render(active=_active_rows(conn), target="encodes")


# -- plumbing ------------------------------------------------------------------


def _with_conn(request: Request, fn, *args, **kwargs):
    conn = db.connect(request.app.state.config.db_path)
    try:
        return fn(conn, *args, **kwargs)
    finally:
        conn.close()


def _services(request: Request):
    return request.app.state.services


async def _respond(request: Request, *, error: str | None = None, notice: str | None = None,
                   status_code: int = 200) -> HTMLResponse:
    """The re-rendered films list, or just the dashboard panel when the action came from there."""
    if request.headers.get("HX-Target") == "encodes":
        html = await asyncio.to_thread(_with_conn, request, render_encodes)
    else:
        html = await asyncio.to_thread(_with_conn, request,
                                       lambda conn: render_films(conn, error=error, notice=notice, oob=True))
    return HTMLResponse(html, status_code=status_code)


def _preset_error(preset: str) -> str | None:
    return None if preset in encoding.PRESETS else "Unknown preset."


# -- pages and actions ---------------------------------------------------------


@router.get("/films")
async def films_page(request: Request):
    html = await asyncio.to_thread(_with_conn, request, render_films)
    return templates.TemplateResponse(request, "films.html", {"films_html": html})


@router.post("/films/encode")
async def encode_one(request: Request, rip_id: int = Form(...), title: int = Form(...), preset: str = Form(...)):
    if (bad := _preset_error(preset)):
        return await _respond(request, error=bad, status_code=400)
    encode_id, reason = await _services(request).queue_encode_result(rip_id, title, preset)
    if encode_id is None:
        return await _respond(request, error=reason, status_code=409)
    return await _respond(request)


def _rip_titles(conn, rip_id: int) -> list[int]:
    row = conn.execute("SELECT files FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
    files = json.loads(row["files"] or "{}") if row else {}
    return sorted(int(k) for k, v in files.items() if not v.get("raw_deleted_at"))


@router.post("/films/encode-all")
async def encode_all(request: Request, rip_id: int = Form(...), preset: str = Form(...)):
    if (bad := _preset_error(preset)):
        return await _respond(request, error=bad, status_code=400)
    numbers = await asyncio.to_thread(_with_conn, request, _rip_titles, rip_id)
    queued, reasons = 0, []
    for number in numbers:
        encode_id, reason = await _services(request).queue_encode_result(rip_id, number, preset)
        if encode_id is not None:
            queued += 1
        elif reason and reason not in reasons:
            reasons.append(reason)
    notice = f"Queued {queued} of {len(numbers)} files."
    error = " ".join(reasons) if reasons else None
    return await _respond(request, notice=notice, error=error)


@router.post("/encodes/{encode_id}/cancel")
async def cancel_encode(request: Request, encode_id: int):
    row = await asyncio.to_thread(_with_conn, request, encodes.get, encode_id)
    if row is None:
        return HTMLResponse('<p class="error">Unknown encode.</p>', status_code=404)
    await _services(request).cancel_encode(encode_id)
    return await _respond(request)


@router.post("/encodes/{encode_id}/retry")
async def retry_encode(request: Request, encode_id: int):
    row = await asyncio.to_thread(_with_conn, request, encodes.get, encode_id)
    if row is None:
        return HTMLResponse('<p class="error">Unknown encode.</p>', status_code=404)
    new_id, reason = await _services(request).retry_encode_result(encode_id)
    if new_id is None:
        return await _respond(request, error=reason, status_code=409)
    return await _respond(request)


@router.post("/films/delete-raw")
async def delete_raw(request: Request, rip_id: int = Form(...), title: int = Form(...)):
    error = await _services(request).delete_raw_now(rip_id, title)
    if error is not None:
        return await _respond(request, error=error, status_code=409)
    return await _respond(request, notice="The raw rip was deleted.")


@router.get("/encodes/{encode_id}/log")
async def encode_log(request: Request, encode_id: int):
    path = request.app.state.config.config_dir / joblog.LOG_DIRNAME / f"encode-{encode_id}.log"
    try:
        text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
    except OSError:
        return PlainTextResponse(f"No log for encode #{encode_id}", status_code=404)
    return PlainTextResponse(text)
