#!/usr/bin/env bash
# music-watcher.sh — watches MUSIC_DIR for new files and triggers beets + Nextcloud rescan
#
# Deploy to: /usr/local/lib/ipod-sync/music-watcher.sh
# Managed by: server/watcher/music-watcher.service (systemd)
#
# Dependencies: inotify-tools, beets, curl
#   apt install inotify-tools

set -euo pipefail

CONFIG_FILE="${CONFIG_FILE:-/etc/ipod-sync/config.env}"
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: config not found at $CONFIG_FILE" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$CONFIG_FILE"

LOG_DIR="${LOG_DIR:-/var/log/ipod-sync}"
mkdir -p "$LOG_DIR"
LOGFILE="$LOG_DIR/watcher.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"; }

# ── Nextcloud rescan ──────────────────────────────────────────────────────────
nextcloud_rescan() {
  log "Triggering Nextcloud files:scan for $NEXTCLOUD_USER..."
  # Nextcloud OCC via Docker — adjust container name if yours differs
  if docker ps --format '{{.Names}}' | grep -q 'nextcloud'; then
    docker exec nextcloud php occ files:scan --path="$NEXTCLOUD_USER/files/FLAC" --quiet 2>>"$LOGFILE" \
      && log "Nextcloud rescan complete." \
      || log "WARN: Nextcloud rescan failed (non-fatal)."
  else
    # Fallback: WebDAV PROPFIND to nudge the client (less reliable)
    curl -s -u "${NEXTCLOUD_USER}:${NEXTCLOUD_APP_PASSWORD}" \
      -X PROPFIND "${NEXTCLOUD_URL}/remote.php/dav/files/${NEXTCLOUD_USER}/FLAC" \
      -o /dev/null && log "Nextcloud WebDAV nudge sent." \
      || log "WARN: Nextcloud WebDAV nudge failed."
  fi
}

# ── Beets import ──────────────────────────────────────────────────────────────
run_beets() {
  local target="$1"
  log "Running beets on: $target"
  beet import -q "$target" >> "$LOGFILE" 2>&1 \
    && log "beets import complete: $target" \
    || log "WARN: beets reported issues for $target — check $LOG_DIR/beets-import.log"
}

# ── Debounce logic ────────────────────────────────────────────────────────────
# We collect events for DEBOUNCE_SECS seconds after the last file event
# before triggering beets, so a 50-track album import fires beets once, not 50x.
DEBOUNCE_SECS=30
PENDING_DIRS=()
LAST_EVENT_TIME=0

flush_pending() {
  if [[ ${#PENDING_DIRS[@]} -eq 0 ]]; then return; fi

  # De-duplicate directories
  declare -A seen
  local unique_dirs=()
  for d in "${PENDING_DIRS[@]}"; do
    if [[ -z "${seen[$d]+_}" ]]; then
      seen[$d]=1
      unique_dirs+=("$d")
    fi
  done

  for dir in "${unique_dirs[@]}"; do
    run_beets "$dir"
  done

  nextcloud_rescan

  PENDING_DIRS=()
  LAST_EVENT_TIME=0
}

# ── Main watch loop ───────────────────────────────────────────────────────────
log "Starting music watcher on: $MUSIC_DIR"

# inotifywait outputs one event per line: DIR EVENT FILENAME
# We watch for close_write (file fully written) and moved_to (rsync/mv arrival)
inotifywait \
  --monitor \
  --recursive \
  --quiet \
  --format '%w%f' \
  --event close_write,moved_to,create \
  "$MUSIC_DIR" \
| while IFS= read -r filepath; do
    # Only care about audio files and cover art
    case "${filepath,,}" in
      *.flac|*.mp3|*.ogg|*.opus|*.m4a|*.aac|*.wav|*.jpg|*.png) ;;
      *) continue ;;
    esac

    dir="$(dirname "$filepath")"
    PENDING_DIRS+=("$dir")
    LAST_EVENT_TIME=$(date +%s)
    log "Event: $filepath"

    # Check if debounce window has elapsed; if so, flush
    # (This runs in the read loop so we also check on each new event)
    now=$(date +%s)
    if (( now - LAST_EVENT_TIME >= DEBOUNCE_SECS )) && [[ ${#PENDING_DIRS[@]} -gt 0 ]]; then
      flush_pending
    fi
  done &

WATCH_PID=$!

# Separate timer process to flush even if no new events arrive
while true; do
  sleep "$DEBOUNCE_SECS"
  now=$(date +%s)
  if [[ $LAST_EVENT_TIME -gt 0 ]] && (( now - LAST_EVENT_TIME >= DEBOUNCE_SECS )); then
    flush_pending
  fi
done &

TIMER_PID=$!

# Graceful shutdown
trap 'log "Shutting down watcher."; kill $WATCH_PID $TIMER_PID 2>/dev/null; exit 0' TERM INT

wait $WATCH_PID
