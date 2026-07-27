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

from pathlib import PurePosixPath

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
