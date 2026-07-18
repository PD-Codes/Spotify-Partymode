"""SQLite persistence layer.

Tables:
  kv         - simple key/value store (admin token, party state, active playlist).
  blacklist  - admin-blocked artists or tracks.
  wish       - the app-managed wish queue with "who added" metadata.
  guest_token_usage - per-guest skip-token consumption, bucketed per hour.
  guest_block       - per-guest artist/track blocks, scoped to a party session.
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

CREATE TABLE IF NOT EXISTS guest_token_usage (
    device_id   TEXT NOT NULL,          -- persistent per-browser id (survives logout)
    hour_bucket INTEGER NOT NULL,       -- floor(unix_time / 3600)
    skips_used  INTEGER NOT NULL DEFAULT 0,
    adds_used   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, hour_bucket)
);

CREATE TABLE IF NOT EXISTS guest_block (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,                 -- party scope; NULL = no active session
    device_id  TEXT NOT NULL,           -- owner (persistent per-browser id)
    guest_name TEXT NOT NULL,           -- display label at time of blocking
    kind       TEXT NOT NULL,           -- 'artist' or 'track'
    spotify_id TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (session_id, device_id, kind, spotify_id)
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
        # Migration: earlier previews keyed guest tokens/blocks by name. Recreate
        # those tables on the device_id-based schema (counters carry no history
        # worth preserving). Must run BEFORE executescript, which is a no-op for
        # already-existing tables.
        for table in ("guest_token_usage", "guest_block"):
            existing = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if existing and "device_id" not in existing:
                conn.execute(f"DROP TABLE {table}")
        conn.executescript(_SCHEMA)
        # Migration: add wish.session_id to databases created before sessions.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(wish)").fetchall()]
        if "session_id" not in cols:
            conn.execute("ALTER TABLE wish ADD COLUMN session_id INTEGER")
        # Migration: add guest_token_usage.adds_used (add-a-song tokens).
        tcols = [r["name"] for r in conn.execute("PRAGMA table_info(guest_token_usage)").fetchall()]
        if tcols and "adds_used" not in tcols:
            conn.execute("ALTER TABLE guest_token_usage ADD COLUMN adds_used INTEGER NOT NULL DEFAULT 0")


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


def is_track_in_queue(track_uri: str) -> bool:
    """True if this exact track is already waiting in the queue (pending or queued)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM wish WHERE track_uri = ? AND status IN ('pending', 'queued') LIMIT 1",
            (track_uri,),
        ).fetchone() is not None


def next_pending_wish() -> Optional[dict]:
    """Return the first pending wish (lowest position), or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM wish WHERE status = 'pending' ORDER BY position ASC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def _served_counts() -> dict[str, int]:
    """How many wishes each guest already had served (queued or played) this party.

    Used to keep round-robin fair over time: a guest who was already served does
    not jump back to the front just because their earlier wish left the pending
    set. Rejected wishes do not count as served.
    """
    sid = current_session_id()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT added_by, COUNT(*) AS c FROM wish WHERE status IN ('queued', 'played') "
            "AND (session_id = ? OR (session_id IS NULL AND ? IS NULL)) GROUP BY added_by",
            (sid, sid),
        ).fetchall()
    return {r["added_by"]: int(r["c"]) for r in rows}


def _fair_order(pending: list[dict], served: dict[str, int]) -> list[dict]:
    """Round-robin interleave pending wishes across guests (by added_by).

    One song from each guest per round, then the next round, and so on:
        round 1: A B C D   (everyone's 1st pending song)
        round 2: A B D     (C already ran out)
        round 3: A B       (D ran out) ...  then the heavy contributor's tail.
    A guest with many songs never blocks the others (everyone gets a turn each
    round), and the surplus of a heavy contributor naturally trails at the end.

    The round of a wish is purely its index among that guest's *pending* songs
    (oldest first) -- NOT counting how many that guest already had played. Songs
    already played only act as a TIE-BREAKER within a round: when several guests
    have a song in the same round, the one served least so far goes first. This
    keeps two properties at once:
      * a guest who added a lot early is NOT shoved to the very back later just
        because several of their songs already played (they stay in the normal
        round rotation);
      * nobody waits forever -- within each round the least-served guest leads,
        so a light guest is not perpetually edged out by an earlier submitter.
    Fully deterministic -> the order stays stable between polls (no jitter).
    """
    seen: dict[str, int] = {}
    annotated = []
    for w in sorted(pending, key=lambda x: (x["created_at"], x["id"])):
        idx = seen.get(w["added_by"], 0)
        seen[w["added_by"]] = idx + 1
        annotated.append((idx, served.get(w["added_by"], 0), w["created_at"], w["id"], w))
    # Sort by: round index, then fewest already served, then submission order.
    annotated.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
    return [w for *_, w in annotated]


def list_wishes_ordered(fair: bool) -> list[dict]:
    """Wishes for display: queued first, then pending (fair round-robin or by position)."""
    rows = list_wishes(("pending", "queued"))
    queued = sorted((w for w in rows if w["status"] == "queued"), key=lambda w: w["position"])
    pending = [w for w in rows if w["status"] == "pending"]
    if fair:
        pending = _fair_order(pending, _served_counts())
    else:
        pending.sort(key=lambda w: w["position"])
    return queued + pending


def next_pending_wish_ordered(fair: bool) -> Optional[dict]:
    """Return the wish to feed next: fair round-robin winner, or lowest position."""
    if not fair:
        return next_pending_wish()
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM wish WHERE status = 'pending'"
        ).fetchall()]
    if not rows:
        return None
    return _fair_order(rows, _served_counts())[0]


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
    """Return True if the track/artist is blocked (admin blacklist OR guest block).

    Guest blocks are only considered while they belong to the active party
    session, so they apply to the fixed playlist and everything already in the
    Spotify queue (the poller re-checks each track that starts playing) and stop
    mattering once the party ends.
    """
    session_id = current_session_id()
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM blacklist WHERE kind = 'track' AND spotify_id = ?", (track_id,)
        ).fetchone():
            return True
        if track_id and conn.execute(
            "SELECT 1 FROM guest_block WHERE kind = 'track' AND spotify_id = ? "
            "AND (session_id = ? OR (session_id IS NULL AND ? IS NULL))",
            (track_id, session_id, session_id),
        ).fetchone():
            return True
        for aid in artist_ids:
            if conn.execute(
                "SELECT 1 FROM blacklist WHERE kind = 'artist' AND spotify_id = ?", (aid,)
            ).fetchone():
                return True
            if conn.execute(
                "SELECT 1 FROM guest_block WHERE kind = 'artist' AND spotify_id = ? "
                "AND (session_id = ? OR (session_id IS NULL AND ? IS NULL))",
                (aid, session_id, session_id),
            ).fetchone():
                return True
    return False


# --- guest skip tokens -------------------------------------------------------
#
# Keyed by a persistent per-browser device_id (a long-lived cookie that
# survives logout), so a guest cannot refill their budget by simply logging
# out and re-joining under a new name.

# Token kinds map to their column in guest_token_usage.
_TOKEN_COLUMNS = {"skip": "skips_used", "add": "adds_used"}


def get_guest_tokens_used(device_id: str, hour_bucket: int, kind: str) -> int:
    """Return how many tokens of `kind` this device already spent in the hour bucket."""
    col = _TOKEN_COLUMNS[kind]
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {col} AS used FROM guest_token_usage WHERE device_id = ? AND hour_bucket = ?",
            (device_id, hour_bucket),
        ).fetchone()
    return int(row["used"]) if row else 0


def incr_guest_token(device_id: str, hour_bucket: int, kind: str) -> int:
    """Record one spent token of `kind` for this device this hour; return new total.

    Old buckets are pruned opportunistically so the table stays small.
    """
    col = _TOKEN_COLUMNS[kind]
    with get_conn() as conn:
        conn.execute("DELETE FROM guest_token_usage WHERE hour_bucket < ?", (hour_bucket,))
        conn.execute(
            f"INSERT INTO guest_token_usage (device_id, hour_bucket, {col}) VALUES (?, ?, 1) "
            f"ON CONFLICT(device_id, hour_bucket) DO UPDATE SET {col} = {col} + 1",
            (device_id, hour_bucket),
        )
        row = conn.execute(
            f"SELECT {col} AS used FROM guest_token_usage WHERE device_id = ? AND hour_bucket = ?",
            (device_id, hour_bucket),
        ).fetchone()
    return int(row["used"]) if row else 1


# --- guest blocks ------------------------------------------------------------

def count_guest_blocks(session_id: Optional[int], device_id: str, kind: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM guest_block WHERE device_id = ? AND kind = ? "
            "AND (session_id = ? OR (session_id IS NULL AND ? IS NULL))",
            (device_id, kind, session_id, session_id),
        ).fetchone()
    return int(row["c"])


def guest_block_exists(session_id: Optional[int], device_id: str, kind: str, spotify_id: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM guest_block WHERE device_id = ? AND kind = ? AND spotify_id = ? "
            "AND (session_id = ? OR (session_id IS NULL AND ? IS NULL))",
            (device_id, kind, spotify_id, session_id, session_id),
        ).fetchone() is not None


def add_guest_block(
    session_id: Optional[int], device_id: str, guest_name: str, kind: str, spotify_id: str, name: str
) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO guest_block (session_id, device_id, guest_name, kind, "
            "spotify_id, name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, device_id, guest_name, kind, spotify_id, name, time.time()),
        )


def remove_guest_block(block_id: int, device_id: str) -> None:
    """Delete a block only if it belongs to the requesting device."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM guest_block WHERE id = ? AND device_id = ?", (block_id, device_id)
        )


def list_guest_blocks(session_id: Optional[int], device_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM guest_block WHERE device_id = ? "
            "AND (session_id = ? OR (session_id IS NULL AND ? IS NULL)) ORDER BY created_at DESC",
            (device_id, session_id, session_id),
        ).fetchall()
    return [dict(r) for r in rows]


def active_guest_block_sets(session_id: Optional[int]) -> tuple[set[str], set[str]]:
    """Return (blocked track ids, blocked artist ids) from all guests this session."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT kind, spotify_id FROM guest_block "
            "WHERE (session_id = ? OR (session_id IS NULL AND ? IS NULL))",
            (session_id, session_id),
        ).fetchall()
    tracks = {r["spotify_id"] for r in rows if r["kind"] == "track"}
    artists = {r["spotify_id"] for r in rows if r["kind"] == "artist"}
    return tracks, artists
