"""First-run setup, login and logout."""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from .. import auth
from .deps import get_conn, templates

router = APIRouter()
FAILED_LOGIN_DELAY = 1.0


def safe_next(target: str | None) -> str:
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def set_session_cookie(request: Request, response: Response, version: int) -> None:
    sessions: auth.SessionManager = request.app.state.sessions
    response.set_cookie(
        auth.SessionManager.COOKIE_NAME,
        sessions.issue(version),
        max_age=auth.SessionManager.MAX_AGE,
        httponly=True,
        samesite="lax",
    )


@router.get("/setup")
def setup_form(request: Request):
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@router.post("/setup")
def setup_submit(
    request: Request,
    password: str = Form(""),
    confirm: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if auth.is_password_set(conn):
        return RedirectResponse("/login", status_code=303)
    error = None
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        error = f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters."
    elif password != confirm:
        error = "Passwords do not match."
    if error:
        return templates.TemplateResponse(request, "setup.html", {"error": error}, status_code=400)
    auth.set_password(conn, password)
    return RedirectResponse("/login", status_code=303)


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "next": safe_next(next)}
    )


@router.post("/login")
def login_submit(
    request: Request,
    password: str = Form(""),
    next: str = Form("/"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    version = auth.check_login(conn, password)
    if version is None:
        time.sleep(FAILED_LOGIN_DELAY)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Wrong password.", "next": safe_next(next)},
            status_code=401,
        )
    response = RedirectResponse(safe_next(next), status_code=303)
    set_session_cookie(request, response, version)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SessionManager.COOKIE_NAME)
    return response
