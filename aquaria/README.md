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
- Download: single tracks, whole albums or playlists as streamed `.zip` files.
- Playlists: create / add (from album pages or the search bubble) / reorder /
  remove / delete. Mirrored to `Playlists/*.m3u` so `ipod-sync.sh` still pushes
  them; `.m3u` files edited elsewhere are re-imported on the next scan.
- `tests/` — 24 tests (parser edge cases, engine, playlist + download API).

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

FLAC is bind-mounted read-only; `Playlists/` read-write. Deleted playlists are
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
| `GET/POST /api/playlists` · `/api/playlists/<id>[/tracks\|/items/…]` | playlist CRUD |
