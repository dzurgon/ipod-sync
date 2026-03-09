#!/usr/bin/env python3
"""
Playlist Manager — FastAPI + HTMX
Runs on the NUC; accessible from any browser on your LAN or via Tailscale.

Dependencies:
  pip install fastapi uvicorn jinja2 python-multipart watchfiles

Start:
  uvicorn app:app --host 0.0.0.0 --port 8337 --reload

Or via systemd: see playlist-manager.service
"""

import os
import re
import json
import glob
import shutil
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str = "/etc/ipod-sync/config.env") -> dict:
    cfg = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return cfg

cfg = load_config(os.environ.get("CONFIG_FILE", "/etc/ipod-sync/config.env"))

MUSIC_DIR    = Path(cfg.get("MUSIC_DIR",    "/mnt/data/media/music/FLAC"))
PLAYLIST_DIR = Path(cfg.get("PLAYLIST_DIR", "/mnt/data/media/music/Playlists"))
LOG_DIR      = Path(cfg.get("LOG_DIR",      "/var/log/ipod-sync"))

PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)

# ── Event feed (in-memory ring buffer) ────────────────────────────────────────

MAX_FEED_EVENTS = 100
_feed: list[dict] = []

def push_event(kind: str, message: str) -> None:
    _feed.insert(0, {
        "kind":    kind,           # "info" | "success" | "warning" | "error"
        "message": message,
        "time":    datetime.now().strftime("%H:%M:%S"),
    })
    del _feed[MAX_FEED_EVENTS:]


# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="iPod Playlist Manager")

_here = Path(__file__).parent
app.mount("/static", StaticFiles(directory=_here / "static"), name="static")
templates = Jinja2Templates(directory=_here / "templates")
templates.env.filters["urlencode"] = urllib.parse.quote


# ── Library helpers ───────────────────────────────────────────────────────────

AUDIO_EXTS = {".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac"}


def _album_dict(album_dir: Path, rel_path: str, artist: str) -> dict:
    tracks = sorted(t for t in album_dir.iterdir() if t.suffix.lower() in AUDIO_EXTS)
    cover = next(
        (f.name for f in album_dir.iterdir()
         if f.name.lower() in {"cover.jpg", "cover.png", "folder.jpg"}),
        None,
    )
    return {
        "name":        album_dir.name,
        "artist":      artist,
        "rel_path":    rel_path,      # relative to MUSIC_DIR, e.g. "Artist/Album (Year)"
        "track_count": len(tracks),
        "tracks":      [t.name for t in tracks],
        "cover":       cover,
    }


def scan_library() -> list[dict]:
    """Return album dicts, supporting both flat and Artist/Album (Year) layouts."""
    albums = []
    if not MUSIC_DIR.exists():
        return albums
    for entry in sorted(MUSIC_DIR.iterdir()):
        if not entry.is_dir():
            continue
        children = list(entry.iterdir())
        has_audio   = any(f.suffix.lower() in AUDIO_EXTS for f in children if f.is_file())
        has_subdirs = any(f.is_dir() for f in children)

        if has_audio:
            # Flat album at root level (e.g. old-style or un-beeted folder)
            albums.append(_album_dict(entry, entry.name, artist=""))
        elif has_subdirs:
            # Artist folder — iterate album subdirectories
            for album_dir in sorted(entry.iterdir()):
                if album_dir.is_dir():
                    rel = f"{entry.name}/{album_dir.name}"
                    albums.append(_album_dict(album_dir, rel, artist=entry.name))
    return albums


def scan_playlists() -> list[dict]:
    """Return list of playlist dicts."""
    playlists = []
    for pls in sorted(PLAYLIST_DIR.glob("*.m3u")):
        tracks = []
        try:
            with open(pls, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        tracks.append(line)
        except Exception:
            pass
        playlists.append({
            "name":        pls.stem,
            "filename":    pls.name,
            "track_count": len(tracks),
            "tracks":      tracks,
            "modified":    datetime.fromtimestamp(pls.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return playlists


def read_recent_logs(n: int = 5) -> list[dict]:
    """Return metadata for the last n ipod-sync log files."""
    logs = []
    if not LOG_DIR.exists():
        return logs
    log_files = sorted(LOG_DIR.glob("ipod-sync-*.log"), reverse=True)[:n]
    for lf in log_files:
        size = lf.stat().st_size
        mtime = datetime.fromtimestamp(lf.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        logs.append({"name": lf.name, "size": f"{size // 1024} KB", "modified": mtime})
    return logs


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request":   request,
        "albums":    scan_library(),
        "playlists": scan_playlists(),
        "feed":      _feed,
        "logs":      read_recent_logs(),
    })


# ── Album detail ─────────────────────────────────────────────────────────────

@app.get("/album/{rel_path:path}", response_class=HTMLResponse)
async def view_album(request: Request, rel_path: str):
    album_dir = MUSIC_DIR / rel_path
    if not album_dir.is_dir():
        raise HTTPException(404, "Album not found.")
    data = _album_dict(album_dir, rel_path, artist=Path(rel_path).parent.name)
    return templates.TemplateResponse("album.html", {
        "request":   request,
        "album":     data["name"],
        "rel_path":  rel_path,
        "artist":    data["artist"],
        "tracks":    data["tracks"],
        "cover":     data["cover"],
        "playlists": scan_playlists(),
    })


# ── Album art ────────────────────────────────────────────────────────────────

@app.get("/album-art/{image_path:path}")
async def album_art(image_path: str):
    """Serve cover art. image_path is relative to MUSIC_DIR, e.g. Artist/Album/cover.jpg"""
    if ".." in image_path:
        raise HTTPException(400, "Invalid path.")
    full_path = MUSIC_DIR / image_path
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(404, "Cover art not found.")
    media_type = "image/jpeg" if full_path.suffix.lower() == ".jpg" else "image/png"
    return FileResponse(full_path, media_type=media_type)


# ── Playlist CRUD ─────────────────────────────────────────────────────────────

@app.get("/playlists", response_class=HTMLResponse)
async def playlists_partial(request: Request):
    """HTMX partial — returns just the playlist list."""
    return templates.TemplateResponse("_playlists.html", {
        "request":   request,
        "playlists": scan_playlists(),
    })


@app.post("/playlists/new")
async def create_playlist(name: str = Form(...)):
    name = re.sub(r"[^\w\s\-]", "", name).strip()
    if not name:
        raise HTTPException(400, "Playlist name is empty or invalid.")
    path = PLAYLIST_DIR / f"{name}.m3u"
    if path.exists():
        raise HTTPException(409, f"Playlist '{name}' already exists.")
    path.write_text("#EXTM3U\n", encoding="utf-8")
    push_event("success", f"Created playlist '{name}'.")
    return RedirectResponse("/", status_code=303)


@app.get("/playlists/{name}", response_class=HTMLResponse)
async def view_playlist(request: Request, name: str):
    path = PLAYLIST_DIR / f"{name}.m3u"
    if not path.exists():
        raise HTTPException(404, "Playlist not found.")
    tracks = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                tracks.append(line)
    return templates.TemplateResponse("playlist.html", {
        "request": request,
        "name":    name,
        "tracks":  tracks,
        "albums":  scan_library(),
    })


@app.post("/playlists/{name}/add")
async def add_track(name: str, track_path: str = Form(...), next: str = Form(default="")):
    """Add a relative track path to a playlist. Redirects to `next` if provided."""
    path = PLAYLIST_DIR / f"{name}.m3u"
    if not path.exists():
        raise HTTPException(404, "Playlist not found.")
    rel = track_path.replace("\\", "/").strip("/")
    # Prevent duplicate entries
    existing = path.read_text(encoding="utf-8", errors="replace")
    if rel not in [l.strip() for l in existing.splitlines()]:
        with open(path, "a", encoding="utf-8") as f:
            f.write(rel + "\n")
        push_event("success", f"Added to '{name}': {Path(rel).name}")
    else:
        push_event("info", f"Already in '{name}': {Path(rel).name}")
    redirect_to = next if (next and next.startswith("/")) else f"/playlists/{name}"
    return RedirectResponse(redirect_to, status_code=303)


@app.post("/playlists/{name}/remove")
async def remove_track(name: str, track_path: str = Form(...)):
    path = PLAYLIST_DIR / f"{name}.m3u"
    if not path.exists():
        raise HTTPException(404, "Playlist not found.")
    rel = track_path.replace("\\", "/").strip("/")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    new_lines = [l for l in lines if l.strip() != rel]
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    push_event("info", f"Removed track from '{name}': {Path(rel).name}")
    return RedirectResponse(f"/playlists/{name}", status_code=303)


@app.post("/playlists/{name}/delete")
async def delete_playlist(name: str):
    path = PLAYLIST_DIR / f"{name}.m3u"
    if not path.exists():
        raise HTTPException(404, "Playlist not found.")
    path.unlink()
    push_event("warning", f"Deleted playlist '{name}'.")
    return RedirectResponse("/", status_code=303)


# ── Feed partial (HTMX polling) ───────────────────────────────────────────────

@app.get("/feed", response_class=HTMLResponse)
async def feed_partial(request: Request):
    return templates.TemplateResponse("_feed.html", {
        "request": request,
        "feed":    _feed,
    })


# ── Log viewer ────────────────────────────────────────────────────────────────

@app.get("/logs/{filename}", response_class=HTMLResponse)
async def view_log(request: Request, filename: str):
    # Sanitise filename — no path traversal
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename.")
    path = LOG_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Log not found.")
    content = path.read_text(encoding="utf-8", errors="replace")
    return templates.TemplateResponse("log.html", {
        "request":  request,
        "filename": filename,
        "content":  content,
    })
