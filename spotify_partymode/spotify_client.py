"""Thin Spotify Web API client with OAuth (Authorization Code flow).

The admin (the Spotify account owner) authenticates once; the resulting tokens
are persisted in the kv store and refreshed automatically when they expire.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import urllib.parse
from typing import Any, Optional

import httpx

from . import db, settings_store
from .config import SPOTIFY_SCOPES

_AUTH_URL = "https://accounts.spotify.com/authorize"
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_API_BASE = "https://api.spotify.com/v1"

_TOKEN_KEY = "spotify_token"  # kv key holding the token dict

logger = logging.getLogger("partymode.spotify")

# Serializes token refreshes so concurrent requests cannot race each other
# (Spotify may rotate the refresh token, so a lost race can invalidate it).
_refresh_lock = asyncio.Lock()


def build_authorize_url(state: str) -> str:
    """Return the Spotify consent URL the admin should be redirected to."""
    client_id, _, redirect_uri = settings_store.get_spotify_credentials()
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SPOTIFY_SCOPES,
        "state": state,
    }
    return f"{_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _basic_auth_header() -> dict[str, str]:
    client_id, client_secret, _ = settings_store.get_spotify_credentials()
    raw = f"{client_id}:{client_secret}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


async def exchange_code(code: str) -> None:
    """Exchange an authorization code for tokens and persist them."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _TOKEN_URL,
            headers=_basic_auth_header(),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings_store.get_spotify_credentials()[2],
            },
        )
        resp.raise_for_status()
        token = resp.json()
    token["expires_at"] = time.time() + token.get("expires_in", 3600)
    db.kv_set(_TOKEN_KEY, token)


async def _refresh_token(token: dict) -> dict:
    """Refresh an expired access token using the stored refresh token.

    On an unusable/rejected refresh token the stored token is cleared and a
    PermissionError is raised so the admin knows Spotify must be re-linked.
    """
    refresh = token.get("refresh_token")
    if not refresh:
        # Without a refresh token the stored token is useless once expired.
        db.kv_set(_TOKEN_KEY, None)
        logger.error("Stored Spotify token has no refresh_token; cleared. Re-link Spotify.")
        raise PermissionError("Spotify token is missing a refresh token. Please re-link Spotify.")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _TOKEN_URL,
                headers=_basic_auth_header(),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
            )
            resp.raise_for_status()
            new = resp.json()
    except httpx.HTTPStatusError as exc:
        # Refresh token revoked/invalid: clear it so the UI shows "not connected".
        db.kv_set(_TOKEN_KEY, None)
        logger.error(
            "Spotify token refresh failed (%s); cleared stored token. Re-link Spotify.",
            exc.response.status_code,
        )
        raise PermissionError("Spotify token refresh failed. Please re-link Spotify.") from exc
    # Spotify may not return a new refresh token; keep the old one if missing.
    token["access_token"] = new["access_token"]
    token["expires_at"] = time.time() + new.get("expires_in", 3600)
    if new.get("refresh_token"):
        token["refresh_token"] = new["refresh_token"]
    db.kv_set(_TOKEN_KEY, token)
    return token


async def _valid_access_token() -> Optional[str]:
    """Return a valid access token, refreshing if needed. None if not logged in."""
    token = db.kv_get(_TOKEN_KEY)
    if not token:
        return None
    if token.get("expires_at", 0) <= time.time() + 30:
        async with _refresh_lock:
            # Re-read: another task may have refreshed while we waited.
            token = db.kv_get(_TOKEN_KEY)
            if not token:
                return None
            if token.get("expires_at", 0) <= time.time() + 30:
                token = await _refresh_token(token)
    return token["access_token"]


def is_admin_authenticated() -> bool:
    return db.kv_get(_TOKEN_KEY) is not None


def logout_admin() -> None:
    db.kv_set(_TOKEN_KEY, None)


async def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    """Make an authenticated request against the Spotify Web API."""
    access = await _valid_access_token()
    if access is None:
        raise PermissionError("Admin is not authenticated with Spotify.")
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {access}"
    async with httpx.AsyncClient(timeout=15) as client:
        return await client.request(method, f"{_API_BASE}{path}", headers=headers, **kwargs)


# --- high-level helpers ------------------------------------------------------

def _track_to_dict(item: dict) -> dict:
    """Normalize a Spotify track object into the shape the app uses."""
    images = (item.get("album", {}) or {}).get("images", [])
    return {
        "uri": item["uri"],
        "id": item["id"],
        "name": item["name"],
        "artist": ", ".join(a["name"] for a in item.get("artists", [])),
        "artist_ids": [a["id"] for a in item.get("artists", [])],
        "album": (item.get("album", {}) or {}).get("name", ""),
        "image_url": images[-1]["url"] if images else "",
    }


async def search_tracks(query: str, limit: int = 20) -> list[dict]:
    resp = await _request("GET", "/search", params={"q": query, "type": "track", "limit": limit})
    resp.raise_for_status()
    items = resp.json().get("tracks", {}).get("items", [])
    return [_track_to_dict(i) for i in items]


async def get_playback() -> Optional[dict]:
    """Return the current playback state, or None if nothing is active."""
    resp = await _request("GET", "/me/player")
    if resp.status_code == 204:
        return None
    resp.raise_for_status()
    return resp.json()


async def get_queue() -> dict:
    """Return Spotify's current queue (currently_playing + queue list)."""
    resp = await _request("GET", "/me/player/queue")
    if resp.status_code == 204:
        return {"currently_playing": None, "queue": []}
    resp.raise_for_status()
    return resp.json()


async def add_to_queue(track_uri: str) -> None:
    resp = await _request("POST", "/me/player/queue", params={"uri": track_uri})
    resp.raise_for_status()


async def start_playlist(playlist: str) -> None:
    """Start playback of the given playlist (URI or bare id)."""
    context_uri = playlist if playlist.startswith("spotify:") else f"spotify:playlist:{playlist}"
    resp = await _request("PUT", "/me/player/play", json={"context_uri": context_uri})
    resp.raise_for_status()


async def skip_next() -> None:
    resp = await _request("POST", "/me/player/next")
    resp.raise_for_status()
