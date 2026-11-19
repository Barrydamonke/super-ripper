"""Dashboard: live drive cards, the schedule panel, actions, /events and /api/busy."""

from __future__ import annotations

import asyncio
import errno
import hmac
import math
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .. import busy, cd_rips, db, drives, jobs, schedule_source
from ..cdtoc import musicbrainz_disc_id
from ..clock import utc_now
from ..events import format_sse
from ..services import Services, drive_key
from ..space import GB
from .deps import templates

router = APIRouter()

KEEPALIVE_SECONDS = 15.0
EJECT_LOCK_TIMEOUT = 5.0
DEFAULT_TEST_MINUTES = 20
MINUTES_RANGE = (1, 360)
SIZE_GB_RANGE = (0.0, 500.0)


# -- rendering (also used by Services to push updates) -------------------------

def _past_grace(guard, finish: datetime | None) -> bool:
    """Would something finishing at `finish` run past the end of the grace period?"""
    if finish is None or guard.grace_ends_at is None or guard.phase not in ("in_window", "draining"):
        return False
    return finish > guard.grace_ends_at


def render_drive_card(
    conn: sqlite3.Connection, drive_row: sqlite3.Row, now: datetime, *, error: str | None = None
) -> str:
    guard = schedule_source.current_guard(conn, now)
    grouped = jobs.drive_jobs(conn, drive_row["id"])
    running_finish = None
    if grouped.running is not None and grouped.running["estimated_finish"]:
        running_finish = datetime.fromisoformat(grouped.running["estimated_finish"])
    audio = drive_row["media_state"] == "disc" and drive_row["disc_type"] in ("audio", "mixed")
    disc = cd_rips.disc_in_drive(conn, drive_row["id"]) if audio else None
    blocking = cd_rips.blocking_rip_for_disc(conn, disc["disc_id"]) if disc else None
    rips = {
        row["job_id"]: row
        for job in grouped.recent
        if job["kind"] == "rip_cd" and (row := cd_rips.get(conn, job["id"])) is not None
    }
    return templates.get_template("_drive_card.html").render(
        drive=drive_row,
        key=drive_key(drive_row["id"]),
        jobs=grouped,
        guard=guard,
        error=error,
        audio=audio,
        disc=disc,
        blocking=blocking,
        rips=rips,
        default_minutes=DEFAULT_TEST_MINUTES,
        running_past_grace=_past_grace(guard, running_finish),
        new_job_past_grace=_past_grace(guard, now + timedelta(minutes=DEFAULT_TEST_MINUTES)),
    )


def render_guard(conn: sqlite3.Connection, now: datetime) -> str:
    cached = schedule_source.load_cached(conn)
    today = cached.entries[now.weekday()] if cached else None
    return templates.get_template("_guard.html").render(
        guard=schedule_source.current_guard(conn, now), cached=cached, today=today,
        grace_hours=_grace_hours(conn),
    )


def _grace_hours(conn: sqlite3.Connection) -> float:
    from .. import settings

    return settings.get(conn, "shutdown_grace_hours")


def _services(request: Request) -> Services:
    return request.app.state.services


def _with_conn(request: Request, fn, *args, **kwargs):
    conn = db.connect(request.app.state.config.db_path)
    try:
        return fn(conn, *args, **kwargs)
    finally:
        conn.close()


def _drive(conn: sqlite3.Connection, drive_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM drives WHERE id = ?", (drive_id,)).fetchone()


def _card_sync(conn, drive_id: str, error: str | None) -> str | None:
    row = _drive(conn, drive_id)
    if row is None:
        return None
    return render_drive_card(conn, row, datetime.now().astimezone(), error=error)


async def _card(request: Request, drive_id: str, *, error: str | None = None, status_code: int = 200):
    html = await asyncio.to_thread(_with_conn, request, _card_sync, drive_id, error)
    if html is None:
        return HTMLResponse('<p class="error">Unknown drive.</p>', status_code=404)
    return HTMLResponse(html, status_code=status_code)


# -- pages ---------------------------------------------------------------------

def _dashboard_sync(conn) -> dict:
    now = datetime.now().astimezone()
    cards = [
        {"key": drive_key(row["id"]), "drive_id": row["id"], "html": render_drive_card(conn, row, now)}
        for row in drives.list_drives(conn)
        if row["present"] and row["enabled"]
    ]
    return {"cards": cards, "guard_html": render_guard(conn, now), "default_minutes": DEFAULT_TEST_MINUTES}


@router.get("/")
async def dashboard(request: Request):
    context = await asyncio.to_thread(_with_conn, request, _dashboard_sync)
    return templates.TemplateResponse(request, "dashboard.html", context)


# -- actions -------------------------------------------------------------------

async def _drive_action(request: Request, drive_id: str, action: str) -> str | None:
    """Eject or close the tray. Returns an error message, or None."""
    svc = _services(request)
    row = await asyncio.to_thread(_with_conn, request, _drive, drive_id)
    if row is None or not row["sr_name"]:
        return "This drive has no /dev/sr node."
    running = await asyncio.to_thread(_with_conn, request, jobs.drive_jobs, drive_id)
    if action == "eject" and running.running is not None and running.running["uses_drive"]:
        await svc.runner.cancel(running.running["id"])
    path = request.app.state.config.dev_root / row["sr_name"]

    def run():
        lock = svc.monitor.drive_lock(drive_id)
        if not lock.acquire(timeout=EJECT_LOCK_TIMEOUT):
            raise OSError(errno.EBUSY, "the drive is busy (still reading the disc?)")
        try:
            drive = svc.drive_factory(path)
            drive.eject() if action == "eject" else drive.close_tray()
        finally:
            lock.release()

    try:
        await asyncio.to_thread(run)
    except OSError as exc:
        verb = "Eject" if action == "eject" else "Close tray"
        return f"{verb} failed: {exc.strerror or exc}"
    return None


@router.post("/dashboard/eject")
async def eject(request: Request, drive_id: str = Form(...)):
    return await _card(request, drive_id, error=await _drive_action(request, drive_id, "eject"))


@router.post("/dashboard/close")
async def close_tray(request: Request, drive_id: str = Form(...)):
    return await _card(request, drive_id, error=await _drive_action(request, drive_id, "close"))


@router.post("/dashboard/rip")
async def rip_cd(request: Request, drive_id: str = Form(...), again: str = Form("")):
    """Rip the audio CD in a drive (the Rip / Rip again buttons)."""
    row = await asyncio.to_thread(_with_conn, request, _drive, drive_id)
    if row is None:
        return HTMLResponse('<p class="error">Unknown drive.</p>', status_code=404)
    if row["media_state"] != "disc" or row["disc_type"] not in ("audio", "mixed"):
        return await _card(request, drive_id, error="Only audio CDs can be ripped here so far.", status_code=400)
    svc = _services(request)
    try:
        toc = await asyncio.to_thread(svc.read_toc, drive_id)
    except OSError as exc:
        return await _card(request, drive_id, error=f"Couldn't read the disc: {exc}", status_code=400)
    blocking = await asyncio.to_thread(
        _with_conn, request, cd_rips.blocking_rip_for_disc, musicbrainz_disc_id(toc)
    )
    if blocking is not None and not again:
        return await _card(request, drive_id)  # the card explains and offers Rip again
    await svc.enqueue_cd_rip(drive_id, toc)
    return await _card(request, drive_id)


def _parse_test_job(minutes: str, size_gb: str) -> tuple[int, float]:
    try:
        mins = int(minutes.strip())
        size = float(size_gb.strip() or "0")
    except ValueError:
        raise ValueError("Minutes must be a whole number and size a number.") from None
    if not MINUTES_RANGE[0] <= mins <= MINUTES_RANGE[1]:
        raise ValueError(f"Minutes must be between {MINUTES_RANGE[0]} and {MINUTES_RANGE[1]}.")
    if not math.isfinite(size) or not SIZE_GB_RANGE[0] <= size <= SIZE_GB_RANGE[1]:
        raise ValueError(f"Size must be between {SIZE_GB_RANGE[0]:g} and {SIZE_GB_RANGE[1]:g} GB.")
    return mins, size


@router.post("/jobs/test")
async def queue_test_job(
    request: Request, drive_id: str = Form(...), minutes: str = Form(""), size_gb: str = Form("0")
):
    row = await asyncio.to_thread(_with_conn, request, _drive, drive_id)
    if row is None:
        return HTMLResponse('<p class="error">Unknown drive.</p>', status_code=404)
    try:
        mins, size = _parse_test_job(minutes, size_gb)
    except ValueError as exc:
        return await _card(request, drive_id, error=str(exc), status_code=400)
    await asyncio.to_thread(
        _with_conn, request, jobs.enqueue, drive_id, "test",
        {"duration_s": mins * 60, "size_gb": size}, needs_bytes=int(size * GB), now=utc_now(),
    )
    _services(request).wake(drive_id)
    return await _card(request, drive_id)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: int):
    row = await asyncio.to_thread(_with_conn, request, jobs.get, job_id)
    if row is None:
        return HTMLResponse('<p class="error">Unknown job.</p>', status_code=404)
    await _services(request).runner.cancel(job_id)
    # A running job finishes cancelling a moment later; the SSE update brings the card up to date.
    return await _card(request, row["drive_id"])


@router.post("/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: int):
    row = await asyncio.to_thread(_with_conn, request, jobs.get, job_id)
    if row is None:
        return HTMLResponse('<p class="error">Unknown job.</p>', status_code=404)
    new_id = await asyncio.to_thread(_with_conn, request, jobs.retry, job_id, utc_now())
    if new_id is not None:
        _services(request).wake(row["drive_id"])
    return await _card(request, row["drive_id"])


# -- live updates ----------------------------------------------------------------

async def event_stream(svc: Services, request) -> AsyncIterator[str]:
    with svc.broker.subscribe() as sub:
        # Subscribe first so nothing published during the burst is missed.
        conn = db.connect(svc.config.db_path)
        try:
            burst = await asyncio.to_thread(_dashboard_sync, conn)
        finally:
            conn.close()
        yield format_sse("guard", burst["guard_html"])
        for card in burst["cards"]:
            yield format_sse(f"drive-{card['key']}", card["html"])
        while not sub.dropped and not await request.is_disconnected():
            try:
                name, data = await asyncio.wait_for(sub.queue.get(), KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            yield format_sse(name, data)


@router.get("/events")
async def events(request: Request):
    return StreamingResponse(
        event_stream(_services(request), request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# -- shutdown guard ----------------------------------------------------------------

def _busy_sync(conn) -> dict:
    return busy.busy_report(conn, schedule_source.current_guard(conn, datetime.now().astimezone()))


@router.get("/api/busy")
async def api_busy(request: Request):
    secret = request.app.state.config.busy_secret
    if not secret:
        return JSONResponse({"error": "BUSY_SECRET not set"}, status_code=503)
    given = request.headers.get("x-shutdown-secret", "")
    if not hmac.compare_digest(given.encode(), secret.encode()):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(await asyncio.to_thread(_with_conn, request, _busy_sync))
