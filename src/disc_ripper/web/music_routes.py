"""Music page: review ripped CDs, resolve the ones that need a look, edit filed albums."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .. import cd_rips, cdrip, db, filing, musicbrainz, tagging
from .deps import is_htmx, templates

router = APIRouter()
_YEAR = re.compile(r"^(\d{4})?$")


def _conn(request: Request) -> sqlite3.Connection:
    return db.connect(request.app.state.config.db_path)


def _call(request: Request, fn, *args, **kwargs):
    conn = _conn(request)
    try:
        return fn(conn, *args, **kwargs)
    finally:
        conn.close()


def _done(request: Request, target: str) -> Response:
    if is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


# -- pages ----------------------------------------------------------------------


@router.get("/music")
def music_page(request: Request):
    def load(conn):
        return {
            "waiting": cd_rips.needs_look(conn),
            "filed": cd_rips.recently_filed(conn),
            "refresh_error": request.app.state.services.refresher.last_error,
        }

    context = _call(request, load)
    for row_list in ("waiting",):
        context[row_list] = [{"row": r, "reasons": json.loads(r["reasons"])} for r in context[row_list]]
    return templates.TemplateResponse(request, "music.html", context)


def _item_context(conn, job_id: int) -> dict | None:
    rip = cd_rips.get(conn, job_id)
    if rip is None:
        return None
    checks = {c["track"]: c for c in json.loads(rip["accuraterip"] or "[]")}
    tracks = []
    for n, path in enumerate(cdrip.rip_files(rip), start=1):
        tags = tagging.read_tags(path)
        tracks.append({
            "n": n, "title": tags.get("TITLE", ""), "artist": tags.get("ARTIST", ""),
            "duration": _duration(tagging.duration_seconds(path)),
            "check": checks.get(n, {}).get("status", "unknown"),
        })
    albums = conn.execute("SELECT album_key, album_artist, album, year FROM albums ORDER BY album_artist, album")
    return {
        "rip": rip,
        "reasons": json.loads(rip["reasons"]),
        "candidates": cdrip.stored_candidates(rip),
        "tracks": tracks,
        "albums": albums.fetchall(),
    }


def _item(request: Request, job_id: int, *, error: str | None = None, status_code: int = 200, form=None):
    context = _call(request, _item_context, job_id)
    if context is None:
        return HTMLResponse("<p>Unknown rip.</p>", status_code=404)
    context.update(error=error, form=form or {})
    return templates.TemplateResponse(request, "music_item.html", context, status_code=status_code)


@router.get("/music/{job_id}")
def music_item(request: Request, job_id: int):
    return _item(request, job_id)


@router.get("/music/{job_id}/track/{n}")
def music_track(request: Request, job_id: int, n: int):
    rip = _call(request, cd_rips.get, job_id)
    if rip is None:
        return HTMLResponse("Not found", status_code=404)
    files = cdrip.rip_files(rip)
    if not 1 <= n <= len(files):
        return HTMLResponse("Not found", status_code=404)
    path = files[n - 1].resolve()
    root = Path(request.app.state.config.downloads_dir).resolve()
    if root not in path.parents:
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(path, media_type="audio/flac")


# -- resolving ----------------------------------------------------------------------


async def _resolve(request: Request, job_id: int, make_plan, *, allow_duplicate=False, form=None) -> Response:
    """Build a plan (in a thread), file it, and go back to the Music page."""

    def run(conn):
        if not cdrip.claim(conn, job_id):
            raise cdrip.ResolveError("This rip isn't waiting for a look any more.")
        try:
            plan = make_plan(conn, cd_rips.get(conn, job_id))
            return cdrip.file_resolved(conn, job_id, plan, request.app.state.config.music_dir,
                                       allow_duplicate=allow_duplicate)
        except BaseException:
            cdrip.release_claim(conn, job_id)
            raise

    try:
        await asyncio.to_thread(_call, request, run)
    except cdrip.ResolveError as exc:
        return _item(request, job_id, error=str(exc), status_code=400, form=form)
    except filing.FileExists:
        return _item(request, job_id, error="A file with that name is already in the library.", status_code=400,
                     form=form)
    request.app.state.services.refresher.trigger()
    return _done(request, "/music")


@router.post("/music/{job_id}/use-release")
async def use_release(request: Request, job_id: int):
    release_id = str((await request.form()).get("release_id", ""))

    def plan(conn, rip):
        match = [c for c in cdrip.stored_candidates(rip) if c.release_id == release_id]
        if not match:
            raise cdrip.ResolveError("Pick one of the releases listed.")
        return cdrip.plan_for_candidate(conn, rip, match[0])

    return await _resolve(request, job_id, plan)


@router.post("/music/{job_id}/use-url")
async def use_url(request: Request, job_id: int):
    form = await request.form()
    release_id = musicbrainz.parse_release_ref(str(form.get("ref", "")))
    if release_id is None:
        return _item(request, job_id, error="Paste a MusicBrainz release link (musicbrainz.org/release/…).",
                     status_code=400)
    try:
        release = await asyncio.to_thread(request.app.state.services.musicbrainz.get_release, release_id)
    except musicbrainz.MusicBrainzUnavailable as exc:
        return _item(request, job_id, error=str(exc), status_code=400)
    medium = str(form.get("medium", "")).strip()

    def plan(conn, rip):
        n_files = len(cdrip.rip_files(rip))
        media = release.get("media", [])
        by_disc = [m for m in media if any(d.get("id") == rip["disc_id"] for d in m.get("discs", []))]
        options = by_disc or [m for m in media if len(m.get("tracks", [])) == n_files]
        if medium.isdigit():
            options = [m for m in media if int(m["position"]) == int(medium)]
        if not options:
            raise cdrip.ResolveError(f"No disc of that release has {n_files} tracks.")
        if len(options) > 1:
            positions = ", ".join(str(m["position"]) for m in options)
            raise cdrip.ResolveError(f"Several discs of that release could be this one ({positions}): "
                                     "enter the disc number too.")
        return cdrip.plan_for_candidate(conn, rip, musicbrainz.candidate_for_medium(release, int(options[0]["position"])))

    return await _resolve(request, job_id, plan, form={"ref": form.get("ref", ""), "medium": medium})


def _int(value, default: int) -> int | None:
    text = str(value or "").strip()
    if not text:
        return default
    return int(text) if text.isdigit() and 0 < int(text) < 100 else None


@router.post("/music/{job_id}/manual")
async def manual(request: Request, job_id: int):
    form = {k: str(v).strip() for k, v in (await request.form()).items()}
    album, album_artist, year = form.get("album", ""), form.get("album_artist", ""), form.get("year", "")
    disc, disc_total = _int(form.get("disc"), 1), _int(form.get("disc_total"), 1)

    def plan(conn, rip):
        n_files = len(cdrip.rip_files(rip))
        titles = [form.get(f"title_{n}", "") for n in range(1, n_files + 1)]
        artists = [form.get(f"artist_{n}", "") for n in range(1, n_files + 1)]
        if not album or not album_artist:
            raise cdrip.ResolveError("Album and album artist are needed.")
        if not _YEAR.match(year):
            raise cdrip.ResolveError("Year must be four digits, or left blank.")
        if disc is None or disc_total is None or disc > disc_total:
            raise cdrip.ResolveError("Check the disc number and the number of discs.")
        if not all(titles):
            raise cdrip.ResolveError("Every track needs a title.")
        return cdrip.plan_manual(rip, album=album, album_artist=album_artist, year=year, disc=disc,
                                 disc_total=disc_total, titles=titles, artists=artists)

    return await _resolve(request, job_id, plan, form=form)


@router.post("/music/{job_id}/add-to")
async def add_to(request: Request, job_id: int):
    form = {k: str(v).strip() for k, v in (await request.form()).items()}
    disc = _int(form.get("disc"), 0)

    def plan(conn, rip):
        if not disc:
            raise cdrip.ResolveError("Enter which disc of the album this is.")
        n_files = len(cdrip.rip_files(rip))
        titles = [form.get(f"addto_title_{n}", "") for n in range(1, n_files + 1)]
        if any(titles) and not all(titles):
            raise cdrip.ResolveError("Every track needs a title.")
        return cdrip.plan_add_to(conn, rip, form.get("album_key", ""), disc, titles if any(titles) else None)

    return await _resolve(request, job_id, plan, form=form)


@router.post("/music/{job_id}/file-anyway")
async def file_anyway(request: Request, job_id: int):
    def plan(conn, rip):
        match = [c for c in cdrip.stored_candidates(rip) if c.release_id == rip["release_id"]]
        if not match:
            raise cdrip.ResolveError("Choose a release or enter the details first.")
        return cdrip.plan_for_candidate(conn, rip, match[0])

    return await _resolve(request, job_id, plan, allow_duplicate=True)


@router.post("/music/{job_id}/discard")
async def discard(request: Request, job_id: int):
    rip = await asyncio.to_thread(_call, request, cd_rips.get, job_id)
    if rip is None:
        return HTMLResponse("<p>Unknown rip.</p>", status_code=404)
    if not await asyncio.to_thread(_call, request, cdrip.discard, job_id):
        return _item(request, job_id, error="This rip isn't waiting for a look any more.", status_code=400)
    return _done(request, "/music")


# -- editing filed albums -----------------------------------------------------------


def _album(conn, album_key: str):
    return conn.execute("SELECT * FROM albums WHERE album_key = ?", (album_key,)).fetchone()


def _edit_page(request: Request, album, *, error=None, status_code=200, form=None):
    return templates.TemplateResponse(
        request, "music_album_edit.html", {"album": album, "error": error, "form": form or {}},
        status_code=status_code,
    )


@router.get("/music/album/{album_key}/edit")
def edit_album_page(request: Request, album_key: str):
    album = _call(request, _album, album_key)
    if album is None:
        return HTMLResponse("<p>Unknown album.</p>", status_code=404)
    return _edit_page(request, album)


@router.post("/music/album/{album_key}/edit")
async def edit_album(request: Request, album_key: str):
    form = {k: str(v).strip() for k, v in (await request.form()).items()}
    album = await asyncio.to_thread(_call, request, _album, album_key)
    if album is None:
        return HTMLResponse("<p>Unknown album.</p>", status_code=404)
    if not form.get("album") or not form.get("album_artist") or not _YEAR.match(form.get("year", "")):
        return _edit_page(request, album, error="Album and album artist are needed; year is four digits or blank.",
                          status_code=400, form=form)
    tags = {"ALBUM": form["album"], "ALBUMARTIST": form["album_artist"], "DATE": form.get("year", "")}
    music_dir = request.app.state.config.music_dir

    def run(conn):
        folder = filing.retag_album(conn, album_key, tags, music_dir, "")
        conn.execute(
            "UPDATE cd_rips SET filed_path = ?, summary = ? WHERE album_key = ?",
            (str(folder), cdrip.summary_for(form["album_artist"], form["album"], form.get("year", "")), album_key),
        )
        conn.commit()

    try:
        await asyncio.to_thread(_call, request, run)
    except filing.FileExists:
        return _edit_page(request, album, error="An album folder with that name already exists.", status_code=400,
                          form=form)
    request.app.state.services.refresher.trigger()
    return _done(request, "/music")
