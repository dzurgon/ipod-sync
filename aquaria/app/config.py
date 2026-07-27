"""Runtime configuration (env-driven, Docker-friendly)."""

from __future__ import annotations

import os
from pathlib import Path

MUSIC_ROOT = Path(os.environ.get("AQUARIA_MUSIC_ROOT", "/mnt/data/media/music/FLAC"))
DB_PATH = Path(os.environ.get("AQUARIA_DB", "/data/aquaria.db"))
PORT = int(os.environ.get("AQUARIA_PORT", "8337"))

# Default aquamarine backdrop used when a user has not chosen one.
DEFAULT_BACKDROP = os.environ.get(
    "AQUARIA_DEFAULT_BACKDROP",
    "linear-gradient(160deg,#012a36 0%,#034e5c 38%,#0a7c86 72%,#3fd0c9 100%)",
)
