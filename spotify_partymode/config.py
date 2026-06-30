"""Bootstrap configuration.

Only the few values needed to start the server and locate the database live
here (and may be overridden via environment variables, e.g. for Docker).
Everything else - Spotify credentials, redirect URI, default playlist, session
secret, poll interval - is stored in the database and managed from the in-app
setup wizard / admin settings.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Bootstrap(BaseSettings):
    """Minimal startup settings read from the environment (all optional)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # SQLite database file path.
    database_path: str = "partymode.db"

    # Web server bind address.
    host: str = "0.0.0.0"
    port: int = 8000


bootstrap = Bootstrap()

# OAuth scopes required to control playback and read the queue.
SPOTIFY_SCOPES = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "user-read-currently-playing "
    "playlist-read-private"
)
