"""Process-wide state shared by the routers: the SQLite connection and the writer lock."""

from __future__ import annotations

import threading

from . import config, db

# Use the configured DB path when its directory exists (the /data volume in
# Docker); otherwise fall back to a local file for dev runs outside a container.
conn = db.connect(config.DB_PATH if config.DB_PATH.parent.exists() else "aquaria.db")

# One shared connection; serialise writers (scan, playlist edits, …) against each other.
write_lock = threading.Lock()
