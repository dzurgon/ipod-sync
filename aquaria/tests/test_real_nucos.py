"""Aquaria against the real nucOS code (not the fake): when the nucOS source is on this machine
(~/src/nucos), build a throwaway nucOS with its own test harness, install the built-in Listening
pack as a new habit, connect, and run Aquaria's sender against it in-process. Nothing touches
the running nucOS. Skipped when the source isn't there."""

import datetime as dt
import importlib.util
import os
import sys
from pathlib import Path

import pytest

NUCOS = Path(os.environ.get("NUCOS_SRC", "/home/jimin/src/nucos"))
H = {"X-Aquaria-Token": "host-secret"}
SIG = "300000000000A2700149D8299"


@pytest.fixture
def real_nucos(tmp_path, monkeypatch):
    if not (NUCOS / "server" / "organizer_server" / "integrations.py").is_file():
        pytest.skip("nucOS source not available")
    for p in (NUCOS / "server", NUCOS / "tools" / "organizer_parser", NUCOS / "server" / "tests"):
        monkeypatch.syspath_prepend(str(p))
    ours = sys.modules.pop("conftest", None)     # nucOS's harness imports its own `conftest`
    try:
        spec = importlib.util.spec_from_file_location("conftest", NUCOS / "server" / "tests" / "conftest.py")
        theirs = importlib.util.module_from_spec(spec)
        sys.modules["conftest"] = theirs
        spec.loader.exec_module(theirs)
        from organizer_server import integrations, users
        from test_links import make
    except Exception as e:                       # its dependencies may not be installed here
        pytest.skip(f"can't import nucOS: {e}")
    finally:
        sys.modules.pop("conftest", None)
        if ours is not None:
            sys.modules["conftest"] = ours
    monkeypatch.setattr(users, "ITERATIONS", 1_000)
    (tmp_path / "nucos").mkdir()
    c, napp = make(tmp_path / "nucos", "## Habits\n- [ ] Floss #hygiene\n")
    yield c, napp, integrations
    for m in [m for m in sys.modules if m.startswith(("organizer_server", "organizer_parser", "test_links"))]:
        sys.modules.pop(m, None)


def test_ipod_plays_and_backfill_round_trip_with_real_nucos(client, real_nucos, monkeypatch):
    from app import main, nucos, plays, state
    c, napp, integrations = real_nucos

    # 1. nucOS: the Listening pack as a new habit, and a connection for AquariaPod.
    inst = c.post("/ui/api/market", json={"install": {"pack": "music.listening", "name": "Listening",
                                                      "sphere": "enrichment"}}).json()["install"]
    token = c.post("/ui/api/market", json={"connect": inst["id"]}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    def transport(method, path, body):          # Aquaria's client, talking to nucOS in-process
        r = c.request(method, path, json=body, headers=auth)
        return r.status_code, r.json()

    monkeypatch.setenv("NUCOS_URL", "http://testserver")
    monkeypatch.setenv("NUCOS_TOKEN", token)
    monkeypatch.setattr(nucos, "make_client", lambda: nucos.Client("http://testserver", token, transport))
    monkeypatch.setattr(main, "NUCOS", None)
    worker = nucos.Worker(state.conn, state.write_lock, client_factory=nucos.make_client)

    # Test connection = GET /me.
    t = client.post("/api/nucos/test").json()
    assert t["ok"] and t["message"] == "Connected to nucOS: Listening (enrichment)"
    roles = t["me"]["roles"]

    # 2. Yesterday on the iPod (Rockbox logs Chicago wall time): two listened tracks, one skip.
    client.post(f"/api/host/ipods/{SIG}/seen", headers=H, json={"metadata": {}})
    client.post("/api/ipods", json={"signature": SIG, "name": "JiminPod"})
    tz = plays.zone("America/Chicago")
    y = dt.date.today() - dt.timedelta(days=1)
    noon = dt.datetime(y.year, y.month, y.day, 12, 0, tzinfo=dt.timezone.utc)   # wall clock, Rockbox-style
    local = int(noon.timestamp())
    sulky = client.get("/api/search?q=sulky").json()["tracks"][0]["rel_path"]
    three60 = client.get("/api/search?q=360").json()["tracks"][0]["rel_path"]
    lines = [f"{local}:200000:201000:/<HDD0>/FLAC/{sulky}",
             f"{local + 300}:150000:160000:/FLAC/{three60}",
             f"{local + 600}:5000:160000:/FLAC/{three60}"]
    r = client.post(f"/api/host/ipods/{SIG}/plays", headers=H, json={"lines": lines, "tz": "America/Chicago"})
    assert r.json()["recorded"] == 2
    assert worker.step(force=True) == nucos.IDLE_POLL
    assert client.get("/api/nucos").json()["status"] == "ok"

    # nucOS answered the habit for that day from the items.
    Y = y.isoformat()
    day = c.get(f"/api/integrations/v1/days?from={Y}&to={Y}", headers=auth).json()["days"][Y]["answers"]
    assert day["listened"] is True and day["tracks"] == 2 and day["ipod"] is True
    assert day["minutes"] == round((200 + 150) / 60)                    # durations: whole minutes
    conn_id = t["me"]["connection"]["id"]
    seen = integrations.all_(napp.state.store)[conn_id]
    assert seen["pushes"] == 1 and seen["items"] == 2 and seen["last_seen"]

    # 3. Sending the same plays again (a re-read of the iPod's history) pushes nothing.
    client.post(f"/api/host/ipods/{SIG}/plays", headers=H, json={"history": lines, "tz": "America/Chicago"})
    worker.step(force=True)
    assert integrations.all_(napp.state.store)[conn_id]["pushes"] == 1

    # 4. Last.fm backfill, twice: the iPod's own scrobble is a duplicate; a phone play is new.
    utc0 = plays.local_clock_to_utc(local, tz)

    def fetch(params):
        if params["method"] == "track.getInfo":
            return {"track": {"duration": "240000"}}
        return {"recenttracks": {"@attr": {"totalPages": "1"}, "track": [
            {"artist": {"#text": "yeule"}, "name": "sulky baby", "album": {"#text": "softscars"},
             "date": {"uts": str(utc0 + 20)}},
            {"artist": {"#text": "Big Thief"}, "name": "Not", "album": {"#text": "Two Hands"},
             "date": {"uts": str(utc0 + 3600)}}]}}
    uid = state.conn.execute("SELECT id FROM users WHERE username='jimin'").fetchone()["id"]
    for _ in range(2):
        plays.backfill_lastfm(state.conn, state.write_lock, uid, "blgondzur", 7, fetch=fetch)
        worker.step(force=True)
    day = c.get(f"/api/integrations/v1/days?from={Y}&to={Y}", headers=auth).json()["days"][Y]["answers"]
    assert day["tracks"] == 3 and day["minutes"] == round((200 + 150 + 240) / 60)
    assert integrations.all_(napp.state.store)[conn_id]["pushes"] == 2   # the repeat sent nothing

    # 5. Your answer wins: minutes set by hand in nucOS stay; the app's push reports them as kept.
    field = roles["minutes"]["field"]
    assert c.post("/ui/api/answers", json={"date": Y, "answers": [
        {"id": field, "op": "set", "value": 90}]}).status_code == 200
    with state.write_lock:
        plays.record(state.conn, uid, device="ipod", ts=utc0 + 7200, seconds=300, artist="Low", title="Words",
                     tz=tz)
        state.conn.commit()
    worker.step(force=True)
    day = c.get(f"/api/integrations/v1/days?from={Y}&to={Y}", headers=auth).json()["days"][Y]["answers"]
    assert day["minutes"] == 90 and day["tracks"] == 4
    s = client.get("/api/nucos").json()
    assert s["status"] == "ok" and s["last_push"]["kept"] >= 1 and s["pending"] == 0
