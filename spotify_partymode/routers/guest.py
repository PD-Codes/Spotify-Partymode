"""Guest-facing API: view state, search tracks, add wishes."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from .. import db, queue_manager, settings_store, spotify_client
from .deps import DeviceId, GuestName

router = APIRouter(prefix="/api", tags=["guest"])


def _hour_bucket() -> int:
    """Current whole-hour bucket used to refill skip tokens."""
    return int(time.time() // 3600)


def _token_status(device_id: str) -> dict:
    """Skip-token budget for a device in the current hour."""
    maximum = settings_store.get_skip_tokens_per_hour()
    bucket = _hour_bucket()
    used = db.get_guest_skips_used(device_id, bucket)
    return {
        "max": maximum,
        "used": used,
        "remaining": max(0, maximum - used),
        "resets_at": (bucket + 1) * 3600,
    }


# Short shared throttle so many guests polling at once don't multiply Spotify
# calls: one live fetch every _SPOTIFY_THROTTLE seconds, reused by all requests.
_SPOTIFY_THROTTLE = 2.0
_pb_cache: dict = {"ts": 0.0, "data": None}
_q_cache: dict = {"ts": 0.0, "data": None}


async def _throttled(cache: dict, fetch) -> object:
    now = time.time()
    if cache["ts"] and now - cache["ts"] < _SPOTIFY_THROTTLE:
        return cache["data"]
    try:
        data = await fetch()
    except Exception:  # noqa: BLE001 - playback may be inactive / transient errors
        data = None
    cache["ts"] = now
    cache["data"] = data
    return data


def _current_from_playback(pb, blacklist) -> dict | None:
    """Build the "now playing" record from /me/player (the authoritative source).

    Unlike the queue endpoint's `currently_playing` (which returns the NEXT real
    track while a local file plays), /me/player carries the real current track in
    `item` — including local files (is_local=True, id=null, name/artist from the
    file metadata). We use `item` whenever it is present, REGARDLESS of the
    is_playing flag: Spotify reports is_playing=false for local files even while
    they play, so gating on it is exactly what made the app show the next song.

    Returns None only when there is no track object at all, so the caller may
    fall back to the queue snapshot.
    """
    if not pb:
        return None
    item = pb.get("item")
    if not item:
        # Playing but no track object at all -> unrepresentable content (ad).
        if pb.get("is_playing"):
            cpt = (pb.get("currently_playing_type") or "").lower()
            label = "Advertisement" if cpt == "ad" else "Local playback"
            return {"uri": None, "name": label, "artist": "", "album": "",
                    "image_url": "", "blacklisted": False}
        return None
    is_local = bool(item.get("is_local"))
    images = (item.get("album", {}) or {}).get("images", [])
    track_id = item.get("id")
    artist_ids = [a.get("id") for a in item.get("artists", []) if a.get("id")]
    blacklisted = False
    if blacklist is not None:
        blocked_tracks, blocked_artists = blacklist
        blacklisted = bool(
            (track_id and track_id in blocked_tracks)
            or any(a in blocked_artists for a in artist_ids)
        )
    return {
        "uri": item.get("uri"),
        "name": item.get("name") or ("Local playback" if is_local else ""),
        "artist": ", ".join(a.get("name", "") for a in item.get("artists", []))
        or ("Local file" if is_local else ""),
        "album": (item.get("album", {}) or {}).get("name", ""),
        "image_url": images[-1]["url"] if images else "",
        "blacklisted": blacklisted,
    }


@router.get("/state")
async def get_state(_: str = GuestName, device_id: str = DeviceId) -> dict:
    """Return current track, the wish queue (with who added) and playlist upcoming."""
    party_on = queue_manager.is_party_on()
    current = None
    upcoming: list[dict] = []
    if spotify_client.is_admin_authenticated():
        # Load the blacklist once per request instead of one query per track.
        blacklist = _blacklist_sets() if party_on else None
        # Authoritative "now playing" comes from /me/player, NOT the queue's
        # currently_playing (which shows the next song while a local file plays).
        pb = await _throttled(_pb_cache, spotify_client.get_playback)
        current = _current_from_playback(pb, blacklist)
        queue = await _throttled(_q_cache, spotify_client.get_queue) or {
            "currently_playing": None, "queue": []
        }
        # Only if nothing is actively playing, fall back to the queue snapshot.
        if current is None:
            cp = queue.get("currently_playing")
            if cp:
                current = _simplify(cp, blacklist)
        upcoming = [_simplify(t, blacklist) for t in (queue.get("queue") or [])[:12]]

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
        "tokens": _token_status(device_id),
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
        "uri": p.get("track_uri", ""),
        "source": p["source"],
        "added_by": p["added_by"],
        "played_at": p["played_at"],
    }


@router.get("/search")
async def search(
    q: str,
    type_: str = Query("track", alias="type"),
    _: str = GuestName,
) -> dict:
    """Search Spotify for tracks (default) or artists. Requires a guest session."""
    if not spotify_client.is_admin_authenticated():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Admin is not connected to Spotify.")
    if type_ == "artist":
        results = await spotify_client.search_artists(q)
        return {"results": results, "type": "artist"}
    results = await spotify_client.search_tracks(q)
    return {"results": results, "type": "track"}


# --- skip tokens -------------------------------------------------------------

@router.get("/tokens")
def get_tokens(_: str = GuestName, device_id: str = DeviceId) -> dict:
    """Return the guest's current skip-token budget."""
    return {"tokens": _token_status(device_id)}


@router.post("/skip")
async def guest_skip(_: str = GuestName, device_id: str = DeviceId) -> dict:
    """Spend one skip token to skip the currently playing track."""
    if not queue_manager.is_party_on():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "The party is not running.")
    if not spotify_client.is_admin_authenticated():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Admin is not connected to Spotify.")
    maximum = settings_store.get_skip_tokens_per_hour()
    bucket = _hour_bucket()
    if db.get_guest_skips_used(device_id, bucket) >= maximum:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "No skip tokens left — they refill at the next full hour.",
        )
    try:
        await spotify_client.skip_next()
    except Exception as exc:  # noqa: BLE001 - only charge a token on a real skip
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Could not skip right now. Please try again."
        ) from exc
    # Charge the token only after the skip actually succeeded.
    db.incr_guest_skip(device_id, bucket)
    return {"ok": True, "tokens": _token_status(device_id)}


# --- personal blocks ---------------------------------------------------------

class BlockBody(BaseModel):
    kind: str  # 'artist' or 'track'
    spotify_id: str
    name: str


def _block_limits(session_id, device_id: str) -> dict:
    return {
        "artists_max": settings_store.get_guest_block_artists_max(),
        "tracks_max": settings_store.get_guest_block_tracks_max(),
        "artists_used": db.count_guest_blocks(session_id, device_id, "artist"),
        "tracks_used": db.count_guest_blocks(session_id, device_id, "track"),
    }


@router.get("/blocks")
def get_blocks(_: str = GuestName, device_id: str = DeviceId) -> dict:
    """List this device's own blocks for the current party and their limits."""
    sid = db.current_session_id()
    return {"blocks": db.list_guest_blocks(sid, device_id), "limits": _block_limits(sid, device_id)}


@router.post("/block")
def add_block(body: BlockBody, guest_name: str = GuestName, device_id: str = DeviceId) -> dict:
    """Block an artist or track for the party (skipped for everyone)."""
    if body.kind not in ("artist", "track"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kind must be 'artist' or 'track'.")
    spotify_id = body.spotify_id.strip()
    name = body.name.strip()
    if not spotify_id or not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A valid selection is required.")
    sid = db.current_session_id()
    if db.guest_block_exists(sid, device_id, body.kind, spotify_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "You already blocked that.")
    maximum = (
        settings_store.get_guest_block_artists_max()
        if body.kind == "artist"
        else settings_store.get_guest_block_tracks_max()
    )
    if db.count_guest_blocks(sid, device_id, body.kind) >= maximum:
        label = "artists" if body.kind == "artist" else "tracks"
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"You reached your limit of {maximum} blocked {label}."
        )
    db.add_guest_block(sid, device_id, guest_name, body.kind, spotify_id, name)
    return {"ok": True, "limits": _block_limits(sid, device_id)}


@router.delete("/block/{block_id}")
def delete_block(block_id: int, _: str = GuestName, device_id: str = DeviceId) -> dict:
    """Remove one of this device's own blocks."""
    db.remove_guest_block(block_id, device_id)
    sid = db.current_session_id()
    return {"ok": True, "limits": _block_limits(sid, device_id)}


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
    """Return (blocked track ids, blocked artist ids): admin blacklist + guest blocks."""
    items = db.list_blacklist()
    tracks = {i["spotify_id"] for i in items if i["kind"] == "track"}
    artists = {i["spotify_id"] for i in items if i["kind"] == "artist"}
    guest_tracks, guest_artists = db.active_guest_block_sets(db.current_session_id())
    return tracks | guest_tracks, artists | guest_artists


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
