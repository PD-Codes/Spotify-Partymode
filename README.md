# Spotify-Partymode

A self-hosted party mode for Spotify. A fixed playlist keeps playing, and guests
add their own songs from their phones — those wishes play **next**, then the
playlist automatically resumes.

- **Guests** log in with just a name, search Spotify and add songs to the queue.
- **Admin** logs in with a local account (username/password) and controls
  everything: party on/off, the fixed playlist, reordering and rejecting wishes,
  blacklisting artists/tracks, and skipping. Spotify is linked to the admin
  account afterwards (and the admin can log in even before linking Spotify).
- **No .env needed** — all configuration lives in the database and is entered
  through an in-app setup wizard on first launch.
- Mobile-first responsive UI (built for phones).
- Runs via `pip` (Python + FastAPI) or Docker.

> **Author:** Domekologe · **License:** MIT

## How it works

Spotify's ad-hoc queue cannot be reordered or cleared via the API. So the app
keeps the wish list in its own database and pushes only **one wish at a time**
to Spotify — right after the previous one finishes. A background poller watches
playback and feeds the next wish at the right moment. This keeps full reorder /
reject control in the app, while Spotify handles playback and the "resume the
playlist" behaviour natively.

A small linking table maps each queued track back to the guest who added it, so
the UI can show **who added** each song.

## Requirements

- A **Spotify Premium** account for the admin (playback control needs Premium).
- A Spotify app (Client ID/Secret) from the
  [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
- An active Spotify device (the admin should have Spotify open and playing once,
  so a device is available to control).

## Setup (pip)

```bash
pip install .
python -m spotify_partymode
```

Open http://127.0.0.1:8000 and use the **Admin login** link → the first launch
shows the **setup wizard**: create the admin account and (optionally) enter your
Spotify credentials.

### Configure the Spotify app

In the Spotify Developer Dashboard, add this redirect URI to your app (must match
the Redirect URI you enter in the setup wizard / settings):

```
http://127.0.0.1:8000/auth/callback
```

For phones on the same network, use the host machine's LAN IP instead of
`127.0.0.1`, e.g. `http://192.168.1.50:8000/auth/callback`.

## Setup (Docker)

```bash
docker compose up --build
```

The SQLite database (and therefore all configuration) is stored in the
`partymode-data` volume.

## Usage

1. Open the site → **Admin login** → complete the setup wizard (admin account +
   optional Spotify credentials).
2. Log in, then click **Connect** to link Spotify (OAuth).
3. Set the **fixed playlist**, click **Start playlist**, turn **Party mode on**.
4. Guests open the site, enter a name, search and add songs.
5. Wishes play next in order; the admin can reorder, reject or blacklist.

## Configuration

All app settings are stored in the database and managed from the **setup wizard**
and the **admin settings panel** (Spotify credentials, redirect URI, default
playlist, poll interval). The session secret is generated automatically on first
run.

Only optional *bootstrap* values can be set via environment / `.env` (mainly for
Docker):

| Variable | Purpose | Default |
| --- | --- | --- |
| `DATABASE_PATH` | SQLite file location | `partymode.db` |
| `HOST` / `PORT` | Web server bind address | `0.0.0.0` / `8000` |

## Project layout

```
spotify_partymode/
  config.py          # minimal bootstrap settings (db path, host, port)
  settings_store.py  # all app settings, stored in the database
  security.py        # password hashing (PBKDF2, stdlib only)
  spotify_client.py  # Spotify OAuth + API wrapper
  db.py              # SQLite (admin account, wishes, blacklist, who-added)
  queue_manager.py   # wish orchestration + background poller
  main.py            # FastAPI app + lifespan
  routers/           # auth/setup, guest, admin endpoints
static/              # mobile-first frontend (setup, guest, admin)
```

## Notes & limitations

- Once a wish is pushed to Spotify's queue it can only be removed by **skipping**
  (Spotify API limitation). Reordering/rejecting therefore applies to **pending**
  wishes only — which is why the app feeds them one at a time.
- This is an early scaffold intended for testing and iteration.
