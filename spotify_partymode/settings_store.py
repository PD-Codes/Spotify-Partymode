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
# Number of skip tokens each guest gets per hour (reset every full hour).
SKIP_TOKENS_PER_HOUR = "skip_tokens_per_hour"
# Number of add-a-song tokens each guest gets per hour (reset every full hour).
ADD_TOKENS_PER_HOUR = "add_tokens_per_hour"
# How many artists / individual tracks a single guest may block per party.
GUEST_BLOCK_ARTISTS_MAX = "guest_block_artists_max"
GUEST_BLOCK_TRACKS_MAX = "guest_block_tracks_max"
# Fair play order: interleave pending wishes round-robin across guests so a
# guest who adds many songs cannot make everyone else wait.
FAIR_QUEUE = "fair_queue"

_DEFAULTS = {
    SPOTIFY_CLIENT_ID: "",
    SPOTIFY_CLIENT_SECRET: "",
    SPOTIFY_REDIRECT_URI: "http://127.0.0.1:8000/auth/callback",
    DEFAULT_PLAYLIST: "",
    POLL_INTERVAL: 4,
    # Closed by default: self-registered accounts get full manager rights,
    # so the admin must explicitly opt in to open registration.
    REGISTRATION_OPEN: False,
    INSERT_LEAD: 20,
    SKIP_TOKENS_PER_HOUR: 3,
    ADD_TOKENS_PER_HOUR: 5,
    GUEST_BLOCK_ARTISTS_MAX: 3,
    GUEST_BLOCK_TRACKS_MAX: 5,
    FAIR_QUEUE: False,
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


def get_skip_tokens_per_hour() -> int:
    """Skip tokens granted to each guest, refilled every full hour."""
    try:
        return max(0, int(get(SKIP_TOKENS_PER_HOUR)))
    except (TypeError, ValueError):
        return 3


def get_add_tokens_per_hour() -> int:
    """Add-a-song tokens granted to each guest, refilled every full hour."""
    try:
        return max(0, int(get(ADD_TOKENS_PER_HOUR)))
    except (TypeError, ValueError):
        return 5


def is_fair_queue() -> bool:
    return bool(get(FAIR_QUEUE))


def get_guest_block_artists_max() -> int:
    try:
        return max(0, int(get(GUEST_BLOCK_ARTISTS_MAX)))
    except (TypeError, ValueError):
        return 3


def get_guest_block_tracks_max() -> int:
    try:
        return max(0, int(get(GUEST_BLOCK_TRACKS_MAX)))
    except (TypeError, ValueError):
        return 5
