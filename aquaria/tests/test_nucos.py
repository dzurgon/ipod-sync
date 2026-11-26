"""nucOS push of plays (Listening habit), against a fake nucOS. The round trip against
nucOS's real code is test_real_nucos.py."""

import datetime as dt
import json
import time

import pytest

H = {"X-Aquaria-Token": "host-secret"}
SIG = "300000000000A2700149D8299"


class FakeNucos:
    """Records calls; replies like nucOS (accepted / rejected / kept) or with a status code."""
    def __init__(self):
        self.calls, self.status, self.reject, self.kept = [], 200, set(), []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if self.status != 200:
            return self.status, {"detail": "nope"}
        if path.endswith("/me"):
            return 200, {"connection": {"id": "54a080769b", "app": "aquariapod", "name": "AquariaPod"},
                         "pack": {"id": "music.listening", "version": 1},
                         "habit": {"id": "habit.music", "name": "Listening", "sphere": "enrichment"}}
        items = body["items"]
        rej = [{"id": i["id"], "error": "bad"} for i in items if i["id"] in self.reject]
        return 200, {"accepted": len(items) - len(rej), "deleted": len(body["deleted"]), "rejected": rej,
                     "days": {}, "kept": self.kept}

    def pushes(self):
        return [b for m, p, b in self.calls if p.endswith("/activity")]


@pytest.fixture
def nuc(client, monkeypatch):
    from app import main, nucos, state
    monkeypatch.setenv("NUCOS_URL", "http://organizer:8000")
    monkeypatch.setenv("NUCOS_TOKEN", "nucos_secret_token")
    fake = FakeNucos()
    w = nucos.Worker(state.conn, state.write_lock,
                     client_factory=lambda: nucos.Client("http://organizer:8000", "x", transport=fake))
    monkeypatch.setattr(main, "NUCOS", None)          # tests drive the worker by hand
    client.post(f"/api/host/ipods/{SIG}/seen", headers=H, json={"metadata": {}})
    client.post("/api/ipods", json={"signature": SIG, "name": "JiminPod"})
    return fake, w


def track(client, q):
    return client.get(f"/api/search?q={q}").json()["tracks"][0]["rel_path"]


def ipod_lines(client, start_local):
    """Two listened plays + a skip, as Rockbox logs them (local clock time, ms)."""
    sulky, three60 = track(client, "sulky"), track(client, "360")
    return [f"{start_local}:200000:201000:/<HDD0>/FLAC/{sulky}",
            f"{start_local + 300}:150000:160000:/FLAC/{three60}",
            f"{start_local + 600}:5000:160000:/FLAC/{three60}"]


CHICAGO_LOCAL = 1_763_000_000          # 2025-11-13 02:13 on the iPod's (Chicago) clock


def test_not_connected_without_env(client, monkeypatch):
    from app import nucos, state
    monkeypatch.delenv("NUCOS_URL", raising=False)
    w = nucos.Worker(state.conn, state.write_lock)
    assert w.step() is None
    s = client.get("/api/nucos").json()
    assert s["status"] == "not_connected" and s["error"] == "nucOS: not connected"
    assert client.post("/api/nucos/test").json() == {"ok": False, "message": "nucOS: not connected"}


def test_ipod_plays_become_listen_items_once(client, nuc):
    fake, w = nuc
    body = {"lines": ipod_lines(client, CHICAGO_LOCAL), "tz": "America/Chicago"}
    assert client.post(f"/api/host/ipods/{SIG}/plays", headers=H, json=body).json()["recorded"] == 2
    assert w.step() == 15.0
    (push,) = fake.pushes()
    a, b = push["items"]
    assert a["id"].startswith(f"ipod-{CHICAGO_LOCAL + 6 * 3600}-") and len(a["id"].split("-")[2]) == 12
    assert a == {"id": a["id"], "kind": "listen", "at": "2025-11-13T02:13:20-06:00", "seconds": 200,
                 "artist": "yeule", "title": "sulky baby", "album": "softscars", "device": "ipod"}
    assert (b["title"], b["seconds"], b["device"]) == ("360", 150, "ipod") and push["deleted"] == []

    # The same lines again (history, a re-read): nothing new is pushed.
    client.post(f"/api/host/ipods/{SIG}/plays", headers=H, json=body)
    client.post(f"/api/host/ipods/{SIG}/plays", headers=H, json={"history": body["lines"], "tz": "America/Chicago"})
    w.step()
    assert len(fake.pushes()) == 1
    s = client.get("/api/nucos").json()
    assert s["status"] == "ok" and s["pending"] == 0 and s["plays"] == 2
    assert s["me"]["habit"]["name"] == "Listening" and s["last_push"]["sent"] == 2
    assert "nucos_secret_token" not in json.dumps(s)


def test_fast_ipod_clock_is_corrected(client, nuc):
    from app import plays
    fake, w = nuc
    now = int(time.time())
    real_start_utc = now - 600                    # played 10 minutes ago
    rel = track(client, "sulky")
    clock_local = real_start_utc - 6 * 3600 + 3600   # iPod still on daylight time: an hour fast
    line = f"{clock_local}:200000:201000:/FLAC/{rel}"
    r = client.post(f"/api/host/ipods/{SIG}/plays", headers=H,
                    json={"lines": [line], "tz": "America/Chicago"}).json()
    if plays.zone("America/Chicago").utcoffset(dt.datetime.now()) == dt.timedelta(hours=-6):
        assert r["clock_skew"] == 3600
        w.step()
        (item,) = fake.pushes()[0]["items"]
        assert item["id"].startswith(f"ipod-{real_start_utc}-")


def test_web_play_recorded_without_lastfm(client, nuc):
    fake, w = nuc
    r = client.post("/api/me/scrobble", json={"rel_path": track(client, "sulky"), "started": 1_700_000_000,
                                               "duration": 201, "heard": 190, "tz": "Europe/London"})
    assert r.json() == {"ok": False, "reason": "not connected", "recorded": True}
    w.step()
    (item,) = fake.pushes()[0]["items"]
    assert (item["device"], item["seconds"], item["at"]) == ("web", 190, "2023-11-14T22:13:20+00:00")
    # A play claiming to start in the future is clamped to now.
    client.post("/api/me/scrobble", json={"rel_path": track(client, "360"), "started": int(time.time()) + 9999})
    assert client.get("/api/plays").json()["plays"][0]["ts"] <= time.time()


def test_backoff_then_stop_on_401(client, nuc):
    from app import nucos
    fake, w = nuc
    client.post(f"/api/host/ipods/{SIG}/plays", headers=H,
                json={"lines": ipod_lines(client, CHICAGO_LOCAL), "tz": "America/Chicago"})
    fake.status = 503
    t = [1000.0]
    w.clock = lambda: t[0]
    assert w.step() == 30.0 and w.step() == 30.0           # waits out the backoff
    t[0] += 31
    assert w.step() == 60.0                                # doubles
    assert client.get("/api/nucos").json()["status"] == "retrying"
    fake.status = 401
    assert w.step(force=True) is None and w.step() is None  # stopped until woken
    s = client.get("/api/nucos").json()
    assert s["status"] == "token_rejected" and "New token" in s["error"] and s["pending"] == 1 + 1
    fake.status = 200
    assert w.step(force=True) == 15.0 and client.get("/api/nucos").json()["pending"] == 0


def test_rejected_logged_kept_is_fine_future_held(client, nuc):
    from app import nucos, plays, state
    fake, w = nuc
    client.post(f"/api/host/ipods/{SIG}/plays", headers=H,
                json={"lines": ipod_lines(client, CHICAGO_LOCAL), "tz": "America/Chicago"})
    first = state.conn.execute("SELECT id FROM plays ORDER BY ts").fetchone()["id"]
    fake.reject, fake.kept = {first}, [{"date": "2025-11-13", "role": "minutes"}]
    with state.write_lock:                                 # a play dated tomorrow waits
        uid = state.conn.execute("SELECT id FROM users WHERE username='jimin'").fetchone()["id"]
        plays.record(state.conn, uid, device="web", ts=int(time.time()) + 86400, seconds=100,
                     artist="Low", title="Words")
        state.conn.commit()
    w.step()
    assert len(fake.pushes()[0]["items"]) == 2               # not the future one
    s = client.get("/api/nucos").json()
    assert s["status"] == "ok" and s["rejected"][0]["id"] == first and s["last_push"]["kept"] == 1
    assert s["pending"] == 1                                  # held for its day
    r = nucos.flush_once(state.conn, state.write_lock, w._client, "54a080769b")
    assert r.held == 1 and not r.sent


def test_batches_of_500(client, nuc):
    from app import plays, state
    fake, w = nuc
    with state.write_lock:
        uid = state.conn.execute("SELECT id FROM users WHERE username='jimin'").fetchone()["id"]
        for i in range(501):
            plays.record(state.conn, uid, device="web", ts=1_700_000_000 + i * 300, seconds=60,
                         artist="Low", title=f"Song {i}")
        state.conn.commit()
    assert w.step() == 0.0 and len(fake.pushes()[0]["items"]) == 500
    assert w.step() == 15.0 and len(fake.pushes()[1]["items"]) == 1


def test_test_connection_and_only_for_the_nucos_user(client, other, nuc, monkeypatch):
    from app import main
    fake, w = nuc
    monkeypatch.setattr("app.nucos.make_client", lambda: w.client_factory())
    assert client.post("/api/nucos/test").json()["message"] == "Connected to nucOS: Listening (enrichment)"
    other.post("/api/auth/register", json={"username": "roomie", "password": "correct horse"})
    assert other.get("/api/nucos").json() == {"mine": False, "user": "jimin"}
    assert other.post("/api/nucos/test").status_code == 403
    # plays of other users never reach the outbox
    other.post("/api/me/scrobble", json={"rel_path": track(client, "sulky"), "started": 1_700_000_000})
    assert w.step() == 15.0 and fake.pushes() == []


def lastfm_page(*tracks, pages=1):
    return {"recenttracks": {"@attr": {"totalPages": str(pages)}, "track": list(tracks)}}


def scrobble(ts, artist, title, album=""):
    return {"artist": {"#text": artist}, "name": title, "album": {"#text": album}, "date": {"uts": str(ts)}}


def test_lastfm_backfill_is_safe_to_repeat_and_dedupes(client, nuc):
    from app import plays, state
    fake, w = nuc
    client.post(f"/api/host/ipods/{SIG}/plays", headers=H,
                json={"lines": ipod_lines(client, CHICAGO_LOCAL), "tz": "America/Chicago"})
    ipod_ts = CHICAGO_LOCAL + 6 * 3600
    now = ipod_ts + 86400
    calls = []

    def fetch(params):
        calls.append(params["method"])
        if params["method"] == "track.getInfo":
            return {"track": {"duration": "180000"}}
        return lastfm_page(
            {"artist": {"#text": "Low"}, "name": "Now", "@attr": {"nowplaying": "true"}},
            scrobble(ipod_ts + 30, "yeule", "sulky baby", "softscars"),     # the iPod's own play
            scrobble(ipod_ts + 4000, "Phoebe Bridgers", "Motion Sickness", "Stranger in the Alps"))
    uid = state.conn.execute("SELECT id FROM users WHERE username='jimin'").fetchone()["id"]
    r1 = plays.backfill_lastfm(state.conn, state.write_lock, uid, "blgondzur", 30, fetch=fetch, now=now)
    assert (r1["seen"], r1["recorded"], r1["duplicates"]) == (2, 1, 1)
    r2 = plays.backfill_lastfm(state.conn, state.write_lock, uid, "blgondzur", 30, fetch=fetch, now=now)
    assert (r2["recorded"], r2["duplicates"]) == (0, 1) and calls.count("track.getInfo") == 2   # cached: none on run 2
    w.step()
    phone = [i for p in fake.pushes() for i in p["items"] if i["device"] == "lastfm"]
    assert [(i["title"], i["seconds"]) for i in phone] == [("Motion Sickness", 180)]
    w.step()
    assert sum(len(p["items"]) for p in fake.pushes()) == 3     # second backfill sent nothing

    # A later iPod history line for the backfilled phone play would replace it (iPod wins).
    with state.write_lock:
        plays.record(state.conn, uid, device="ipod", ts=ipod_ts + 4010, seconds=170,
                     artist="Phoebe Bridgers", title="Motion Sickness")
        state.conn.commit()
    w.step()
    last = fake.pushes()[-1]
    assert last["deleted"] == [phone[0]["id"]] and last["items"][0]["device"] == "ipod"
