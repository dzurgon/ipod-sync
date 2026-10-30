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

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
# legacy avatar name, still honoured after the <Folder Name>.<ext> convention
_LEGACY_IMG_RE = re.compile(r"^artist\.(jpg|jpeg|png|webp)$", re.IGNORECASE)


def find_folder_image(folder: Path) -> Path | None:
    """<folder>/<folder name>.<img ext> (any case), else legacy <folder>/artist.<ext>."""
    try:
        files = [p for p in folder.iterdir() if p.is_file()]
    except OSError:
        return None
    want = folder.name.casefold()
    for p in files:
        if p.stem.casefold() == want and p.suffix.lower() in IMAGE_EXTS:
            return p
    return next((p for p in files if _LEGACY_IMG_RE.match(p.name)), None)


def refresh_folder_images(conn, root: Path) -> int:
    """Re-check artwork for known genre/artist folders, so images dropped into the tree by
    hand show up without a full scan (~3 ms for ~100 folders). Returns rows changed."""
    changed = 0
    for table in ("genre_folders", "artist_folders"):
        for rel_dir, image in conn.execute(f"SELECT rel_dir, image FROM {table}").fetchall():
            img = find_folder_image(root / rel_dir)
            rel = img.relative_to(root).as_posix() if img else None
            if rel != image:
                conn.execute(f"UPDATE {table} SET image=? WHERE rel_dir=?", (rel, rel_dir))
                changed += 1
    if changed:
        conn.commit()
    return changed


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
    artist_dirs: dict[str, tuple[str, str]] = {}   # rel_dir -> (artist_key, artist)
    genre_dirs: dict[str, str] = {}                # rel_dir -> genre label
    n_files = 0

    cur = conn.cursor()
    if not root.exists():
        return {"ok": False, "error": f"music root not found: {root}", "tracks": 0}
    before = {r[0] for r in cur.execute("SELECT rel_path FROM tracks")}

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()

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

        if pt.genre is not None:
            genre_dirs.setdefault(rel.split("/")[0], pt.genre)

        # A real artist folder sits right under the genre bucket (or the root).
        if pt.artist and not pt.artist_inferred:
            depth = 2 if pt.genre is not None else 1
            artist_dirs.setdefault("/".join(rel.split("/")[:depth]),
                                   (norm_key(pt.artist), pt.artist))

    # Prune vanished files.
    existing = {r[0] for r in cur.execute("SELECT rel_path FROM tracks")}
    gone = existing - seen
    if gone:
        cur.executemany("DELETE FROM tracks WHERE rel_path=?", ((g,) for g in gone))

    cur.execute("DELETE FROM artist_folders")
    for rel_dir, (key, name) in artist_dirs.items():
        img = find_folder_image(root / rel_dir)
        cur.execute(
            "INSERT INTO artist_folders(rel_dir, artist_key, artist, image) VALUES (?,?,?,?)",
            (rel_dir, key, name, img.relative_to(root).as_posix() if img else None),
        )
    cur.execute("DELETE FROM genre_folders")
    for rel_dir, genre in genre_dirs.items():
        img = find_folder_image(root / rel_dir)
        cur.execute("INSERT INTO genre_folders(rel_dir, genre, image) VALUES (?,?,?)",
                    (rel_dir, genre, img.relative_to(root).as_posix() if img else None))

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
        "added": len(seen - before),
        "pruned": len(gone),
        "folder_images": sum(1 for (i,) in cur.execute(
            "SELECT image FROM genre_folders UNION ALL SELECT image FROM artist_folders") if i),
        "artist_folders": len(artist_dirs),
        "seconds": round(time.time() - started, 2),
    }
