"""Wish-queue orchestration and the background poller.

Design goal: a fixed playlist keeps playing; guest "wish" songs are injected so
they play *next*, and the playlist resumes automatically afterwards.

Spotify's ad-hoc queue cannot be reordered or cleared once items are in it, so
we keep the wish list inside the app and push only ONE wish to Spotify at a
time -- right after the previous one has finished. That keeps full reorder /
reject control in the app while letting Spotify handle the actual playback and
the "resume the playlist" behaviour for free.
"""

from __future__ import annotations

import asyncio
import logging

from . import db, settings_store, spotify_client

logger = logging.getLogger("partymode.queue")

# kv keys for party state.
KEY_PARTY_ON = "party_on"
KEY_PLAYLIST = "active_playlist"
KEY_IN_FLIGHT = "in_flight"  # {"wish_id", "uri", "state": "queued"|"playing"}


# --- party state -------------------------------------------------------------

def is_party_on() -> bool:
    return bool(db.kv_get(KEY_PARTY_ON, False))


def set_party_on(value: bool) -> None:
    db.kv_set(KEY_PARTY_ON, bool(value))


def get_active_playlist() -> str:
    return db.kv_get(KEY_PLAYLIST, settings_store.get(settings_store.DEFAULT_PLAYLIST)) or ""


def set_active_playlist(playlist: str) -> None:
    db.kv_set(KEY_PLAYLIST, playlist)


# --- adding wishes -----------------------------------------------------------

class WishRejected(Exception):
    """Raised when a wish cannot be added (e.g. blacklisted or party off)."""


def add_guest_wish(track: dict, added_by: str) -> int:
    """Validate and enqueue a guest wish. Returns the new wish id."""
    if not is_party_on():
        raise WishRejected("Party mode is currently off.")
    if db.is_blacklisted(track["id"], track.get("artist_ids", [])):
        raise WishRejected("This track or artist is blocked.")
    return db.add_wish(track, added_by)


# --- the poller --------------------------------------------------------------
#
# Strategy: we feed ONE pending wish into Spotify's queue shortly before the
# current track ends (default 20s, configurable). This sits before a crossfade
# pre-load so the wish becomes the genuine next track. The logic is stateless
# apart from two self-healing markers, so it can never get permanently stuck:
#   - KEY_FEED_MARKER : {"uri", "fed"} -> at most one feed per track instance.
#   - KEY_CURRENT_WISH: {"id", "uri"}  -> the queued wish currently playing,
#                                          used only to mark it 'played' for the
#                                          display once the track moves on.

KEY_FEED_MARKER = "feed_marker"
KEY_CURRENT_WISH = "current_wish"
KEY_LAST_PLAYED = "last_played_uri"


def _item_artist(item: dict) -> str:
    return ", ".join(a.get("name", "") for a in item.get("artists", []))


def _item_image(item: dict) -> str:
    images = (item.get("album", {}) or {}).get("images", [])
    return images[-1]["url"] if images else ""


async def _tick() -> None:
    """One poller iteration: skip blacklisted tracks, log plays, feed next wish."""
    if not is_party_on() or not spotify_client.is_admin_authenticated():
        return

    playback = await spotify_client.get_playback()
    if playback is None or not playback.get("is_playing"):
        return

    item = playback.get("item") or {}
    current_uri = item.get("uri")
    track_id = item.get("id")
    artist_ids = [a.get("id") for a in item.get("artists", []) if a.get("id")]
    duration_ms = item.get("duration_ms")
    progress_ms = playback.get("progress_ms")

    # --- blacklist skip (party mode only, which is guaranteed above) ---
    if track_id and db.is_blacklisted(track_id, artist_ids):
        try:
            await spotify_client.skip_next()
            logger.info("Skipped blacklisted track '%s'", item.get("name"))
        except Exception:  # noqa: BLE001
            logger.exception("Failed to skip blacklisted track")
        return  # do not log or feed on a blacklisted track

    # --- play-history logging (only with an active session) ---
    session_id = db.current_session_id()
    if session_id and current_uri and db.kv_get(KEY_LAST_PLAYED) != current_uri:
        wish = db.get_wish_by_uri(session_id, current_uri)
        db.add_play(
            session_id,
            {"uri": current_uri, "name": item.get("name", ""),
             "artist": _item_artist(item), "image_url": _item_image(item)},
            source="wish" if wish else "playlist",
            added_by=wish["added_by"] if wish else None,
        )
        db.kv_set(KEY_LAST_PLAYED, current_uri)

    # --- display cleanup: mark a queued wish as played once the track moves on ---
    current_wish = db.kv_get(KEY_CURRENT_WISH)
    if current_wish and current_uri != current_wish.get("uri"):
        db.set_wish_status(current_wish["id"], "played")
        db.kv_set(KEY_CURRENT_WISH, None)
        current_wish = None
    if current_wish is None and current_uri:
        for w in db.list_wishes(("queued",)):
            if w["track_uri"] == current_uri:
                db.kv_set(KEY_CURRENT_WISH, {"id": w["id"], "uri": current_uri})
                break

    # --- timed feeding of the next pending wish ---
    if current_uri is None or duration_ms is None or progress_ms is None:
        return  # local file / ad / unknown timing -> skip this tick safely
    remaining_ms = duration_ms - progress_ms
    lead_ms = settings_store.get_insert_lead() * 1000

    marker = db.kv_get(KEY_FEED_MARKER) or {"uri": None, "fed": False}
    if marker.get("uri") != current_uri:
        marker = {"uri": current_uri, "fed": False}  # new track instance

    if not marker["fed"] and 0 < remaining_ms <= lead_ms:
        nxt = db.next_pending_wish()
        if nxt is not None:
            try:
                await spotify_client.add_to_queue(nxt["track_uri"])
            except Exception:  # noqa: BLE001 - log and retry next tick (don't mark fed)
                logger.exception("Failed to push wish %s to Spotify queue", nxt["id"])
                db.kv_set(KEY_FEED_MARKER, marker)
                return
            db.set_wish_status(nxt["id"], "queued")
            marker["fed"] = True
            logger.info("Fed wish '%s' (%.0fs before end)", nxt["track_name"], remaining_ms / 1000)

    db.kv_set(KEY_FEED_MARKER, marker)


async def run_poller(stop_event: asyncio.Event) -> None:
    """Run the poller loop until the stop event is set."""
    logger.info("Queue poller started")
    while not stop_event.is_set():
        try:
            await _tick()
        except Exception:  # noqa: BLE001 - never let the loop die
            logger.exception("Unexpected error in poller tick")
        try:
            # Read the interval each cycle so changes apply without a restart.
            await asyncio.wait_for(stop_event.wait(), timeout=settings_store.get_poll_interval())
        except asyncio.TimeoutError:
            pass
    logger.info("Queue poller stopped")
