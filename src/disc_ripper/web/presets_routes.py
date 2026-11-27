"""The Presets page: list, edit form with live greying and command preview, After ripping default."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import presets
from ..presets import PresetError, PresetSettings
from .deps import get_conn, templates

router = APIRouter()

_FILM = 2 * 3600  # card estimates are for a 2-hour film
_FILM_SIZE = {"bluray": 25_000_000_000, "dvd": 6_000_000_000}


def _values(s: PresetSettings, name: str) -> dict[str, str]:
    """The form's field values (all text) for these settings."""
    v = {k: str(x) for k, x in s.to_dict().items() if not isinstance(x, (list, bool)) and x is not None}
    v["max_height"] = "none" if s.max_height is None else str(s.max_height)
    v["audio_languages"] = ", ".join(s.audio_languages)
    v["subtitle_languages"] = ", ".join(s.subtitle_languages)
    v["keep_commentary"] = "1" if s.keep_commentary else ""
    v["name"] = name
    return v


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _form(request: Request) -> dict[str, str]:
    return {k: v for k, v in (await request.form()).items() if isinstance(v, str)}


def _id(value: str) -> bool:
    """A usable preset id in a form: digits, short enough not to overflow SQLite."""
    return value.isdigit() and value.isascii() and len(value) <= 10


def _base(conn: sqlite3.Connection, form: dict[str, str]) -> tuple[PresetSettings, int | None]:
    """The settings that fields left out of the form keep: the preset being edited, else the one copied."""
    for key in ("id", "from"):
        if _id(form.get(key, "")):
            found = presets.get_preset(conn, int(form[key]))
            if found:
                return found.settings, found.id if key == "id" else None
    return presets.STANDARD, None


def _edit(request: Request, conn: sqlite3.Connection, form: dict[str, str], values: dict[str, str],
          errors: dict[str, str], s: PresetSettings | None, *, preview_name: str | None = None):
    previews: list[dict] = []
    if s is not None and not errors:
        previews = presets.preview(conn, s, preview_name or values["name"])
    return templates.TemplateResponse(request, "preset_edit.html", {
        "v": values, "errors": errors, "preset_id": form.get("id", ""), "from_id": form.get("from", ""),
        "disabled": presets.disabled_fields(s) if s else {},
        "blocked": presets.disabled_choices(s) if s else {},
        "previews": previews,
        "ENGINES": presets.ENGINES, "MODES": presets.MODES, "CODECS": presets.SOFTWARE_CODECS + presets.HARDWARE_CODECS,
        "SPEEDS": presets.SPEEDS, "MAX_HEIGHTS": presets.MAX_HEIGHTS, "DEINTERLACE": presets.DEINTERLACE,
        "CROP": presets.CROP, "AUDIO_CODECS": presets.AUDIO_CODECS, "AUDIO_CHANNELS": presets.AUDIO_CHANNELS,
        "AUDIO_TRACKS": presets.AUDIO_TRACKS, "SUBTITLES": presets.SUBTITLES,
        "CODEC_LABEL": presets.CODEC_LABEL, "AUDIO_LABEL": presets.AUDIO_LABEL,
    })


def _list(request: Request, conn: sqlite3.Connection, *, error: str | None = None):
    cards = []
    for p in presets.list_presets(conn):
        video, audio = presets.summary(p.settings)
        estimates = [(label, presets.estimate_text(conn, p.settings, kind, _FILM, _FILM_SIZE[kind]))
                     for kind, label in (("bluray", "Blu-ray"), ("dvd", "DVD"))]
        cards.append({"preset": p, "video": video, "audio": audio, "estimates": estimates})
    return templates.TemplateResponse(request, "presets.html", {
        "cards": cards, "choices": presets.choices(conn), "default": presets.default_choice(conn), "error": error,
    })


@router.get("/presets")
def presets_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _list(request, conn)


@router.post("/presets/default")
async def set_default(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await _form(request)
    error = presets.set_default_choice(conn, form.get("choice", ""))
    if error:
        return _list(request, conn, error=error)
    return RedirectResponse("/presets", status_code=303)


@router.get("/presets/new")
def new_preset(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    source = request.query_params.get("from", "")
    found = presets.get_preset(conn, int(source)) if _id(source) else None
    found = found or presets.get_preset(conn, 1)
    s, name = (found.settings, f"{found.name} copy") if found else (presets.STANDARD, "New preset")
    form = {"from": str(found.id)} if found else {}
    return _edit(request, conn, form, _values(s, name), {}, s)


@router.get("/presets/{preset_id:int}")
def edit_preset(preset_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    found = presets.get_preset(conn, preset_id)
    if found is None:
        raise HTTPException(404, "That preset's stored settings are invalid." if presets.exists(conn, preset_id)
                            else presets.UNKNOWN)
    return _edit(request, conn, {"id": str(preset_id)}, _values(found.settings, found.name), {}, found.settings)


def _parse(conn: sqlite3.Connection, form: dict[str, str]):
    """(base, own id, name, settings, errors, values): settings is None when the form doesn't parse."""
    base, own = _base(conn, form)
    try:
        name, s = presets.from_form(form, base)
    except PresetError as exc:
        values = {**_values(base, ""), **{k: v for k, v in form.items() if k not in ("id", "from")}}
        if "audio_tracks" in form:  # an unticked box posts nothing, so it must stay unticked
            values["keep_commentary"] = "1" if "keep_commentary" in form else ""
        return own, form.get("name", "").strip(), None, {exc.field: exc.message}, values
    return own, name, s, {}, _values(s, name)


@router.post("/presets/save")
async def save(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await _form(request)
    own, name, s, errors, values = _parse(conn, form)
    if _id(form.get("id", "")) and own is None:
        raise HTTPException(404, presets.UNKNOWN)
    if s is not None:
        try:
            if own is None:
                presets.create_preset(conn, name, s, _now())
            else:
                presets.update_preset(conn, own, name, s, _now())
            return RedirectResponse("/presets", status_code=303)
        except PresetError as exc:
            errors = {exc.field: exc.message}
    return _edit(request, conn, form, values, errors, s)


@router.post("/presets/preview")
async def preview(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await _form(request)
    _own, name, s, errors, values = _parse(conn, form)
    return _edit(request, conn, form, values, errors, s, preview_name=name or "preset")


@router.post("/presets/{preset_id:int}/delete")
def delete(preset_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    error = presets.delete_preset(conn, preset_id)
    if error:
        return _list(request, conn, error=error)
    return RedirectResponse("/presets", status_code=303)
