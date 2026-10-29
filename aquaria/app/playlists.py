"""
Playlist logic — the "unity between playlists and the library" requirement.

Playlists are text files of paths. Two realities must reconcile:
  * NEW playlists made in Aquaria reference tracks by their canonical identity.
  * OLD/messy playlists have lines like:
        (2023) yeule - softscars [FLAC]/01. x w x.flac
    whose folder string no longer exists after the server restructure.

resolve_line() takes any raw line and finds the matching track_id by:
  1. exact rel_path hit,
  2. normalized (artist, album, title) match via the legacy normaliser,
  3. fuzzy title+artist FTS fallback.
This lets old playlists keep working and lets iPod projections rebuild correct
paths for whichever layout (genre / flat) a user's iPod uses.
"""

from __future__ import annotations

import re
import time
from pathlib import Path, PurePosixPath

from .parser import normalize_legacy_album, parse_track_filename
from .indexer import norm_key


def resolve_line(conn, raw_line: str) -> tuple[int | None, int]:
    """
    Return (track_id, resolved_flag). resolved_flag: 0=none, 1=exact, 2=fuzzy.
    """
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None, 0

    # 1. exact rel_path
    row = conn.execute("SELECT id FROM tracks WHERE rel_path = ?", (line,)).fetchone()
    if row:
        return row["id"], 1

    # Break the line into <album-string>/<filename>
    p = PurePosixPath(line)
    filename = p.name
    album_dir = p.parent.name if p.parent.name else ""
    _, _, title = parse_track_filename(filename)
    artist, album, year = normalize_legacy_album(album_dir)

    # 2. normalized (album, title) [+ year when available]
    row = conn.execute(
        """
        SELECT id FROM tracks
        WHERE album_key = ?
          AND LOWER(title) = LOWER(?)
          {}
        LIMIT 1
        """.format("AND year = ?" if year else ""),
        ((norm_key(album), title, year) if year else (norm_key(album), title)),
    ).fetchone()
    if row:
        return row["id"], 2

    # 3. fuzzy FTS on title (+ artist if parsed)
    terms = title.split()
    if artist:
        terms += artist.split()
    if terms:
        fts_q = " ".join(f"{t}*" for t in terms)
        row = conn.execute(
            "SELECT t.id FROM tracks_fts f JOIN tracks t ON t.id=f.track_id "
            "WHERE tracks_fts MATCH ? ORDER BY rank LIMIT 1",
            (fts_q,),
        ).fetchone()
        if row:
            return row["id"], 2

    return None, 0


def ipod_path(conn, track_id: int, layout: str = "genre") -> str | None:
    """
    Project a track to the path it should live at on an iPod, per that iPod's
    chosen layout. 'genre' mirrors the server (0_GENRE/Artist/Album (Year)/file);
    'flat' drops the genre bucket (Artist/Album (Year)/file) for a simple setup.
    """
    t = conn.execute("SELECT rel_path, genre FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not t:
        return None
    rel = t["rel_path"]
    if layout == "flat" and t["genre"]:
        # strip the leading genre bucket component
        parts = rel.split("/")
        if parts and parts[0].split("_", 1)[0].isdigit():
            return "/".join(parts[1:])
    return rel


# ── Write path: CRUD + .m3u round-trip ───────────────────────────────────────
#
# The DB is what the UI edits; each playlist is mirrored to an .m3u (one rel_path per
# line, relative to MUSIC_ROOT): <PLAYLIST_DIR>/<username>/<name>.m3u for playlists made
# in Aquaria, while legacy files in the PLAYLIST_DIR root stay owned by the default user.
# Files edited outside Aquaria are re-imported on the next sync_dir() when their mtime is
# newer than the DB copy. Playlists are public (is_global=1) or private to their owner.


def safe_name(name: str) -> str:
    """Same rule as the legacy app: word chars, spaces and dashes only."""
    return re.sub(r"[^\w\s\-]", "", name).strip()


def ensure_user(conn, username: str) -> int:
    row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO users(username, settings, created_at) VALUES (?,?,?)",
        (username, "{}", time.time()),
    )
    return cur.lastrowid


def _read_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]


def _set_items(conn, playlist_id: int, lines: list[str]) -> None:
    conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
    for pos, line in enumerate(lines):
        tid, flag = resolve_line(conn, line)
        conn.execute(
            "INSERT INTO playlist_items(playlist_id, position, raw_line, track_id, resolved) "
            "VALUES (?,?,?,?,?)",
            (playlist_id, pos, line, tid, flag),
        )


def write_file(conn, playlist_id: int) -> None:
    pl = conn.execute("SELECT file_path FROM playlists WHERE id=?", (playlist_id,)).fetchone()
    if not pl or not pl["file_path"]:
        return
    lines = [r["raw_line"] for r in conn.execute(
        "SELECT raw_line FROM playlist_items WHERE playlist_id=? ORDER BY position",
        (playlist_id,),
    )]
    path = Path(pl["file_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".m3u.tmp")
    tmp.write_text("#EXTM3U\n" + "".join(l + "\n" for l in lines), encoding="utf-8")
    tmp.replace(path)
    # Record the file's own mtime so sync_dir() doesn't treat our write as an external edit.
    conn.execute("UPDATE playlists SET updated_at=? WHERE id=?",
                 (path.stat().st_mtime, playlist_id))


def sync_dir(conn, playlist_dir: Path, owner_id: int) -> dict:
    """Import new .m3u files and reload ones changed on disk since we last wrote them.

    Root-level files belong to owner_id (the default user); files in a subfolder named
    after an existing user belong to that user."""
    imported = reloaded = 0
    if not playlist_dir.is_dir():
        return {"imported": 0, "reloaded": 0}
    owned = [(p, owner_id) for p in sorted(playlist_dir.glob("*.m3u"))]
    for sub in sorted(d for d in playlist_dir.iterdir() if d.is_dir()):
        u = conn.execute("SELECT id FROM users WHERE username=? COLLATE NOCASE", (sub.name,)).fetchone()
        if u:
            owned += [(p, u["id"]) for p in sorted(sub.glob("*.m3u"))]
    for path, uid in owned:
        mtime = path.stat().st_mtime
        row = conn.execute("SELECT id, updated_at FROM playlists WHERE file_path=?",
                           (str(path),)).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO playlists(user_id, name, is_global, file_path, created_at, updated_at) "
                "VALUES (?,?,1,?,?,?)",
                (uid, path.stem, str(path), mtime, mtime),
            )
            _set_items(conn, cur.lastrowid, _read_lines(path))
            imported += 1
        elif mtime > row["updated_at"] + 1:
            _set_items(conn, row["id"], _read_lines(path))
            conn.execute("UPDATE playlists SET updated_at=? WHERE id=?", (mtime, row["id"]))
            reloaded += 1
        else:
            # Re-resolve lines against a possibly rescanned library (ids survive
            # rescans, but pruned tracks were nulled and may have reappeared).
            for it in conn.execute(
                "SELECT id, raw_line FROM playlist_items WHERE playlist_id=? AND track_id IS NULL",
                (row["id"],),
            ).fetchall():
                tid, flag = resolve_line(conn, it["raw_line"])
                if tid:
                    conn.execute("UPDATE playlist_items SET track_id=?, resolved=? WHERE id=?",
                                 (tid, flag, it["id"]))
    conn.commit()
    return {"imported": imported, "reloaded": reloaded}


def list_playlists(conn, viewer_id: int | None = None) -> list[dict]:
    """Public playlists plus the viewer's own private ones."""
    rows = conn.execute("""
        SELECT p.id, p.name, p.updated_at, u.username AS owner, p.user_id = ? AS is_mine,
               p.is_global AS is_public,
               COUNT(i.id) AS n_tracks,
               SUM(CASE WHEN i.id IS NOT NULL AND i.track_id IS NULL THEN 1 ELSE 0 END) AS n_missing
        FROM playlists p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN playlist_items i ON i.playlist_id = p.id
        WHERE p.is_global = 1 OR p.user_id = ?
        GROUP BY p.id ORDER BY LOWER(p.name)
    """, (viewer_id or -1, viewer_id or -1)).fetchall()
    return [dict(r) | {"is_mine": bool(r["is_mine"]), "is_public": bool(r["is_public"])}
            for r in rows]


def get_playlist(conn, playlist_id: int, viewer_id: int | None = None) -> dict | None:
    """The playlist with its items, or None if it doesn't exist or is private to someone else."""
    pl = conn.execute(
        "SELECT p.id, p.name, p.updated_at, p.user_id, p.is_global, u.username AS owner "
        "FROM playlists p JOIN users u ON u.id = p.user_id WHERE p.id=?",
        (playlist_id,)).fetchone()
    if not pl or (not pl["is_global"] and pl["user_id"] != viewer_id):
        return None
    items = conn.execute("""
        SELECT i.id AS item_id, i.position, i.raw_line, i.resolved,
               t.id AS track_id, t.rel_path, t.title, t.artist, t.album, t.year, t.ext
        FROM playlist_items i LEFT JOIN tracks t ON t.id = i.track_id
        WHERE i.playlist_id=? ORDER BY i.position
    """, (playlist_id,)).fetchall()
    return {**dict(pl), "is_public": bool(pl["is_global"]), "is_mine": pl["user_id"] == viewer_id,
            "items": [dict(i) for i in items]}


def create_playlist(conn, playlist_dir: Path, owner_id: int, name: str,
                    is_public: bool = True, lines: list[str] | None = None) -> int:
    """New playlist for owner_id at <playlist_dir>/<username>/<name>.m3u (names unique per user)."""
    name = safe_name(name)
    if not name:
        raise ValueError("playlist name is empty or invalid")
    owner = conn.execute("SELECT username FROM users WHERE id=?", (owner_id,)).fetchone()
    path = playlist_dir / owner["username"] / f"{name}.m3u"
    if path.exists() or conn.execute(
            "SELECT 1 FROM playlists WHERE user_id=? AND LOWER(name)=LOWER(?)",
            (owner_id, name)).fetchone():
        raise FileExistsError(f"you already have a playlist called '{name}'")
    now = time.time()
    cur = conn.execute(
        "INSERT INTO playlists(user_id, name, is_global, file_path, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (owner_id, name, int(is_public), str(path), now, now),
    )
    if lines:
        _set_items(conn, cur.lastrowid, lines)
    write_file(conn, cur.lastrowid)
    conn.commit()
    return cur.lastrowid


def set_public(conn, playlist_id: int, is_public: bool) -> None:
    conn.execute("UPDATE playlists SET is_global=? WHERE id=?", (int(is_public), playlist_id))
    conn.commit()


def add_tracks(conn, playlist_id: int, rel_paths: list[str]) -> int:
    """Append tracks (by rel_path), skipping ones already present. Returns # added."""
    existing = {r["track_id"] for r in conn.execute(
        "SELECT track_id FROM playlist_items WHERE playlist_id=?", (playlist_id,))}
    pos = conn.execute("SELECT COALESCE(MAX(position)+1, 0) FROM playlist_items WHERE playlist_id=?",
                       (playlist_id,)).fetchone()[0]
    added = 0
    for rel in rel_paths:
        t = conn.execute("SELECT id FROM tracks WHERE rel_path=?", (rel,)).fetchone()
        if not t or t["id"] in existing:
            continue
        conn.execute(
            "INSERT INTO playlist_items(playlist_id, position, raw_line, track_id, resolved) "
            "VALUES (?,?,?,?,1)",
            (playlist_id, pos, rel, t["id"]),
        )
        existing.add(t["id"])
        pos += 1
        added += 1
    if added:
        write_file(conn, playlist_id)
    conn.commit()
    return added


def remove_item(conn, playlist_id: int, item_id: int) -> None:
    conn.execute("DELETE FROM playlist_items WHERE id=? AND playlist_id=?", (item_id, playlist_id))
    write_file(conn, playlist_id)
    conn.commit()


def move_item(conn, playlist_id: int, item_id: int, delta: int) -> None:
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM playlist_items WHERE playlist_id=? ORDER BY position", (playlist_id,))]
    if item_id not in ids:
        return
    i = ids.index(item_id)
    j = max(0, min(len(ids) - 1, i + delta))
    ids.insert(j, ids.pop(i))
    for pos, iid in enumerate(ids):
        conn.execute("UPDATE playlist_items SET position=? WHERE id=?", (pos, iid))
    write_file(conn, playlist_id)
    conn.commit()


def delete_playlist(conn, playlist_id: int) -> None:
    """Drop from the DB; the .m3u is renamed to *.m3u.deleted (not unlinked) so it's recoverable."""
    pl = conn.execute("SELECT file_path FROM playlists WHERE id=?", (playlist_id,)).fetchone()
    if pl and pl["file_path"]:
        p = Path(pl["file_path"])
        if p.exists():
            p.replace(p.with_name(p.name + ".deleted"))
    conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    conn.commit()
