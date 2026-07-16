#!/usr/bin/env bash
# cd-sync.sh — push new CD rips from the Mac's Apple Music library to the NUC.
#
# Triggered by: com.ben.cd-sync.plist (launchd WatchPaths) whenever the Apple Music
# library folder changes. Also runnable by hand any time.
#
# Flow:
#   Insert CD → Apple Music imports to MUSIC_APP_DIR/<Artist>/<Album>/*.m4a
#   → this script debounces, then rsyncs new/changed files to the NUC over Tailscale
#   → the server's music-watcher picks them up → beets → iPod on next plug-in.
#
# Design notes:
#   - NO --delete on this hop. The Mac is a SOURCE, not the authority; deleting a CD
#     from Apple Music must never wipe it from the server.
#   - Debounce: a full album import writes many files; we wait for quiet before pushing.
#   - After a successful push we POST a structured event to the web app so "CD import:
#     <album>" shows up on the homeserver webpage.

set -uo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
# Mac has no /etc/ipod-sync; keep a local copy or edit these defaults.
CONFIG_FILE="${CONFIG_FILE:-$HOME/.config/ipod-sync/config.env}"
[[ -f "$CONFIG_FILE" ]] && source "$CONFIG_FILE"

MUSIC_APP_DIR="${MUSIC_APP_DIR:-/Users/ben/Music/Music/Media.localized/Music}"
NUC_HOST="${NUC_HOST:-100.94.158.95}"
SYNC_USER="${SYNC_USER:-jimin}"
REMOTE_MUSIC_DIR="${REMOTE_MUSIC_DIR:-/mnt/data/media/music/FLAC}"
WEB_PORT="${WEB_PORT:-8337}"
EVENT_TOKEN="${EVENT_TOKEN:-}"
DEBOUNCE_SECS="${DEBOUNCE_SECS:-20}"

LOG_FILE="$HOME/Library/Logs/ipod-sync/cd-sync.log"
LOCK_FILE="$HOME/Library/Caches/ipod-sync-cd-sync.lock"
mkdir -p "$(dirname "$LOG_FILE")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# ── POST a structured event to the homeserver web app ───────────────────────────
post_event() {
  local kind="$1" message="$2" detail="${3:-}"
  local url="http://${NUC_HOST}:${WEB_PORT}/events"
  local args=(-s -m 5 -X POST "$url"
              -H "Content-Type: application/json"
              -d "$(python3 -c 'import json,sys; print(json.dumps({"source":"cd","kind":sys.argv[1],"message":sys.argv[2],"detail":sys.argv[3]}))' "$kind" "$message" "$detail")")
  [[ -n "$EVENT_TOKEN" ]] && args+=(-H "X-Event-Token: $EVENT_TOKEN")
  curl "${args[@]}" >/dev/null 2>&1 || log "WARN: could not POST event to $url (non-fatal)."
}

# ── Single-instance lock (launchd may fire rapidly during a big import) ─────────
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Another cd-sync is already running — exiting."
  exit 0
fi

# ── Debounce: wait until the library stops changing ─────────────────────────────
# Compare a cheap signature of the tree; loop until it's stable for DEBOUNCE_SECS.
signature() {
  # count + latest mtime of audio files; fast enough for a personal library
  find "$MUSIC_APP_DIR" -type f \( -iname '*.m4a' -o -iname '*.aac' -o -iname '*.mp3' \
       -o -iname '*.flac' \) -not -name '._*' 2>/dev/null \
    | wc -l | tr -d ' '
}

if [[ ! -d "$MUSIC_APP_DIR" ]]; then
  log "ERROR: Apple Music dir not found: $MUSIC_APP_DIR"
  exit 1
fi

log "Change detected. Debouncing (${DEBOUNCE_SECS}s of quiet)…"
prev="$(signature)"
while true; do
  sleep "$DEBOUNCE_SECS"
  cur="$(signature)"
  [[ "$cur" == "$prev" ]] && break
  log "Still importing (files: $prev → $cur) — waiting…"
  prev="$cur"
done
log "Library stable ($prev audio files). Pushing to $NUC_HOST…"

# ── Push over Tailscale SSH ──────────────────────────────────────────────────────
# -rt + modify-window=2 : recurse, preserve times, tolerate vfat/APFS clock skew.
# NO --delete. --itemize-changes so we can report what actually moved.
RSYNC_OUT="$(rsync \
  --recursive --times --modify-window=2 \
  --itemize-changes --human-readable \
  --exclude='.DS_Store' --exclude='._*' \
  --exclude='.Spotlight-*' --exclude='.Trashes' --exclude='*.tmp' \
  -e ssh \
  "$MUSIC_APP_DIR/" \
  "${SYNC_USER}@${NUC_HOST}:${REMOTE_MUSIC_DIR}/" 2>&1)"
RSYNC_CODE=$?

echo "$RSYNC_OUT" >> "$LOG_FILE"

# Count transferred files (itemize lines beginning with > = received-to-remote sends)
CHANGED="$(echo "$RSYNC_OUT" | grep -cE '^>f' || true)"

if [[ $RSYNC_CODE -ne 0 && $RSYNC_CODE -ne 23 && $RSYNC_CODE -ne 24 ]]; then
  log "ERROR: rsync failed (code $RSYNC_CODE)."
  post_event error "CD push to server failed (rsync $RSYNC_CODE)"
  osascript -e "display notification \"rsync failed (code $RSYNC_CODE)\" with title \"iPod Sync\"" 2>/dev/null || true
  exit "$RSYNC_CODE"
fi

if [[ "${CHANGED:-0}" -gt 0 ]]; then
  log "Pushed $CHANGED new/changed file(s) to server."
  post_event success "CD import: $CHANGED new file(s) pushed to server" "beets will tag them next"
  osascript -e "display notification \"$CHANGED files → server (beets will tag)\" with title \"iPod Sync\" subtitle \"CD import synced\"" 2>/dev/null || true
else
  log "Nothing new to push."
fi
