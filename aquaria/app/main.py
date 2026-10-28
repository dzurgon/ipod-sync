"""
Aquaria Music Manager — FastAPI backend.

Engine: scan → index → the Library projections + smart search + playlist resolution.
This iteration adds: browse into artists/albums, streaming + download (single
track, whole album or playlist as a .zip), and the playlist write-path mirrored
to PLAYLIST_DIR/*.m3u. Users / iPods / activity are still scaffolded.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import artist_images, config, db, indexer, library, playlists

# Use the configured DB path when its directory exists (the /data volume in
# Docker); otherwise fall back to a local file for dev runs outside a container.
if config.DB_PATH.parent.exists():
    _db_path = config.DB_PATH
else:
    _db_path = "aquaria.db"
conn = db.connect(_db_path)

# One shared connection; serialise writers (scan, playlist edits) against each other.
write_lock = threading.Lock()

STATIC = Path(__file__).parent / "static"


def _scan_and_sync() -> dict:
    with write_lock:
        summary = indexer.scan(conn, config.MUSIC_ROOT)
        owner = playlists.ensure_user(conn, config.DEFAULT_USER)
        summary["playlists"] = playlists.sync_dir(conn, config.PLAYLIST_DIR, owner)
    return summary


@asynccontextmanager
async def lifespan(_app):
    # Index in the background so the server is reachable immediately on boot.
    threading.Thread(target=_scan_and_sync, daemon=True).start()
    yield


app = FastAPI(title="Aquaria Music Manager", version="0.3.0", lifespan=lifespan)


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

def _music_file(rel_path: str) -> Path:
    f = (config.MUSIC_ROOT / rel_path).resolve()
    if config.MUSIC_ROOT.resolve() not in f.parents or not f.is_file():
        raise HTTPException(404)
    return f


def _attachment(filename: str) -> dict:
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "'")
    return {"Content-Disposition": f'attachment; filename="{ascii_name}"; '
                                   f"filename*=UTF-8''{urllib.parse.quote(filename)}"}


@app.get("/media/{rel_path:path}")
def media(rel_path: str):
    return FileResponse(_music_file(rel_path))


@app.get("/art/{folder:path}")
def art(folder: str):
    d = (config.MUSIC_ROOT / folder).resolve()
    for name in ("cover.jpg", "cover.png", "folder.jpg", "cover.jpeg"):
        p = d / name
        if p.is_file():
            return FileResponse(p)
    raise HTTPException(404, "no art")


# ── Downloads ────────────────────────────────────────────────────────────────

class _Sink:
    """Write-only buffer that zipfile streams into; drained after every chunk."""
    def __init__(self):
        self.buf = bytearray()

    def write(self, b):
        self.buf += b
        return len(b)

    def flush(self):
        pass

    def drain(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def _zip_stream(entries: list[tuple[Path, str]], extra: dict[str, str] | None = None):
    """Stream a STORED (uncompressed — FLAC doesn't compress) zip without temp files."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for src, arcname in entries:
            zi = zipfile.ZipInfo.from_file(src, arcname)
            with open(src, "rb") as f, zf.open(zi, "w", force_zip64=True) as w:
                while chunk := f.read(1 << 20):
                    w.write(chunk)
                    yield sink.drain()
        for arcname, text in (extra or {}).items():
            zf.writestr(arcname, text)
    yield sink.drain()


def _zip_response(name: str, entries, extra=None) -> StreamingResponse:
    return StreamingResponse(_zip_stream(entries, extra), media_type="application/zip",
                             headers=_attachment(f"{name}.zip"))


@app.get("/download/track/{rel_path:path}")
def download_track(rel_path: str):
    f = _music_file(rel_path)
    return FileResponse(f, headers=_attachment(f.name))


@app.get("/download/album")
def download_album(folder: str):
    tracks = library.album_tracks(conn, folder)
    if not tracks:
        raise HTTPException(404, "album not found")
    album_dir = _music_file(tracks[0]["rel_path"]).parent
    # Every file in the folder (tracks + cover art etc.), flat under one top dir.
    t = tracks[0]
    top = f'{t["artist"] or "Unknown Artist"} - {t["album"]}' + (f' ({t["year"]})' if t["year"] else "")
    entries = [(p, f"{top}/{p.name}") for p in sorted(album_dir.iterdir()) if p.is_file()]
    return _zip_response(top, entries)


@app.get("/download/playlist/{playlist_id}")
def download_playlist(playlist_id: int):
    pl = playlists.get_playlist(conn, playlist_id)
    if not pl:
        raise HTTPException(404, "playlist not found")
    entries, m3u = [], ["#EXTM3U"]
    found = [i for i in pl["items"] if i["rel_path"]]
    width = max(2, len(str(len(found))))
    for n, it in enumerate(found, 1):
        try:
            src = _music_file(it["rel_path"])
        except HTTPException:
            continue
        name = f'{n:0{width}d} - {it["artist"]} - {it["title"]}{src.suffix}'.replace("/", "_")
        entries.append((src, f'{pl["name"]}/{name}'))
        m3u.append(name)
    return _zip_response(pl["name"], entries,
                         {f'{pl["name"]}/{pl["name"]}.m3u': "\n".join(m3u) + "\n"})


# ── Playlists ────────────────────────────────────────────────────────────────

class NewPlaylist(BaseModel):
    name: str


class AddTracks(BaseModel):
    rel_paths: list[str] = []
    folder: str | None = None        # add a whole album


class MoveItem(BaseModel):
    delta: int


def _owner_id() -> int:
    return playlists.ensure_user(conn, config.DEFAULT_USER)


@app.get("/api/playlists")
def list_playlists():
    return {"playlists": playlists.list_playlists(conn)}


@app.post("/api/playlists")
def create_playlist(body: NewPlaylist):
    with write_lock:
        try:
            pid = playlists.create_playlist(conn, config.PLAYLIST_DIR, _owner_id(), body.name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except FileExistsError as e:
            raise HTTPException(409, str(e))
    return playlists.get_playlist(conn, pid)


@app.get("/api/playlists/{playlist_id}")
def get_playlist(playlist_id: int):
    pl = playlists.get_playlist(conn, playlist_id)
    if not pl:
        raise HTTPException(404, "playlist not found")
    return pl


@app.post("/api/playlists/{playlist_id}/tracks")
def add_tracks(playlist_id: int, body: AddTracks):
    get_playlist(playlist_id)
    rels = list(body.rel_paths)
    if body.folder:
        rels += [t["rel_path"] for t in library.album_tracks(conn, body.folder)]
    with write_lock:
        added = playlists.add_tracks(conn, playlist_id, rels)
    return {"added": added}


@app.delete("/api/playlists/{playlist_id}/items/{item_id}")
def remove_item(playlist_id: int, item_id: int):
    with write_lock:
        playlists.remove_item(conn, playlist_id, item_id)
    return {"ok": True}


@app.post("/api/playlists/{playlist_id}/items/{item_id}/move")
def move_item(playlist_id: int, item_id: int, body: MoveItem):
    with write_lock:
        playlists.move_item(conn, playlist_id, item_id, body.delta)
    return {"ok": True}


@app.delete("/api/playlists/{playlist_id}")
def delete_playlist(playlist_id: int):
    get_playlist(playlist_id)
    with write_lock:
        playlists.delete_playlist(conn, playlist_id)
    return {"ok": True}


# ── Artist photos ────────────────────────────────────────────────────────────
# Stored in the tree as <artist folder>/<artist folder name>.<jpg|png|webp>.

class ArtistDir(BaseModel):
    rel_dir: str


@app.get("/api/artist-folders")
def artist_folders():
    rows = conn.execute("SELECT rel_dir, artist_key, artist, image FROM artist_folders "
                        "ORDER BY artist_key").fetchall()
    return {"folders": [dict(r) for r in rows]}


@app.post("/api/artist-folders/fetch")
def fetch_artist_image(body: ArtistDir):
    try:
        with write_lock:
            img = artist_images.fetch_one(conn, config.MUSIC_ROOT, body.rel_dir)
    except FileNotFoundError:
        raise HTTPException(404, "not an artist folder")
    except OSError as e:
        raise HTTPException(502, f"lookup failed: {e}")
    if not img:
        raise HTTPException(404, "no exact match with a photo on Deezer — upload one instead")
    return {"image": img}


@app.post("/api/artist-folders/fetch-missing")
def fetch_missing_artist_images():
    return artist_images.start_fetch_missing(conn, config.MUSIC_ROOT, write_lock)


@app.get("/api/artist-folders/fetch-missing")
def fetch_missing_status():
    return artist_images.JOB


@app.post("/api/artist-folders/upload")
async def upload_artist_image(rel_dir: str = Form(...), file: UploadFile = File(...)):
    data = await file.read(artist_images.MAX_BYTES + 1)
    if not conn.execute("SELECT 1 FROM artist_folders WHERE rel_dir=?", (rel_dir,)).fetchone():
        raise HTTPException(404, "not an artist folder")
    try:
        with write_lock:
            artist_images.save_image(config.MUSIC_ROOT, rel_dir, data)
            img = artist_images.record(conn, config.MUSIC_ROOT, rel_dir)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError:
        raise HTTPException(404, "not an artist folder")
    return {"image": img}


# ── Scan ─────────────────────────────────────────────────────────────────────

@app.post("/api/scan")
def rescan():
    return _scan_and_sync()


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


# ── Frontend (single-file UI) ────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@app.get("/healthz")
def healthz():
    return {"ok": True, "music_root": str(config.MUSIC_ROOT),
            "exists": config.MUSIC_ROOT.exists()}
