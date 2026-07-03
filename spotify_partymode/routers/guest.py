"""Guest-facing API: view state, search tracks, add wishes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from .. import db, queue_manager, spotify_client
from .deps import GuestName

router = APIRouter(prefix="/api", tags=["guest"])


@router.get("/state")
async def get_state(_: str = GuestName) -> dict:
    """Return current track, the wish queue (with who added) and playlist upcoming."""
    party_on = queue_manager.is_party_on()
    current = None
    upcoming: list[dict] = []
    if spotify_client.is_admin_authenticated():
        try:
            queue = await spotify_client.get_queue()
        except Exception:  # noqa: BLE001 - playback may be inactive
            queue = {"currently_playing": None, "queue": []}
        # Load the blacklist once per request instead of one query per track.
        blacklist = _blacklist_sets() if party_on else None
        cp = queue.get("currently_playing")
        if cp:
            current = _simplify(cp, blacklist)
        upcoming = [_simplify(t, blacklist) for t in queue.get("queue", [])[:12]]

    current_uri = current["uri"] if current else None
    wishes = [
        {
            "id": w["id"],
            "name": w["track_name"],
            "artist": w["artist"],
            "album": w["album"],
            "image_url": w["image_url"],
            "added_by": w["added_by"],
            "status": w["status"],
            # A queued wish whose track is the one playing right now.
            "is_current": bool(current_uri and w["track_uri"] == current_uri),
        }
        for w in db.list_wishes()
    ]
    session = db.get_session(db.current_session_id()) if db.current_session_id() else None
    return {
        "party_on": party_on,
        "playlist": queue_manager.get_active_playlist(),
        "current": current,
        "wishes": wishes,
        "upcoming": upcoming,
        "session": {"id": session["id"], "name": session["name"]} if session else None,
    }


@router.get("/history")
def get_history(_: str = GuestName) -> dict:
    """Wish history (played/rejected) for the current session."""
    return {"history": [_wish_history_row(w) for w in db.list_history()]}


@router.get("/play-history")
def get_play_history(_: str = GuestName) -> dict:
    """Play history (everything that played) for the current session."""
    return {"history": [_play_row(p) for p in db.list_play_history(db.current_session_id())]}


@router.get("/session")
def get_session() -> dict:
    """Return the current session (or null) for guests."""
    sid = db.current_session_id()
    session = db.get_session(sid) if sid else None
    return {"session": {"id": session["id"], "name": session["name"]} if session else None}


def _wish_history_row(w: dict) -> dict:
    return {
        "id": w["id"],
        "name": w["track_name"],
        "artist": w["artist"],
        "image_url": w["image_url"],
        "added_by": w["added_by"],
        "status": w["status"],
        "created_at": w["created_at"],
    }


def _play_row(p: dict) -> dict:
    return {
        "id": p["id"],
        "name": p["track_name"],
        "artist": p["artist"],
        "image_url": p["image_url"],
        "source": p["source"],
        "added_by": p["added_by"],
        "played_at": p["played_at"],
    }


@router.get("/search")
async def search(q: str, _: str = GuestName) -> dict:
    """Search Spotify tracks. Requires a guest session."""
    if not spotify_client.is_admin_authenticated():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Admin is not connected to Spotify.")
    results = await spotify_client.search_tracks(q)
    return {"results": results}


class WishBody(BaseModel):
    uri: str
    id: str
    name: str
    artist: str
    artist_ids: list[str] = []
    album: str = ""
    image_url: str = ""


@router.post("/wish")
async def add_wish(body: WishBody, guest_name: str = GuestName) -> dict:
    """Add a track to the wish queue."""
    try:
        wish_id = queue_manager.add_guest_wish(body.model_dump(), guest_name)
    except queue_manager.WishRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"ok": True, "wish_id": wish_id}


def _blacklist_sets() -> tuple[set[str], set[str]]:
    """Return (blocked track ids, blocked artist ids) loaded in one query."""
    items = db.list_blacklist()
    tracks = {i["spotify_id"] for i in items if i["kind"] == "track"}
    artists = {i["spotify_id"] for i in items if i["kind"] == "artist"}
    return tracks, artists


def _simplify(item: dict, blacklist: tuple[set[str], set[str]] | None = None) -> dict:
    """Reduce a Spotify track object to display fields, incl. a blacklist flag.

    `blacklist` is the preloaded (track ids, artist ids) pair; None means
    party mode is off, in which case nothing is flagged (skipping only
    happens while party mode is on).
    """
    images = (item.get("album", {}) or {}).get("images", [])
    track_id = item.get("id")
    artist_ids = [a.get("id") for a in item.get("artists", []) if a.get("id")]
    blacklisted = False
    if blacklist is not None:
        blocked_tracks, blocked_artists = blacklist
        blacklisted = bool(
            (track_id and track_id in blocked_tracks)
            or any(aid in blocked_artists for aid in artist_ids)
        )
    return {
        "uri": item.get("uri"),
        "name": item.get("name"),
        "artist": ", ".join(a["name"] for a in item.get("artists", [])),
        "album": (item.get("album", {}) or {}).get("name", ""),
        "image_url": images[-1]["url"] if images else "",
        "blacklisted": blacklisted,
    }
