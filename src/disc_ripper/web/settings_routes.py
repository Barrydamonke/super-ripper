"""Settings page and admin password change."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Form, Request

from .. import auth, settings
from .auth_routes import set_session_cookie
from .deps import get_conn, templates

router = APIRouter()


def _display(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _render(
    request: Request,
    conn: sqlite3.Connection,
    *,
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
            "message": message,
            "password_error": password_error,
            "password_message": password_message,
        },
        status_code=status_code,
    )


@router.get("/settings")
def settings_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _render(request, conn)


@router.post("/settings")
async def settings_save(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        parsed = settings.parse_form(form)
    except settings.SettingsError as exc:
        submitted: dict[str, Any] = {}
        for d in settings.SETTINGS:
            submitted[d.key] = d.key in form if d.kind == "bool" else form.get(d.key, "")
        return _render(request, conn, values=submitted, errors=exc.errors, status_code=400)
    settings.save(conn, parsed)
    return _render(request, conn, message="Settings saved.")


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
        return _render(request, conn, password_error=error, status_code=400)
    response = _render(request, conn, password_message="Password changed.")
    set_session_cookie(request, response, version)
    return response
