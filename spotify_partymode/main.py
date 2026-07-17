"""FastAPI application factory, lifespan and static file serving."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import db, queue_manager, settings_store
from .routers import admin, auth, guest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("partymode.main")

# Static files live inside the package so they ship with `pip install` too.
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the background queue poller for the app lifetime."""
    stop_event = asyncio.Event()
    poller = asyncio.create_task(queue_manager.run_poller(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        await poller


def _serve_page(name: str):
    """Return a static HTML page, or a clear 500 if the file is missing."""
    path = STATIC_DIR / name
    if path.is_file():
        return FileResponse(path)
    return JSONResponse(
        {"detail": f"UI file '{name}' was not found. Looked in: {STATIC_DIR}"},
        status_code=500,
    )


def create_app() -> FastAPI:
    # The DB must exist before we can read the session secret from it.
    db.init_db()
    secret_key = settings_store.ensure_secret_key()

    app = FastAPI(title="Spotify-Partymode", version="0.1.0", lifespan=lifespan)
    # 30-day persistent cookie so guests stay logged in across reloads/restarts.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        max_age=60 * 60 * 24 * 30,
        same_site="lax",
    )

    # Persistent per-browser device id. Unlike the session cookie it is NOT
    # cleared on logout, so skip-token and block budgets (keyed by device id)
    # cannot be reset by logging out and re-joining under a new name.
    _DEVICE_COOKIE = "device_id"
    _DEVICE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

    @app.middleware("http")
    async def ensure_device_id(request: Request, call_next):
        device_id = request.cookies.get(_DEVICE_COOKIE)
        issue = device_id if device_id else secrets.token_urlsafe(16)
        request.state.device_id = issue
        response = await call_next(request)
        if not device_id:
            response.set_cookie(
                _DEVICE_COOKIE,
                issue,
                max_age=_DEVICE_MAX_AGE,
                httponly=True,
                samesite="lax",
            )
        return response

    app.include_router(auth.router)
    app.include_router(guest.router)
    app.include_router(admin.router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    # Static assets (css/js). Mount only if the directory is present.
    if STATIC_DIR.is_dir():
        logger.info("Serving static UI from %s", STATIC_DIR)
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    else:
        logger.warning("Static directory NOT found at %s - the UI cannot be served", STATIC_DIR)

    # Page routes are always registered so the user gets a helpful message
    # instead of a bare 404 if the static files are missing.
    @app.get("/")
    async def index():
        # First run: no admin account yet -> guide the user to setup.
        if not db.admin_exists():
            return RedirectResponse("/setup.html")
        return _serve_page("index.html")

    @app.get("/index.html")
    async def index_html():
        return RedirectResponse("/")

    @app.get("/admin.html")
    async def admin_page():
        return _serve_page("admin.html")

    @app.get("/setup.html")
    async def setup_page():
        return _serve_page("setup.html")

    @app.get("/register.html")
    async def register_page():
        return _serve_page("register.html")

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    return app


app = create_app()
