"""Accounts, sessions, profiles and playlist privacy."""


def register(c, name, pw="correct horse battery"):
    return c.post("/api/auth/register", json={"username": name, "password": pw})


def test_register_login_logout(other):
    assert other.get("/api/auth/me").json()["user"] is None
    r = register(other, "roomie")
    assert r.status_code == 200 and not r.json()["claimed"]
    assert other.get("/api/auth/me").json()["user"]["username"] == "roomie"
    other.post("/api/auth/logout")
    assert other.get("/api/auth/me").json()["user"] is None
    assert other.post("/api/auth/login", json={"username": "ROOMIE", "password": "nope"}).status_code == 401
    assert other.post("/api/auth/login", json={"username": "ROOMIE",
                                               "password": "correct horse battery"}).status_code == 200


def test_register_rules(other):
    assert register(other, "jimin").status_code == 409           # claimed already by the fixture
    assert register(other, "x").status_code == 400                # too short
    assert register(other, "ok_name", "short").status_code == 400
    assert register(other, "bad name!").status_code == 400


def test_password_never_stored_plain(client):
    from app import state
    h = state.conn.execute("SELECT password_hash FROM users WHERE username='jimin'").fetchone()[0]
    assert h.startswith("scrypt$") and "hunter2" not in h
    from app.accounts import check_password
    assert check_password("hunter2hunter2", h) and not check_password("hunter2", h)


def test_guests_read_but_cannot_write(other):
    assert other.get("/api/library?view=artist").status_code == 200
    assert other.get("/api/playlists").status_code == 200
    assert other.post("/api/playlists", json={"name": "x"}).status_code == 401
    assert other.post("/api/scan").status_code == 401
    assert other.post("/api/folder-art/fetch-missing").status_code == 401
    assert other.get("/api/ipods").status_code == 401


def test_private_playlists_and_ownership(client, other):
    priv = client.post("/api/playlists", json={"name": "Secret", "is_public": False}).json()
    pub = client.post("/api/playlists", json={"name": "Shared"}).json()
    assert (client.pls / "jimin" / "Secret.m3u").exists()

    register(other, "roomie")
    names = {p["name"] for p in other.get("/api/playlists").json()["playlists"]}
    assert "Shared" in names and "Secret" not in names
    assert other.get(f"/api/playlists/{priv['id']}").status_code == 404
    assert other.get(f"/download/playlist/{priv['id']}").status_code == 404
    # public but not theirs: readable, not editable
    assert other.get(f"/api/playlists/{pub['id']}").json()["is_mine"] is False
    assert other.post(f"/api/playlists/{pub['id']}/tracks",
                      json={"folder": "0_POP/Charli XCX/BRAT (2025)"}).status_code == 403
    assert other.delete(f"/api/playlists/{pub['id']}").status_code == 403
    # same name is fine for a different user
    assert other.post("/api/playlists", json={"name": "Shared"}).status_code == 200
    assert (other.pls / "roomie" / "Shared.m3u").exists()

    # owner flips privacy
    client.patch(f"/api/playlists/{priv['id']}", json={"is_public": True})
    assert other.get(f"/api/playlists/{priv['id']}").status_code == 200


def test_profiles(client, other):
    client.post("/api/playlists", json={"name": "Hidden", "is_public": False})
    me = client.get("/api/users/jimin").json()
    assert me["is_me"] and {"Hidden", "Legacy"} <= {p["name"] for p in me["playlists"]}
    register(other, "roomie")
    them = other.get("/api/users/jimin").json()
    assert not them["is_me"] and "Hidden" not in {p["name"] for p in them["playlists"]}
    users = {u["username"]: u for u in other.get("/api/users").json()["users"]}
    assert users["jimin"]["claimed"] and users["roomie"]["n_public"] == 0
    assert other.get("/api/users/nobody").status_code == 404


def test_lastfm_callback_stores_session(client, monkeypatch):
    from app import accounts
    monkeypatch.setattr(accounts, "lastfm_get_session", lambda token: ("jimin_fm", "sk-" + token))
    r = client.get("/lastfm/callback?token=abc", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/user/jimin"
    assert client.get("/api/users/jimin").json()["lastfm_user"] == "jimin_fm"
    client.delete("/api/me/lastfm")
    assert client.get("/api/users/jimin").json()["lastfm_user"] is None
