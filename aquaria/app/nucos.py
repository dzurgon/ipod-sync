"""
nucOS: the API client and the outbox sender for the Listening habit.

The contract (nucOS docs/integrations.md) is fixed:
- POST /api/integrations/v1/activity {"items": [...], "deleted": [...]} (≤ 500), Bearer token,
  5 s timeouts; GET /api/integrations/v1/me says which habit the token feeds;
- plays.record() writes an outbox row in the same transaction as the play; this module sends
  unsent rows oldest first, rows for the same id collapsed to the newest. 2xx marks them sent;
  a network error or 5xx backs off 30 s, doubling to 1 h, forever; 401 stops ("token
  rejected"), 410 stops ("habit removed"). `rejected` items are logged (they won't succeed on
  retry); `kept` days are nucOS answers made by hand, which win — not an error;
- nothing is sent without NUCOS_URL and NUCOS_TOKEN; items dated in the future wait for their
  time; the token is never logged.

What a connection already accepted is remembered per connection id (from GET /me), so an
unchanged item is never sent twice (a backfill is safe to repeat) and a brand-new
connection gets everything again.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from . import config

log = logging.getLogger("aquaria.nucos")

MAX_BATCH = 500
BACKOFF_FIRST = 30.0
BACKOFF_MAX = 3600.0
IDLE_POLL = 15.0

MSG_NOT_CONNECTED = "nucOS: not connected"
MSG_TOKEN = "nucOS token rejected: make a new one in nucOS (Marketplace → Listening → New token…)"
MSG_GONE = "the Listening habit was removed in nucOS"


# ---------------------------------------------------------------- client

class NucosError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# (method, path, json body or None) -> (status, parsed JSON or None)
Transport = Callable[[str, str, dict | None], tuple[int, object]]


def _urllib_transport(url: str, token: str) -> Transport:
    def send(method: str, path: str, body: dict | None):
        req = urllib.request.Request(
            url + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "User-Agent": "Aquaria"})
        try:
            with urllib.request.urlopen(req, timeout=config.NUCOS_TIMEOUT) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        try:
            return status, json.loads(raw or b"null")
        except ValueError:
            return status, raw[:200].decode(errors="replace")
    return send


class Client:
    def __init__(self, url: str, token: str, transport: Transport | None = None):
        self.url = url
        self._send = transport or _urllib_transport(url, token)

    def __repr__(self) -> str:      # never show the token
        return f"<nucOS client {self.url}>"

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            status, data = self._send(method, path, body)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise NucosError(f"can't reach nucOS ({type(e).__name__})") from None
        if not 200 <= status < 300:
            detail = (data.get("error") or data.get("detail") or "") if isinstance(data, dict) else data
            raise NucosError(f"nucOS answered {status}{': ' + str(detail) if detail else ''}", status)
        if not isinstance(data, dict):
            raise NucosError("nucOS answered with something that isn't JSON", status)
        return data

    def me(self) -> dict:
        return self._call("GET", "/api/integrations/v1/me")

    def push(self, items: list[dict], deleted: list[str]) -> dict:
        return self._call("POST", "/api/integrations/v1/activity", {"items": items, "deleted": deleted})

    def days(self, start: dt.date, end: dt.date) -> dict:
        return self._call("GET", f"/api/integrations/v1/days?from={start.isoformat()}&to={end.isoformat()}")


def make_client() -> Client | None:
    if not config.nucos_configured():
        return None
    return Client(config.nucos_url(), config.nucos_token())


def describe_me(me: dict) -> str:
    h = me.get("habit") or {}
    return f"Connected to nucOS: {h.get('name', '?')} ({h.get('sphere', '?')})"


# ---------------------------------------------------------------- small key/value state (meta table)

def kv_get(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def kv_set(conn, key: str, value: str | None) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


# ---------------------------------------------------------------- the outbox

@dataclass
class FlushResult:
    sent: list[str] = field(default_factory=list)       # ids pushed (items and deletions)
    skipped: int = 0                                     # unchanged since nucOS last accepted them
    held: int = 0                                        # dated in the future: waits for its time
    rejected: list[dict] = field(default_factory=list)
    kept: list[dict] = field(default_factory=list)
    more: bool = False                                   # another batch is waiting
    reply: dict | None = None


def _digest(payload: str | None) -> str:
    return "deleted" if payload is None else hashlib.sha256(payload.encode()).hexdigest()


def pending_count(conn) -> int:
    return conn.execute("SELECT COUNT(DISTINCT item_id) FROM nucos_outbox WHERE sent_at IS NULL").fetchone()[0]


def _mark_sent(conn, item_id: str, upto_seq: int, now: float) -> None:
    conn.execute("UPDATE nucos_outbox SET sent_at=? WHERE item_id=? AND seq<=? AND sent_at IS NULL",
                 (now, item_id, upto_seq))


def flush_once(conn, lock, client: Client, connection: str, now: float | None = None) -> FlushResult:
    """Send one batch. Raises NucosError (status set for HTTP errors) and marks nothing on failure."""
    now = time.time() if now is None else now
    rows = conn.execute("SELECT seq, item_id, payload FROM nucos_outbox WHERE sent_at IS NULL ORDER BY seq").fetchall()
    first: dict[str, int] = {}
    newest: dict[str, object] = {}
    for r in rows:
        first.setdefault(r["item_id"], r["seq"])
        newest[r["item_id"]] = r                         # same id → the newest row wins
    sent_digest = {r["item_id"]: r["digest"] for r in conn.execute(
        "SELECT item_id, digest FROM nucos_sent WHERE connection=?", (connection,))}

    res = FlushResult()
    batch, unchanged = [], []
    for item_id in sorted(newest, key=first.__getitem__):       # oldest first
        r = newest[item_id]
        if r["payload"] is not None:
            at = json.loads(r["payload"]).get("at", "")
            try:
                if dt.datetime.fromisoformat(at).timestamp() > now + 60:
                    res.held += 1                        # never send a future date
                    continue
            except ValueError:
                pass
        prev = sent_digest.get(item_id)
        if prev == _digest(r["payload"]) or (r["payload"] is None and prev is None):
            unchanged.append(r)                          # nucOS has exactly this (or never had it)
            continue
        if len(batch) == MAX_BATCH:
            res.more = True
            break
        batch.append(r)

    with lock:
        for r in unchanged:
            _mark_sent(conn, r["item_id"], r["seq"], now)
        conn.commit()
    res.skipped = len(unchanged)
    if not batch:
        return res

    items = [json.loads(r["payload"]) for r in batch if r["payload"] is not None]
    deleted = [r["item_id"] for r in batch if r["payload"] is None]
    reply = client.push(items, deleted)
    res.reply = reply
    res.rejected = [x for x in (reply.get("rejected") or []) if isinstance(x, dict)]
    res.kept = [x for x in (reply.get("kept") or []) if isinstance(x, dict)]
    for x in res.rejected:                               # they won't succeed on retry
        log.warning("nucOS rejected %s: %s", x.get("id"), x.get("error"))
    with lock:
        for r in batch:
            _mark_sent(conn, r["item_id"], r["seq"], now)
            conn.execute("INSERT INTO nucos_sent(connection, item_id, digest) VALUES (?, ?, ?) "
                         "ON CONFLICT(connection, item_id) DO UPDATE SET digest=excluded.digest",
                         (connection, r["item_id"], _digest(r["payload"])))
        if res.rejected:
            old = json.loads(kv_get(conn, "nucos.rejected", "[]"))
            stamp = dt.datetime.now().replace(microsecond=0).isoformat()
            kv_set(conn, "nucos.rejected", json.dumps(([{**x, "at": stamp} for x in res.rejected] + old)[:50]))
        conn.execute("DELETE FROM nucos_outbox WHERE sent_at IS NOT NULL AND sent_at < ?", (now - 30 * 86400,))
        conn.commit()
    res.sent = [r["item_id"] for r in batch]
    return res


# ---------------------------------------------------------------- the worker

class Worker:
    """Sends the outbox in the background. `step()` is one attempt and returns how long to wait
    (None = stopped until something wakes it: not configured, token rejected, habit removed)."""

    def __init__(self, conn, lock, client_factory=make_client, clock=time.monotonic):
        self.conn, self.lock = conn, lock
        self.client_factory = client_factory
        self.clock = clock
        self.backoff: float | None = None
        self.retry_at: float | None = None
        self.halted: str | None = None
        self.connection: str | None = None
        self._client: Client | None = None
        self._wake = threading.Event()
        self._force = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _status(self, status: str, error: str | None = None) -> None:
        with self.lock:
            kv_set(self.conn, "nucos.status", status)
            kv_set(self.conn, "nucos.error", error)
            self.conn.commit()

    def _fail(self, e: NucosError) -> float | None:
        if e.status in (401, 410):
            msg = MSG_TOKEN if e.status == 401 else MSG_GONE
            self.halted, self.connection, self._client = msg, None, None
            self._status("token_rejected" if e.status == 401 else "habit_removed", msg)
            log.error(msg)
            return None
        self.backoff = BACKOFF_FIRST if self.backoff is None else min(self.backoff * 2, BACKOFF_MAX)
        self.retry_at = self.clock() + self.backoff
        self._status("retrying", f"{e} — retrying in {int(self.backoff)} s")
        log.warning("nucOS push failed (%s); retrying in %d s", e, self.backoff)
        return self.backoff

    def step(self, force: bool = False) -> float | None:
        if not config.nucos_configured():
            self._status("not_connected", MSG_NOT_CONNECTED)
            self._client = None
            return None
        if self.halted and not force:
            return None
        if not force and self.retry_at is not None and self.clock() < self.retry_at:
            return self.retry_at - self.clock()
        if self._client is None:
            self._client = self.client_factory()
        try:
            if self.connection is None:
                me = self._client.me()
                self.connection = str((me.get("connection") or {}).get("id") or "default")
                with self.lock:
                    kv_set(self.conn, "nucos.me", json.dumps(me))
                    self.conn.commit()
            res = flush_once(self.conn, self.lock, self._client, self.connection)
        except NucosError as e:
            return self._fail(e)
        self.backoff = self.retry_at = None
        self.halted = None
        self._status("ok")
        if res.sent:
            with self.lock:
                kv_set(self.conn, "nucos.last_sync", dt.datetime.now().replace(microsecond=0).isoformat())
                kv_set(self.conn, "nucos.last_push", json.dumps(
                    {"sent": len(res.sent), "rejected": len(res.rejected), "kept": len(res.kept),
                     "accepted": (res.reply or {}).get("accepted")}))
                self.conn.commit()
            log.info("sent %d item(s) to nucOS (%d rejected, %d kept)", len(res.sent), len(res.rejected), len(res.kept))
        return 0.0 if res.more else IDLE_POLL

    def wake(self, force: bool = False) -> None:
        self._force = self._force or force
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            force, self._force = self._force, False
            try:
                delay = self.step(force=force)
            except Exception:                           # never let the sender die; the outbox keeps everything
                log.exception("nucOS worker error")
                delay = BACKOFF_FIRST
            if delay:
                self._wake.wait(timeout=delay)
            elif delay is None:
                self._wake.wait()
            self._wake.clear()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="nucos-outbox", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


def status(conn) -> dict:
    configured = config.nucos_configured()
    st = kv_get(conn, "nucos.status") if configured else "not_connected"
    me = kv_get(conn, "nucos.me")
    return {
        "configured": configured,
        "url": config.nucos_url() or None,
        "user": config.NUCOS_USER,
        "status": st or "starting",
        "error": MSG_NOT_CONNECTED if not configured else kv_get(conn, "nucos.error"),
        "me": json.loads(me) if me and configured else None,
        "last_sync": kv_get(conn, "nucos.last_sync"),
        "last_push": json.loads(kv_get(conn, "nucos.last_push", "null") or "null"),
        "pending": pending_count(conn),
        "rejected": json.loads(kv_get(conn, "nucos.rejected", "[]")),
    }
