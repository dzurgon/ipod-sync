#!/usr/bin/env bash
# music-watcher.sh — watches MUSIC_DIR for new files and triggers beets + Nextcloud rescan
#
# Deploy to: /usr/local/lib/ipod-sync/music-watcher.sh
# Managed by: server/watcher/music-watcher.service (systemd)
#
# Dependencies: inotify-tools, beets, curl, python3
#   apt install inotify-tools
#
# NOTE on the debounce (this was the long-standing bug):
#   The old version ran the inotify reader in one subshell (right of the pipe) and
#   the flush timer in a *second* backgrounded subshell. Each got its own copy of
#   PENDING_DIRS / LAST_EVENT_TIME, so the timer never saw the reader's events and
#   beets never flushed. This version uses a SINGLE loop: it blocks for the first
#   event, then drains further events with `read -t $DEBOUNCE_SECS`; when the read
#   times out (quiet period) the same shell flushes. One process, one set of vars.

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
EVENT_SOURCE="beets"

# Shared helpers (log/warn/die, emit_event, require_healthy_mount)
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"
[[ -f "$LIB_DIR/common.sh" ]] || LIB_DIR="/usr/local/lib/ipod-sync/lib"
# shellcheck source=/dev/null
source "$LIB_DIR/common.sh"

DEBOUNCE_SECS="${DEBOUNCE_SECS:-30}"

# ── Nextcloud rescan ──────────────────────────────────────────────────────────
nextcloud_rescan() {
  log "Triggering Nextcloud files:scan for $NEXTCLOUD_USER..."
  if docker ps --format '{{.Names}}' | grep -q 'nextcloud'; then
    docker exec nextcloud php occ files:scan --path="$NEXTCLOUD_USER/files/FLAC" --quiet 2>>"$LOGFILE" \
      && log "Nextcloud rescan complete." \
      || log "WARN: Nextcloud rescan failed (non-fatal)."
  else
    curl -s -u "${NEXTCLOUD_USER}:${NEXTCLOUD_APP_PASSWORD}" \
      -X PROPFIND "${NEXTCLOUD_URL}/remote.php/dav/files/${NEXTCLOUD_USER}/FLAC" \
      -o /dev/null && log "Nextcloud WebDAV nudge sent." \
      || log "WARN: Nextcloud WebDAV nudge failed."
  fi
}

# ── Beets import ──────────────────────────────────────────────────────────────
run_beets() {
  local target="$1"
  local album; album="$(basename "$target")"
  log "Running beets on: $target"
  if beet import -q "$target" >> "$LOGFILE" 2>&1; then
    log "beets import complete: $target"
    emit_event beets success "Tagged & filed: $album"
  else
    log "WARN: beets reported issues for $target — check $LOG_DIR/beets-import.log"
    emit_event beets warning "beets had issues with: $album" "See beets-import.log"
  fi
}

# ── Flush: run beets on each pending dir, then rescan Nextcloud ─────────────────
flush_pending() {
  (( ${#PENDING_DIRS[@]} == 0 )) && return

  declare -A seen
  local unique_dirs=()
  for d in "${PENDING_DIRS[@]}"; do
    if [[ -z "${seen[$d]+_}" ]]; then
      seen[$d]=1
      unique_dirs+=("$d")
    fi
  done

  log "Debounce elapsed — flushing ${#unique_dirs[@]} folder(s)."
  for dir in "${unique_dirs[@]}"; do
    run_beets "$dir"
  done
  nextcloud_rescan
  PENDING_DIRS=()
}

# ── Main watch loop (single process) ───────────────────────────────────────────
log "Starting music watcher on: $MUSIC_DIR (debounce ${DEBOUNCE_SECS}s)"
emit_event beets info "Music watcher started"

is_audio_or_art() {
  case "${1,,}" in
    *.flac|*.mp3|*.ogg|*.opus|*.m4a|*.aac|*.wav|*.jpg|*.png) return 0 ;;
    *) return 1 ;;
  esac
}

PENDING_DIRS=()

# inotifywait streams "DIR/FILE" per line into this while-read loop. The loop lives
# in one subshell (right of the pipe) and does BOTH collection and flushing.
inotifywait \
  --monitor --recursive --quiet \
  --format '%w%f' \
  --event close_write,moved_to,create \
  "$MUSIC_DIR" \
| while true; do
    # Block indefinitely for the first event of a burst.
    if ! IFS= read -r filepath; then
      break   # inotifywait exited
    fi
    if is_audio_or_art "$filepath"; then
      PENDING_DIRS+=("$(dirname "$filepath")")
      emit_event upload info "New file: $(basename "$filepath")"
      log "Event: $filepath"
    fi

    # Drain the rest of the burst; each read waits up to DEBOUNCE_SECS.
    # When a read times out (quiet), we fall through and flush.
    while IFS= read -r -t "$DEBOUNCE_SECS" filepath; do
      if is_audio_or_art "$filepath"; then
        PENDING_DIRS+=("$(dirname "$filepath")")
        emit_event upload info "New file: $(basename "$filepath")"
        log "Event: $filepath"
      fi
    done

    flush_pending
  done
