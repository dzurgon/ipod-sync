"""
Artist photos — enrichment that lives in the file tree, not just the DB.

Convention: an artist folder's photo is named after the folder, inside it:

    0_POP/Charli XCX/Charli XCX.jpg

so it travels with the library (Nextcloud, rsync, backups) and any tool can find
it. Photos come from Deezer's public API (no key needed) or a manual upload.
Fetches only accept an exact (normalised) name match, so an ambiguous artist
gets no photo rather than the wrong one.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .indexer import IMAGE_EXTS, find_artist_image, norm_key

DEEZER_SEARCH = "https://api.deezer.com/search/artist?limit=10&q="
USER_AGENT = "Aquaria/0.4 (self-hosted music manager)"
MAX_BYTES = 15 * 2**20

# magic bytes -> extension
_SIGS = ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG\r\n\x1a\n", ".png"))


def sniff_ext(data: bytes) -> str | None:
    for sig, ext in _SIGS:
        if data.startswith(sig):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(MAX_BYTES + 1)


def deezer_lookup(artist: str) -> str | None:
    """URL of the best exact-name match's 1000px photo, or None."""
    data = json.loads(_get(DEEZER_SEARCH + urllib.parse.quote(artist)))
    key = norm_key(artist)
    matches = [a for a in data.get("data", [])
               if norm_key(a.get("name", "")) == key
               # Deezer's "no photo" placeholder has an empty hash: .../artist//1000x1000...
               and "/artist//" not in (a.get("picture_xl") or "/artist//")]
    if not matches:
        return None
    return max(matches, key=lambda a: a.get("nb_fan", 0))["picture_xl"]


def save_image(music_root: Path, rel_dir: str, data: bytes) -> str:
    """Write <rel_dir>/<folder name>.<ext>, replacing any previous convention photo."""
    if len(data) > MAX_BYTES:
        raise ValueError("image is larger than 15 MB")
    ext = sniff_ext(data)
    if not ext:
        raise ValueError("not a JPEG, PNG or WebP image")
    root = music_root.resolve()
    folder = (root / rel_dir).resolve()
    if root not in folder.parents or not folder.is_dir():
        raise FileNotFoundError(rel_dir)
    for old in folder.iterdir():
        if old.stem.casefold() == folder.name.casefold() and old.suffix.lower() in IMAGE_EXTS:
            old.unlink()
    target = folder / f"{folder.name}{ext}"
    tmp = folder / f".{folder.name}{ext}.tmp"
    tmp.write_bytes(data)
    tmp.replace(target)
    return target.relative_to(root).as_posix()


def record(conn, music_root: Path, rel_dir: str) -> str | None:
    img = find_artist_image(music_root / rel_dir)
    rel = img.relative_to(music_root).as_posix() if img else None
    conn.execute("UPDATE artist_folders SET image=? WHERE rel_dir=?", (rel, rel_dir))
    conn.commit()
    return rel


def fetch_one(conn, music_root: Path, rel_dir: str) -> str | None:
    """Fetch + save a photo for one artist folder. Returns the image rel_path or None."""
    row = conn.execute("SELECT artist FROM artist_folders WHERE rel_dir=?", (rel_dir,)).fetchone()
    if not row:
        raise FileNotFoundError(rel_dir)
    url = deezer_lookup(row["artist"])
    if not url:
        return None
    save_image(music_root, rel_dir, _get(url))
    return record(conn, music_root, rel_dir)


# ── Bulk "fetch all missing" job (one at a time, in a background thread) ─────

JOB = {"running": False, "done": 0, "total": 0, "found": 0, "missing": [], "error": None}
_job_lock = threading.Lock()


def start_fetch_missing(conn, music_root: Path, write_lock: threading.Lock) -> dict:
    with _job_lock:
        if JOB["running"]:
            return dict(JOB)
        dirs = [r["rel_dir"] for r in conn.execute(
            "SELECT rel_dir FROM artist_folders WHERE image IS NULL ORDER BY rel_dir")]
        JOB.update(running=True, done=0, total=len(dirs), found=0, missing=[], error=None)

    def run():
        try:
            for rel_dir in dirs:
                try:
                    with write_lock:
                        got = fetch_one(conn, music_root, rel_dir)
                except Exception as e:          # network hiccup etc. — note it, keep going
                    got, JOB["error"] = None, f"{rel_dir}: {e}"
                if got:
                    JOB["found"] += 1
                else:
                    JOB["missing"].append(rel_dir)
                JOB["done"] += 1
                time.sleep(0.25)                # stay well under Deezer's 50 req / 5 s
        finally:
            JOB["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return dict(JOB)
