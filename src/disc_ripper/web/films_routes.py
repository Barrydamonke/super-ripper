"""Films page: ripped DVDs and Blu-rays, their files and encodes, with the encode actions."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import db, encodes, encoding, film_imports, filmid, filmimport, filmundo, joblog, presets, settings
from ..clock import utc_now
from ..radarr import RadarrError, to_candidate
from .deps import templates

log = logging.getLogger(__name__)
router = APIRouter()

# Rips whose import action (Identify, Replace, Retry) was just started in the background.
_STARTING: set[int] = set()
# Radarr's recycle bin cleanup days, for "Undo available until"; refreshed when the page is asked for.
_BIN_DAYS: dict = {"days": None, "at": -1e9}
_BIN_DAYS_TTL = 600.0


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
        item["preset_label"] = row["preset_name"]
        item["percent"] = int(round((row["progress"] or 0) * 100))
        if row["state"] == "queued":
            est = encodes.try_estimate(conn, row)
            if est is not None:
                item["estimate"] = encoding._hours_minutes(est.seconds)
                item["rough"] = est.rough_time
        rows.append(item)
    return rows


def _raw_countdown(done: list[sqlite3.Row], keep_hours: float, now: datetime) -> str | None:
    stamps = [_parse_utc(r["finished_at"]) for r in done if r["finished_at"]]
    if not stamps:
        return None
    left = (min(stamps).timestamp() + keep_hours * 3600 - now.timestamp()) / 3600
    return "soon" if left <= 0 else f"in {math.ceil(left)} h"


def _file_view(key: str, entry: dict, scan_titles: dict, rows: list[sqlite3.Row],
               keep_hours: float, now: datetime, until_jellyfin: str | None = None) -> dict:
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
        r["preset_label"] = r["preset_name"]
        r["percent"] = int(round((r["progress"] or 0) * 100))
        r["output_name"] = Path(r["output"]).name if r["output"] else ""
        r["superseded"] = any(o["id"] > r["id"] for o in mine)  # a later attempt exists: no Retry
    theirs = [r for r in rows if str(r["title"]) == key]
    done = [r for r in theirs if r["state"] == "done"]
    good = encodes.good_encodes(theirs, path)  # the same rule the raw sweep and Delete use
    busy = any(r["state"] in ("queued", "running") for r in mine)
    waiting = not (deleted or busy or not good)  # a raw the sweep may be holding or counting down for
    lost_subs = waiting and encodes.lost_subtitles(theirs)
    name = f"{path.parent.name}/{path.name}" if path.parent.name == "extras" else path.name
    return {
        "title": int(key), "name": name, "role": entry.get("role", ""),
        "seconds": scan_titles.get(int(key), {}).get("seconds"),
        "size": size, "deleted": deleted, "encodes": mine,
        "lost_subs": lost_subs,
        "until_jellyfin": waiting and not lost_subs and until_jellyfin is not None,
        "jellyfin_note": until_jellyfin or "",
        "countdown": None if not waiting or lost_subs or until_jellyfin is not None else _raw_countdown(good, keep_hours, now),
        "no_intact": bool(done) and not good and not deleted and not busy,
        "can_delete": bool(good) and not deleted and not busy,
    }


def _file_name(path: str | None) -> str:
    return Path((path or "").replace("\\", "/")).name


def _import_view(row: sqlite3.Row) -> dict:
    """A film_imports row for the template: JSON columns parsed, file names pulled out of paths."""
    item = dict(row)
    for key, empty in (("candidates", []), ("replace", {}), ("extras", [])):
        try:
            value = json.loads(row[key]) if row[key] else empty
        except (ValueError, TypeError):
            value = empty
        item[key] = value if isinstance(value, type(empty)) else empty
    for part in ("old", "new", "checks", "reasons"):
        if not isinstance(item["replace"].get(part), dict):
            item["replace"][part] = {}
    item["replaced"] = bool(row["old_path"])
    item["new_name"] = _file_name(row["new_path"])
    item["old_name"] = _file_name(row["old_path"])
    item["undo_until"] = None
    if item["replaced"] and row["imported_at"] and _BIN_DAYS["days"]:
        try:
            item["undo_until"] = (_parse_utc(row["imported_at"]) + timedelta(days=_BIN_DAYS["days"])).isoformat()
        except ValueError:
            pass
    return item


def _radarr_set_up(conn: sqlite3.Connection) -> bool:
    return bool(settings.get(conn, "radarr_url") and settings.get(conn, "radarr_api_key"))



def _failed_title(conn: sqlite3.Connection, rip: sqlite3.Row, imports: dict) -> tuple[int, str] | None:
    """The failed (title, kind) that alone keeps this film from importing (the owner can import without it)."""
    if rip["main_title"] is None or rip["import_anyway"] or rip["rip_id"] in imports or not _radarr_set_up(conn):
        return None
    title = filmimport.failed_title(conn, rip["rip_id"])
    if title is None or not filmimport.ready_to_import(conn, rip["rip_id"], ignore_failed=True):
        return None
    return title


def films_context(conn: sqlite3.Connection, *, now: datetime | None = None, error: str | None = None,
                  notice: str | None = None, oob: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    keep_hours = settings.get(conn, "raw_keep_hours")
    default_choice = presets.default_choice(conn)
    choice_names = {**dict(presets.choices(conn)), "off": "not encoded automatically"}
    rips = []
    import_rows = conn.execute("SELECT * FROM film_imports").fetchall()
    imports = {r["rip_id"]: _import_view(r) for r in import_rows}
    # Imported: the files went to the library and the rip folder is cleaned up, so no raw/encode controls.
    in_library = {r["rip_id"] for r in import_rows if r["state"] in ("imported", "in_jellyfin")}
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
        hold = film_imports.jellyfin_hold(conn, rip["rip_id"])
        if hold is not None and not hold and rip["rip_id"] not in imports:
            failed = _failed_title(conn, rip, imports)
            if failed:
                hold = f"Title {failed[0]} failed; Retry or Import anyway"
        until_jellyfin = None if hold is None else hold
        views = [_file_view(key, files[key], scan_titles, rows, keep_hours, now, until_jellyfin)
                 for key in sorted(files, key=int)]
        logs = [r[0] for r in conn.execute(
            "SELECT id FROM jobs WHERE kind = 'rip_title' AND json_extract(params, '$.rip_id') = ? ORDER BY id",
            (rip["rip_id"],))]
        rips.append({
            "rip_id": rip["rip_id"], "label": disc["label"] if disc else "",
            "disc_type": DISC_NAMES.get(disc["disc_type"], "") if disc else "",
            "created_at": rip["created_at"], "files": views, "logs": logs,
            "imp": imports.get(rip["rip_id"]), "starting": rip["rip_id"] in _STARTING,
            "in_library": rip["rip_id"] in in_library,
            "failed_title": (ft := _failed_title(conn, rip, imports)),
            "failed_interrupted": ft is not None and ft[1] == "encode" and bool(
                [e["state"] for e in rows if e["title"] == ft[0]][-1:] == ["interrupted"]),
            "can_encode_all": rip["rip_id"] not in in_library and any(not f["deleted"] for f in views),
            # A rip with no choice of its own follows the setting only while it's still ripping.
            "encode_with": choice_names.get(
                rip["encode_preset"] or (default_choice if rip["state"] == "ripping" else ""), ""),
        })
    recent = [imports[r["rip_id"]] for r in film_imports.recent(conn)]
    return {"active": _active_rows(conn), "rips": rips, "presets": [c for c in presets.choices(conn) if c[0] != "off"],
            "recent": recent, "radarr_set_up": _radarr_set_up(conn), "no_radarr": filmimport.NO_RADARR,
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


def _bin_days_needed(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM film_imports WHERE state IN ('imported', 'in_jellyfin') "
                        "AND old_path IS NOT NULL LIMIT 1").fetchone() is not None


async def _refresh_bin_days(request: Request) -> None:
    """Ask Radarr how long its recycle bin keeps files, when a replaced film's Undo date needs it."""
    if time.monotonic() - _BIN_DAYS["at"] < _BIN_DAYS_TTL:
        return
    if not await asyncio.to_thread(_with_conn, request, _bin_days_needed):
        return
    client = _services(request).radarr()
    if client is None:
        return
    try:
        _BIN_DAYS["days"] = (await asyncio.to_thread(client.recycle_bin))[1]
    except RadarrError:
        return  # not cached: the next render asks again
    _BIN_DAYS["at"] = time.monotonic()


async def _respond(request: Request, *, error: str | None = None, notice: str | None = None,
                   status_code: int = 200) -> HTMLResponse:
    """The re-rendered films list, or just the dashboard panel when the action came from there."""
    await _refresh_bin_days(request)
    if request.headers.get("HX-Target") == "encodes":
        html = await asyncio.to_thread(_with_conn, request, render_encodes)
    else:
        html = await asyncio.to_thread(_with_conn, request,
                                       lambda conn: render_films(conn, error=error, notice=notice, oob=True))
    return HTMLResponse(html, status_code=status_code)


def _preset_error(conn, preset: str) -> str | None:
    return presets.parse_choice(conn, preset, allow_off=False)


# -- pages and actions ---------------------------------------------------------


@router.get("/films")
async def films_page(request: Request):
    await _refresh_bin_days(request)
    html = await asyncio.to_thread(_with_conn, request, render_films)
    return templates.TemplateResponse(request, "films.html", {"films_html": html})


@router.post("/films/encode")
async def encode_one(request: Request, rip_id: int = Form(...), title: int = Form(...), preset: str = Form(...)):
    if (bad := await asyncio.to_thread(_with_conn, request, _preset_error, preset)):
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
    if (bad := await asyncio.to_thread(_with_conn, request, _preset_error, preset)):
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


# -- identify, replace, undo, skip, retry ------------------------------------------------


def _import_state(conn: sqlite3.Connection, rip_id: int) -> tuple[bool, str | None]:
    """(the rip is a film, its import state or None)."""
    rip = conn.execute("SELECT main_title FROM video_rips WHERE rip_id = ?", (rip_id,)).fetchone()
    row = film_imports.get(conn, rip_id)
    return bool(rip is not None and rip["main_title"] is not None), (row["state"] if row else None)


def _record_refusal(conn: sqlite3.Connection, rip_id: int, text: str) -> None:
    if film_imports.get(conn, rip_id) is not None:
        film_imports.update(conn, rip_id, utc_now(), message=text)


def _start(request: Request, rip_id: int, work) -> None:
    """Run a film-import action (it can take minutes) in the background. A refusal from it is shown
    on the rip's row the next time the list is drawn."""
    services = _services(request)  # the rip is already in _STARTING (see _claim)

    async def run() -> None:
        task = asyncio.ensure_future(work(services))
        try:
            # The rip counts as "starting" only until the flow holds its lock (then the row shows the real step).
            while not task.done() and not services.import_lock(rip_id).locked():
                await asyncio.sleep(0.01)
            _STARTING.discard(rip_id)
            services.films_changed()
            refusal = await task
            if refusal and refusal != filmimport.BUSY:
                await asyncio.to_thread(services._db, _record_refusal, rip_id, refusal)
        except Exception:
            log.exception("film import: rip #%s action crashed", rip_id)
        finally:
            _STARTING.discard(rip_id)
            services.films_changed()

    filmimport._spawn(services, run(), f"film import {rip_id}")


def _claim(services, rip_id: int) -> str | None:
    """The quick refusals shared by every import action. On success the rip is in _STARTING (checked and
    added with no await between, so two requests can't both pass); the caller must _start it or release it."""
    if services.radarr() is None:
        return filmimport.NO_RADARR
    if services.import_lock(rip_id).locked() or rip_id in _STARTING:
        return filmimport.BUSY
    _STARTING.add(rip_id)
    return None


def _results_html(rip_id: int, found: list[dict], error: str | None = None) -> str:
    return templates.get_template("_film_results.html").render(rip_id=rip_id, cands=found, error=error, searched=True)


def _candidate_dict(lookup: dict) -> dict:
    cand = to_candidate(lookup)
    return {"tmdb_id": cand.tmdb_id, "title": cand.title, "year": cand.year, "runtime_min": cand.runtime_min,
            "poster": cand.poster, "in_library": cand.in_library, "folder": cand.folder}


def _search(client, query: str) -> list[dict]:
    """Radarr lookups for what the owner typed: an IMDb or TMDb reference, or a title. A bare number is
    both a TMDb ID and possibly a title ("1917"), so both are tried and merged."""
    ref = filmid.parse_ref(query)
    items: list[dict] = []
    first_error: RadarrError | None = None

    def attempt(fn, *args):
        nonlocal first_error
        try:
            found = fn(*args)
        except RadarrError as exc:
            first_error = first_error or exc
            return
        items.extend(found if isinstance(found, list) else [found])

    if ref is None:
        attempt(client.lookup, query)
    elif ref[0] == "imdb":
        attempt(client.lookup_imdb, ref[1])
    else:
        if re.fullmatch(r"\d+", query):
            attempt(client.lookup, query)
        attempt(client.lookup_tmdb, int(ref[1]))
    if not items and first_error is not None:
        raise first_error
    found: dict[int, dict] = {}
    for item in items:
        try:
            cand = _candidate_dict(item)
        except (KeyError, TypeError, ValueError):
            continue
        found.setdefault(cand["tmdb_id"], cand)
    return list(found.values())[:10]


@router.post("/films/identify/search")
async def identify_search(request: Request, rip_id: int = Form(...), q: str = Form("")):
    query = q.strip()
    if not query:
        return HTMLResponse(_results_html(rip_id, [], "Type a film name, or paste an IMDb or TMDb ID or link."))
    client = _services(request).radarr()
    if client is None:
        return HTMLResponse(_results_html(rip_id, [], filmimport.NO_RADARR))
    try:
        found = await asyncio.to_thread(_search, client, query)
    except RadarrError as exc:
        return HTMLResponse(_results_html(rip_id, [], str(exc)))
    return HTMLResponse(_results_html(rip_id, found))


@router.post("/films/identify/choose")
async def identify_choose(request: Request, rip_id: int = Form(...), tmdb_id: str = Form(...),
                          root: str = Form(filmimport.DEFAULT_ROOT)):
    services = _services(request)
    if filmid.parse_ref(tmdb_id) is None:
        return await _respond(request, error="Enter an IMDb ID, a TMDb ID or a TMDb/IMDb link.", status_code=400)
    refusal = _claim(services, rip_id)
    if refusal is None:
        try:
            is_film, state = await asyncio.to_thread(services._db, _import_state, rip_id)
            if not is_film:
                refusal = "Only films can be imported."
            elif state is not None and state not in filmimport._CHOOSABLE:
                refusal = "This rip can't be changed now."
        except BaseException:
            _STARTING.discard(rip_id)
            raise
        if refusal:
            _STARTING.discard(rip_id)
    if refusal:
        return await _respond(request, error=refusal, status_code=409)
    if root not in ("Movies", "Other movies"):
        root = filmimport.DEFAULT_ROOT
    _start(request, rip_id, lambda s: filmimport.choose_film(s, rip_id, tmdb_id, root))
    return await _respond(request)


async def _bin_visible(services) -> bool:
    try:
        path = (await asyncio.to_thread(services.radarr().recycle_bin))[0]
    except RadarrError:
        return False
    return bool(path) and await asyncio.to_thread(Path(path).is_dir)


@router.post("/films/replace")
async def replace(request: Request, rip_id: int = Form(...), typed: str = Form("")):
    services = _services(request)
    refusal = _claim(services, rip_id)
    if refusal is None:
        try:
            _, state = await asyncio.to_thread(services._db, _import_state, rip_id)
            if state != "needs_replace_ok":
                refusal = "This rip isn't waiting to replace a film."
            elif typed != "REPLACE" and not await _bin_visible(services):
                refusal = ("Radarr's recycle bin isn't set (or isn't visible to the ripper), so the old file can't "
                           "be recovered. Type REPLACE to replace it anyway.")
        except BaseException:
            _STARTING.discard(rip_id)
            raise
        if refusal:
            _STARTING.discard(rip_id)
    if refusal:
        return await _respond(request, error=refusal, status_code=409)
    _start(request, rip_id, lambda s: filmimport.approve_replace(s, rip_id, typed))
    return await _respond(request)


@router.post("/films/import/retry")
async def import_retry(request: Request, rip_id: int = Form(...)):
    services = _services(request)
    refusal = _claim(services, rip_id)
    if refusal is None:
        try:
            _, state = await asyncio.to_thread(services._db, _import_state, rip_id)
            if state not in ("failed", "waiting_other"):
                refusal = "Nothing to retry."
        except BaseException:
            _STARTING.discard(rip_id)
            raise
        if refusal:
            _STARTING.discard(rip_id)
    if refusal:
        return await _respond(request, error=refusal, status_code=409)
    _start(request, rip_id, lambda s: filmimport.retry_import(s, rip_id))
    return await _respond(request)


@router.post("/films/import-anyway")
async def import_anyway(request: Request, rip_id: int = Form(...)):
    services = _services(request)
    await filmimport.import_anyway(services, rip_id)
    services.films_changed()
    return await _respond(request)


@router.post("/films/skip")
async def skip(request: Request, rip_id: int = Form(...)):
    services = _services(request)
    refusal = await filmundo.skip(services, rip_id)
    services.films_changed()
    if refusal:
        return await _respond(request, error=refusal, status_code=409)
    return await _respond(request, notice="Skipped. The rip's files were deleted.")


@router.post("/films/undo")
async def undo(request: Request, rip_id: int = Form(...)):
    services = _services(request)
    result = await filmundo.undo(services, rip_id)
    services.films_changed()
    if result and result.startswith("Undone"):
        return await _respond(request, notice=result)
    return await _respond(request, error=result or "Undo didn't finish.", status_code=409)


@router.get("/films/import/{rip_id}/log")
async def import_log(request: Request, rip_id: int):
    path = request.app.state.config.config_dir / joblog.LOG_DIRNAME / f"import-{rip_id}.log"
    try:
        text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
    except OSError:
        return PlainTextResponse(f"No log for the import of rip #{rip_id}", status_code=404)
    return PlainTextResponse(text)
