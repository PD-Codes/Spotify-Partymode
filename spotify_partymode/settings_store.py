"""Application settings persisted in the database (kv store).

These replace the old .env values. They are seeded with defaults on first run
and edited through the setup wizard / admin settings panel.
"""

from __future__ import annotations

import secrets

from . import db

# Setting keys.
SPOTIFY_CLIENT_ID = "spotify_client_id"
SPOTIFY_CLIENT_SECRET = "spotify_client_secret"
SPOTIFY_REDIRECT_URI = "spotify_redirect_uri"
DEFAULT_PLAYLIST = "default_playlist"
POLL_INTERVAL = "poll_interval_seconds"
SECRET_KEY = "secret_key"
REGISTRATION_OPEN = "registration_open"
INSERT_LEAD = "insert_lead_seconds"

_DEFAULTS = {
    SPOTIFY_CLIENT_ID: "",
    SPOTIFY_CLIENT_SECRET: "",
    SPOTIFY_REDIRECT_URI: "http://127.0.0.1:8000/auth/callback",
    DEFAULT_PLAYLIST: "",
    POLL_INTERVAL: 4,
    REGISTRATION_OPEN: True,
    INSERT_LEAD: 20,
}


def get(key: str):
    """Return a setting value, falling back to the built-in default."""
    return db.kv_get(f"setting:{key}", _DEFAULTS.get(key))


def set(key: str, value) -> None:  # noqa: A001 - intentional simple API
    db.kv_set(f"setting:{key}", value)


def get_many(keys: list[str]) -> dict:
    return {k: get(k) for k in keys}


def ensure_secret_key() -> str:
    """Return the session secret, generating and storing one on first run."""
    value = db.kv_get(f"setting:{SECRET_KEY}")
    if not value:
        value = secrets.token_hex(32)
        set(SECRET_KEY, value)
    return value


def get_spotify_credentials() -> tuple[str, str, str]:
    return (
        get(SPOTIFY_CLIENT_ID) or "",
        get(SPOTIFY_CLIENT_SECRET) or "",
        get(SPOTIFY_REDIRECT_URI) or _DEFAULTS[SPOTIFY_REDIRECT_URI],
    )


def has_spotify_credentials() -> bool:
    cid, secret, _ = get_spotify_credentials()
    return bool(cid and secret)


def get_poll_interval() -> int:
    try:
        return max(2, int(get(POLL_INTERVAL)))
    except (TypeError, ValueError):
        return 4


def get_insert_lead() -> int:
    """Seconds before a track ends at which the next wish is pushed to Spotify."""
    try:
        return max(6, int(get(INSERT_LEAD)))
    except (TypeError, ValueError):
        return 20


def is_registration_open() -> bool:
    return bool(get(REGISTRATION_OPEN))
