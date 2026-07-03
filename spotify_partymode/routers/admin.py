"""Admin-only API: party control, queue management, blacklist."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from .. import db, queue_manager, security, settings_store, spotify_client
from . import deps
from .deps import AdminGuard

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[AdminGuard])


# --- settings ----------------------------------------------------------------

@router.get("/settings")
def get_settings() -> dict:
    """Return current settings. The client secret is masked, never sent back."""
    cid, secret, redirect = settings_store.get_spotify_credentials()
    return {
        "spotify_client_id": cid,
        "spotify_client_secret_set": bool(secret),
        "spotify_redirect_uri": redirect,
        "default_playlist": settings_store.get(settings_store.DEFAULT_PLAYLIST) or "",
        "poll_interval_seconds": settings_store.get_poll_interval(),
        "insert_lead_seconds": settings_store.get_insert_lead(),
        "registration_open": settings_store.is_registration_open(),
        "spotify_connected": spotify_client.is_admin_authenticated(),
    }


class SettingsBody(BaseModel):
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None  # only set when non-empty
    spotify_redirect_uri: str | None = None
    default_playlist: str | None = None
    poll_interval_seconds: int | None = None
    insert_lead_seconds: int | None = None
    registration_open: bool | None = None


@router.post("/settings")
def update_settings(body: SettingsBody) -> dict:
    """Update settings. Empty/None fields are left unchanged."""
    if body.spotify_client_id is not None:
        settings_store.set(settings_store.SPOTIFY_CLIENT_ID, body.spotify_client_id.strip())
    if body.spotify_client_secret:  # ignore empty -> keep existing secret
        settings_store.set(settings_store.SPOTIFY_CLIENT_SECRET, body.spotify_client_secret.strip())
    if body.spotify_redirect_uri is not None:
        settings_store.set(settings_store.SPOTIFY_REDIRECT_URI, body.spotify_redirect_uri.strip())
    if body.default_playlist is not None:
        settings_store.set(settings_store.DEFAULT_PLAYLIST, body.default_playlist.strip())
    if body.poll_interval_seconds is not None:
        settings_store.set(settings_store.POLL_INTERVAL, int(body.poll_interval_seconds))
    if body.insert_lead_seconds is not None:
        settings_store.set(settings_store.INSERT_LEAD, int(body.insert_lead_seconds))
    if body.registration_open is not None:
        settings_store.set(settings_store.REGISTRATION_OPEN, bool(body.registration_open))
    return {"ok": True}


# --- user / account management -----------------------------------------------

@router.get("/users")
def list_users() -> dict:
    """List all manager accounts."""
    return {"users": db.list_admins()}


class NewUser(BaseModel):
    username: str
    password: str


@router.post("/users")
def create_user(body: NewUser) -> dict:
    """Create a new manager account (admin-initiated)."""
    username = body.username.strip()
    if len(username) < 3 or len(body.password) < 6:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Username must be >= 3 chars and password >= 6 chars."
        )
    if db.get_admin_by_username(username):
        raise HTTPException(status.HTTP_409_CONFLICT, "That username is already taken.")
    db.create_admin(username, security.hash_password(body.password))
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request) -> dict:
    """Delete a manager account. Cannot delete yourself or the last account."""
    target = db.get_admin_by_id(user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    if target["username"] == request.session.get("admin_username"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete your own account.")
    if db.count_admins() <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot delete the last account.")
    db.delete_admin(user_id)
    # Invalidate all admin sessions so the deleted account is logged out.
    deps.bump_session_generation()
    deps.stamp_admin_session(request)
    return {"ok": True}


class PasswordBody(BaseModel):
    username: str
    current_password: str
    new_password: str


@router.post("/password")
def change_password(body: PasswordBody, request: Request) -> dict:
    """Change the local admin password (only for the logged-in account)."""
    deps.check_rate_limit(request, "change_password")
    username = body.username.strip()
    # Only allow changing the password of the account owning this session.
    if username != request.session.get("admin_username"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only change your own password.")
    admin = db.get_admin_by_username(username)
    if not admin or not security.verify_password(body.current_password, admin["password_hash"]):
        deps.record_failed_attempt(request, "change_password")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Current credentials are invalid.")
    if len(body.new_password) < 6:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "New password must be >= 6 chars.")
    db.update_admin_password(admin["id"], security.hash_password(body.new_password))
    deps.reset_rate_limit(request, "change_password")
    # Invalidate all existing admin sessions, then re-stamp the current one.
    deps.bump_session_generation()
    deps.stamp_admin_session(request)
    return {"ok": True}


class PartyToggle(BaseModel):
    on: bool


@router.post("/party")
def toggle_party(body: PartyToggle) -> dict:
    """Turn party mode on or off."""
    queue_manager.set_party_on(body.on)
    return {"ok": True, "party_on": body.on}


class PlaylistBody(BaseModel):
    playlist: str
    start: bool = False


@router.post("/playlist")
async def set_playlist(body: PlaylistBody) -> dict:
    """Set the fixed playlist, optionally starting playback immediately."""
    queue_manager.set_active_playlist(body.playlist.strip())
    if body.start and body.playlist.strip():
        await spotify_client.start_playlist(body.playlist.strip())
    return {"ok": True, "playlist": queue_manager.get_active_playlist()}


@router.post("/start")
async def start_playback() -> dict:
    """Start playback of the active playlist."""
    playlist = queue_manager.get_active_playlist()
    if not playlist:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No playlist configured.")
    await spotify_client.start_playlist(playlist)
    return {"ok": True}


@router.post("/skip")
async def skip() -> dict:
    """Skip the currently playing track."""
    await spotify_client.skip_next()
    return {"ok": True}


class ReorderBody(BaseModel):
    ids: list[int]


@router.post("/reorder")
def reorder(body: ReorderBody) -> dict:
    """Reorder the pending wish queue. Already-queued songs cannot be moved."""
    db.reorder_wishes(body.ids)
    return {"ok": True}


class WishIdBody(BaseModel):
    wish_id: int


@router.post("/reject")
def reject(body: WishIdBody) -> dict:
    """Reject a pending wish. (Songs already pushed to Spotify can only be skipped.)"""
    db.set_wish_status(body.wish_id, "rejected")
    return {"ok": True}


@router.post("/history/clear")
def clear_history() -> dict:
    """Clear the current session's wish history (keeps the active queue)."""
    db.clear_history()
    return {"ok": True}


@router.delete("/history/{wish_id}")
def remove_history_entry(wish_id: int) -> dict:
    """Remove a single entry from the wish history."""
    db.delete_wish(wish_id)
    return {"ok": True}


# --- session control ---------------------------------------------------------

class SessionStart(BaseModel):
    name: str = ""


@router.post("/sessions/start")
def start_session(body: SessionStart) -> dict:
    """Start a new party session. Also turns party mode on (coupled)."""
    import time as _time

    name = body.name.strip() or _time.strftime("Party %Y-%m-%d %H:%M")
    sid = db.start_session(name)
    queue_manager.set_party_on(True)
    return {"ok": True, "session_id": sid, "name": name}


@router.post("/sessions/end")
def end_session() -> dict:
    """End the current session. Also turns party mode off (coupled)."""
    db.end_session()
    queue_manager.set_party_on(False)
    return {"ok": True}


@router.get("/sessions")
def list_sessions() -> dict:
    return {"sessions": db.list_sessions()}


@router.get("/sessions/{session_id}/history")
def session_wish_history(session_id: int) -> dict:
    return {
        "history": [
            {
                "id": w["id"],
                "name": w["track_name"],
                "artist": w["artist"],
                "image_url": w["image_url"],
                "added_by": w["added_by"],
                "status": w["status"],
                "created_at": w["created_at"],
            }
            for w in db.list_history(session_id)
        ]
    }


@router.get("/sessions/{session_id}/play-history")
def session_play_history(session_id: int) -> dict:
    return {
        "history": [
            {
                "id": p["id"],
                "name": p["track_name"],
                "artist": p["artist"],
                "image_url": p["image_url"],
                "source": p["source"],
                "added_by": p["added_by"],
                "played_at": p["played_at"],
            }
            for p in db.list_play_history(session_id)
        ]
    }


@router.post("/play-history/clear")
def clear_play_history() -> dict:
    """Clear the current session's play history."""
    db.clear_play_history(db.current_session_id())
    return {"ok": True}


@router.delete("/play-history/{entry_id}")
def remove_play_entry(entry_id: int) -> dict:
    db.delete_play_entry(entry_id)
    return {"ok": True}


@router.get("/blacklist")
def get_blacklist() -> dict:
    return {"items": db.list_blacklist()}


class BlacklistBody(BaseModel):
    kind: str  # 'artist' or 'track'
    spotify_id: str
    name: str


@router.post("/blacklist")
def add_to_blacklist(body: BlacklistBody) -> dict:
    if body.kind not in ("artist", "track"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kind must be 'artist' or 'track'.")
    db.add_blacklist(body.kind, body.spotify_id, body.name)
    return {"ok": True}


@router.delete("/blacklist/{entry_id}")
def delete_blacklist(entry_id: int) -> dict:
    db.remove_blacklist(entry_id)
    return {"ok": True}
