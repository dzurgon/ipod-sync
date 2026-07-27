"""
Scanner: walk the music root, parse every audio file, populate the SQLite index.

Idempotent: a full rescan upserts by rel_path and prunes rows whose files have
disappeared. Cheap enough to run on a file-watcher event or a manual "Rescan"
button. (Incremental mtime-based scanning is a later optimisation; correctness
first.)
"""

from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path

from .parser import parse_rel_path, is_audio

# case-insensitive artist.<img> avatar files
_ARTIST_IMG_RE = re.compile(r"^artist\.(jpg|jpeg|png|webp|gif)$", re.IGNORECASE)


def norm_key(s: str) -> str:
    """Normalize a name for dedupe/matching: fold case, strip accents & punctuation."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def scan(conn, music_root: str | Path) -> dict:
    """Full rescan. Returns a summary dict."""
    root = Path(music_root)
    started = time.time()
    seen: set[str] = set()
    n_files = 0

    cur = conn.cursor()
    if not root.exists():
        return {"ok": False, "error": f"music root not found: {root}", "tracks": 0}

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()

        # Cache artist avatar images (artist.* directly under an artist folder).
        if _ARTIST_IMG_RE.match(path.name):
            artist_folder = path.parent.name
            cur.execute(
                "INSERT OR REPLACE INTO artist_images(artist_key, rel_path) VALUES (?,?)",
                (norm_key(artist_folder), rel),
            )
            continue

        if not is_audio(path.name):
            continue

        pt = parse_rel_path(rel)
        if pt is None:
            continue

        try:
            st = path.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = None, None

        cur.execute(
            """
            INSERT INTO tracks
              (rel_path, genre, genre_order, artist, artist_key, artist_inferred,
               album, album_key, year, disc_no, track_no, title, ext, size_bytes, mtime)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(rel_path) DO UPDATE SET
              genre=excluded.genre, genre_order=excluded.genre_order,
              artist=excluded.artist, artist_key=excluded.artist_key,
              artist_inferred=excluded.artist_inferred,
              album=excluded.album, album_key=excluded.album_key, year=excluded.year,
              disc_no=excluded.disc_no, track_no=excluded.track_no, title=excluded.title,
              ext=excluded.ext, size_bytes=excluded.size_bytes, mtime=excluded.mtime
            """,
            (
                pt.rel_path, pt.genre, pt.genre_order,
                pt.artist, norm_key(pt.artist), int(pt.artist_inferred),
                pt.album, norm_key(pt.album), pt.year,
                pt.disc_no, pt.track_no, pt.title, pt.ext, size, mtime,
            ),
        )
        seen.add(pt.rel_path)
        n_files += 1

    # Prune vanished files.
    existing = {r[0] for r in cur.execute("SELECT rel_path FROM tracks")}
    gone = existing - seen
    if gone:
        cur.executemany("DELETE FROM tracks WHERE rel_path=?", ((g,) for g in gone))

    # Rebuild FTS from scratch (simple + correct).
    cur.execute("DELETE FROM tracks_fts")
    cur.execute(
        "INSERT INTO tracks_fts(track_id, title, album, artist, genre) "
        "SELECT id, title, album, artist, COALESCE(genre,'') FROM tracks"
    )
    conn.commit()

    return {
        "ok": True,
        "tracks": n_files,
        "pruned": len(gone),
        "seconds": round(time.time() - started, 2),
    }
