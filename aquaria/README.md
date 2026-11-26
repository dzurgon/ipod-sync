# Aquaria Music Manager

v2 of `ipod-sync` — a Docker web service for a shared FLAC library + per-user
iPod sync, served over LAN/Tailscale. See **[ARCHITECTURE.md](ARCHITECTURE.md)**
for the full design; this is the quick start.

## What's built (core-engine pass)

- `app/parser.py` — path → canonical metadata (the schema-unification logic).
- `app/db.py` — SQLite index schema (tracks, users, iPods, playlists, activity).
- `app/indexer.py` — scans `0_GENRE/…` into the index; never mutates disk.
- `app/library.py` — the four Library views + FTS smart search.
- `app/playlists.py` — resolves messy/legacy playlist lines to tracks; iPod path projection.
- `app/main.py` — FastAPI API + single-file aquamarine UI.
- Browse: **Folders** view mirrors `FLAC/` on disk (`0_GENRE` → artist → album),
  plus Artist and All Albums views; album pages with in-browser playback.
- Every page has its own URL (back/forward, bookmarks, new tabs work):
  `/library/0_FOLK/Big Thief`, `/album/0_FOLK/Big Thief/Masterpiece (2016)`,
  `/artist/Big Thief`, `/artists`, `/albums`, `/playlists`, `/playlist/<id>`.
- Download: single tracks, whole albums or playlists as streamed `.zip` files.
- Playlists: create / add (from album pages or the search bubble) / reorder /
  remove / delete. Mirrored to `Playlists/*.m3u` so `ipod-sync.sh` still pushes
  them; `.m3u` files edited elsewhere are re-imported on the next scan.
- Folder artwork: genre and artist folders carry a picture named after the folder,
  inside it — `0_POP/0_POP.jpg`, `0_POP/Charli XCX/Charli XCX.jpg` (`.png`/`.webp`
  and legacy `artist.*` also read; albums keep beets' `cover.jpg`). Artists come
  from Deezer's artist search (exact-name matches only); genres from Deezer's genre
  art by name, or per `AQUARIA_GENRE_ART` (`config.py`: e.g. `KWUR` → its Wikipedia
  logo). Fetch one, fetch all missing, or upload by hand from the folder's page.
- Album covers: "Upload cover" on any album page writes `cover.jpg` into the album
  folder (converted to a baseline JPEG ≤1000px so Rockbox can show it).
- `tests/` — 44 tests (parser edge cases, engine, playlist + download API).

## Accounts, playlists and iPods

- **Accounts**: username + password (scrypt), 30-day session cookie. Guests can
  browse, play and download the library and public playlists; anything that writes
  needs a login. A user that existed before accounts (e.g. `jimin`, owner of the
  imported `Playlists/*.m3u`) is claimed by signing up with that name.
- **Profiles**: `/user/<name>` shows a user's public playlists and iPods; your own
  profile also shows private playlists and "Connect Last.fm".
- **Playlists** are public or private, unique per user, mirrored to
  `Playlists/<user>/<name>.m3u` (legacy root-level files stay with the default user).
- **iPods** (My iPod tab): plug one into the server and it appears as "New iPod
  detected" — link it to your account, then choose its saved sync profile:
  whole library or selected genres (+ albums in synced playlists), `0_GENRE/Artist/Album`
  or flat `Artist/Album` layout, which playlists (yours and others' public ones), and
  scrobbling to your own Last.fm. Every connect syncs that profile and reports back to
  the iPod card and the Activity tab.

### Host side (udev → `ipod-sync.sh`)

`server/scripts/aquaria_sync.py` identifies the iPod (USB serial, else a
`.aquaria/id` marker on it), asks Aquaria for its plan, stages it as a symlink tree and
`rsync --copy-links` mirrors that onto the iPod; playlists go to `/Playlists` with
absolute `/FLAC/...` paths. Unlinked iPods are only registered (no sync). If Aquaria is
unreachable, the legacy full-library mirror runs instead.

```bash
# ~/docker/aquaria/.env (mode 600, never committed)
AQUARIA_HOST_TOKEN=<random, e.g. openssl rand -hex 32>
LASTFM_API_KEY=...        # same Last.fm API account as /etc/ipod-sync/config.env
LASTFM_API_SECRET=...
NUCOS_URL=http://organizer:8000   # optional: nucOS's Listening habit (see below)
NUCOS_TOKEN=nucos_...             # nucOS → Marketplace → Listening → Connect (shown once)

# then install the host scripts + udev rule and share the token with them
sudo bash ~/code/ipod-sync/server/scripts/install-host.sh
```

**nucOS (Listening habit).** Every play Aquaria sees — iPod plays from Rockbox's
`playback.log` (and `.scrobbler.log`), web-player plays, and a Last.fm backfill — is stored
with a stable id (`ipod-<unix start>-<track hash>`, `web-…`, `lastfm-…`) and, for
`NUCOS_USER` (default the `AQUARIA_DEFAULT_USER`), queued in an outbox in the same
transaction. A background sender posts `listen` items to `/api/integrations/v1/activity`
(≤ 500 per request, 5 s timeouts; retries 30 s → 1 h; stops on 401/410). The profile page
shows the status, **Test connection** (`GET /me`), **Send now**, and **Backfill from
Last.fm** (safe to repeat: a scrobble the iPod or web player already recorded — same artist +
title within 2 minutes — isn't counted twice). Stock-firmware Play Counts aren't supported:
the sync runs on Rockbox.

## Run locally (dev)

```bash
pip install -r requirements.txt
python scripts/make_sample_library.py            # fake FLAC tree for testing
AQUARIA_MUSIC_ROOT=./sample_library AQUARIA_DB=./aquaria.db \
  uvicorn app.main:app --reload --port 8337
# open http://localhost:8337  (indexes on startup; POST /api/scan to rescan)
```

## Run tests

```bash
pip install pytest && pytest tests/
```

## Run in Docker (homelab)

Follows the `~/docker/<service>/` convention; the image builds from the repo clone.

```bash
git -C ~/code/ipod-sync pull
mkdir -p ~/docker/aquaria && cp ~/code/ipod-sync/aquaria/docker-compose.yml ~/docker/aquaria/
cd ~/docker/aquaria && docker compose up -d --build
# http://100.94.158.95:8337  — indexes the library automatically on start
```

FLAC is bind-mounted read-write, but the only thing Aquaria writes there is artwork
(`<Folder>/<Folder>.jpg`, album `cover.jpg`); audio files are never touched.
`Playlists/` is read-write. Deleted playlists are
renamed to `*.m3u.deleted`, never unlinked.

## Key endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/scan` | (re)index the library |
| `GET /api/library?view=genre\|artist\|all_albums` | the four Library views |
| `GET /api/album?folder=…` | tracks in an album |
| `GET /api/search?q=…` | grouped smart search (artists/albums/tracks) |
| `GET /api/users` | users + linked iPods |
| `GET /media/<rel_path>` · `GET /art/<folder>` | stream audio / cover art |
| `GET /download/track/<rel_path>` | download one file |
| `GET /download/album?folder=…` · `GET /download/playlist/<id>` | download as `.zip` |
| `GET /api/folder-art` · `POST …/fetch` · `POST …/upload` · `POST …/fetch-missing` | genre + artist artwork |
| `GET/POST /api/playlists` · `/api/playlists/<id>[/tracks\|/items/…]` | playlist CRUD |
