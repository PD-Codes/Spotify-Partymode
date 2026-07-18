"""Shared FastAPI dependencies for identity checks."""

from __future__ import annotations

import secrets
import time

from fastapi import Depends, HTTPException, Request, status

from .. import db


def get_guest_name(request: Request) -> str:
    """Return the logged-in guest name or raise 401."""
    name = request.session.get("guest_name")
    if not name:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please log in as a guest first.")
    return name


def get_device_id(request: Request) -> str:
    """Return the persistent per-browser device id, stored in the session cookie.

    Created once per browser and preserved across logout (see auth.logout), so
    per-guest limits (skip/add tokens, blocks) stay anchored to the browser and
    cannot be reset by logging out and re-joining under a new name. Living inside
    the signed session cookie makes it reliable behind proxies / in in-app
    browsers (no separate cookie to lose).
    """
    device_id = request.session.get("device_id")
    if not device_id:
        device_id = secrets.token_urlsafe(16)
        request.session["device_id"] = device_id
    return device_id


# --- session generation (global logout on credential changes) -----------------

_SESSION_GEN_KEY = "session_generation"


def current_session_generation() -> int:
    """Return the global admin-session generation counter."""
    try:
        return int(db.kv_get(_SESSION_GEN_KEY, 0))
    except (TypeError, ValueError):
        return 0


def bump_session_generation() -> int:
    """Invalidate all existing admin sessions (e.g. after a password change)."""
    gen = current_session_generation() + 1
    db.kv_set(_SESSION_GEN_KEY, gen)
    return gen


def stamp_admin_session(request: Request) -> None:
    """Embed the current generation into a freshly authenticated admin session."""
    request.session["session_gen"] = current_session_generation()


def require_admin(request: Request) -> None:
    """Ensure the caller is logged in as the local admin (Spotify not required)."""
    if not request.session.get("is_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required.")
    # Reject sessions issued before the last credential change / account delete.
    if request.session.get("session_gen") != current_session_generation():
        request.session.clear()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Session expired. Please log in again.")


AdminGuard = Depends(require_admin)
GuestName = Depends(get_guest_name)
DeviceId = Depends(get_device_id)


# --- simple in-memory per-IP rate limiting for auth endpoints ------------------

_RATE_MAX_ATTEMPTS = 5
_RATE_WINDOW_SECONDS = 15 * 60
_failed_attempts: dict[tuple[str, str], list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def check_rate_limit(request: Request, bucket: str) -> None:
    """Raise 429 if this IP exceeded the failed-attempt budget for the bucket."""
    key = (bucket, _client_ip(request))
    now = time.time()
    attempts = [t for t in _failed_attempts.get(key, []) if now - t < _RATE_WINDOW_SECONDS]
    _failed_attempts[key] = attempts
    if len(attempts) >= _RATE_MAX_ATTEMPTS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Please try again later.",
        )


def record_failed_attempt(request: Request, bucket: str) -> None:
    key = (bucket, _client_ip(request))
    _failed_attempts.setdefault(key, []).append(time.time())


def reset_rate_limit(request: Request, bucket: str) -> None:
    _failed_attempts.pop((bucket, _client_ip(request)), None)
