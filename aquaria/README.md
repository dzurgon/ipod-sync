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
- `app/main.py` — FastAPI API + minimal aquamarine demo UI.
- `tests/` — 20 tests (parser edge cases + end-to-end engine).

## Run locally (dev)

```bash
pip install -r requirements.txt
python scripts/make_sample_library.py            # fake FLAC tree for testing
AQUARIA_MUSIC_ROOT=./sample_library AQUARIA_DB=./aquaria.db \
  uvicorn app.main:app --reload --port 8337
# open http://localhost:8337  → POST /api/scan once to index
```

## Run tests

```bash
pip install pytest && pytest tests/
```

## Run in Docker (server)

```bash
docker compose up -d --build   # binds /mnt/data/media/music/FLAC read-only
```

## Key endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/scan` | (re)index the library |
| `GET /api/library?view=genre\|artist\|all_albums` | the four Library views |
| `GET /api/album?folder=…` | tracks in an album |
| `GET /api/search?q=…` | grouped smart search (artists/albums/tracks) |
| `GET /api/users` | users + linked iPods |
| `GET /media/<rel_path>` · `GET /art/<folder>` | stream audio / cover art |
