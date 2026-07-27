"""End-to-end: build sample tree -> scan -> assert the four Library views."""

import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import db, indexer, library, playlists


def build(tmp_path):
    lib = tmp_path / "lib"
    subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "scripts" / "make_sample_library.py"), str(lib)],
        check=True,
    )
    conn = db.connect(tmp_path / "test.db")
    summary = indexer.scan(conn, lib)
    return conn, summary, lib


def test_scan_counts(tmp_path):
    conn, summary, _ = build(tmp_path)
    assert summary["ok"]
    assert summary["tracks"] == 14  # matches make_sample_library TRACKS


def test_genre_view(tmp_path):
    conn, _, _ = build(tmp_path)
    buckets = library.view_genre(conn)
    names = {b["genre"] for b in buckets}
    assert "POP" in names and "FOLK" in names
    # flat (no-genre) tracks are surfaced, not dropped
    assert "(no genre)" in names


def test_artist_view_pete_seeger(tmp_path):
    conn, _, _ = build(tmp_path)
    artists = {a["artist"] for a in library.view_artist(conn)}
    assert "Pete Seeger" in artists
    assert "0_FOLK" not in artists           # the old bug
    assert "Passion Pit" in artists          # artist-less surfaced by parse


def test_artist_avatar_detected(tmp_path):
    conn, _, _ = build(tmp_path)
    charli = next(a for a in library.view_artist(conn) if a["artist"] == "Charli XCX")
    assert charli["image"] and charli["image"].lower().endswith(("artist.jpg", "artist.png"))


def test_all_albums_labels(tmp_path):
    conn, _, _ = build(tmp_path)
    labels = {a["display_all_albums"] for a in library.view_all_albums(conn)}
    assert "Charli XCX - BRAT (2025)" in labels
    assert "Passion Pit - Gossamer (2013)" in labels


def test_search(tmp_path):
    conn, _, _ = build(tmp_path)
    res = library.search(conn, "sulky")
    assert any(t["title"] == "sulky baby" for t in res["tracks"])


def test_legacy_playlist_resolves(tmp_path):
    conn, _, _ = build(tmp_path)
    # messy legacy line should resolve to the restructured softscars track
    tid, flag = playlists.resolve_line(conn, "(2023) yeule - softscars [FLAC]/02. sulky baby.flac")
    assert tid is not None and flag in (1, 2)
    row = conn.execute("SELECT title FROM tracks WHERE id=?", (tid,)).fetchone()
    assert row["title"] == "sulky baby"


def test_ipod_flat_layout_strips_genre(tmp_path):
    conn, _, _ = build(tmp_path)
    tid = conn.execute("SELECT id FROM tracks WHERE title='360'").fetchone()["id"]
    assert playlists.ipod_path(conn, tid, "genre").startswith("0_POP/")
    assert playlists.ipod_path(conn, tid, "flat").startswith("Charli XCX/")
