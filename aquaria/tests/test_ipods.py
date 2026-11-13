"""iPod linking, sync profiles, plans and the host API."""

import json
import shutil

H = {"X-Aquaria-Token": "host-secret"}
SIG = "000A27001A2B3C4D"


def seen(c, sig=SIG):
    return c.post(f"/api/host/ipods/{sig}/seen", headers=H,
                  json={"metadata": {"model": "iPod Video 5.5G", "rockbox": "4.0"}}).json()


def link(c, sig=SIG, name="Ben's iPod"):
    return c.post("/api/ipods", json={"signature": sig, "name": name}).json()


def test_host_api_needs_token(client):
    assert client.post(f"/api/host/ipods/{SIG}/seen", json={}).status_code == 401
    assert client.get(f"/api/host/ipods/{SIG}/plan", headers={"X-Aquaria-Token": "x"}).status_code == 401


def test_unknown_ipod_is_listed_then_linked(client, other):
    assert seen(client) == {"linked": False}
    un = client.get("/api/ipods").json()["unclaimed"]
    assert [u["signature"] for u in un] == [SIG] and un[0]["metadata"]["model"] == "iPod Video 5.5G"
    assert client.get(f"/api/host/ipods/{SIG}/plan", headers=H).status_code == 404

    ip = link(client)
    assert ip["sync"]["music"] == "all" and ip["layout"] == "genre"
    assert seen(client)["owner"] == "jimin"
    assert client.get("/api/ipods").json()["unclaimed"] == []
    other.post("/api/auth/register", json={"username": "roomie", "password": "correct horse"})
    assert other.post("/api/ipods", json={"signature": SIG, "name": "mine!"}).status_code == 409
    assert other.patch(f"/api/ipods/{ip['id']}", json={"name": "x"}).status_code == 404


def test_plan_all_genre_layout(client):
    seen(client); link(client)
    plan = client.get(f"/api/host/ipods/{SIG}/plan", headers=H).json()
    src = {m["src"] for m in plan["music"]}
    assert "0_POP/Charli XCX/BRAT (2025)" in src                 # album root, not single files
    assert "0_POP/Passion Pit - Gossamer (2013)" in src          # artist-less album folder
    assert all(m["src"] == m["dst"] for m in plan["music"])
    assert plan["summary"]["tracks"] == 14
    legacy = next(p for p in plan["playlists"] if p["name"] == "Legacy")
    assert legacy["m3u"] == "#EXTM3U\n/FLAC/0_ELECTRONIC/yeule/softscars (2023)/02. sulky baby.flac\n"
    assert plan["scrobble"] == {"enabled": False, "lastfm_user": None, "session_key": None}


def test_plan_includes_albums_added_since_last_scan(client, tmp_path):
    seen(client); link(client)
    shutil.copytree(tmp_path / "lib/0_POP/Charli XCX/BRAT (2025)", tmp_path / "lib/0_WORLD/Boukan Ginen/Jou A Rive")
    shutil.rmtree(tmp_path / "lib/0_POP/Passion Pit - Gossamer (2013)")
    src = {m["src"] for m in client.get(f"/api/host/ipods/{SIG}/plan", headers=H).json()["music"]}
    assert "0_WORLD/Boukan Ginen/Jou A Rive" in src
    assert "0_POP/Passion Pit - Gossamer (2013)" not in src


def test_plan_selected_flat_and_scrobble(client, other):
    seen(client); ip = link(client)
    other.post("/api/auth/register", json={"username": "roomie", "password": "correct horse"})
    theirs = other.post("/api/playlists", json={"name": "Folk Mix"}).json()
    other.post(f"/api/playlists/{theirs['id']}/tracks", json={"folder": "0_FOLK/Pete Seeger/We Shall Overcome (1963)"})

    r = client.patch(f"/api/ipods/{ip['id']}", json={"layout": "flat", "sync": {
        "music": "selected", "genres": ["0_ROCK"], "own_playlists": False,
        "playlists": [theirs["id"]], "scrobble": True}})
    assert r.status_code == 200 and r.json()["summary"]["albums"] == 2   # ROCK album + Folk Mix album

    from app import state
    state.conn.execute("UPDATE users SET lastfm_user='jfm', lastfm_session='sk1' WHERE username='jimin'")
    plan = client.get(f"/api/host/ipods/{SIG}/plan", headers=H).json()
    dst = {m["dst"] for m in plan["music"]}
    assert dst == {"Radiohead/OK Computer (1997)", "Pete Seeger/We Shall Overcome (1963)"}
    assert [p["name"] for p in plan["playlists"]] == ["Folk Mix"]
    assert "/FLAC/Pete Seeger/We Shall Overcome (1963)/" in plan["playlists"][0]["m3u"]
    assert plan["scrobble"] == {"enabled": True, "lastfm_user": "jfm", "session_key": "sk1"}

    # their playlist going private drops it from my iPod
    other.patch(f"/api/playlists/{theirs['id']}", json={"is_public": False})
    assert client.get(f"/api/host/ipods/{SIG}/plan", headers=H).json()["playlists"] == []


def test_pull_playlist_from_ipod_and_report(client):
    seen(client); ip = link(client)
    r = client.post(f"/api/host/ipods/{SIG}/playlists", headers=H, json={
        "name": "made on ipod", "lines": ["/FLAC/0_POP/Charli XCX/BRAT (2025)/01 - 360.flac"]})
    pl = client.get(f"/api/playlists/{r.json()['id']}").json()
    assert pl["is_public"] is False and pl["items"][0]["title"] == "360"
    assert (client.pls / "jimin" / "made on ipod.m3u").exists()
    act = client.get("/api/activity").json()["activity"][0]
    assert act["detail"] == {"playlist_id": pl["id"], "playlist": "made on ipod"}   # UI links it

    client.post(f"/api/host/ipods/{SIG}/report", headers=H,
                json={"status": "ok", "message": "Synced 14 tracks", "stats": {"tracks": 14}})
    mine = client.get("/api/ipods").json()["ipods"][0]
    assert mine["last_status"] == "ok: Synced 14 tracks"
    kinds = [a["kind"] for a in client.get("/api/activity").json()["activity"]]
    assert kinds == ["sync", "playlist", "link"]     # sighting before linking logs nothing for the owner


def test_sync_in_progress_then_result(client):
    seen(client); link(client)
    client.post(f"/api/host/ipods/{SIG}/report", headers=H, json={"status": "syncing"})
    mine = client.get("/api/ipods").json()["ipods"][0]
    assert mine["host"]["state"] == "syncing" and mine["host"]["sync_started"]
    assert mine["last_sync"] is None and mine["last_status"] is None   # not a result yet

    client.post(f"/api/host/ipods/{SIG}/report", headers=H, json={"status": "ok", "message": "Synced"})
    mine = client.get("/api/ipods").json()["ipods"][0]
    assert mine["host"]["state"] == "ok" and mine["host"]["sync_started"] is None
    assert mine["last_status"] == "ok: Synced"
    msgs = [a["message"] for a in client.get("/api/activity").json()["activity"]]
    assert msgs[0] == "Synced" and msgs[1].startswith("Sync of ") and msgs[1].endswith(" started")
    for live in ("unmounting", "safe"):                      # state only, result kept
        client.post(f"/api/host/ipods/{SIG}/report", headers=H, json={"status": live})
        mine = client.get("/api/ipods").json()["ipods"][0]
        assert mine["host"]["state"] == live and mine["last_status"] == "ok: Synced"
    assert len(client.get("/api/activity").json()["activity"]) == len(msgs)
    assert client.post(f"/api/host/ipods/{SIG}/report", headers=H,
                       json={"status": "bogus"}).status_code == 422


def test_rockbox_m3u8_paths_imported(client):
    seen(client); link(client)
    r = client.post(f"/api/host/ipods/{SIG}/playlists", headers=H, json={
        "name": "SUM", "lines": ["/<HDD0>/FLAC/0_POP/Charli XCX/BRAT (2025)/01 - 360.flac"]})
    pl = client.get(f"/api/playlists/{r.json()['id']}").json()
    assert [i["title"] for i in pl["items"]] == ["360"]


def test_unlink(client):
    seen(client); ip = link(client)
    client.delete(f"/api/ipods/{ip['id']}")
    assert client.get("/api/ipods").json()["ipods"] == []
    assert [u["signature"] for u in client.get("/api/ipods").json()["unclaimed"]] == [SIG]


def test_same_ipod_under_another_serial_is_not_a_new_device(client):
    rockbox, apple = "300000000000A2700149D8299", "000A2700149D8299"   # one iPod, two firmwares
    meta = lambda serial, fs="5906-AD78": {"metadata": {"usb_serial": serial, "fs_uuid": fs}}
    client.post(f"/api/host/ipods/{rockbox}/seen", headers=H, json=meta(rockbox))
    link(client, rockbox, "JiminPod")

    r = client.post(f"/api/host/ipods/{apple}/seen", headers=H, json=meta(apple)).json()
    assert r == {"linked": True, "name": "JiminPod", "owner": "jimin"}
    assert client.get(f"/api/host/ipods/{apple}/plan", headers=H).status_code == 200
    mine = client.get("/api/ipods").json()
    assert mine["unclaimed"] == [] and [i["name"] for i in mine["ipods"]] == ["JiminPod"]
    assert client.post("/api/ipods", json={"signature": apple, "name": "dupe"}).status_code == 409

    client.post(f"/api/host/ipods/{apple}/report", headers=H, json={"status": "syncing"})
    assert client.get("/api/ipods").json()["ipods"][0]["host"]["state"] == "syncing"

    # A different iPod (other disk, unrelated serial) is still new.
    client.post("/api/host/ipods/000A2700FFFF0000/seen", headers=H, json=meta("000A2700FFFF0000", "1234-ABCD"))
    assert [u["signature"] for u in client.get("/api/ipods").json()["unclaimed"]] == ["000A2700FFFF0000"]


def test_playlist_cover_shows_in_app_and_travels_to_the_ipod(client, other):
    import io
    from PIL import Image
    png = io.BytesIO(); Image.new("RGB", (1600, 1200), "teal").save(png, "PNG")
    seen(client); link(client)
    pid = client.post("/api/playlists", json={"name": "Night Drive", "is_public": False}).json()["id"]

    assert client.post(f"/api/playlists/{pid}/cover", files={"file": ("x.txt", b"nope")}).status_code == 400
    pl = client.post(f"/api/playlists/{pid}/cover", files={"file": ("c.png", png.getvalue())}).json()
    assert pl["cover"]
    jpg = client.get(f"/playlist-cover/{pid}")
    assert jpg.headers["content-type"] == "image/jpeg"
    img = Image.open(io.BytesIO(jpg.content))
    assert img.format == "JPEG" and max(img.size) <= 1000 and not img.info.get("progressive")
    assert other.get(f"/playlist-cover/{pid}").status_code == 404         # private playlist
    assert other.post(f"/api/playlists/{pid}/cover", files={"file": ("c.png", png.getvalue())}).status_code == 401

    entry = next(p for p in client.get(f"/api/host/ipods/{SIG}/plan", headers=H).json()["playlists"]
                 if p["id"] == pid)
    assert entry["cover"] is True
    assert client.get(f"/api/host/ipods/{SIG}/playlists/{pid}/cover", headers=H).content == jpg.content

    assert client.delete(f"/api/playlists/{pid}/cover").json()["cover"] is None
    assert client.get(f"/playlist-cover/{pid}").status_code == 404
    assert client.get(f"/api/host/ipods/{SIG}/playlists/{pid}/cover", headers=H).status_code == 404


def test_ipod_gets_small_baseline_artwork(client, tmp_path):
    import io
    from PIL import Image
    seen(client); link(client)
    album = tmp_path / "lib/0_POP/Charli XCX/BRAT (2025)"
    Image.new("RGB", (2800, 2800), "lime").save(album / "cover.jpg", "JPEG", progressive=True)
    Image.new("RGB", (6000, 4000), "red").save(album / "back.png")

    url = f"/api/host/ipods/{SIG}/art/0_POP/Charli XCX/BRAT (2025)/"
    for name, size in (("cover.jpg", (320, 320)), ("back.png", (320, 213))):
        r = client.get(url + name, headers=H)
        img = Image.open(io.BytesIO(r.content))
        assert r.headers["content-type"] == "image/jpeg" and img.format == "JPEG"
        assert img.size == size and not img.info.get("progressive")
    assert client.get(url + "../../../../data/aquaria.db", headers=H).status_code == 404
    assert client.get(f"/api/host/ipods/{SIG}/art/" + "0_POP/Charli XCX/BRAT (2025)/01 - 360.flac",
                      headers=H).status_code == 404
    assert client.get(f"/api/host/ipods/NOPE/art/0_POP/Charli XCX/BRAT (2025)/cover.jpg",
                      headers=H).status_code == 404
    assert len(list((tmp_path / "data/ipod-art").glob("*.jpg"))) == 2
