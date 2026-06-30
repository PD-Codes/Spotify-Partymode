"""Shared FastAPI dependencies for identity checks."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status


def get_guest_name(request: Request) -> str:
    """Return the logged-in guest name or raise 401."""
    name = request.session.get("guest_name")
    if not name:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please log in as a guest first.")
    return name


def require_admin(request: Request) -> None:
    """Ensure the caller is logged in as the local admin (Spotify not required)."""
    if not request.session.get("is_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required.")


AdminGuard = Depends(require_admin)
GuestName = Depends(get_guest_name)
