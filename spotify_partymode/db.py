"""SQLite persistence layer.

Tables:
  kv        - simple key/value store (admin token, party state, active playlist).
  blacklist - blocked artists or tracks.
  wish      - the app-managed wish queue with "who added" metadata.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .config import bootstrap

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_account (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS blacklist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,          -- 'artist' or 'track'
    spotify_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (kind, spotify_id)
);

CREATE TABLE IF NOT EXISTS wish (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    track_uri  TEXT NOT NULL,
    track_id   TEXT NOT NULL,
    track_name TEXT NOT NULL,
    artist     TEXT NOT NULL,
    artist_ids TEXT NOT NULL DEFAULT '[]',
    album      TEXT NOT NULL DEFAULT '',
    image_url  TEXT NOT NULL DEFAULT '',
    added_by   TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending|queued|played|rejected
    position   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    session_id INTEGER
);

CREATE TABLE IF NOT EXISTS session (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at   REAL
);

CREATE TABLE IF NOT EXISTS play_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    track_uri  TEXT NOT NULL,
    track_name TEXT NOT NULL,
    artist     TEXT NOT NULL DEFAULT '',
    image_url  TEXT NOT NULL DEFAULT '',
    played_at  REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT 'playlist',  -- playlist|wish
    added_by   TEXT
);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection with row access by column name."""
    conn = sqlite3.connect(bootstrap.database_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they do not exist yet (and migrate older databases)."""
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        # Migration: add wish.session_id to databases created before sessions.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(wish)").fetchall()]
        if "session_id" not in cols:
            conn.execute("ALTER TABLE wish ADD COLUMN session_id INTEGER")


# --- session helpers ---------------------------------------------------------

_CURRENT_SESSION = "current_session_id"


def current_session_id() -> Optional[int]:
    return kv_get(_CURRENT_SESSION)


def start_session(name: str) -> int:
    """End any open session and start a new one. Returns the new session id."""
    end_session()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO session (name, started_at) VALUES (?, ?)", (name, time.time())
        )
        sid = int(cur.lastrowid)
    kv_set(_CURRENT_SESSION, sid)
    return sid


def end_session() -> None:
    sid = current_session_id()
    if sid:
        with get_conn() as conn:
            conn.execute(
                "UPDATE session SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                (time.time(), sid),
            )
    kv_set(_CURRENT_SESSION, None)


def get_session(session_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def list_sessions() -> list[dict]:
    cur = current_session_id()
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM session ORDER BY started_at DESC").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["is_current"] = d["id"] == cur
        result.append(d)
    return result


# --- play-history helpers ----------------------------------------------------

def add_play(session_id: Optional[int], track: dict, source: str, added_by: Optional[str]) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO play_history (session_id, track_uri, track_name, artist, image_url, "
            "played_at, source, added_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                track.get("uri", ""),
                track.get("name", ""),
                track.get("artist", ""),
                track.get("image_url", ""),
                time.time(),
                source,
                added_by,
            ),
        )


def list_play_history(session_id: Optional[int], limit: int = 200) -> list[dict]:
    if session_id is None:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM play_history WHERE session_id = ? ORDER BY played_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def clear_play_history(session_id: Optional[int]) -> None:
    if session_id is None:
        return
    with get_conn() as conn:
        conn.execute("DELETE FROM play_history WHERE session_id = ?", (session_id,))


def delete_play_entry(entry_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM play_history WHERE id = ?", (entry_id,))


def get_wish_by_uri(session_id: Optional[int], track_uri: str) -> Optional[dict]:
    """Find a wish in the session matching this track (queued or played)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM wish WHERE track_uri = ? AND status IN ('queued', 'played') "
            "AND (session_id = ? OR ? IS NULL) ORDER BY created_at DESC LIMIT 1",
            (track_uri, session_id, session_id),
        ).fetchone()
    return dict(row) if row else None


# --- key/value helpers -------------------------------------------------------

def kv_get(key: str, default: Any = None) -> Any:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def kv_set(key: str, value: Any) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


# --- admin account helpers ---------------------------------------------------

def admin_exists() -> bool:
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM admin_account LIMIT 1").fetchone() is not None


def create_admin(username: str, password_hash: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO admin_account (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, time.time()),
        )
        return int(cur.lastrowid)


def get_admin_by_username(username: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM admin_account WHERE username = ?", (username,)
        ).fetchone()
    return dict(row) if row else None


def update_admin_password(admin_id: int, password_hash: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE admin_account SET password_hash = ? WHERE id = ?", (password_hash, admin_id)
        )


def list_admins() -> list[dict]:
    """Return all admin/manager accounts (without password hashes)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, username, created_at FROM admin_account ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def count_admins() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM admin_account").fetchone()["c"]


def delete_admin(admin_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM admin_account WHERE id = ?", (admin_id,))


def get_admin_by_id(admin_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, created_at FROM admin_account WHERE id = ?", (admin_id,)
        ).fetchone()
    return dict(row) if row else None


# --- wish queue helpers ------------------------------------------------------

def add_wish(track: dict, added_by: str) -> int:
    """Insert a wish at the end of the pending queue. Returns the new row id."""
    with get_conn() as conn:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS m FROM wish WHERE status IN ('pending','queued')"
        ).fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO wish (track_uri, track_id, track_name, artist, artist_ids, "
            "album, image_url, added_by, status, position, created_at, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                track["uri"],
                track["id"],
                track["name"],
                track["artist"],
                json.dumps(track.get("artist_ids", [])),
                track.get("album", ""),
                track.get("image_url", ""),
                added_by,
                max_pos + 1,
                time.time(),
                current_session_id(),
            ),
        )
        return int(cur.lastrowid)


def list_wishes(statuses: tuple[str, ...] = ("pending", "queued")) -> list[dict]:
    placeholders = ",".join("?" for _ in statuses)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM wish WHERE status IN ({placeholders}) ORDER BY position ASC",
            statuses,
        ).fetchall()
    return [dict(r) for r in rows]


def next_pending_wish() -> Optional[dict]:
    """Return the first pending wish (lowest position), or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM wish WHERE status = 'pending' ORDER BY position ASC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def set_wish_status(wish_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE wish SET status = ? WHERE id = ?", (status, wish_id))


def reorder_wishes(ordered_ids: list[int]) -> None:
    """Apply a new ordering for pending wishes based on the given id sequence."""
    with get_conn() as conn:
        for pos, wid in enumerate(ordered_ids, start=1):
            conn.execute(
                "UPDATE wish SET position = ? WHERE id = ? AND status = 'pending'",
                (pos, wid),
            )


def list_history(session_id: Optional[int] = "__current__", limit: int = 200) -> list[dict]:
    """Return completed wishes (played/rejected). Scoped to a session if given.

    Pass an explicit session_id (or None for "no session" -> empty). The default
    sentinel resolves to the current session.
    """
    if session_id == "__current__":
        session_id = current_session_id()
    if session_id is None:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM wish WHERE status IN ('played', 'rejected') AND session_id IS NULL "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM wish WHERE status IN ('played', 'rejected') AND session_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_wish(wish_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM wish WHERE id = ?", (wish_id,))


def clear_history(session_id: Optional[int] = "__current__") -> None:
    """Delete completed wishes for a session; keeps the active (pending/queued) queue."""
    if session_id == "__current__":
        session_id = current_session_id()
    with get_conn() as conn:
        if session_id is None:
            conn.execute(
                "DELETE FROM wish WHERE status IN ('played', 'rejected') AND session_id IS NULL"
            )
        else:
            conn.execute(
                "DELETE FROM wish WHERE status IN ('played', 'rejected') AND session_id = ?",
                (session_id,),
            )


# --- blacklist helpers -------------------------------------------------------

def add_blacklist(kind: str, spotify_id: str, name: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO blacklist (kind, spotify_id, name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (kind, spotify_id, name, time.time()),
        )


def remove_blacklist(entry_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM blacklist WHERE id = ?", (entry_id,))


def list_blacklist() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM blacklist ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def is_blacklisted(track_id: str, artist_ids: list[str]) -> bool:
    """Return True if the track id or any of its artist ids is blacklisted."""
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM blacklist WHERE kind = 'track' AND spotify_id = ?", (track_id,)
        ).fetchone():
            return True
        for aid in artist_ids:
            if conn.execute(
                "SELECT 1 FROM blacklist WHERE kind = 'artist' AND spotify_id = ?", (aid,)
            ).fetchone():
                return True
    return False
