"""Playlist write-path + download endpoints, against the sample library."""

import io
import json
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


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 32


def _folders(client):
    return {f["rel_dir"]: f for f in client.get("/api/artist-folders").json()["folders"]}


def test_artist_folders_indexed(client):
    f = _folders(client)
    assert "0_FOLK/Pete Seeger" in f and f["0_FOLK/Pete Seeger"]["image"] is None
    assert not any("Passion Pit" in d for d in f)          # inferred artists have no folder


def test_same_name_convention_detected_on_scan(client):
    lib = Path(client.get("/healthz").json()["music_root"])
    (lib / "0_FOLK/Pete Seeger/pete seeger.JPG").write_bytes(JPG)   # case-insensitive
    client.post("/api/scan")
    assert _folders(client)["0_FOLK/Pete Seeger"]["image"] == "0_FOLK/Pete Seeger/pete seeger.JPG"


def test_upload_writes_convention_file(client):
    lib = Path(client.get("/healthz").json()["music_root"])
    r = client.post("/api/artist-folders/upload", data={"rel_dir": "0_FOLK/Pete Seeger"},
                    files={"file": ("x.png", PNG, "image/png")})
    assert r.json()["image"] == "0_FOLK/Pete Seeger/Pete Seeger.png"
    # a second upload in another format replaces it rather than piling up
    client.post("/api/artist-folders/upload", data={"rel_dir": "0_FOLK/Pete Seeger"},
                files={"file": ("x.jpg", JPG, "image/jpeg")})
    imgs = sorted(p.name for p in (lib / "0_FOLK/Pete Seeger").iterdir() if p.is_file())
    assert imgs == ["Pete Seeger.jpg"]
    pete = next(a for a in client.get("/api/library?view=artist").json()["items"]
                if a["artist"] == "Pete Seeger")
    assert pete["image"] == "0_FOLK/Pete Seeger/Pete Seeger.jpg"

    bad = client.post("/api/artist-folders/upload", data={"rel_dir": "0_FOLK/Pete Seeger"},
                      files={"file": ("x.txt", b"hello", "text/plain")})
    assert bad.status_code == 400
    assert client.post("/api/artist-folders/upload", data={"rel_dir": "../../etc"},
                       files={"file": ("x.png", PNG, "image/png")}).status_code == 404


def test_fetch_uses_exact_match_only(client, monkeypatch):
    from app import artist_images
    calls = []
    def fake_get(url, timeout=15):
        calls.append(url)
        if "api.deezer.com" in url:
            return json.dumps({"data": [
                {"name": "Pete Seeger & Friends", "nb_fan": 9999, "picture_xl": "https://x/artist/aaa/1.jpg"},
                {"name": "pete seeger", "nb_fan": 10, "picture_xl": "https://x/artist/bbb/1.jpg"},
                {"name": "Pete Seeger", "nb_fan": 5, "picture_xl": "https://x/artist//1.jpg"},
            ]}).encode()
        return JPG
    monkeypatch.setattr(artist_images, "_get", fake_get)
    r = client.post("/api/artist-folders/fetch", json={"rel_dir": "0_FOLK/Pete Seeger"})
    assert r.json()["image"] == "0_FOLK/Pete Seeger/Pete Seeger.jpg"
    assert calls[-1] == "https://x/artist/bbb/1.jpg"       # exact name, real photo, not the placeholder

    monkeypatch.setattr(artist_images, "_get", lambda url, timeout=15: b'{"data": []}')
    assert client.post("/api/artist-folders/fetch", json={"rel_dir": "0_POP/Charli XCX"}).status_code == 404
