"""Runtime configuration (env-driven, Docker-friendly)."""

from __future__ import annotations

import json
import os
from pathlib import Path

MUSIC_ROOT = Path(os.environ.get("AQUARIA_MUSIC_ROOT", "/mnt/data/media/music/FLAC"))
PLAYLIST_DIR = Path(os.environ.get("AQUARIA_PLAYLIST_DIR", "/mnt/data/media/music/Playlists"))
DB_PATH = Path(os.environ.get("AQUARIA_DB", "/data/aquaria.db"))
PORT = int(os.environ.get("AQUARIA_PORT", "8337"))

# Where genre-folder artwork is looked up, by genre label ("0_POP" -> "POP").
# Default for any genre is Deezer's genre of the same name ("deezer:POP" matches "Pop").
# "wikipedia:<page>" uses that article's lead image. Extend/override with JSON in
# AQUARIA_GENRE_ART, e.g. {"WORLD": "deezer:African Music"}.
GENRE_ART_SOURCES = {
    "HIPHOP-SOUL": "deezer:Rap/Hip Hop",
    "ELECTRONIC": "deezer:Electro",
    "KWUR": "wikipedia:KWUR",              # WashU college radio — its station logo
    **json.loads(os.environ.get("AQUARIA_GENRE_ART", "{}")),
}

# ── Accounts / integrations (secrets come from the environment, never the repo) ──
SESSION_DAYS = int(os.environ.get("AQUARIA_SESSION_DAYS", "30"))
# Shared secret the host-side ipod-sync script sends as X-Aquaria-Token. Unset = host API off.
HOST_TOKEN = os.environ.get("AQUARIA_HOST_TOKEN", "")
# Last.fm API account (https://www.last.fm/api/account/create) — enables "Connect Last.fm".
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")
LASTFM_API_SECRET = os.environ.get("LASTFM_API_SECRET", "")

# Owner for playlists imported from PLAYLIST_DIR / created before per-user login exists.
DEFAULT_USER = os.environ.get("AQUARIA_DEFAULT_USER", "jimin")

# Default aquamarine backdrop used when a user has not chosen one.
DEFAULT_BACKDROP = os.environ.get(
    "AQUARIA_DEFAULT_BACKDROP",
    "linear-gradient(160deg,#012a36 0%,#034e5c 38%,#0a7c86 72%,#3fd0c9 100%)",
)
