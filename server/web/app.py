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

def scan_library() -> list[dict]:
    """Return list of album dicts from MUSIC_DIR."""
    albums = []
    if not MUSIC_DIR.exists():
        return albums
    for album_dir in sorted(MUSIC_DIR.iterdir()):
        if not album_dir.is_dir():
            continue
        tracks = sorted(
            t for t in album_dir.iterdir()
            if t.suffix.lower() in {".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac"}
        )
        cover = next((
            str(f.name) for f in album_dir.iterdir()
            if f.name.lower() in {"cover.jpg", "cover.png", "folder.jpg"}
        ), None)
        albums.append({
            "name":       album_dir.name,
            "path":       str(album_dir),
            "track_count": len(tracks),
            "tracks":     [t.name for t in tracks],
            "cover":      cover,
        })
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


# ── Album art ────────────────────────────────────────────────────────────────

@app.get("/album-art/{album_name}/{filename}")
async def album_art(album_name: str, filename: str):
    """Serve cover art images directly from the music library."""
    # Sanitise both path components — no traversal
    if ".." in album_name or ".." in filename or "/" in filename:
        raise HTTPException(400, "Invalid path.")
    image_path = MUSIC_DIR / album_name / filename
    if not image_path.exists():
        raise HTTPException(404, "Cover art not found.")
    media_type = "image/jpeg" if filename.lower().endswith(".jpg") else "image/png"
    return FileResponse(image_path, media_type=media_type)


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
async def add_track(name: str, track_path: str = Form(...)):
    """Add a relative track path to a playlist."""
    path = PLAYLIST_DIR / f"{name}.m3u"
    if not path.exists():
        raise HTTPException(404, "Playlist not found.")
    # Normalise to forward-slash relative path
    rel = track_path.replace("\\", "/").strip("/")
    with open(path, "a", encoding="utf-8") as f:
        f.write(rel + "\n")
    push_event("info", f"Added track to '{name}': {Path(rel).name}")
    return RedirectResponse(f"/playlists/{name}", status_code=303)


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
