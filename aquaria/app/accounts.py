"""
Accounts: username + password login, cookie sessions, public profiles, Last.fm.

* Passwords are hashed with scrypt (stdlib). A user row with no password_hash is
  "unclaimed" — e.g. the default owner of playlists imported before logins existed —
  and registering that username claims it, keeping its playlists.
* Sessions are random tokens in an HttpOnly cookie; only their SHA-256 is stored.
* Guests may read (library, public playlists, profiles); anything that writes
  depends on `require_user`.
* Last.fm uses the web-auth flow, so each user links their own scrobbling account.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from . import config, state

router = APIRouter()

COOKIE = "aquaria_session"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
MIN_PASSWORD = 8


# ── Passwords + sessions ─────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    try:
        algo, salt, want = (stored or "").split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(got.hex(), want)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(response: Response, user_id: int) -> None:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with state.write_lock:
        state.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        state.conn.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (_token_hash(token), user_id, now, now + config.SESSION_DAYS * 86400))
        state.conn.commit()
    response.set_cookie(COOKIE, token, max_age=config.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", path="/")


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = state.conn.execute(
        "SELECT u.id, u.username, u.lastfm_user FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > ?", (_token_hash(token), time.time())).fetchone()
    return dict(row) if row else None


def require_user(user: dict | None = Depends(current_user)) -> dict:
    if not user:
        raise HTTPException(401, "log in first")
    return user


def find_user(username: str):
    return state.conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE",
                              (username,)).fetchone()


# ── Auth endpoints ───────────────────────────────────────────────────────────

class Credentials(BaseModel):
    username: str
    password: str


@router.post("/api/auth/register")
def register(body: Credentials, response: Response):
    name = body.username.strip()
    if not USERNAME_RE.match(name):
        raise HTTPException(400, "username: 2–32 letters, digits, '.', '_' or '-'")
    if len(body.password) < MIN_PASSWORD:
        raise HTTPException(400, f"password must be at least {MIN_PASSWORD} characters")
    existing = find_user(name)
    if existing and existing["password_hash"]:
        raise HTTPException(409, "that username is taken")
    with state.write_lock:
        if existing:      # unclaimed account (e.g. owner of imported playlists) — claim it
            uid = existing["id"]
            state.conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                               (hash_password(body.password), uid))
        else:
            uid = state.conn.execute(
                "INSERT INTO users(username, settings, created_at, password_hash) VALUES (?,?,?,?)",
                (name, "{}", time.time(), hash_password(body.password))).lastrowid
        state.conn.commit()
    start_session(response, uid)
    return {"username": find_user(name)["username"], "claimed": bool(existing)}


@router.post("/api/auth/login")
def login(body: Credentials, response: Response):
    u = find_user(body.username.strip())
    if not u or not check_password(body.password, u["password_hash"]):
        raise HTTPException(401, "wrong username or password")
    start_session(response, u["id"])
    return {"username": u["username"]}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        with state.write_lock:
            state.conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
            state.conn.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/api/auth/me")
def me(user: dict | None = Depends(current_user)):
    return {"user": user and {"username": user["username"], "lastfm_user": user["lastfm_user"]},
            "lastfm_available": bool(config.LASTFM_API_KEY and config.LASTFM_API_SECRET)}


# ── Profiles ─────────────────────────────────────────────────────────────────

@router.get("/api/users")
def list_users():
    rows = state.conn.execute("""
        SELECT u.username, u.password_hash IS NOT NULL AS claimed,
               (SELECT COUNT(*) FROM playlists p WHERE p.user_id=u.id AND p.is_global=1) AS n_public,
               (SELECT COUNT(*) FROM ipods i WHERE i.user_id=u.id) AS n_ipods
        FROM users u ORDER BY LOWER(u.username)""").fetchall()
    return {"users": [dict(r) | {"claimed": bool(r["claimed"])} for r in rows],
            "default_backdrop": config.DEFAULT_BACKDROP}


@router.get("/api/users/{username}")
def profile(username: str, viewer: dict | None = Depends(current_user)):
    u = find_user(username)
    if not u:
        raise HTTPException(404, "no such user")
    is_me = bool(viewer and viewer["id"] == u["id"])
    pls = state.conn.execute("""
        SELECT p.id, p.name, p.is_global, p.updated_at, COUNT(i.id) AS n_tracks
        FROM playlists p LEFT JOIN playlist_items i ON i.playlist_id = p.id
        WHERE p.user_id = ? AND (p.is_global = 1 OR ?)
        GROUP BY p.id ORDER BY LOWER(p.name)""", (u["id"], is_me)).fetchall()
    ipods = state.conn.execute(
        "SELECT name, last_sync, last_status FROM ipods WHERE user_id=? ORDER BY name",
        (u["id"],)).fetchall()
    return {
        "username": u["username"], "is_me": is_me, "claimed": bool(u["password_hash"]),
        "member_since": u["created_at"],
        "lastfm_user": u["lastfm_user"],
        "playlists": [dict(p) | {"is_public": bool(p["is_global"])} for p in pls],
        "ipods": [dict(i) for i in ipods],
    }


# ── Last.fm (web auth: https://www.last.fm/api/webauth) ──────────────────────

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"


def _lastfm_sig(params: dict) -> str:
    raw = "".join(f"{k}{params[k]}" for k in sorted(params)) + config.LASTFM_API_SECRET
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def lastfm_get_session(token: str) -> tuple[str, str]:
    params = {"method": "auth.getSession", "api_key": config.LASTFM_API_KEY, "token": token}
    params["api_sig"] = _lastfm_sig(params)
    url = LASTFM_API + "?" + urllib.parse.urlencode(params | {"format": "json"})
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.loads(r.read())
    if "session" not in data:
        raise RuntimeError(data.get("message", "Last.fm refused the token"))
    return data["session"]["name"], data["session"]["key"]


@router.get("/api/me/lastfm/connect")
def lastfm_connect(request: Request, user: dict = Depends(require_user)):
    if not config.LASTFM_API_KEY:
        raise HTTPException(503, "Last.fm isn't configured on this server")
    cb = str(request.url_for("lastfm_callback"))
    return RedirectResponse("https://www.last.fm/api/auth/?" + urllib.parse.urlencode(
        {"api_key": config.LASTFM_API_KEY, "cb": cb}))


@router.get("/lastfm/callback", name="lastfm_callback")
def lastfm_callback(token: str = "", user: dict | None = Depends(current_user)):
    if not user:
        return RedirectResponse("/login?next=/ipod")
    try:
        name, key = lastfm_get_session(token)
    except Exception as e:
        return RedirectResponse(f"/user/{urllib.parse.quote(user['username'])}?lastfm_error="
                                + urllib.parse.quote(str(e)))
    with state.write_lock:
        state.conn.execute("UPDATE users SET lastfm_user=?, lastfm_session=? WHERE id=?",
                           (name, key, user["id"]))
        state.conn.commit()
    return RedirectResponse(f"/user/{urllib.parse.quote(user['username'])}")


@router.delete("/api/me/lastfm")
def lastfm_disconnect(user: dict = Depends(require_user)):
    with state.write_lock:
        state.conn.execute("UPDATE users SET lastfm_user=NULL, lastfm_session=NULL WHERE id=?",
                           (user["id"],))
        state.conn.commit()
    return {"ok": True}
