"""
Aquaria Music Manager — FastAPI backend (core-engine iteration).

This pass focuses on the metadata engine: scan → index → the four Library
projections + smart search + playlist resolution. Users / iPods / activity have
their tables and basic endpoints scaffolded so the frontend can be built out.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, indexer, library, playlists

app = FastAPI(title="Aquaria Music Manager", version="0.2.0")

# Use the configured DB path when its directory exists (the /data volume in
# Docker); otherwise fall back to a local file for dev runs outside a container.
if config.DB_PATH.parent.exists():
    _db_path = config.DB_PATH
else:
    _db_path = "aquaria.db"
conn = db.connect(_db_path)

STATIC = Path(__file__).parent / "static"


# ── Library ──────────────────────────────────────────────────────────────────

@app.get("/api/library")
def get_library(view: str = Query("artist", pattern="^(genre|artist|all_albums)$")):
    if view == "genre":
        data = library.view_genre(conn)
    elif view == "all_albums":
        data = library.view_all_albums(conn)
    else:
        data = library.view_artist(conn)
    return {"view": view, "items": data}


@app.get("/api/album")
def get_album(folder: str):
    tracks = library.album_tracks(conn, folder)
    if not tracks:
        raise HTTPException(404, "album not found")
    first = tracks[0]
    return {
        "folder": folder,
        "album": first["album"], "artist": first["artist"],
        "year": first["year"], "genre": first["genre"],
        "tracks": tracks,
    }


@app.get("/api/search")
def get_search(q: str, limit: int = 50):
    return library.search(conn, q, limit)


@app.get("/api/stats")
def get_stats():
    row = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT artist_key) a, "
        "COUNT(DISTINCT artist_key||album_key) al, COUNT(DISTINCT genre) g FROM tracks"
    ).fetchone()
    return {"tracks": row["n"], "artists": row["a"], "albums": row["al"], "genres": row["g"]}


# ── Media (audio + art) ──────────────────────────────────────────────────────

@app.get("/media/{rel_path:path}")
def media(rel_path: str):
    f = (config.MUSIC_ROOT / rel_path).resolve()
    if config.MUSIC_ROOT.resolve() not in f.parents or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


@app.get("/art/{folder:path}")
def art(folder: str):
    d = (config.MUSIC_ROOT / folder).resolve()
    for name in ("cover.jpg", "cover.png", "folder.jpg", "cover.jpeg"):
        p = d / name
        if p.is_file():
            return FileResponse(p)
    raise HTTPException(404, "no art")


# ── Scan ─────────────────────────────────────────────────────────────────────

@app.post("/api/scan")
def rescan():
    return indexer.scan(conn, config.MUSIC_ROOT)


# ── Users (scaffold) ─────────────────────────────────────────────────────────

@app.get("/api/users")
def list_users():
    rows = conn.execute("SELECT id, username, settings FROM users ORDER BY username").fetchall()
    users = []
    for u in rows:
        ipods = conn.execute(
            "SELECT id, name, signature, layout, last_seen FROM ipods WHERE user_id=?",
            (u["id"],),
        ).fetchall()
        users.append({
            "id": u["id"], "username": u["username"],
            "settings": json.loads(u["settings"] or "{}"),
            "ipods": [dict(i) for i in ipods],
        })
    return {"users": users, "default_backdrop": config.DEFAULT_BACKDROP}


@app.post("/api/users")
def create_user(username: str):
    try:
        cur = conn.execute(
            "INSERT INTO users(username, settings, created_at) VALUES (?,?,?)",
            (username, "{}", time.time()),
        )
        conn.commit()
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"id": cur.lastrowid, "username": username}


# ── Frontend (minimal aquamarine demo) ───────────────────────────────────────

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@app.get("/healthz")
def healthz():
    return {"ok": True, "music_root": str(config.MUSIC_ROOT),
            "exists": config.MUSIC_ROOT.exists()}
