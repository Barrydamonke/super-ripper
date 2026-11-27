"""Settings page and admin password change."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Form, Request

from .. import auth, filmimport, settings
from .auth_routes import set_session_cookie
from .deps import get_conn, templates

router = APIRouter()
log = logging.getLogger(__name__)


def _display(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _radarr_options(request: Request) -> dict[str, list[tuple[str, str]]]:
    """Radarr's own root folders and quality profiles for the two dropdowns; empty when Radarr isn't set up
    or doesn't answer quickly (the page then shows the stored value in a text box). Blocks: run off the loop."""
    services = getattr(request.app.state, "services", None)
    client = services.radarr() if services is not None else None
    if client is None:
        return {}
    roots, profiles = filmimport.radarr_choices(client)
    return {"radarr_root_folder": [(r["path"], r["path"]) for r in roots],
            "radarr_quality_profile": [(p["name"], p["name"]) for p in profiles]}


def _render(
    request: Request,
    conn: sqlite3.Connection,
    *,
    options: dict[str, list[tuple[str, str]]] | None = None,
    values: dict[str, Any] | None = None,
    errors: dict[str, str] | None = None,
    message: str | None = None,
    password_error: str | None = None,
    password_message: str | None = None,
    status_code: int = 200,
):
    if values is None:
        values = {k: _display(v) for k, v in settings.get_all(conn).items()}
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "definitions": settings.SETTINGS,
            "values": values,
            "errors": errors or {},
            "options": options or {},
            "message": message,
            "password_error": password_error,
            "password_message": password_message,
            # Only whether each secret is set; the value itself never reaches the page.
            "secrets_set": {d.key: bool(settings.get(conn, d.key)) for d in settings.SETTINGS if d.kind == "secret"},
        },
        status_code=status_code,
    )


@router.get("/settings")
async def settings_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _render(request, conn, options=await asyncio.to_thread(_radarr_options, request))


@router.post("/settings")
async def settings_save(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        parsed = settings.parse_form(form, current=settings.get_all(conn))
    except settings.SettingsError as exc:
        submitted: dict[str, Any] = {}
        for d in settings.SETTINGS:
            if d.kind == "secret":
                continue  # never echo a secret back, even a rejected one
            submitted[d.key] = d.key in form if d.kind == "bool" else form.get(d.key, "")
        options = await asyncio.to_thread(_radarr_options, request)
        return _render(request, conn, values=submitted, errors=exc.errors, options=options, status_code=400)
    settings.save(conn, parsed)
    services = getattr(request.app.state, "services", None)
    if services is not None:  # Radarr may be set up now: import the films that were waiting for it
        try:
            await filmimport.sweep_ready(services)
        except Exception:
            log.exception("film import: looking for rips ready to import failed")
    options = await asyncio.to_thread(_radarr_options, request)
    return _render(request, conn, message="Settings saved.", options=options)


@router.post("/settings/password")
def change_password(
    request: Request,
    current: str = Form(""),
    new: str = Form(""),
    confirm: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    error = None
    if auth.check_login(conn, current) is None:
        error = "Current password is wrong."
    elif new != confirm:
        error = "New passwords do not match."
    else:
        try:
            version = auth.set_password(conn, new)
        except ValueError as exc:
            error = str(exc)
    if error:
        return _render(request, conn, password_error=error, options=_radarr_options(request), status_code=400)
    response = _render(request, conn, password_message="Password changed.", options=_radarr_options(request))
    set_session_cookie(request, response, version)
    return response
