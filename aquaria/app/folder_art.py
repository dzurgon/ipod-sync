"""
Folder artwork — enrichment that lives in the file tree, not just the DB.

Convention: a folder's picture sits inside it and is named after it:

    0_POP/0_POP.jpg                    genre bucket artwork
    0_POP/Charli XCX/Charli XCX.jpg    artist photo

so it travels with the library (Nextcloud, rsync, backups) and any tool can find
it. Album folders keep beets' cover.jpg instead (uploads are converted to a
baseline JPEG, which is what Rockbox can display). Pictures come from online
sources or a manual upload:

  * artists — Deezer's public artist search (no key needed), exact-name matches
    only, so an ambiguous artist gets no photo rather than the wrong one.
  * genres  — config.GENRE_ART_SOURCES: Deezer's genre artwork by name, or a
    Wikipedia article's lead image (e.g. the KWUR station logo).
  * albums  — artwork embedded in the album's own files first (exact, offline), then
    Deezer and iTunes album search by artist + album name. Only matching artist and
    title are accepted (ignoring [VINYL]-style tags); iTunes prefers the folder's year.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageFile

# A cover missing its last few bytes (an interrupted download) still decodes fine.
ImageFile.LOAD_TRUNCATED_IMAGES = True

from . import config
from .indexer import IMAGE_EXTS, find_folder_image, norm_key

DEEZER_SEARCH = "https://api.deezer.com/search/artist?limit=10&q="
DEEZER_GENRES = "https://api.deezer.com/genre"
DEEZER_ALBUMS = "https://api.deezer.com/search/album?limit=10&q="
ITUNES_ALBUMS = "https://itunes.apple.com/search?entity=album&limit=25&term="
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
USER_AGENT = "Aquaria/0.4 (self-hosted music manager)"
MAX_BYTES = 15 * 2**20

# Which table holds each kind of folder, and the column naming the thing in it.
KINDS = {"artist": ("artist_folders", "artist"), "genre": ("genre_folders", "genre")}

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


# Deezer's "no picture" placeholder has an empty hash: .../images/artist//1000x1000...
def _real(url: str | None) -> bool:
    return bool(url) and "//1000x1000" not in url


def deezer_artist(artist: str) -> str | None:
    """URL of the best exact-name match's 1000px photo, or None."""
    data = json.loads(_get(DEEZER_SEARCH + urllib.parse.quote(artist)))
    key = norm_key(artist)
    matches = [a for a in data.get("data", [])
               if norm_key(a.get("name", "")) == key and _real(a.get("picture_xl"))]
    if not matches:
        return None
    return max(matches, key=lambda a: a.get("nb_fan", 0))["picture_xl"]


def deezer_genre(name: str) -> str | None:
    key = norm_key(name)
    for g in json.loads(_get(DEEZER_GENRES)).get("data", []):
        if norm_key(g.get("name", "")) == key and _real(g.get("picture_xl")):
            return g["picture_xl"]
    return None


def wikipedia_image(page: str) -> str | None:
    data = json.loads(_get(WIKI_SUMMARY + urllib.parse.quote(page.replace(" ", "_"), safe="")))
    return (data.get("originalimage") or data.get("thumbnail") or {}).get("source")


def genre_lookup(genre: str) -> str | None:
    source, _, name = config.GENRE_ART_SOURCES.get(genre, f"deezer:{genre}").partition(":")
    if source == "wikipedia":
        return wikipedia_image(name)
    return deezer_genre(name)


# What the album page shows as the cover (GET /art), in order.
ALBUM_ART_NAMES = ("cover.jpg", "cover.png", "cover.jpeg", "cover.webp", "folder.jpg")


def album_cover(folder: Path) -> Path | None:
    return next((folder / n for n in ALBUM_ART_NAMES if (folder / n).is_file()), None)


def missing_album_covers(conn, music_root: Path) -> list[dict]:
    """Albums (as the library lists them) whose folder has no cover image."""
    from .library import _albums
    return [{"rel_dir": a["folder"], "artist": a["artist"], "album": a["album"], "year": a["year"]}
            for a in _albums(conn) if a["folder"] and not album_cover(music_root / a["folder"])]


def embedded_art(files: list[Path]) -> bytes | None:
    """The picture embedded in the first of these tracks that has one (ffmpeg)."""
    ffmpeg = shutil.which("ffmpeg")
    for f in files[:4] if ffmpeg else []:
        r = subprocess.run([ffmpeg, "-v", "error", "-i", str(f), "-map", "0:v:0", "-frames:v", "1",
                            "-c", "copy", "-f", "image2pipe", "pipe:1"],
                           capture_output=True, timeout=30)
        if r.returncode == 0 and sniff_ext(r.stdout):
            return r.stdout
    return None


def _core(s: str) -> str:
    """Name for matching: drop (…)/[…] parts like (Deluxe Edition) or [VINYL] [FLAC]."""
    return norm_key(re.sub(r"[\(\[][^)\]]*[\)\]]", " ", s or ""))


def deezer_album(artist: str, album: str) -> str | None:
    q = f'artist:"{artist}" album:"{album}"'
    for a in json.loads(_get(DEEZER_ALBUMS + urllib.parse.quote(q))).get("data", []):
        if (_core(a.get("artist", {}).get("name", "")) == _core(artist)
                and _core(a.get("title", "")) == _core(album) and _real(a.get("cover_xl"))):
            return a["cover_xl"]
    return None


_itunes_last = 0.0


def itunes_album(artist: str, album: str, year: int | None) -> str | None:
    global _itunes_last
    time.sleep(max(0.0, _itunes_last + 3 - time.time()))   # iTunes allows ~20 searches/min
    _itunes_last = time.time()
    data = json.loads(_get(ITUNES_ALBUMS + urllib.parse.quote(f"{artist} {album}")))
    hits = [r for r in data.get("results", [])
            if _core(r.get("artistName", "")) == _core(artist)
            and _core(r.get("collectionName", "")) == _core(album) and r.get("artworkUrl100")]
    if not hits:
        return None
    best = next((r for r in hits if year and str(r.get("releaseDate", "")).startswith(str(year))), hits[0])
    return best["artworkUrl100"].replace("100x100bb", "1000x1000bb")


def lookup_names(artist: str, album: str) -> tuple[str, str]:
    """Artist + album as the online catalogues spell them, from how folders are named:
    'Album_Artist' (KWUR rips; 'various.artists' → Various Artists), and album folders
    that repeat the artist ('Black Sabbath - Technical Ecstasy')."""
    if not artist and "_" in album:
        album, artist = (x.strip() for x in album.rsplit("_", 1))
        artist = artist.replace(".", " ") if norm_key(artist) == "various artists" else artist
    if artist and album.lower().startswith(artist.lower() + " - "):
        album = album[len(artist) + 3:].strip()
    return artist, album


def fetch_album(conn, music_root: Path, rel_dir: str) -> tuple[str, str] | None:
    """Find + save cover.jpg for an album folder: embedded art, else Deezer, else iTunes.
    Returns (image rel_path, where it came from) or None."""
    rows = conn.execute(
        "SELECT rel_path, artist, artist_inferred, album, year FROM tracks "
        "WHERE substr(rel_path, 1, length(?1) + 1) = ?1 || '/' ORDER BY rel_path", (rel_dir,)).fetchall()
    if not rows:
        raise FileNotFoundError(rel_dir)
    data, source = embedded_art([music_root / r["rel_path"] for r in rows]), "embedded in the files"
    t = rows[0]
    artist, album = lookup_names(t["artist"], t["album"])
    if not data and artist and album and not t["artist_inferred"]:
        url, source = deezer_album(artist, album), "Deezer"
        if not url:
            url, source = itunes_album(artist, album, t["year"]), "iTunes"
        data = url and _get(url)
    if not data:
        return None
    return save_cover(music_root, rel_dir, data), source


def kind_of(conn, rel_dir: str) -> tuple[str, str] | None:
    """('artist'|'genre'|'album', display name) for a known folder, else None."""
    for kind, (table, col) in KINDS.items():
        row = conn.execute(f"SELECT {col} AS name FROM {table} WHERE rel_dir=?", (rel_dir,)).fetchone()
        if row:
            return kind, row["name"]
    # An album folder is one with tracks directly inside it.
    row = conn.execute(
        "SELECT album FROM tracks WHERE substr(rel_path, 1, length(?1) + 1) = ?1 || '/' "
        "AND instr(substr(rel_path, length(?1) + 2), '/') = 0 LIMIT 1", (rel_dir,)).fetchone()
    return ("album", row["album"]) if row else None


COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp")
COVER_MAX_PX = 1000


def to_jpeg(data: bytes) -> bytes:
    """Any JPEG/PNG/WebP -> a baseline JPEG <= COVER_MAX_PX, which Rockbox can also show."""
    if len(data) > MAX_BYTES:
        raise ValueError("image is larger than 15 MB")
    if not sniff_ext(data):
        raise ValueError("not a JPEG, PNG or WebP image")
    try:
        img = Image.open(io.BytesIO(data))
        img.thumbnail((COVER_MAX_PX, COVER_MAX_PX))
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=90, progressive=False)
    except Exception as e:
        raise ValueError(f"could not read image: {e}")
    return out.getvalue()


def ipod_art(src: Path, cache_dir: Path, max_px: int) -> Path:
    """A baseline JPEG of src no larger than max_px, cached by path + mtime + size."""
    st = src.stat()
    key = hashlib.sha1(f"{src}\0{st.st_mtime_ns}\0{st.st_size}\0{max_px}".encode()).hexdigest()
    out = cache_dir / f"{key}.jpg"
    if out.is_file():
        return out
    cache_dir.mkdir(parents=True, exist_ok=True)
    img = Image.open(src)
    img.draft("RGB", (max_px, max_px))         # JPEG: decode at reduced scale, much faster
    img.thumbnail((max_px, max_px))
    tmp = out.with_suffix(f".{os.getpid()}.tmp")
    img.convert("RGB").save(tmp, "JPEG", quality=88, progressive=False)
    tmp.replace(out)
    return out


def save_cover(music_root: Path, rel_dir: str, data: bytes) -> str:
    """Write <album>/cover.jpg as a baseline JPEG (<= 1000px), replacing any other cover.*."""
    if len(data) > MAX_BYTES:
        raise ValueError("image is larger than 15 MB")
    if not sniff_ext(data):
        raise ValueError("not a JPEG, PNG or WebP image")
    root = music_root.resolve()
    folder = (root / rel_dir).resolve()
    if root not in folder.parents or not folder.is_dir():
        raise FileNotFoundError(rel_dir)
    jpeg = to_jpeg(data)
    for old in folder.iterdir():
        if old.name.lower() in COVER_NAMES:
            old.unlink()
    tmp = folder / ".cover.jpg.tmp"
    tmp.write_bytes(jpeg)
    tmp.replace(folder / "cover.jpg")
    return (folder / "cover.jpg").relative_to(root).as_posix()


def save_image(music_root: Path, rel_dir: str, data: bytes) -> str:
    """Write <rel_dir>/<folder name>.<ext>, replacing any previous convention image."""
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
    img = find_folder_image(music_root / rel_dir)
    rel = img.relative_to(music_root).as_posix() if img else None
    for table, _ in KINDS.values():
        conn.execute(f"UPDATE {table} SET image=? WHERE rel_dir=?", (rel, rel_dir))
    conn.commit()
    return rel


def fetch_one(conn, music_root: Path, rel_dir: str) -> str | None:
    """Fetch + save artwork for one artist or genre folder. Returns its rel_path or None."""
    found = kind_of(conn, rel_dir)
    if not found:
        raise FileNotFoundError(rel_dir)
    kind, name = found
    url = deezer_artist(name) if kind == "artist" else genre_lookup(name)
    if not url:
        return None
    save_image(music_root, rel_dir, _get(url))
    return record(conn, music_root, rel_dir)


# ── Bulk "fetch all missing" job (one at a time, in a background thread) ─────

JOB = {"running": False, "done": 0, "total": 0, "found": 0, "missing": [], "error": None,
       "sources": {}}
_job_lock = threading.Lock()


def start_fetch_missing(conn, music_root: Path, write_lock: threading.Lock,
                        kinds: tuple[str, ...] = ("genre", "artist", "album")) -> dict:
    with _job_lock:
        if JOB["running"]:
            return dict(JOB)
        todo = [(k, r["rel_dir"]) for k in kinds if k in KINDS for r in conn.execute(
            f"SELECT rel_dir FROM {KINDS[k][0]} WHERE image IS NULL ORDER BY rel_dir")]
        if "album" in kinds:
            todo += [("album", a["rel_dir"]) for a in missing_album_covers(conn, music_root)]
        JOB.update(running=True, done=0, total=len(todo), found=0, missing=[], error=None, sources={})

    def run():
        try:
            for kind, rel_dir in todo:
                try:
                    if kind == "album":
                        got = fetch_album(conn, music_root, rel_dir)   # slow I/O outside the lock
                        source = got and got[1]
                    else:
                        with write_lock:
                            got = fetch_one(conn, music_root, rel_dir)
                        source = "Deezer" if kind == "artist" else "genre art"
                except Exception as e:          # network hiccup etc. — note it, keep going
                    got, JOB["error"] = None, f"{rel_dir}: {e}"
                if got:
                    JOB["found"] += 1
                    JOB["sources"][source] = JOB["sources"].get(source, 0) + 1
                else:
                    JOB["missing"].append(rel_dir)
                JOB["done"] += 1
                time.sleep(0.25)                # stay well under Deezer's 50 req / 5 s
        finally:
            JOB["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return dict(JOB)
