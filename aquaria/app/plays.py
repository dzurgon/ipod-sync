"""
Plays: every listen Aquaria knows about, and the nucOS outbox that carries them.

A play's id is stable forever — `<device>-<unix start>-<track hash>` — so seeing the same
play again (an iPod history re-read, a Last.fm backfill run twice) changes nothing.
Recording a play for NUCOS_USER also writes its outbox row in the same transaction
(nucos.py sends the outbox); nucOS receives `listen` items:

    {id, kind: "listen", at, seconds, artist, title, album, device}

Callers hold state.write_lock and commit.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config

# Last.fm backfill: a scrobble within this many seconds of a play we already have, with
# the same artist + title, is that play (the iPod/web player scrobbled it).
SAME_PLAY_WINDOW = 120


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or config.LOCAL_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def iso_at(ts: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).isoformat()


def local_clock_to_utc(local_ts: int, tz: ZoneInfo) -> int:
    """Rockbox logs its clock's wall time as if it were UTC: reinterpret it in its zone."""
    return int(datetime.fromtimestamp(local_ts, timezone.utc).replace(tzinfo=tz).timestamp())


def clock_skew(ends_utc: list[int], now: float | None = None) -> int:
    """How far a device clock runs fast, judged by plays that would have ended after now
    (impossible: they were collected now). Rounded up to 30 minutes — a time zone or a
    missed DST change, not drift. 0 when nothing ends in the future (a slow clock can't
    be detected this way)."""
    over = max(ends_utc, default=0) - (now if now is not None else time.time())
    return int(math.ceil(over / 1800) * 1800) if over > 60 else 0


def track_hash(artist: str, title: str, album: str) -> str:
    key = "\n".join(s.strip().casefold() for s in (artist, title, album))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def play_id(device: str, ts: int, artist: str, title: str, album: str) -> str:
    return f"{device}-{ts}-{track_hash(artist, title, album)}"


def is_nucos_user(conn, user_id: int) -> bool:
    row = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row) and row["username"].casefold() == config.NUCOS_USER.casefold()


def item(play: dict) -> dict:
    """The nucOS `listen` item for a play row."""
    return {"id": play["id"], "kind": "listen", "at": play["at"], "seconds": play["seconds"],
            "artist": play["artist"], "title": play["title"], "album": play["album"],
            "device": play["device"]}


def record(conn, user_id: int, *, device: str, ts: int, seconds: int, artist: str, title: str,
           album: str = "", tz: ZoneInfo | None = None, pid: str | None = None) -> str | None:
    """Store one play (and its nucOS outbox row). Returns its id when it's new or changed,
    None when it was already known exactly like this."""
    pid = pid or play_id(device, ts, artist, title, album)
    row = {"id": pid, "user_id": user_id, "ts": int(ts), "at": iso_at(int(ts), tz or zone(None)),
           "seconds": max(0, int(seconds)), "artist": artist or "", "title": title or "",
           "album": album or "", "device": device}
    old = conn.execute("SELECT * FROM plays WHERE id=?", (pid,)).fetchone()
    if old and all(old[k] == row[k] for k in ("ts", "at", "seconds", "artist", "title", "album", "device")):
        return None
    conn.execute(
        "INSERT INTO plays(id, user_id, ts, at, seconds, artist, title, album, device, created_at) "
        "VALUES (:id, :user_id, :ts, :at, :seconds, :artist, :title, :album, :device, :created) "
        "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, at=excluded.at, seconds=excluded.seconds, "
        "artist=excluded.artist, title=excluded.title, album=excluded.album, device=excluded.device",
        row | {"created": time.time()})
    nucos = is_nucos_user(conn, user_id)
    if nucos:
        conn.execute("INSERT INTO nucos_outbox(item_id, payload) VALUES (?, ?)",
                     (pid, json.dumps(item(row), ensure_ascii=False, sort_keys=True)))
    if device != "lastfm":
        # The iPod / web player's own record of a play beats the copy a Last.fm backfill made.
        for dup in conn.execute(
                "SELECT id FROM plays WHERE user_id=? AND device='lastfm' AND ts BETWEEN ? AND ? "
                "AND lower(artist)=lower(?) AND lower(title)=lower(?)",
                (user_id, row["ts"] - SAME_PLAY_WINDOW, row["ts"] + SAME_PLAY_WINDOW, row["artist"],
                 row["title"])).fetchall():
            conn.execute("DELETE FROM plays WHERE id=?", (dup["id"],))
            if nucos:
                conn.execute("INSERT INTO nucos_outbox(item_id, payload) VALUES (?, NULL)", (dup["id"],))
    return pid


def known_near(conn, user_id: int, artist: str, title: str, ts: int,
               window: int = SAME_PLAY_WINDOW) -> bool:
    """A play of this artist + title already recorded within `window` seconds of ts."""
    return conn.execute(
        "SELECT 1 FROM plays WHERE user_id=? AND ts BETWEEN ? AND ? "
        "AND lower(artist)=lower(?) AND lower(title)=lower(?) LIMIT 1",
        (user_id, ts - window, ts + window, artist, title)).fetchone() is not None


def recent(conn, user_id: int, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, ts, at, seconds, artist, title, album, device FROM plays "
        "WHERE user_id=? ORDER BY ts DESC LIMIT ?", (user_id, limit))]


# ---------------------------------------------------------------- Last.fm backfill

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
UNKNOWN_LENGTH = 210                     # 3.5 min when Last.fm doesn't know a track's length

BACKFILL = {"running": False, "days": 0, "pages": 0, "seen": 0, "recorded": 0, "duplicates": 0,
            "error": None, "finished": None}


def _length(conn, lock, artist: str, title: str, fetch) -> int:
    """Seconds: the library file's length, else Last.fm track.getInfo (cached), else 3.5 min."""
    row = conn.execute("SELECT rel_path FROM tracks WHERE lower(artist)=lower(?) AND lower(title)=lower(?) LIMIT 1",
                       (artist, title)).fetchone()
    if row:
        try:
            import mutagen
            f = mutagen.File(config.MUSIC_ROOT / row["rel_path"])
            if f and f.info and f.info.length:
                return int(f.info.length)
        except Exception:
            pass
    key = f"lastfm.length:{track_hash(artist, title, '')}"
    got = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if got:
        return int(got["value"])
    try:
        info = fetch({"method": "track.getInfo", "artist": artist, "track": title}).get("track") or {}
        secs = int(info.get("duration") or 0) // 1000
    except Exception:
        return UNKNOWN_LENGTH                            # don't cache a failed lookup
    with lock:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(secs or UNKNOWN_LENGTH)))
        conn.commit()
    return secs or UNKNOWN_LENGTH


def lastfm_fetch(params: dict) -> dict:
    import urllib.parse
    import urllib.request
    q = urllib.parse.urlencode(params | {"api_key": config.LASTFM_API_KEY, "format": "json"})
    req = urllib.request.Request(LASTFM_API + "?" + q, headers={"User-Agent": "Aquaria"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def backfill_lastfm(conn, lock, user_id: int, lastfm_user: str, days: int, fetch=lastfm_fetch,
                    now: float | None = None, on_page=None) -> dict:
    """Record the last `days` of this user's Last.fm scrobbles as plays (device "lastfm").
    Safe to run any number of times: ids are stable, and a scrobble the iPod or web player
    already recorded (same artist + title within 2 minutes) is skipped as a duplicate."""
    now = time.time() if now is None else now
    since = int(now - days * 86400)
    tz = zone(None)
    out = {"pages": 0, "seen": 0, "recorded": 0, "duplicates": 0}
    page, pages = 1, 1
    while page <= pages:
        data = fetch({"method": "user.getRecentTracks", "user": lastfm_user, "from": since,
                      "limit": 200, "page": page})
        rt = data.get("recenttracks") or {}
        pages = int((rt.get("@attr") or {}).get("totalPages") or 1)
        tracks = rt.get("track") or []
        tracks = tracks if isinstance(tracks, list) else [tracks]
        found = []                                       # network lookups first, then the writes
        for t in tracks:
            if (t.get("@attr") or {}).get("nowplaying") or not (t.get("date") or {}).get("uts"):
                continue                                 # playing right now: not a scrobble yet
            ts = int(t["date"]["uts"])
            artist = (t.get("artist") or {}).get("#text") or (t.get("artist") or {}).get("name") or ""
            title, album = t.get("name") or "", (t.get("album") or {}).get("#text") or ""
            if artist and title and ts <= now:
                found.append((ts, artist, title, album, _length(conn, lock, artist, title, fetch)))
        with lock:
            for ts, artist, title, album, secs in found:
                out["seen"] += 1
                pid = play_id("lastfm", ts, artist, title, album)
                mine = conn.execute("SELECT 1 FROM plays WHERE id=?", (pid,)).fetchone()
                if not mine and known_near(conn, user_id, artist, title, ts):
                    out["duplicates"] += 1               # the iPod / web player's own play
                    continue
                if record(conn, user_id, device="lastfm", ts=ts, seconds=secs,
                          artist=artist, title=title, album=album, tz=tz, pid=pid):
                    out["recorded"] += 1
            conn.commit()
        out["pages"] = page
        if on_page:
            on_page(out)
        page += 1
    return out
