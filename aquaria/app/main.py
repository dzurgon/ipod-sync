"""
Aquaria Music Manager — FastAPI backend.

Engine: scan → index → the Library projections + smart search + playlist resolution.
Browse/stream/download the library, per-user public/private playlists mirrored to
.m3u files, folder artwork, accounts (accounts.py) and iPod sync profiles + the host
sync API (ipods.py). Guests can read; writes need a logged-in user.
"""

from __future__ import annotations

import threading
import urllib.parse
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import accounts, config, folder_art, indexer, ipods, library, playlists, state
from .accounts import current_user, require_user

conn = state.conn
write_lock = state.write_lock

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


app = FastAPI(title="Aquaria Music Manager", version="0.4.0", lifespan=lifespan)
app.include_router(accounts.router)
app.include_router(ipods.router)


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
    for name in ("cover.jpg", "cover.png", "cover.jpeg", "cover.webp", "folder.jpg"):
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
def download_playlist(playlist_id: int, user: dict | None = Depends(current_user)):
    pl = playlists.get_playlist(conn, playlist_id, user and user["id"])
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
    is_public: bool = True


class PlaylistPatch(BaseModel):
    is_public: bool


class AddTracks(BaseModel):
    rel_paths: list[str] = []
    folder: str | None = None        # add a whole album


class MoveItem(BaseModel):
    delta: int


def _viewer_id(user: dict | None) -> int | None:
    return user["id"] if user else None


def _own_playlist(playlist_id: int, user: dict) -> dict:
    pl = playlists.get_playlist(conn, playlist_id, user["id"])
    if not pl:
        raise HTTPException(404, "playlist not found")
    if not pl["is_mine"]:
        raise HTTPException(403, "only the playlist's owner can change it")
    return pl


@app.get("/api/playlists")
def list_playlists(user: dict | None = Depends(current_user)):
    return {"playlists": playlists.list_playlists(conn, _viewer_id(user))}


@app.post("/api/playlists")
def create_playlist(body: NewPlaylist, user: dict = Depends(require_user)):
    with write_lock:
        try:
            pid = playlists.create_playlist(conn, config.PLAYLIST_DIR, user["id"], body.name,
                                            is_public=body.is_public)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except FileExistsError as e:
            raise HTTPException(409, str(e))
    return playlists.get_playlist(conn, pid, user["id"])


@app.get("/api/playlists/{playlist_id}")
def get_playlist(playlist_id: int, user: dict | None = Depends(current_user)):
    pl = playlists.get_playlist(conn, playlist_id, _viewer_id(user))
    if not pl:
        raise HTTPException(404, "playlist not found")
    return pl


@app.patch("/api/playlists/{playlist_id}")
def patch_playlist(playlist_id: int, body: PlaylistPatch, user: dict = Depends(require_user)):
    _own_playlist(playlist_id, user)
    with write_lock:
        playlists.set_public(conn, playlist_id, body.is_public)
    return playlists.get_playlist(conn, playlist_id, user["id"])


@app.post("/api/playlists/{playlist_id}/tracks")
def add_tracks(playlist_id: int, body: AddTracks, user: dict = Depends(require_user)):
    _own_playlist(playlist_id, user)
    rels = list(body.rel_paths)
    if body.folder:
        rels += [t["rel_path"] for t in library.album_tracks(conn, body.folder)]
    with write_lock:
        added = playlists.add_tracks(conn, playlist_id, rels)
    return {"added": added}


@app.delete("/api/playlists/{playlist_id}/items/{item_id}")
def remove_item(playlist_id: int, item_id: int, user: dict = Depends(require_user)):
    _own_playlist(playlist_id, user)
    with write_lock:
        playlists.remove_item(conn, playlist_id, item_id)
    return {"ok": True}


@app.post("/api/playlists/{playlist_id}/items/{item_id}/move")
def move_item(playlist_id: int, item_id: int, body: MoveItem, user: dict = Depends(require_user)):
    _own_playlist(playlist_id, user)
    with write_lock:
        playlists.move_item(conn, playlist_id, item_id, body.delta)
    return {"ok": True}


@app.delete("/api/playlists/{playlist_id}")
def delete_playlist(playlist_id: int, user: dict = Depends(require_user)):
    _own_playlist(playlist_id, user)
    with write_lock:
        playlists.delete_playlist(conn, playlist_id)
    return {"ok": True}


# ── Folder artwork (genre buckets + artist folders) ──────────────────────────
# Stored in the tree as <folder>/<folder name>.<jpg|png|webp>.

class FolderRef(BaseModel):
    rel_dir: str


@app.get("/api/folder-art")
def folder_art_list():
    return {
        "genres": [dict(r) for r in conn.execute(
            "SELECT rel_dir, genre, image FROM genre_folders ORDER BY rel_dir")],
        "artists": [dict(r) for r in conn.execute(
            "SELECT rel_dir, artist_key, artist, image FROM artist_folders ORDER BY artist_key")],
    }


@app.post("/api/folder-art/fetch", dependencies=[Depends(require_user)])
def folder_art_fetch(body: FolderRef):
    if (folder_art.kind_of(conn, body.rel_dir) or ("",))[0] == "album":
        raise HTTPException(400, "album covers come from beets (fetchart) — upload one instead")
    try:
        with write_lock:
            img = folder_art.fetch_one(conn, config.MUSIC_ROOT, body.rel_dir)
    except FileNotFoundError:
        raise HTTPException(404, "not a genre or artist folder")
    except OSError as e:
        raise HTTPException(502, f"lookup failed: {e}")
    if not img:
        raise HTTPException(404, "no exact match with artwork online — upload one instead")
    return {"image": img}


@app.post("/api/folder-art/fetch-missing", dependencies=[Depends(require_user)])
def folder_art_fetch_missing():
    return folder_art.start_fetch_missing(conn, config.MUSIC_ROOT, write_lock)


@app.get("/api/folder-art/fetch-missing")
def folder_art_fetch_status():
    return folder_art.JOB


@app.post("/api/folder-art/upload", dependencies=[Depends(require_user)])
async def folder_art_upload(rel_dir: str = Form(...), file: UploadFile = File(...)):
    """Genre/artist folders -> <Folder>/<Folder>.<ext>; album folders -> cover.jpg."""
    data = await file.read(folder_art.MAX_BYTES + 1)
    found = folder_art.kind_of(conn, rel_dir)
    if not found:
        raise HTTPException(404, "not a genre, artist or album folder")
    try:
        with write_lock:
            if found[0] == "album":
                img = folder_art.save_cover(config.MUSIC_ROOT, rel_dir, data)
            else:
                folder_art.save_image(config.MUSIC_ROOT, rel_dir, data)
                img = folder_art.record(conn, config.MUSIC_ROOT, rel_dir)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError:
        raise HTTPException(404, "not a genre, artist or album folder")
    return {"image": img}


# ── Scan ─────────────────────────────────────────────────────────────────────

@app.post("/api/scan", dependencies=[Depends(require_user)])
def rescan():
    return _scan_and_sync()


# ── Frontend (single-file UI) ────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


# Page URLs (/album/<folder>, /artist/<name>, /user/<name>, /playlist/<id>, …) are
# routed client-side; the server answers each with the same single-page app.
PAGES = ("library", "artists", "albums", "artist", "album", "playlists", "playlist",
         "activity", "ipod", "user", "users", "login", "signup")
for _page in PAGES:
    app.add_api_route(f"/{_page}", index, response_class=HTMLResponse, include_in_schema=False)
    app.add_api_route(f"/{_page}/{{rest:path}}", index, response_class=HTMLResponse,
                      include_in_schema=False)


@app.get("/healthz")
def healthz():
    return {"ok": True, "music_root": str(config.MUSIC_ROOT),
            "exists": config.MUSIC_ROOT.exists()}
