"""Playlist write-path + download endpoints, against the sample library."""

import io
import json
import zipfile
from pathlib import Path


def test_legacy_m3u_imported(client):
    pls = client.get("/api/playlists").json()["playlists"]
    legacy = next(p for p in pls if p["name"] == "Legacy")
    assert legacy["n_tracks"] == 1 and legacy["n_missing"] == 0


def test_playlist_crud_roundtrips_to_m3u(client):
    p = client.post("/api/playlists", json={"name": "Road Trip!"}).json()
    assert p["name"] == "Road Trip"
    m3u = client.pls / "jimin" / "Road Trip.m3u"
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
    assert not m3u.exists() and (client.pls / "jimin" / "Road Trip.m3u.deleted").exists()


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
    return {f["rel_dir"]: f for f in client.get("/api/folder-art").json()["artists"]}


def test_artist_folders_indexed(client):
    f = _folders(client)
    assert "0_FOLK/Pete Seeger" in f and f["0_FOLK/Pete Seeger"]["image"] is None
    assert not any("Passion Pit" in d for d in f)          # inferred artists have no folder


def test_same_name_convention_detected_on_scan(client):
    lib = Path(client.get("/healthz").json()["music_root"])
    (lib / "0_FOLK/Pete Seeger/pete seeger.JPG").write_bytes(JPG)   # case-insensitive
    client.post("/api/scan")
    assert _folders(client)["0_FOLK/Pete Seeger"]["image"] == "0_FOLK/Pete Seeger/pete seeger.JPG"


def test_art_added_on_disk_shows_without_scan(client):
    lib = Path(client.get("/healthz").json()["music_root"])
    genre = client.get("/api/folder-art").json()["genres"][0]["rel_dir"]
    img = lib / genre / f"{genre}.jpg"
    img.write_bytes(JPG)
    assert next(g for g in client.get("/api/folder-art").json()["genres"]
                if g["rel_dir"] == genre)["image"] == f"{genre}/{genre}.jpg"
    img.unlink()                                           # and removal is noticed too
    assert next(g for g in client.get("/api/folder-art").json()["genres"]
                if g["rel_dir"] == genre)["image"] is None


def test_scan_summary_counts(client):
    r = client.post("/api/scan").json()
    assert r["added"] == 0 and r["pruned"] == 0 and r["tracks"] > 0
    assert {"folder_images", "playlists", "seconds"} <= r.keys()


def test_upload_writes_convention_file(client):
    lib = Path(client.get("/healthz").json()["music_root"])
    r = client.post("/api/folder-art/upload", data={"rel_dir": "0_FOLK/Pete Seeger"},
                    files={"file": ("x.png", PNG, "image/png")})
    assert r.json()["image"] == "0_FOLK/Pete Seeger/Pete Seeger.png"
    # a second upload in another format replaces it rather than piling up
    client.post("/api/folder-art/upload", data={"rel_dir": "0_FOLK/Pete Seeger"},
                files={"file": ("x.jpg", JPG, "image/jpeg")})
    imgs = sorted(p.name for p in (lib / "0_FOLK/Pete Seeger").iterdir() if p.is_file())
    assert imgs == ["Pete Seeger.jpg"]
    pete = next(a for a in client.get("/api/library?view=artist").json()["items"]
                if a["artist"] == "Pete Seeger")
    assert pete["image"] == "0_FOLK/Pete Seeger/Pete Seeger.jpg"

    bad = client.post("/api/folder-art/upload", data={"rel_dir": "0_FOLK/Pete Seeger"},
                      files={"file": ("x.txt", b"hello", "text/plain")})
    assert bad.status_code == 400
    assert client.post("/api/folder-art/upload", data={"rel_dir": "../../etc"},
                       files={"file": ("x.png", PNG, "image/png")}).status_code == 404


def test_fetch_uses_exact_match_only(client, monkeypatch):
    from app import folder_art
    calls = []
    def fake_get(url, timeout=15):
        calls.append(url)
        if "api.deezer.com" in url:
            return json.dumps({"data": [
                {"name": "Pete Seeger & Friends", "nb_fan": 9999, "picture_xl": "https://x/artist/aaa/1.jpg"},
                {"name": "pete seeger", "nb_fan": 10, "picture_xl": "https://x/artist/bbb/1.jpg"},
                {"name": "Pete Seeger", "nb_fan": 5, "picture_xl": "https://x/images/artist//1000x1000-0.jpg"},
            ]}).encode()
        return JPG
    monkeypatch.setattr(folder_art, "_get", fake_get)
    r = client.post("/api/folder-art/fetch", json={"rel_dir": "0_FOLK/Pete Seeger"})
    assert r.json()["image"] == "0_FOLK/Pete Seeger/Pete Seeger.jpg"
    assert calls[-1] == "https://x/artist/bbb/1.jpg"       # exact name, real photo, not the placeholder

    monkeypatch.setattr(folder_art, "_get", lambda url, timeout=15: b'{"data": []}')
    assert client.post("/api/folder-art/fetch", json={"rel_dir": "0_POP/Charli XCX"}).status_code == 404


def test_genre_folders_indexed_and_fetched(client, monkeypatch):
    from app import config, folder_art
    g = {x["rel_dir"]: x for x in client.get("/api/folder-art").json()["genres"]}
    assert g["0_POP"]["genre"] == "POP" and g["0_POP"]["image"] is None

    monkeypatch.setitem(config.GENRE_ART_SOURCES, "JAZZ", "wikipedia:KWUR")
    def fake_get(url, timeout=15):
        if url.endswith("/genre"):
            return json.dumps({"data": [
                {"name": "Pop", "picture_xl": "https://x/images/misc/pop/1000x1000-0.jpg"}]}).encode()
        if "wikipedia.org" in url:
            assert url.endswith("/summary/KWUR")
            return json.dumps({"originalimage": {"source": "https://w/Kwur_splotch.png"}}).encode()
        return PNG if url.endswith(".png") else JPG
    monkeypatch.setattr(folder_art, "_get", fake_get)

    r = client.post("/api/folder-art/fetch", json={"rel_dir": "0_POP"})
    assert r.json()["image"] == "0_POP/0_POP.jpg"                  # Deezer "Pop" by label
    r = client.post("/api/folder-art/fetch", json={"rel_dir": "0_JAZZ"})
    assert r.json()["image"] == "0_JAZZ/0_JAZZ.png"                # wikipedia alias, png kept
    assert client.post("/api/folder-art/fetch", json={"rel_dir": "0_FOLK"}).status_code == 404
    # the genre's own artwork must not be mistaken for anything else on rescan
    client.post("/api/scan")
    g = {x["rel_dir"]: x for x in client.get("/api/folder-art").json()["genres"]}
    assert g["0_POP"]["image"] == "0_POP/0_POP.jpg"
    assert client.get("/api/stats").json()["tracks"] == 14


def test_album_cover_upload_becomes_baseline_jpeg(client):
    from PIL import Image
    lib = Path(client.get("/healthz").json()["music_root"])
    album = "0_POP/Charli XCX/BRAT (2025)"
    (lib / album / "cover.png").write_bytes(PNG)              # an older cover in another format
    buf = io.BytesIO()
    Image.new("RGBA", (2000, 1500), (138, 206, 0, 255)).save(buf, "PNG")

    r = client.post("/api/folder-art/upload", data={"rel_dir": album},
                    files={"file": ("brat.png", buf.getvalue(), "image/png")})
    assert r.json()["image"] == f"{album}/cover.jpg"
    covers = sorted(p.name for p in (lib / album).iterdir() if p.name.startswith("cover"))
    assert covers == ["cover.jpg"]
    img = Image.open(lib / album / "cover.jpg")
    assert img.format == "JPEG" and img.size == (1000, 750) and not img.info.get("progressive")
    assert client.get("/art/" + album).content == (lib / album / "cover.jpg").read_bytes()

    # artist-less album folders count as albums too; folders above albums are not albums
    r = client.post("/api/folder-art/upload", data={"rel_dir": "0_POP/Passion Pit - Gossamer (2013)"},
                    files={"file": ("x.jpg", buf.getvalue(), "image/png")})
    assert r.status_code == 200
    assert client.post("/api/folder-art/fetch", json={"rel_dir": album}).status_code == 400
    assert client.post("/api/folder-art/upload", data={"rel_dir": "0_POP/Charli"},
                       files={"file": ("x.png", PNG, "image/png")}).status_code == 404


def test_page_urls_serve_the_app(client):
    for url in ("/library/0_FOLK/Pete Seeger", "/album/0_POP/Charli XCX/BRAT (2025)",
                "/artist/Charli XCX", "/playlists", "/playlist/1", "/albums"):
        r = client.get(url)
        assert r.status_code == 200 and "<title>Aquaria" in r.text, url
    assert client.get("/api/stats").headers["content-type"].startswith("application/json")
    assert client.get("/nope").status_code == 404
