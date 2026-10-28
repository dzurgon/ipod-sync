"""Playlist write-path + download endpoints, against the sample library."""

import io
import importlib
import subprocess
import sys
import zipfile
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
    from app import config, main
    importlib.reload(config); main = importlib.reload(main)
    main._scan_and_sync()
    with TestClient(main.app) as c:
        c.pls = pls
        yield c


def test_legacy_m3u_imported(client):
    pls = client.get("/api/playlists").json()["playlists"]
    legacy = next(p for p in pls if p["name"] == "Legacy")
    assert legacy["n_tracks"] == 1 and legacy["n_missing"] == 0


def test_playlist_crud_roundtrips_to_m3u(client):
    p = client.post("/api/playlists", json={"name": "Road Trip!"}).json()
    assert p["name"] == "Road Trip"
    m3u = client.pls / "Road Trip.m3u"
    assert m3u.read_text() == "#EXTM3U\n"

    folder = "0_POP/Charli XCX/BRAT (2025)"
    assert client.post(f"/api/playlists/{p['id']}/tracks", json={"folder": folder}).json()["added"] == 2
    assert client.post(f"/api/playlists/{p['id']}/tracks", json={"folder": folder}).json()["added"] == 0
    lines = m3u.read_text().splitlines()[1:]
    assert lines == [f"{folder}/01 - 360.flac", f"{folder}/02 - Club classics.flac"]

    items = client.get(f"/api/playlists/{p['id']}").json()["items"]
    client.post(f"/api/playlists/{p['id']}/items/{items[1]['item_id']}/move", json={"delta": -1})
    assert m3u.read_text().splitlines()[1].endswith("Club classics.flac")
    client.delete(f"/api/playlists/{p['id']}/items/{items[0]['item_id']}")
    assert len(m3u.read_text().splitlines()) == 2

    assert client.post("/api/playlists", json={"name": "road trip"}).status_code == 409
    client.delete(f"/api/playlists/{p['id']}")
    assert not m3u.exists() and (client.pls / "Road Trip.m3u.deleted").exists()


def test_download_track_and_album(client):
    r = client.get("/download/track/0_POP/Charli XCX/BRAT (2025)/01 - 360.flac")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert client.get("/download/track/../../etc/passwd").status_code == 404

    r = client.get("/download/album", params={"folder": "0_POP/Charli XCX/BRAT (2025)"})
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert "Charli XCX - BRAT (2025)/01 - 360.flac" in z.namelist()
    assert z.read("Charli XCX - BRAT (2025)/01 - 360.flac") == b"fLaC01 - 360.flac"


def test_download_playlist_zip(client):
    pid = next(p["id"] for p in client.get("/api/playlists").json()["playlists"] if p["name"] == "Legacy")
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/download/playlist/{pid}").content))
    names = z.namelist()
    assert "Legacy/01 - yeule - sulky baby.flac" in names
    assert z.read("Legacy/Legacy.m3u").decode().splitlines() == ["#EXTM3U", "01 - yeule - sulky baby.flac"]
