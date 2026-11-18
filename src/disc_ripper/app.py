"""FastAPI application factory.

Run with: uvicorn disc_ripper.app:create_app --factory
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import auth, db, drives
from .clock import utc_now
from .config import AppConfig, load_config
from .services import Services
from .web import auth_routes, drive_routes, settings_routes
from .web.deps import is_htmx

_PACKAGE_DIR = Path(__file__).resolve().parent
_OPEN_PATHS = {"/healthz"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
}


def is_cross_origin(request: Request) -> bool:
    """True for a state-changing request sent from another origin.

    SameSite=Lax doesn't cover this: the other web UIs on the same server
    (Unraid, Jellyfin, Radarr...) differ only by port, so they count as same-site.
    """
    if request.method in _SAFE_METHODS:
        return False
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    if origin is not None:
        if origin == "null":
            return True
        if urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
            return True
    return False


def _redirect(request: Request, target: str) -> Response:
    if is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


def _login_target(request: Request) -> str:
    if request.method != "GET":
        return "/login"
    path = request.url.path
    if request.url.query:
        path += "?" + request.url.query
    return "/login?next=" + quote(path, safe="/")


def _auth_decision(config: AppConfig, sessions: auth.SessionManager, request: Request) -> str | None:
    """Return a redirect target, or None if the request may proceed."""
    path = request.url.path
    conn = db.connect(config.db_path)
    try:
        if not auth.is_password_set(conn):
            return None if path == "/setup" else "/setup"
        if path == "/setup":
            return "/login"
        if path == "/login":
            return None
        version = sessions.read(request.cookies.get(auth.SessionManager.COOKIE_NAME))
        if version is None or version != auth.current_session_version(conn):
            return _login_target(request)
        request.state.logged_in = True
        return None
    finally:
        conn.close()


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or load_config()
    conn = db.connect(config.db_path)
    try:
        db.migrate(conn)
        drives.sync(conn, drives.discover(config.sysfs_root), utc_now())
    finally:
        conn.close()

    services = Services(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if config.run_background:
            await services.start()
        try:
            yield
        finally:
            if config.run_background:
                await services.stop()

    app = FastAPI(title="Disc Ripper", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.config = config
    app.state.services = services
    app.state.sessions = auth.SessionManager(auth.load_or_create_secret(config.secret_key_path))

    async def _guarded(request: Request, call_next) -> Response:
        request.state.logged_in = False
        if is_cross_origin(request):
            return PlainTextResponse("Cross-site request blocked.", status_code=403)
        path = request.url.path
        if path in _OPEN_PATHS or path.startswith("/static/"):
            return await call_next(request)
        target = _auth_decision(config, app.state.sessions, request)
        if target is not None:
            return _redirect(request, target)
        return await call_next(request)

    @app.middleware("http")
    async def security(request: Request, call_next):
        response = await _guarded(request, call_next)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/")
    def root():
        return RedirectResponse("/drives", status_code=303)

    app.mount("/static", StaticFiles(directory=str(_PACKAGE_DIR / "static")), name="static")
    app.include_router(auth_routes.router)
    app.include_router(drive_routes.router)
    app.include_router(settings_routes.router)
    return app
