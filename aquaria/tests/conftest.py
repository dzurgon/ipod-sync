"""Shared fixture: a TestClient over a fresh sample library, logged in as 'jimin'."""

import importlib
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def client(tmp_path, monkeypatch):
    lib, pls, data = tmp_path / "lib", tmp_path / "Playlists", tmp_path / "data"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample_library.py"), str(lib)], check=True)
    for f in lib.rglob("*.flac"):
        f.write_bytes(b"fLaC" + f.name.encode())   # non-empty so zips carry real bytes
    pls.mkdir(); data.mkdir()
    (pls / "Legacy.m3u").write_text("#EXTM3U\n(2023) yeule - softscars [FLAC]/02. sulky baby.flac\n")
    monkeypatch.setenv("AQUARIA_MUSIC_ROOT", str(lib))
    monkeypatch.setenv("AQUARIA_PLAYLIST_DIR", str(pls))
    monkeypatch.setenv("AQUARIA_DB", str(data / "aquaria.db"))
    monkeypatch.setenv("AQUARIA_HOST_TOKEN", "host-secret")
    from app import accounts, config, ipods, main, state
    for m in (config, state, accounts, ipods):
        importlib.reload(m)
    main = importlib.reload(main)
    main._scan_and_sync()
    with TestClient(main.app) as c:
        c.pls = pls
        # Claim the default account that owns the legacy playlists (as the real NUC user would).
        assert c.post("/api/auth/register", json={"username": "jimin", "password": "hunter2hunter2"}).json()["claimed"]
        yield c


@pytest.fixture
def other(client):
    """A second, independent browser session (its own cookie jar) on the same app."""
    c = TestClient(client.app)
    c.pls = client.pls
    return c
