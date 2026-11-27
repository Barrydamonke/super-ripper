"""Dashboard: live drive cards, the schedule panel, actions, /events and /api/busy."""

from __future__ import annotations

import asyncio
import errno
import hmac
import json
import math
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from .. import busy, cd_rips, db, drives, joblog, jobs, makemkv, presets, schedule_source, settings, video_rips
from .. import titles as titles_mod
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


def _hms(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _gb(size: int) -> str:
    return f"{size / 1e9:.1f} GB"


def _video_context(conn: sqlite3.Connection, drive_row: sqlite3.Row, grouped, config) -> dict:
    """Template context for a DVD/Blu-ray card: the scanned disc, any blocking rip, the current rip."""
    ctx = {"video": None, "video_blocking": None, "video_rip": None, "video_done": {}, "video_notes": {},
           "video_moves": [], "encode_choice": None, "encode_choices": []}
    # Rips from this drive whose move to downloads failed: shown whether or not a disc is in,
    # since the disc ejects once the rip itself is done.
    for rip in conn.execute(
        "SELECT rip_id, message FROM video_rips WHERE state = 'move_failed' "
        "AND rip_id IN (SELECT id FROM jobs WHERE drive_id = ?) ORDER BY rip_id", (drive_row["id"],)
    ).fetchall():
        if not any(s in ("queued", "waiting_space", "running") for _t, _j, s in video_rips.attempts(conn, rip["rip_id"])):
            ctx["video_moves"].append({"rip_id": rip["rip_id"], "message": rip["message"]})
    disc_fp = None
    if drive_row["media_state"] == "disc" and drive_row["disc_type"] in ("dvd", "bluray"):
        # How this disc will be encoded: its rip's choice, else the drive's pending choice, else the setting.
        ctx["encode_choice"] = drive_row["encode_choice"] or presets.default_choice(conn)
        ctx["encode_choices"] = presets.choices(conn)
        row = video_rips.disc_in_drive(conn, drive_row["id"])
        if row is not None:
            disc_fp = row["fingerprint"]
            scan = json.loads(row["scan"])
            parsed = [makemkv.Title(**t) for t in scan.get("titles", [])]
            tv = titles_mod.looks_like_tv(parsed)
            default = titles_mod.main_title(parsed)
            ctx["video"] = {
                "fingerprint": row["fingerprint"],
                "held": json.loads(row["held"]),
                "as_film": bool(row["as_film"]),
                "tv": tv,
                "no_main": tv and not row["as_film"],
                "default_main": default.number if default else None,
                "titles": [
                    {"number": t.number, "length": _hms(t.seconds), "size": _gb(t.size),
                     "chapters": t.chapters, "source": t.source}
                    for t in parsed
                ],
            }
            ctx["video_blocking"] = video_rips.blocking_rip(conn, disc_fp, config.downloads_dir, config.work_dir)
            rip = conn.execute(
                "SELECT * FROM video_rips WHERE fingerprint = ? ORDER BY rip_id DESC LIMIT 1", (disc_fp,)
            ).fetchone()
            if rip is not None:
                latest: dict[int, str] = {}
                for title, _job, state in video_rips.attempts(conn, rip["rip_id"]):
                    latest[title] = state  # attempts are in job order: the last one wins
                ctx["video_rip"] = {
                    "rip_id": rip["rip_id"], "state": rip["state"], "message": rip["message"],
                    "main_title": rip["main_title"], "titles": latest,
                }
                if rip["state"] == "ripping" and rip["encode_preset"]:
                    ctx["encode_choice"] = rip["encode_preset"]
    # The recent line for each finished rip_title job: the file it made and the scanned length/size.
    for job in grouped.recent:
        if job["kind"] != "rip_title" or job["state"] != "done":
            continue
        params = json.loads(job["params"])
        rip = conn.execute("SELECT files, state, message FROM video_rips WHERE rip_id = ?",
                           (params.get("rip_id"),)).fetchone()
        entry = json.loads(rip["files"]).get(str(params.get("title"))) if rip else None
        if not entry and rip is not None and rip["state"] == "move_failed":
            ctx["video_notes"][job["id"]] = rip["message"] or "Moving the rip to downloads failed."
        disc = conn.execute("SELECT scan FROM video_discs WHERE fingerprint = ?",
                            (params.get("fingerprint"),)).fetchone()
        if not entry or disc is None:
            continue
        found = next((t for t in json.loads(disc["scan"]).get("titles", [])
                      if t["number"] == params.get("title")), None)
        if found:
            ctx["video_done"][job["id"]] = {
                "rip_id": params["rip_id"], "file": entry["path"].replace("\\", "/").rsplit("/", 1)[-1],
                "length": _hms(found["seconds"]), "size": _gb(found["size"]),
            }
    return ctx


def render_drive_card(
    conn: sqlite3.Connection, drive_row: sqlite3.Row, now: datetime, *, error: str | None = None,
    config, dvd_wait_pending=None,
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
        **_video_context(conn, drive_row, grouped, config),
        dvd_waiting=bool(dvd_wait_pending and dvd_wait_pending(drive_row["id"])),
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


def _card_sync(conn, config, drive_id: str, error: str | None, dvd_wait_pending=None) -> str | None:
    row = _drive(conn, drive_id)
    if row is None:
        return None
    return render_drive_card(conn, row, datetime.now().astimezone(), error=error, config=config,
                             dvd_wait_pending=dvd_wait_pending)


async def _card(request: Request, drive_id: str, *, error: str | None = None, status_code: int = 200):
    html = await asyncio.to_thread(_with_conn, request, _card_sync, request.app.state.config, drive_id, error,
                                  _services(request).dvd_wait_pending)
    if html is None:
        return HTMLResponse('<p class="error">Unknown drive.</p>', status_code=404)
    return HTMLResponse(html, status_code=status_code)


# -- pages ---------------------------------------------------------------------

def _dashboard_sync(conn, config, dvd_wait_pending=None) -> dict:
    now = datetime.now().astimezone()
    cards = [
        {"key": drive_key(row["id"]), "drive_id": row["id"], "html": render_drive_card(conn, row, now, config=config, dvd_wait_pending=dvd_wait_pending)}
        for row in drives.list_drives(conn)
        if row["present"] and row["enabled"]
    ]
    from .films_routes import render_encodes

    return {"cards": cards, "guard_html": render_guard(conn, now), "encodes_html": render_encodes(conn),
            "default_minutes": DEFAULT_TEST_MINUTES}


def _burst_sync(conn, config, dvd_wait_pending=None) -> dict:
    """What a (re)connecting event stream sends: the dashboard's parts plus the Films list."""
    from .films_routes import render_films

    return {**_dashboard_sync(conn, config, dvd_wait_pending), "films_html": render_films(conn)}


@router.get("/")
async def dashboard(request: Request):
    context = await asyncio.to_thread(_with_conn, request, _dashboard_sync, request.app.state.config,
                                      _services(request).dvd_wait_pending)
    return templates.TemplateResponse(request, "dashboard.html", context)


# -- actions -------------------------------------------------------------------

async def _drive_action(request: Request, drive_id: str, action: str) -> str | None:
    """Eject or close the tray. Returns an error message, or None."""
    svc = _services(request)
    row = await asyncio.to_thread(_with_conn, request, _drive, drive_id)
    if row is None or not row["sr_name"]:
        return "This drive has no /dev/sr node."
    if action == "eject":
        await svc.cancel_for_eject(drive_id)
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
    # Refused (already queued, or already ripped without "again"): the card explains why.
    await svc.enqueue_cd_rip(drive_id, toc, again=bool(again))
    return await _card(request, drive_id)


# -- video discs -----------------------------------------------------------------

RIP_REFUSED = "Couldn't start the rip — the disc may have changed; scan again by re-inserting it."
RIP_NOTHING_TICKED = "Tick at least one title to rip."
RIP_MAIN_IS_EXTRA = "The main title can't also be an extra."
RIP_ACTIVE = "A rip of this disc is already queued or ripping."
RIP_ALREADY = "This disc was already ripped: press Rip again to rip it once more."
EXTRA_REFUSED = "That title is already queued or ripped."


def _video_disc_view(conn, drive_id: str):
    """(disc row, parsed titles) of the disc in a drive, or (None, [])."""
    disc = video_rips.disc_in_drive(conn, drive_id)
    if disc is None:
        return None, []
    return disc, [makemkv.Title(**t) for t in json.loads(disc["scan"]).get("titles", [])]


def _rip_drive(conn, rip_id: int) -> str | None:
    row = conn.execute("SELECT drive_id FROM jobs WHERE id = ?", (rip_id,)).fetchone()
    return row["drive_id"] if row else None


async def _known_drive(request: Request, drive_id: str):
    return await asyncio.to_thread(_with_conn, request, _drive, drive_id)


_UNKNOWN_DRIVE = '<p class="error">Unknown drive.</p>'


@router.post("/dashboard/video/rip")
async def rip_video(request: Request, drive_id: str = Form(...), main: str = Form(""),
                    extras: list[str] = Form([]), again: str = Form("")):
    if await _known_drive(request, drive_id) is None:
        return HTMLResponse(_UNKNOWN_DRIVE, status_code=404)
    disc, parsed = await asyncio.to_thread(_with_conn, request, _video_disc_view, drive_id)
    if disc is None:
        return await _card(request, drive_id, error=RIP_REFUSED, status_code=400)
    try:
        extra_numbers = [int(x) for x in extras]
        if main.strip():
            main_number = int(main)
        elif titles_mod.looks_like_tv(parsed) and not disc["as_film"]:
            main_number = None
        else:
            raise ValueError("main")
    except ValueError:
        return await _card(request, drive_id, error="Pick the main title (or tick the titles to rip).",
                           status_code=400)
    if main_number is None and not extra_numbers:
        return await _card(request, drive_id, error=RIP_NOTHING_TICKED, status_code=400)
    if main_number is not None and main_number in extra_numbers:
        return await _card(request, drive_id, error=RIP_MAIN_IS_EXTRA, status_code=400)
    rip_id = await _services(request).start_video_rip(
        drive_id, disc["fingerprint"], main=main_number, extras=extra_numbers, again=bool(again))
    if rip_id is None:
        config = request.app.state.config
        blocking = await asyncio.to_thread(_with_conn, request, video_rips.blocking_rip, disc["fingerprint"],
                                           config.downloads_dir, config.work_dir)
        if blocking is not None and blocking["state"] == "ripping":
            error = RIP_ACTIVE
        elif blocking is not None and not again:
            error = RIP_ALREADY
        else:  # a title that isn't on the scanned disc
            error = RIP_REFUSED
        return await _card(request, drive_id, error=error)
    return await _card(request, drive_id)


@router.post("/dashboard/video/encode-choice")
async def encode_choice(request: Request, drive_id: str = Form(...), preset: str = Form(...)):
    """How the disc in this drive is encoded once each of its titles is ripped."""
    if await _known_drive(request, drive_id) is None:
        return HTMLResponse(_UNKNOWN_DRIVE, status_code=404)
    error = await _services(request).set_encode_choice(drive_id, preset)
    if error:
        return await _card(request, drive_id, error=error, status_code=400)
    return await _card(request, drive_id)


@router.post("/dashboard/video/hold")
async def hold_video(request: Request, drive_id: str = Form(...)):
    if await _known_drive(request, drive_id) is None:
        return HTMLResponse(_UNKNOWN_DRIVE, status_code=404)
    await _services(request).hold_video(drive_id)
    return await _card(request, drive_id)


@router.post("/dashboard/video/extra")
async def extra_video(request: Request, drive_id: str = Form(...), rip_id: int = Form(...), title: int = Form(...)):
    if await _known_drive(request, drive_id) is None:
        return HTMLResponse(_UNKNOWN_DRIVE, status_code=404)
    if await asyncio.to_thread(_with_conn, request, _rip_drive, rip_id) != drive_id:
        return await _card(request, drive_id, error=EXTRA_REFUSED)
    job_id = await _services(request).add_video_extra(rip_id, title)
    return await _card(request, drive_id, error=None if job_id is not None else EXTRA_REFUSED)


@router.post("/dashboard/video/move")
async def move_video(request: Request, drive_id: str = Form(...), rip_id: int = Form(...)):
    if await _known_drive(request, drive_id) is None:
        return HTMLResponse(_UNKNOWN_DRIVE, status_code=404)
    if await asyncio.to_thread(_with_conn, request, _rip_drive, rip_id) != drive_id:
        return await _card(request, drive_id, error="That rip isn't on this drive.")
    return await _card(request, drive_id, error=await _services(request).move_again(rip_id))


@router.post("/dashboard/video/as-film")
async def as_film_video(request: Request, drive_id: str = Form(...), value: str = Form(...)):
    if await _known_drive(request, drive_id) is None:
        return HTMLResponse(_UNKNOWN_DRIVE, status_code=404)
    if value not in ("0", "1"):
        return await _card(request, drive_id, error="Unknown choice.", status_code=400)

    def run(conn):
        disc = video_rips.disc_in_drive(conn, drive_id)
        if disc is not None:
            video_rips.set_as_film(conn, disc["fingerprint"], value == "1")

    await asyncio.to_thread(_with_conn, request, run)
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
    await _services(request).cancel_job(job_id)
    # A running job finishes cancelling a moment later; the SSE update brings the card up to date.
    return await _card(request, row["drive_id"])


def _read_job_log(config, job_id: int) -> str | None:
    """The job's log, followed by any tool logs kept beside it (job-<id>-<name>.log)."""
    main = joblog.path_for(config, job_id)
    if not main.is_file():
        return None
    parts = [main.read_text(encoding="utf-8", errors="replace")]
    for extra in sorted(main.parent.glob(f"job-{job_id}-*.log")):
        parts.append(f"\n===== {extra.name} =====\n" + extra.read_text(encoding="utf-8", errors="replace"))
    return "".join(parts)


@router.get("/jobs/{job_id}/log")
async def job_log(request: Request, job_id: int):
    text = await asyncio.to_thread(_read_job_log, request.app.state.config, job_id)
    if text is None:
        return PlainTextResponse(f"No log was kept for job #{job_id} (logs started with this version).",
                                 status_code=404)
    return PlainTextResponse(text)


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
            burst = await asyncio.to_thread(_burst_sync, conn, svc.config, svc.dvd_wait_pending)
        finally:
            conn.close()
        yield format_sse("guard", burst["guard_html"])
        for card in burst["cards"]:
            yield format_sse(f"drive-{card['key']}", card["html"])
        yield format_sse("encodes", burst["encodes_html"])
        yield format_sse("films", burst["films_html"])
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
