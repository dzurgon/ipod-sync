#!/usr/bin/env python3
"""
scrobble.py — parse Rockbox .scrobbler.log and submit plays to Last.fm

Rockbox log format (tab-separated, one track per line):
  ARTIST \t ALBUM \t TITLE \t TRACKNUMBER \t DURATION \t RATING \t TIMESTAMP \t MBID

  RATING: L = loved, S = skipped, '' = normal play
  TIMESTAMP: Unix timestamp (UTC) of when play started
  Lines beginning with # are header/comment lines.

Submitted entries are marked by appending '  # SUBMITTED' to the line so they
are never double-scrobbled. The file stays on the iPod across syncs.

Usage:
  python3 scrobble.py --log /path/to/.scrobbler.log --config /etc/ipod-sync/config.env

First-time auth:
  python3 scrobble.py --auth --config /etc/ipod-sync/config.env
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"

# Last.fm only accepts scrobbles within the last 14 days
MAX_SCROBBLE_AGE_SECS = 14 * 24 * 60 * 60

# Last.fm batch limit per request
BATCH_SIZE = 50

SUBMITTED_MARKER = "\t# SUBMITTED"

# ── Config loading ────────────────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    """Source a shell config.env file and return key=value pairs as a dict."""
    cfg = {}
    with open(config_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                # Strip surrounding quotes if present
                val = val.strip().strip("'\"")
                cfg[key.strip()] = val
    return cfg


# ── Last.fm API helpers ───────────────────────────────────────────────────────

def _api_sig(params: dict, secret: str) -> str:
    """Generate Last.fm API method signature (md5 of sorted params + secret)."""
    sig_str = "".join(k + params[k] for k in sorted(params)) + secret
    return hashlib.md5(sig_str.encode("utf-8")).hexdigest()


def _post(params: dict, api_secret: str) -> dict:
    """Sign and POST to Last.fm API; return parsed JSON response."""
    params["api_sig"] = _api_sig(params, api_secret)
    params["format"] = "json"

    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(LASTFM_API_URL, data=data, method="POST")
    req.add_header("User-Agent", "ipod-sync/1.0 (github.com/yourname/ipod-sync)")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        raise RuntimeError(f"Last.fm HTTP {e.code}: {body}") from e


def _get(params: dict) -> dict:
    """Unsigned GET from Last.fm API (for public endpoints)."""
    params["format"] = "json"
    url = LASTFM_API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "ipod-sync/1.0")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Authentication ────────────────────────────────────────────────────────────

def get_session_key(api_key: str, api_secret: str, username: str, session_file: Path) -> str:
    """
    Return a cached session key, or perform the mobile auth flow to get one.

    Mobile auth (auth.getMobileSession) uses username + password MD5.
    This avoids the web OAuth redirect loop, which is impractical for scripts.
    The session key never expires and is stored in session_file.
    """
    if session_file.exists():
        key = session_file.read_text().strip()
        if key:
            return key

    print("No Last.fm session key found. Starting authentication...")
    import getpass
    password = getpass.getpass(f"Last.fm password for '{username}': ")
    password_md5 = hashlib.md5(password.encode("utf-8")).hexdigest()

    params = {
        "method": "auth.getMobileSession",
        "username": username,
        "authToken": hashlib.md5((username + password_md5).encode()).hexdigest(),
        "api_key": api_key,
    }
    resp = _post(params, api_secret)

    if "session" not in resp:
        raise RuntimeError(f"Auth failed: {resp}")

    session_key = resp["session"]["key"]
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(session_key)
    # Restrict permissions — session key is sensitive
    session_file.chmod(0o600)
    print(f"Session key saved to {session_file}")
    return session_key


# ── Scrobbler log parsing ─────────────────────────────────────────────────────

def parse_scrobbler_log(log_path: str) -> tuple[list[dict], list[str]]:
    """
    Parse a Rockbox .scrobbler.log file.

    Returns:
        pending   — list of track dicts ready to submit
        raw_lines — all lines from the file (for rewriting with markers)
    """
    pending = []
    raw_lines = []

    now = int(time.time())

    with open(log_path, encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            raw_lines.append(raw_line.rstrip("\n"))
            line = raw_line.strip()

            # Skip header comments and already-submitted entries
            if line.startswith("#") or SUBMITTED_MARKER.strip() in line:
                continue
            if not line:
                continue

            parts = line.split("\t")
            if len(parts) < 7:
                continue

            artist    = parts[0].strip()
            album     = parts[1].strip()
            title     = parts[2].strip()
            tracknum  = parts[3].strip()
            duration  = parts[4].strip()
            rating    = parts[5].strip()
            timestamp = parts[6].strip()
            mbid      = parts[7].strip() if len(parts) > 7 else ""

            # Skip skipped tracks
            if rating == "S":
                continue

            # Skip if we can't parse the timestamp
            try:
                ts = int(timestamp)
            except ValueError:
                continue

            # Skip plays older than 14 days (Last.fm rejects them)
            if now - ts > MAX_SCROBBLE_AGE_SECS:
                print(f"  SKIP (too old): {artist} - {title} ({timestamp})")
                continue

            # Skip tracks too short to be meaningful (< 30s)
            try:
                dur = int(duration)
            except ValueError:
                dur = 0
            if dur < 30:
                continue

            pending.append({
                "artist":    artist,
                "album":     album,
                "track":     title,
                "trackNumber": tracknum,
                "duration":  duration,
                "timestamp": timestamp,
                "mbid":      mbid,
                "_raw_line": raw_line.rstrip("\n"),
            })

    return pending, raw_lines


# ── Submission ────────────────────────────────────────────────────────────────

def submit_batch(tracks: list[dict], api_key: str, api_secret: str, session_key: str) -> int:
    """Submit a batch of up to 50 tracks. Returns number successfully scrobbled."""
    params: dict = {
        "method": "track.scrobble",
        "api_key": api_key,
        "sk": session_key,
    }

    for i, track in enumerate(tracks):
        params[f"artist[{i}]"]    = track["artist"]
        params[f"track[{i}]"]     = track["track"]
        params[f"timestamp[{i}]"] = track["timestamp"]
        if track["album"]:
            params[f"album[{i}]"] = track["album"]
        if track["duration"]:
            params[f"duration[{i}]"] = track["duration"]
        if track["mbid"]:
            params[f"mbid[{i}]"] = track["mbid"]

    resp = _post(params, api_secret)

    # Parse response
    scrobbles = resp.get("scrobbles", {})
    attr = scrobbles.get("@attr", {})
    accepted = int(attr.get("accepted", 0))
    ignored  = int(attr.get("ignored", 0))

    if ignored:
        print(f"  WARNING: {ignored} tracks ignored by Last.fm (check for bad metadata).")

    return accepted


def mark_submitted(log_path: str, submitted_raw_lines: set[str]) -> None:
    """Rewrite .scrobbler.log marking submitted lines with SUBMITTED_MARKER."""
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.rstrip("\n")
        if stripped in submitted_raw_lines:
            new_lines.append(stripped + SUBMITTED_MARKER + "\n")
        else:
            new_lines.append(line)

    with open(log_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Scrobble Rockbox .scrobbler.log to Last.fm")
    parser.add_argument("--log",    required=False, help="Path to .scrobbler.log on iPod")
    parser.add_argument("--config", required=True,  help="Path to config.env")
    parser.add_argument("--auth",   action="store_true",
                        help="Run authentication only and save session key, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse log and show what would be scrobbled, without submitting")
    args = parser.parse_args()

    cfg = load_config(args.config)

    api_key    = cfg.get("LASTFM_API_KEY", "")
    api_secret = cfg.get("LASTFM_API_SECRET", "")
    username   = cfg.get("LASTFM_USERNAME", "")

    if not all([api_key, api_secret, username]):
        print("ERROR: LASTFM_API_KEY, LASTFM_API_SECRET, LASTFM_USERNAME must be set in config.",
              file=sys.stderr)
        return 1

    # Session key stored separately from config (not in config.env)
    session_file = Path(os.path.expanduser("~/.config/ipod-sync/lastfm-session"))

    # ── Auth-only mode ────────────────────────────────────────────────────────
    if args.auth:
        get_session_key(api_key, api_secret, username, session_file)
        print("Authentication successful.")
        return 0

    # ── Normal scrobble mode ──────────────────────────────────────────────────
    if not args.log:
        print("ERROR: --log is required unless --auth is specified.", file=sys.stderr)
        return 1

    log_path = args.log
    if not os.path.exists(log_path):
        print(f"No scrobbler log at {log_path} — nothing to do.")
        return 0

    print(f"Parsing: {log_path}")
    pending, _raw_lines = parse_scrobbler_log(log_path)

    if not pending:
        print("No new tracks to scrobble.")
        return 0

    print(f"Found {len(pending)} track(s) to scrobble:")
    for t in pending:
        print(f"  [{t['timestamp']}] {t['artist']} — {t['track']} ({t['album']})")

    if args.dry_run:
        print("Dry-run mode — not submitting.")
        return 0

    session_key = get_session_key(api_key, api_secret, username, session_file)

    submitted_raw_lines: set[str] = set()
    total_accepted = 0

    # Submit in batches of 50
    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i : i + BATCH_SIZE]
        print(f"Submitting batch {i // BATCH_SIZE + 1} ({len(batch)} tracks)...")
        try:
            accepted = submit_batch(batch, api_key, api_secret, session_key)
            total_accepted += accepted
            print(f"  Accepted: {accepted}/{len(batch)}")
            # Mark all tracks in this batch as submitted (even if some ignored)
            for track in batch:
                submitted_raw_lines.add(track["_raw_line"])
        except RuntimeError as e:
            print(f"  ERROR submitting batch: {e}", file=sys.stderr)
            # Don't mark as submitted — they'll retry next sync
            continue
        # Last.fm rate limit: max 5 requests/sec; be conservative
        time.sleep(0.5)

    # Rewrite the log with SUBMITTED markers
    if submitted_raw_lines:
        mark_submitted(log_path, submitted_raw_lines)
        print(f"Marked {len(submitted_raw_lines)} line(s) as submitted in {log_path}")

    print(f"Scrobbling complete: {total_accepted} track(s) accepted by Last.fm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
