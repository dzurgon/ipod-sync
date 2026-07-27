"""
SQLite index for Aquaria.

The index is a *projection cache* of the on-disk library plus the app's own
mutable state (users, playlists, linked iPods, activity). The music files on
disk remain the source of truth for audio; this DB is the source of truth for
identity, relationships, and everything the web app needs to answer instantly
without re-walking /mnt/data on every request.

Design choices:
  * A single canonical `tracks` row per audio file (keyed by rel_path).
  * artists / albums / genres are derived dimensions, deduped by normalized key,
    so the four Library views are pure SQL over one table.
  * Playlist entries store BOTH the raw line (for round-tripping the .m3u/.txt)
    and a resolved track_id when we can match it — this is the "unity" between
    messy legacy playlists and the restructured library.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── Library (rebuilt by the scanner) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tracks (
    id            INTEGER PRIMARY KEY,
    rel_path      TEXT UNIQUE NOT NULL,     -- relative to MUSIC_ROOT
    genre         TEXT,                     -- '0_' prefix stripped
    genre_order   INTEGER,
    artist        TEXT NOT NULL DEFAULT '',
    artist_key    TEXT NOT NULL DEFAULT '', -- normalized for dedupe/match
    artist_inferred INTEGER NOT NULL DEFAULT 0,
    album         TEXT NOT NULL DEFAULT '',
    album_key     TEXT NOT NULL DEFAULT '',
    year          INTEGER,
    disc_no       INTEGER,
    track_no      INTEGER,
    title         TEXT NOT NULL DEFAULT '',
    ext           TEXT,
    size_bytes    INTEGER,
    mtime         REAL
);
CREATE INDEX IF NOT EXISTS idx_tracks_genre  ON tracks(genre_order, genre);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist_key);
CREATE INDEX IF NOT EXISTS idx_tracks_album  ON tracks(artist_key, album_key, year);

-- FTS for the smart search / "currently adding" lookup.
-- Standalone (not external-content) FTS5 table, rebuilt on each scan.
CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
    title, album, artist, genre, track_id UNINDEXED
);

-- Artist images (circle-cropped avatar), stored as case-insensitive artist.*
-- inside the artist folder. We just cache the rel_path here.
CREATE TABLE IF NOT EXISTS artist_images (
    artist_key TEXT PRIMARY KEY,
    rel_path   TEXT NOT NULL
);

-- ── App state (persists across scans) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    username   TEXT UNIQUE NOT NULL,        -- no password, per spec
    settings   TEXT NOT NULL DEFAULT '{}',  -- JSON: backdrop, ipod layout pref, etc.
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ipods (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,            -- friendly name, e.g. "iPod Video"
    signature     TEXT UNIQUE NOT NULL,     -- stable hardware id (serial/FirewireGuid)
    metadata      TEXT NOT NULL DEFAULT '{}',
    layout        TEXT NOT NULL DEFAULT 'genre',  -- 'genre' | 'flat'
    last_seen     REAL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    is_global  INTEGER NOT NULL DEFAULT 1,  -- visible to other users (default yes)
    file_path  TEXT,                        -- backing .m3u/.txt if any
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_items (
    id          INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    raw_line    TEXT NOT NULL,              -- original text line (round-trip)
    track_id    INTEGER REFERENCES tracks(id) ON DELETE SET NULL,  -- resolved match
    resolved    INTEGER NOT NULL DEFAULT 0  -- 0=unresolved, 1=exact, 2=fuzzy
);

CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    ipod_id    INTEGER REFERENCES ipods(id) ON DELETE SET NULL,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,               -- connect|disconnect|sync|playlist|scrobble
    message    TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_activity_user ON activity(user_id, ts DESC);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
