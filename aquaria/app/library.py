"""
Library projections — the four Library-tab display modes, as pure SQL over the
`tracks` index. Nothing here reads the filesystem; the scanner already did.

Modes
-----
'genre'      -> genre buckets at the front (exactly like `cd`-ing the server tree)
'artist'     -> "Artist &/or Album": artist tiles (circular avatar), artist-less
                albums surfaced as their parsed artist
'all_albums' -> flat LIST of every album, labelled "Artist - Album (Year)"

(The spec says "4 ways" but lists three named modes; a 4th is easy to add — see
ARCHITECTURE.md. `albums` below is the shared album aggregation the views build on.)
"""

from __future__ import annotations


def _albums(conn, where: str = "", params: tuple = ()) -> list[dict]:
    """Aggregate tracks into album rows. One row per (artist_key, album_key, year)."""
    sql = f"""
        SELECT
            artist, artist_key, MAX(artist_inferred) AS artist_inferred,
            album, album_key, year,
            genre, genre_order,
            COUNT(*) AS n_tracks,
            MIN(rel_path) AS sample_path
        FROM tracks
        {where}
        GROUP BY artist_key, album_key, year
        ORDER BY genre_order, artist_key, year
    """
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        # album folder = dirname of any track in it
        folder = "/".join(r["sample_path"].split("/")[:-1])
        out.append({
            "artist": r["artist"] or "Unknown Artist",
            "artist_key": r["artist_key"],
            "artist_inferred": bool(r["artist_inferred"]),
            "album": r["album"],
            "album_key": r["album_key"],
            "year": r["year"],
            "genre": r["genre"],
            "genre_order": r["genre_order"],
            "n_tracks": r["n_tracks"],
            "folder": folder,
            "display_all_albums": f'{r["artist"] or "Unknown Artist"} - {r["album"]}'
                                  + (f" ({r['year']})" if r["year"] else ""),
        })
    return out


def view_genre(conn) -> list[dict]:
    """Top level = genre buckets, each holding its albums (folder-parity)."""
    genres = conn.execute(
        "SELECT genre, genre_order, COUNT(DISTINCT artist_key||'/'||album_key) AS n_albums "
        "FROM tracks WHERE genre IS NOT NULL "
        "GROUP BY genre ORDER BY genre_order, genre"
    ).fetchall()
    buckets = []
    for g in genres:
        buckets.append({
            "genre": g["genre"],
            "genre_order": g["genre_order"],
            "n_albums": g["n_albums"],
            "albums": _albums(conn, "WHERE genre = ?", (g["genre"],)),
        })
    # Also surface any tracks with no genre so nothing silently disappears.
    ungrouped = _albums(conn, "WHERE genre IS NULL")
    if ungrouped:
        buckets.append({"genre": "(no genre)", "genre_order": 9999,
                        "n_albums": len(ungrouped), "albums": ungrouped})
    return buckets


def view_artist(conn) -> list[dict]:
    """
    "Artist &/or Album": one entry per artist (circular avatar tile), each with
    its albums. Artist-less albums appear under their parsed artist name, so an
    obscure 'Passion Pit - Gossamer (2013)' becomes artist "Passion Pit".
    """
    artists = conn.execute("""
        SELECT t.artist, t.artist_key,
               MAX(t.artist_inferred) AS inferred,
               COUNT(DISTINCT t.album_key||'/'||IFNULL(t.year,'')) AS n_albums,
               COUNT(*) AS n_tracks,
               ai.rel_path AS image
        FROM tracks t
        LEFT JOIN artist_images ai ON ai.artist_key = t.artist_key
        WHERE t.artist <> ''
        GROUP BY t.artist_key
        ORDER BY t.artist_key
    """).fetchall()
    out = []
    for a in artists:
        out.append({
            "artist": a["artist"],
            "artist_key": a["artist_key"],
            "artist_inferred": bool(a["inferred"]),
            "n_albums": a["n_albums"],
            "n_tracks": a["n_tracks"],
            "image": a["image"],          # None -> frontend shows head icon
            "icon": "head",               # circular avatar, not album square
            "albums": _albums(conn, "WHERE artist_key = ?", (a["artist_key"],)),
        })
    return out


def view_all_albums(conn) -> list[dict]:
    """Flat LIST of every album labelled 'Artist - Album (Year)'. No artist layer."""
    albums = _albums(conn)
    return sorted(albums, key=lambda x: (x["artist_key"], x["year"] or 0))


def album_tracks(conn, folder: str) -> list[dict]:
    rows = conn.execute(
        "SELECT rel_path, disc_no, track_no, title, ext, year, album, artist, genre "
        "FROM tracks WHERE rel_path LIKE ? "
        "ORDER BY disc_no, track_no, title",
        (folder + "/%",),
    ).fetchall()
    return [dict(r) for r in rows]


def search(conn, query: str, limit: int = 50) -> dict:
    """
    Smart lookup for the 'currently adding' hover-search. Matches titles,
    albums, and artists; returns grouped results so you can add a single track,
    a whole album, or every album by an artist.
    """
    q = query.strip()
    if not q:
        return {"tracks": [], "albums": [], "artists": []}
    fts_q = " ".join(f'{tok}*' for tok in q.split())
    track_rows = conn.execute(
        "SELECT t.id, t.rel_path, t.title, t.album, t.artist, t.year, t.genre "
        "FROM tracks_fts f JOIN tracks t ON t.id = f.track_id "
        "WHERE tracks_fts MATCH ? ORDER BY rank LIMIT ?",
        (fts_q, limit),
    ).fetchall()
    tracks = [dict(r) for r in track_rows]

    seen_alb, albums = set(), []
    seen_art, artists = set(), []
    for t in tracks:
        ak = (t["artist"], t["album"], t["year"])
        if ak not in seen_alb:
            seen_alb.add(ak)
            albums.append({"artist": t["artist"], "album": t["album"], "year": t["year"]})
        if t["artist"] not in seen_art:
            seen_art.add(t["artist"])
            artists.append({"artist": t["artist"]})
    return {"tracks": tracks, "albums": albums, "artists": artists}
