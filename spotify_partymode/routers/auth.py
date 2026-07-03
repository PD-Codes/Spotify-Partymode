"""Authentication & setup routes.

Flow:
  1. First run -> POST /auth/setup creates the local admin account and stores
     the Spotify credentials in the DB.
  2. Admin logs in locally with username/password (works without Spotify).
  3. Admin optionally links Spotify via GET /auth/spotify/login (OAuth).
  4. Guests log in with just a display name.
"""

from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .. import db, queue_manager, security, settings_store, spotify_client
from . import deps

router = APIRouter(prefix="/auth", tags=["auth"])


# --- status ------------------------------------------------------------------

@router.get("/status")
def status_(request: Request) -> dict:
    """Report setup/identity state so the frontend can route the user."""
    return {
        "setup_complete": db.admin_exists(),
        "guest_name": request.session.get("guest_name"),
        "is_admin": bool(request.session.get("is_admin")),
        "spotify_connected": spotify_client.is_admin_authenticated(),
        "spotify_credentials_set": settings_store.has_spotify_credentials(),
        "registration_open": settings_store.is_registration_open(),
    }


# --- first-run setup ---------------------------------------------------------

class SetupBody(BaseModel):
    username: str
    password: str
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = ""
    default_playlist: str = ""


@router.post("/setup")
def setup(body: SetupBody, request: Request) -> dict:
    """Create the local admin account and store initial settings. One-time only."""
    deps.check_rate_limit(request, "setup")
    if db.admin_exists():
        deps.record_failed_attempt(request, "setup")
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup has already been completed.")
    username = body.username.strip()
    if len(username) < 3 or len(body.password) < 6:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Username must be >= 3 chars and password >= 6 chars.",
        )
    db.create_admin(username, security.hash_password(body.password))

    # Persist Spotify settings (all optional - can be added later).
    if body.spotify_client_id.strip():
        settings_store.set(settings_store.SPOTIFY_CLIENT_ID, body.spotify_client_id.strip())
    if body.spotify_client_secret.strip():
        settings_store.set(settings_store.SPOTIFY_CLIENT_SECRET, body.spotify_client_secret.strip())
    if body.spotify_redirect_uri.strip():
        settings_store.set(settings_store.SPOTIFY_REDIRECT_URI, body.spotify_redirect_uri.strip())
    if body.default_playlist.strip():
        settings_store.set(settings_store.DEFAULT_PLAYLIST, body.default_playlist.strip())

    # Log the new admin straight in.
    request.session["is_admin"] = True
    request.session["guest_name"] = username
    request.session["admin_username"] = username
    deps.stamp_admin_session(request)
    return {"ok": True}


# --- local admin login -------------------------------------------------------

class AdminLogin(BaseModel):
    username: str
    password: str


@router.post("/admin/login")
def admin_login(body: AdminLogin, request: Request) -> dict:
    """Log in with the local admin username/password (no Spotify required)."""
    deps.check_rate_limit(request, "admin_login")
    admin = db.get_admin_by_username(body.username.strip())
    if not admin or not security.verify_password(body.password, admin["password_hash"]):
        deps.record_failed_attempt(request, "admin_login")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password.")
    deps.reset_rate_limit(request, "admin_login")
    request.session["is_admin"] = True
    request.session["guest_name"] = admin["username"]
    request.session["admin_username"] = admin["username"]
    deps.stamp_admin_session(request)
    return {"ok": True, "spotify_connected": spotify_client.is_admin_authenticated()}


class RegisterBody(BaseModel):
    username: str
    password: str


@router.post("/register")
def register(body: RegisterBody, request: Request) -> dict:
    """Public self-registration of a manager account. Gated by the open flag.

    SECURITY NOTE: there is only one account role, so every self-registered
    user gets full manager/admin rights. Because of that, registration is
    CLOSED by default and must be explicitly enabled by the admin (who should
    only do so in a trusted environment).
    """
    if not db.admin_exists():
        raise HTTPException(status.HTTP_409_CONFLICT, "Initial setup is not complete yet.")
    if not settings_store.is_registration_open():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Registration is currently closed.")
    username = body.username.strip()
    if len(username) < 3 or len(body.password) < 6:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Username must be >= 3 chars and password >= 6 chars.",
        )
    if db.get_admin_by_username(username):
        raise HTTPException(status.HTTP_409_CONFLICT, "That username is already taken.")
    db.create_admin(username, security.hash_password(body.password))
    request.session["is_admin"] = True
    request.session["guest_name"] = username
    request.session["admin_username"] = username
    deps.stamp_admin_session(request)
    return {"ok": True}


# --- guest login -------------------------------------------------------------

class GuestLogin(BaseModel):
    name: str


@router.post("/guest")
def guest_login(body: GuestLogin, request: Request) -> dict:
    """Log in as a guest by providing a display name (only while the party runs).

    The name is kept in the signed session cookie, so guests stay logged in
    across reloads and browser restarts for the lifetime of the cookie.
    """
    if not queue_manager.is_party_on():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "The party has not started yet. Please wait for the host."
        )
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Name must not be empty.")
    request.session["guest_name"] = name[:40]
    request.session.pop("is_admin", None)
    return {"ok": True, "guest_name": request.session["guest_name"]}


# --- Spotify linking ---------------------------------------------------------

_OAUTH_STATE_TTL = 600  # seconds a pending OAuth state stays valid


@router.get("/spotify/login")
def spotify_login(request: Request):
    """Start the Spotify OAuth flow to link Spotify to the admin account."""
    if not request.session.get("is_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin login required.")
    if not settings_store.has_spotify_credentials():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Spotify credentials are not configured yet. Add them in settings first.",
        )
    # Store the CSRF state server-side (DB) and bind the flow to the initiating
    # browser session via a nonce kept in the session cookie. The callback must
    # present both the valid single-use state AND the matching session nonce
    # before it is allowed to (re-)establish an admin session.
    state = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    request.session["oauth_nonce"] = nonce
    db.kv_set(
        f"oauth_state:{state}",
        {"created": time.time(), "by": request.session.get("admin_username"), "nonce": nonce},
    )
    return RedirectResponse(spotify_client.build_authorize_url(state))


@router.get("/callback")
async def spotify_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """OAuth redirect target. Exchanges the code and links Spotify to the admin."""
    if error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Spotify authorization failed: {error}")
    pending = db.kv_get(f"oauth_state:{state}") if state else None
    if not code or not pending:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired OAuth state, or missing code.")
    db.kv_set(f"oauth_state:{state}", None)  # single-use
    if time.time() - pending.get("created", 0) > _OAUTH_STATE_TTL:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "OAuth state expired. Please try connecting again.")
    # The callback must arrive in the same browser session that started the
    # flow (nonce match) - otherwise a leaked/forwarded callback URL could be
    # used to obtain an admin session.
    session_nonce = request.session.pop("oauth_nonce", None)
    if not session_nonce or session_nonce != pending.get("nonce"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "OAuth callback did not originate from this session. Please try connecting again.",
        )
    await spotify_client.exchange_code(code)
    # Re-establish the admin session on the callback host (the flow was started
    # by an authenticated admin, proven by state + session nonce).
    request.session["is_admin"] = True
    request.session.setdefault("guest_name", pending.get("by") or "Admin")
    if pending.get("by"):
        request.session["admin_username"] = pending["by"]
    deps.stamp_admin_session(request)
    return RedirectResponse("/admin.html")


@router.post("/spotify/disconnect")
def spotify_disconnect(request: Request) -> dict:
    """Unlink Spotify from the admin account."""
    if not request.session.get("is_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin login required.")
    spotify_client.logout_admin()
    return {"ok": True}


@router.post("/logout")
def logout(request: Request) -> dict:
    """Clear the current session (guest or admin)."""
    request.session.clear()
    return {"ok": True}


@router.get("/me")
def whoami(request: Request) -> dict:
    """Return the current identity for the frontend to adapt its UI."""
    return {
        "guest_name": request.session.get("guest_name"),
        "is_admin": bool(request.session.get("is_admin")),
        "spotify_connected": spotify_client.is_admin_authenticated(),
    }
