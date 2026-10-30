"""
iPods: linking to accounts, saved sync profiles, and the host-side sync API.

The host's udev → ipod-sync.sh pipeline identifies a connected iPod by a stable
*signature* (USB serial, else filesystem UUID) and talks to this API:

    POST /api/host/ipods/<sig>/seen       register the sighting (unlinked iPods stop here)
    POST /api/host/ipods/<sig>/playlists  import a playlist created on the iPod
    GET  /api/host/ipods/<sig>/plan       what this iPod should hold
    POST /api/host/ipods/<sig>/report     sync outcome → status + the owner's activity feed

Each linked iPod stores a sync profile (ipods.sync JSON + ipods.layout):

    music          "all" | "selected"   — whole library, or …
    genres         ["0_POP", …]         — … these genre folders + albums in pushed playlists
    own_playlists  bool                 — push all of the owner's playlists (incl. new ones)
    playlists      [id, …]              — extra playlists to push (e.g. others' public ones)
    scrobble       bool                 — submit .scrobbler.log to the owner's Last.fm
    layout         "genre" | "flat"     — 0_GENRE/Artist/Album or Artist/Album on the iPod

The plan is expressed in *album roots* (the Artist/Album folder, or "Artist - Album"),
so whole folders — cover.jpg, disc subfolders, extras — travel together.
"""

from __future__ import annotations

import hmac
import json
import time

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from . import config, playlists, state
from .accounts import require_user
from .parser import parse_genre

router = APIRouter()

IPOD_MUSIC_DIR = "FLAC"          # music folder at the iPod root (matches ipod-sync.sh)
DEFAULT_SYNC = {"music": "all", "genres": [], "own_playlists": True, "playlists": [],
                "scrobble": True}


def _profile(row) -> dict:
    sync = DEFAULT_SYNC | json.loads(row["sync"] or "{}")
    return {"id": row["id"], "name": row["name"], "signature": row["signature"],
            "layout": row["layout"], "sync": sync,
            "metadata": json.loads(row["metadata"] or "{}"),
            "last_seen": row["last_seen"], "last_sync": row["last_sync"],
            "last_status": row["last_status"]}


def _activity(user_id: int, ipod_id: int | None, kind: str, message: str, detail: dict | None = None):
    state.conn.execute(
        "INSERT INTO activity(user_id, ipod_id, ts, kind, message, detail) VALUES (?,?,?,?,?,?)",
        (user_id, ipod_id, time.time(), kind, message, json.dumps(detail or {})))


# ── Plan ─────────────────────────────────────────────────────────────────────

def album_root(rel_path: str) -> str:
    """The folder that holds a whole album: [genre/]Artist/Album, [genre/]Artist - Album,
    or the file itself when it sits loose in a genre/root folder."""
    parts = rel_path.split("/")
    dirs = parts[:-1]
    g = 1 if dirs and parse_genre(dirs[0])[0] is not None else 0
    rest = dirs[g:]
    if len(rest) >= 2:
        return "/".join(dirs[:g + 2])
    if len(rest) == 1:
        return "/".join(dirs[:g + 1])
    return rel_path


def _ancestors(rel: str):
    parts = rel.split("/")
    return ("/".join(parts[:i]) for i in range(1, len(parts)))


def project(rel: str, layout: str) -> str:
    """Path on the iPod (under FLAC/) for a library-relative path."""
    if layout == "flat":
        head, _, tail = rel.partition("/")
        if tail and parse_genre(head)[0] is not None:
            return tail
    return rel


def pushed_playlist_ids(ipod: dict, owner_id: int) -> list[int]:
    sync = ipod["sync"]
    ids = []
    if sync["own_playlists"]:
        ids += [r["id"] for r in state.conn.execute(
            "SELECT id FROM playlists WHERE user_id=? ORDER BY LOWER(name)", (owner_id,))]
    for pid in sync["playlists"]:
        # others' playlists only while they stay public
        ok = state.conn.execute("SELECT 1 FROM playlists WHERE id=? AND (is_global=1 OR user_id=?)",
                                (pid, owner_id)).fetchone()
        if ok and pid not in ids:
            ids.append(pid)
    return ids


def build_plan(ipod: dict, owner_id: int) -> dict:
    layout, sync = ipod["layout"], ipod["sync"]
    pids = pushed_playlist_ids(ipod, owner_id)
    pl_data = [playlists.get_playlist(state.conn, pid, owner_id) for pid in pids]
    pl_data = [p for p in pl_data if p]

    tracks = state.conn.execute("SELECT rel_path, size_bytes FROM tracks").fetchall()
    if sync["music"] == "all":
        wanted = {album_root(t["rel_path"]) for t in tracks}
    else:
        genres = set(sync["genres"])
        wanted = {album_root(t["rel_path"]) for t in tracks
                  if t["rel_path"].split("/", 1)[0] in genres}
        wanted |= {album_root(i["rel_path"]) for p in pl_data for i in p["items"] if i["rel_path"]}

    # Drop roots nested inside another chosen root (they ride along with the parent).
    roots = sorted(wanted)
    kept: list[str] = []
    for r in roots:
        if not any(r.startswith(k + "/") for k in kept):
            kept.append(r)
    music = [{"src": r, "dst": project(r, layout)} for r in kept]

    # Folder artwork travelling with the music: artist photos, and genre art in genre layout.
    top_dirs = {r.split("/", 1)[0] for r in kept}
    artist_dirs = {r.rsplit("/", 1)[0] for r in kept if "/" in r}
    for a in state.conn.execute("SELECT rel_dir, image FROM artist_folders WHERE image IS NOT NULL"):
        if a["rel_dir"] in artist_dirs:
            music.append({"src": a["image"], "dst": project(a["image"], layout)})
    if layout == "genre":
        for g in state.conn.execute("SELECT rel_dir, image FROM genre_folders WHERE image IS NOT NULL"):
            if g["rel_dir"] in top_dirs:
                music.append({"src": g["image"], "dst": g["image"]})

    kept_set = set(kept)
    included = [t for t in tracks if t["rel_path"] in kept_set
                or any(p in kept_set for p in _ancestors(t["rel_path"]))]
    size = sum(t["size_bytes"] or 0 for t in included)
    n_tracks = len(included)

    # Playlists as Rockbox-friendly absolute paths; duplicate names get the owner appended.
    out_pls, seen = [], set()
    for p in pl_data:
        name = p["name"] if p["name"].lower() not in seen else f'{p["name"]} - {p["owner"]}'
        seen.add(name.lower())
        lines = [f"/{IPOD_MUSIC_DIR}/{project(i['rel_path'], layout)}"
                 for i in p["items"] if i["rel_path"]]
        out_pls.append({"id": p["id"], "name": name, "owner": p["owner"],
                        "m3u": "#EXTM3U\n" + "".join(l + "\n" for l in lines),
                        "n_tracks": len(lines)})

    owner = state.conn.execute("SELECT username, lastfm_user, lastfm_session FROM users WHERE id=?",
                               (owner_id,)).fetchone()
    scrobble_on = bool(sync["scrobble"] and owner["lastfm_session"])
    return {
        "ipod": {"id": ipod["id"], "name": ipod["name"], "layout": layout},
        "owner": owner["username"],
        "music_dir": IPOD_MUSIC_DIR,
        "music": music,
        "playlists": out_pls,
        "scrobble": {"enabled": scrobble_on,
                     "lastfm_user": owner["lastfm_user"] if scrobble_on else None,
                     "session_key": owner["lastfm_session"] if scrobble_on else None},
        "summary": {"albums": len(kept), "tracks": n_tracks, "bytes": size,
                    "playlists": len(out_pls)},
    }


# ── Owner-facing API ─────────────────────────────────────────────────────────

class LinkIpod(BaseModel):
    signature: str
    name: str


class SyncProfile(BaseModel):
    music: str | None = None
    genres: list[str] | None = None
    own_playlists: bool | None = None
    playlists: list[int] | None = None
    scrobble: bool | None = None


class UpdateIpod(BaseModel):
    name: str | None = None
    layout: str | None = None
    sync: SyncProfile | None = None


def _my_ipod(ipod_id: int, user: dict) -> dict:
    row = state.conn.execute("SELECT * FROM ipods WHERE id=? AND user_id=?",
                             (ipod_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "no such iPod on your account")
    return _profile(row)


@router.get("/api/ipods")
def my_ipods(user: dict = Depends(require_user)):
    mine = [_profile(r) for r in state.conn.execute(
        "SELECT * FROM ipods WHERE user_id=? ORDER BY name", (user["id"],))]
    for ip in mine:
        s = state.conn.execute("SELECT metadata, last_seen FROM ipod_sightings WHERE signature=?",
                               (ip["signature"],)).fetchone()
        ip["host"] = json.loads(s["metadata"]) if s else {}
        ip["summary"] = build_plan(ip, user["id"])["summary"]
    month = time.time() - 30 * 86400
    unclaimed = [dict(r) | {"metadata": json.loads(r["metadata"])} for r in state.conn.execute(
        "SELECT signature, metadata, first_seen, last_seen FROM ipod_sightings s "
        "WHERE last_seen > ? AND NOT EXISTS (SELECT 1 FROM ipods i WHERE i.signature=s.signature) "
        "ORDER BY last_seen DESC", (month,))]
    genres = [dict(r) for r in state.conn.execute("SELECT rel_dir, genre FROM genre_folders ORDER BY rel_dir")]
    return {"ipods": mine, "unclaimed": unclaimed, "genres": genres}


@router.post("/api/ipods")
def link_ipod(body: LinkIpod, user: dict = Depends(require_user)):
    name = body.name.strip()[:60] or "iPod"
    if not state.conn.execute("SELECT 1 FROM ipod_sightings WHERE signature=?",
                              (body.signature,)).fetchone():
        raise HTTPException(404, "connect the iPod to the server first so it can be detected")
    if state.conn.execute("SELECT 1 FROM ipods WHERE signature=?", (body.signature,)).fetchone():
        raise HTTPException(409, "that iPod is already linked to an account")
    meta = state.conn.execute("SELECT metadata FROM ipod_sightings WHERE signature=?",
                              (body.signature,)).fetchone()["metadata"]
    with state.write_lock:
        iid = state.conn.execute(
            "INSERT INTO ipods(user_id, name, signature, metadata, layout, last_seen, created_at, sync) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user["id"], name, body.signature, meta, "genre", time.time(), time.time(),
             json.dumps(DEFAULT_SYNC))).lastrowid
        _activity(user["id"], iid, "link", f"Linked {name} to your account")
        state.conn.commit()
    return _my_ipod(iid, user)


@router.patch("/api/ipods/{ipod_id}")
def update_ipod(ipod_id: int, body: UpdateIpod, user: dict = Depends(require_user)):
    ip = _my_ipod(ipod_id, user)
    name = (body.name or ip["name"]).strip()[:60] or ip["name"]
    layout = body.layout or ip["layout"]
    if layout not in ("genre", "flat"):
        raise HTTPException(400, "layout must be 'genre' or 'flat'")
    sync = ip["sync"] | {k: v for k, v in (body.sync.model_dump() if body.sync else {}).items()
                         if v is not None}
    if sync["music"] not in ("all", "selected"):
        raise HTTPException(400, "music must be 'all' or 'selected'")
    with state.write_lock:
        state.conn.execute("UPDATE ipods SET name=?, layout=?, sync=? WHERE id=?",
                           (name, layout, json.dumps(sync), ipod_id))
        state.conn.commit()
    out = _my_ipod(ipod_id, user)
    out["summary"] = build_plan(out, user["id"])["summary"]
    return out


@router.delete("/api/ipods/{ipod_id}")
def unlink_ipod(ipod_id: int, user: dict = Depends(require_user)):
    ip = _my_ipod(ipod_id, user)
    with state.write_lock:
        state.conn.execute("DELETE FROM ipods WHERE id=?", (ipod_id,))
        _activity(user["id"], None, "link", f"Unlinked {ip['name']}")
        state.conn.commit()
    return {"ok": True}


@router.get("/api/activity")
def my_activity(user: dict = Depends(require_user), limit: int = 200):
    rows = state.conn.execute(
        "SELECT a.ts, a.kind, a.message, a.detail, i.name AS ipod FROM activity a "
        "LEFT JOIN ipods i ON i.id = a.ipod_id WHERE a.user_id=? ORDER BY a.ts DESC LIMIT ?",
        (user["id"], limit)).fetchall()
    return {"activity": [dict(r) | {"detail": json.loads(r["detail"] or "{}")} for r in rows]}


# ── Host API (ipod-sync.sh on the server; shared-token auth) ─────────────────

def require_host(x_aquaria_token: str = Header(default="")) -> None:
    if not config.HOST_TOKEN:
        raise HTTPException(503, "host API disabled: set AQUARIA_HOST_TOKEN")
    if not hmac.compare_digest(x_aquaria_token, config.HOST_TOKEN):
        raise HTTPException(401, "bad host token")


def _linked(sig: str):
    row = state.conn.execute("SELECT * FROM ipods WHERE signature=?", (sig,)).fetchone()
    return _profile(row) | {"user_id": row["user_id"]} if row else None


class Seen(BaseModel):
    metadata: dict = {}


class PulledPlaylist(BaseModel):
    name: str
    lines: list[str]


class Report(BaseModel):
    status: Literal["syncing", "ok", "warning", "error"]   # syncing = a sync just started
    message: str = ""
    stats: dict = {}


@router.post("/api/host/ipods/{sig}/seen", dependencies=[Depends(require_host)])
def host_seen(sig: str, body: Seen):
    now = time.time()
    with state.write_lock:
        state.conn.execute(
            "INSERT INTO ipod_sightings(signature, metadata, first_seen, last_seen) VALUES (?,?,?,?) "
            "ON CONFLICT(signature) DO UPDATE SET metadata=excluded.metadata, last_seen=excluded.last_seen",
            (sig, json.dumps(body.metadata | {"state": "connected"}), now, now))
        ip = _linked(sig)
        if ip:
            state.conn.execute("UPDATE ipods SET last_seen=?, metadata=? WHERE id=?",
                               (now, json.dumps(body.metadata), ip["id"]))
            _activity(ip["user_id"], ip["id"], "connect", f"{ip['name']} connected to the server")
        state.conn.commit()
    if not ip:
        return {"linked": False}
    owner = state.conn.execute("SELECT username FROM users WHERE id=?", (ip["user_id"],)).fetchone()
    return {"linked": True, "name": ip["name"], "owner": owner["username"]}


@router.post("/api/host/ipods/{sig}/playlists", dependencies=[Depends(require_host)])
def host_import_playlist(sig: str, body: PulledPlaylist):
    """A playlist made on the iPod itself → a private playlist of the iPod's owner."""
    ip = _linked(sig)
    if not ip:
        raise HTTPException(404, "iPod not linked")
    prefix = f"/{IPOD_MUSIC_DIR}/"
    lines = [l[len(prefix):] if l.startswith(prefix) else l.lstrip("/") for l in body.lines]
    base = playlists.safe_name(body.name) or "iPod playlist"
    with state.write_lock:
        for n in range(1, 50):
            name = base if n == 1 else f"{base} ({n})"
            try:
                pid = playlists.create_playlist(state.conn, config.PLAYLIST_DIR, ip["user_id"], name,
                                                is_public=False, lines=lines)
                break
            except FileExistsError:
                continue
        else:
            raise HTTPException(409, "too many playlists with that name")
        if not ip["sync"]["own_playlists"]:
            sync = ip["sync"] | {"playlists": ip["sync"]["playlists"] + [pid]}
            state.conn.execute("UPDATE ipods SET sync=? WHERE id=?", (json.dumps(sync), ip["id"]))
        _activity(ip["user_id"], ip["id"], "playlist", f"Imported “{name}” from {ip['name']}",
                  {"playlist_id": pid, "playlist": name})
        state.conn.commit()
    return {"id": pid, "name": name}


@router.get("/api/host/ipods/{sig}/plan", dependencies=[Depends(require_host)])
def host_plan(sig: str):
    ip = _linked(sig)
    if not ip:
        raise HTTPException(404, "iPod not linked")
    return build_plan(ip, ip["user_id"])


@router.post("/api/host/ipods/{sig}/report", dependencies=[Depends(require_host)])
def host_report(sig: str, body: Report):
    ip = _linked(sig)
    now = time.time()
    with state.write_lock:
        s = state.conn.execute("SELECT metadata FROM ipod_sightings WHERE signature=?", (sig,)).fetchone()
        if s:
            meta = json.loads(s["metadata"]) | {"state": body.status}
            meta["sync_started"] = now if body.status == "syncing" else None
            state.conn.execute("UPDATE ipod_sightings SET metadata=?, last_seen=? WHERE signature=?",
                               (json.dumps(meta), now, sig))
        if ip and body.status == "syncing":
            _activity(ip["user_id"], ip["id"], "sync", f"Sync of {ip['name']} started")
        elif ip:
            state.conn.execute("UPDATE ipods SET last_sync=?, last_status=? WHERE id=?",
                               (now, f"{body.status}: {body.message}"[:300], ip["id"]))
            _activity(ip["user_id"], ip["id"], "sync", body.message, body.stats | {"status": body.status})
        state.conn.commit()
    return {"ok": True}
