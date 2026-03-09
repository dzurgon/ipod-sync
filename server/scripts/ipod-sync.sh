#!/usr/bin/env bash
# ipod-sync.sh — main iPod sync script, triggered on USB connect
#
# Deploy to: /usr/local/lib/ipod-sync/ipod-sync.sh
#
# What it does (in order):
#   1. Wait for iPod to be fully mounted
#   2. rsync music library → iPod (server is authoritative)
#   3. Sync playlists: push server→iPod, pull any new ones iPod→server
#   4. Parse .scrobbler.log → submit to Last.fm (via scrobble.py)
#   5. Log a timestamped summary

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_FILE="${CONFIG_FILE:-/etc/ipod-sync/config.env}"
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: config not found at $CONFIG_FILE" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$CONFIG_FILE"

LOG_DIR="${LOG_DIR:-/var/log/ipod-sync}"
mkdir -p "$LOG_DIR"
LOGFILE="$LOG_DIR/ipod-sync-$(date '+%Y%m%d-%H%M%S').log"

# Keep only the last 30 sync logs
find "$LOG_DIR" -name 'ipod-sync-*.log' | sort | head -n -30 | xargs -r rm --

SCROBBLE_SCRIPT="/usr/local/lib/ipod-sync/scrobble.py"

# ── Logging helpers ───────────────────────────────────────────────────────────
log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"; }
warn() { log "WARN: $*"; }
die()  { log "ERROR: $*"; exit 1; }

# ── Section headers ───────────────────────────────────────────────────────────
section() {
  log ""
  log "════════════════════════════════════════"
  log "  $*"
  log "════════════════════════════════════════"
}

# ══════════════════════════════════════════════════════════════════════════════
# 1. WAIT FOR MOUNT
# ══════════════════════════════════════════════════════════════════════════════
section "Waiting for iPod mount"

MOUNT_TIMEOUT=60
ELAPSED=0
IPOD_MOUNT=""

# If IPOD_MOUNT is set in config, use it directly; otherwise auto-detect via findmnt
if [[ -n "${IPOD_MOUNT_OVERRIDE:-}" ]]; then
  if mountpoint -q "$IPOD_MOUNT_OVERRIDE"; then
    IPOD_MOUNT="$IPOD_MOUNT_OVERRIDE"
    log "iPod mounted at (from config): $IPOD_MOUNT"
  else
    die "IPOD_MOUNT_OVERRIDE is set to '$IPOD_MOUNT_OVERRIDE' but it is not a mountpoint."
  fi
else
  while [[ $ELAPSED -lt $MOUNT_TIMEOUT ]]; do
    # Try config-specified mount path first, then fall back to label-based detection
    for candidate in "${IPOD_MOUNT_PATH:-}" "$(findmnt -rn -o TARGET -L IPOD 2>/dev/null)"; do
      [[ -z "$candidate" ]] && continue
      if mountpoint -q "$candidate" 2>/dev/null; then
        IPOD_MOUNT="$candidate"
        log "iPod mounted at: $IPOD_MOUNT"
        break 2
      fi
    done
    sleep 2
    ELAPSED=$(( ELAPSED + 2 ))
  done
fi

if [[ -z "$IPOD_MOUNT" ]]; then
  die "iPod not mounted after ${MOUNT_TIMEOUT}s. Aborting."
fi

# Derive per-session paths from the actual mount point
IPOD_MUSIC_DIR="${IPOD_MOUNT}/FLAC"
IPOD_PLAYLIST_DIR="${IPOD_MOUNT}/Playlists"
IPOD_SCROBBLER_LOG="${IPOD_MOUNT}/.scrobbler.log"

# Ensure directories exist on iPod
mkdir -p "$IPOD_MUSIC_DIR" "$IPOD_PLAYLIST_DIR"

# ══════════════════════════════════════════════════════════════════════════════
# 2. SYNC MUSIC  (server → iPod, server is authoritative)
# ══════════════════════════════════════════════════════════════════════════════
section "Syncing music: server → iPod"

log "Source:      $MUSIC_DIR"
log "Destination: $IPOD_MUSIC_DIR"

# Flags chosen for vfat (iPod filesystem):
# --recursive       : recurse into directories
# --times           : preserve modification times (vfat supports this)
# --modify-window=2 : vfat timestamps have 2s granularity — skip files within that window
# --delete          : remove files on iPod that no longer exist on server
# --info=progress2  : single live-updating progress line in terminal (bytes%, speed, ETA,
#                     and "to-chk=N/TOTAL" showing files remaining vs total scanned)
# --log-file        : per-file transfer log goes to the log file (not mixed with progress)
# NO --archive      : archive includes --owner/--group/--perms which vfat rejects
# NO --checksum     : too slow on large libraries; modify-window handles clock skew

rsync \
  --recursive \
  --times \
  --modify-window=2 \
  --delete \
  --human-readable \
  --stats \
  --info=progress2 \
  --exclude='.DS_Store' \
  --exclude='._*' \
  --exclude='.Spotlight-*' \
  --exclude='.Trashes' \
  --exclude='*.tmp' \
  --log-file="$LOGFILE" \
  --log-file-format='%t [rsync] %o %f (%b bytes)' \
  "$MUSIC_DIR/" \
  "$IPOD_MUSIC_DIR/" \
  || {
    rsync_code=$?
    # Exit code 23 = some files skipped (e.g. vfat-incompatible names, permissions)
    # Exit code 24 = some files vanished mid-transfer (harmless race condition)
    # Both are non-fatal — log a warning and continue to scrobble/playlist steps.
    if [[ $rsync_code -eq 23 || $rsync_code -eq 24 ]]; then
      warn "rsync finished with skipped files (code $rsync_code)."
      warn "Likely cause: folder names with special characters (♯ ∞ trailing spaces) that"
      warn "vfat cannot store. Run 'beet import -q' on those albums to rename them cleanly."
    else
      die "rsync failed with code $rsync_code — aborting."
    fi
  }

log "Music sync complete."

# ══════════════════════════════════════════════════════════════════════════════
# 3. SYNC PLAYLISTS  (bidirectional — server wins on conflict)
# ══════════════════════════════════════════════════════════════════════════════
section "Syncing playlists"

mkdir -p "$PLAYLIST_DIR"

# 3a. Pull new/unknown playlists FROM iPod → server
# (playlists the user may have created in Rockbox directly)
log "Pulling new playlists from iPod → server..."
NEW_PLAYLISTS=0
while IFS= read -r -d '' pls; do
  filename="$(basename "$pls")"
  server_copy="$PLAYLIST_DIR/$filename"
  if [[ ! -f "$server_copy" ]]; then
    cp "$pls" "$server_copy"
    log "  Pulled new playlist: $filename"
    NEW_PLAYLISTS=$(( NEW_PLAYLISTS + 1 ))
  fi
done < <(find "$IPOD_PLAYLIST_DIR" -name '*.m3u' -print0 2>/dev/null)

log "  $NEW_PLAYLISTS new playlist(s) pulled from iPod."

# 3b. Push all server playlists → iPod (server is authoritative for edits)
log "Pushing server playlists → iPod..."
rsync \
  --archive \
  --checksum \
  --delete \
  --human-readable \
  --log-file="$LOGFILE" \
  --log-file-format='%t [playlists] %o %f' \
  "$PLAYLIST_DIR/" \
  "$IPOD_PLAYLIST_DIR/" \
  | tee -a "$LOGFILE"

log "Playlist sync complete."

# ══════════════════════════════════════════════════════════════════════════════
# 4. SCROBBLE
# ══════════════════════════════════════════════════════════════════════════════
section "Scrobbling .scrobbler.log → Last.fm"

if [[ -f "$IPOD_SCROBBLER_LOG" ]]; then
  log "Found scrobbler log: $IPOD_SCROBBLER_LOG"
  if [[ -x "$SCROBBLE_SCRIPT" ]]; then
    python3 "$SCROBBLE_SCRIPT" \
      --log "$IPOD_SCROBBLER_LOG" \
      --config "$CONFIG_FILE" \
      2>&1 | tee -a "$LOGFILE"
  else
    warn "scrobble.py not found or not executable at $SCROBBLE_SCRIPT — skipping."
  fi
else
  log "No .scrobbler.log found on iPod — nothing to scrobble."
fi

# ══════════════════════════════════════════════════════════════════════════════
# 5. SYNC COMPLETE
# ══════════════════════════════════════════════════════════════════════════════
section "Sync complete"
log "Log saved to: $LOGFILE"
log "iPod is safe to unplug."

# Optional: desktop notification if running in a graphical session
notify-send "iPod Sync" "Sync complete. Safe to unplug." 2>/dev/null || true
