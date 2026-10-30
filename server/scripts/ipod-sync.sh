#!/usr/bin/env bash
# ipod-sync.sh — main iPod sync script, triggered on USB connect
#
# Deploy to: /usr/local/lib/ipod-sync/ipod-sync.sh
#
# What it does (in order):
#   1. Wait for iPod to be fully mounted
#   2. Ask Aquaria (the web app) which iPod this is and what it should hold:
#        linked     → sync that iPod's saved plan (music selection, layout, playlists,
#                     owner's Last.fm) — see aquaria_sync.py / aquaria/app/ipods.py
#        not linked → register it so its owner can link it in the web UI; no sync
#        unreachable / not configured → legacy mode (steps 3–4: mirror everything)
#   3. rsync music library → iPod (server is authoritative)
#   4. Sync playlists: push server→iPod, pull any new ones iPod→server
#   5. Parse .scrobbler.log → submit to Last.fm (via scrobble.py)
#   6. Log a timestamped summary

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
# ipod-attach.sh passes a file to tell it this log + the iPod's signature, so it can
# log and report the unmount ("safe to unplug") after we exit.
[[ -n "${IPOD_STATE_FILE:-}" ]] && echo "IPOD_LOGFILE=$LOGFILE" > "$IPOD_STATE_FILE"
EVENT_SOURCE="sync"

# Keep only the last 30 sync logs
find "$LOG_DIR" -name 'ipod-sync-*.log' | sort | head -n -30 | xargs -r rm --

SCROBBLE_SCRIPT="/usr/local/lib/ipod-sync/scrobble.py"
AQUARIA_SYNC="/usr/local/lib/ipod-sync/aquaria_sync.py"
AQUARIA_ENV="${AQUARIA_ENV:-/etc/ipod-sync/aquaria.env}"   # AQUARIA_URL + AQUARIA_HOST_TOKEN
# --max-delete safety cap for the music rsync. Shrinking what an iPod holds (e.g.
# "whole library" → a few genres) can legitimately exceed it; raise it for that sync.
IPOD_MAX_DELETE="${IPOD_MAX_DELETE:-200}"

# ── Shared helpers (log/warn/die/section, emit_event, require_healthy_mount) ────
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"
[[ -f "$LIB_DIR/common.sh" ]] || LIB_DIR="/usr/local/lib/ipod-sync/lib"
# shellcheck source=/dev/null
source "$LIB_DIR/common.sh"

# ── Guard: refuse to run if the data volume isn't healthily mounted ─────────────
# Protects against a dropped 4TB drive: an erroring/empty source with --delete
# could otherwise wipe the iPod copy or write into the bare mountpoint.
require_healthy_mount "${DATA_MOUNT:-/mnt/data}"

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
# 2. WHICH iPOD IS THIS?  (Aquaria per-user sync plans)
# ══════════════════════════════════════════════════════════════════════════════
section "Identifying iPod"

MODE="legacy"
IPOD_SIG=""
[[ -f "$AQUARIA_ENV" ]] && source "$AQUARIA_ENV"
export AQUARIA_URL AQUARIA_HOST_TOKEN

if [[ -n "${AQUARIA_HOST_TOKEN:-}" && -f "$AQUARIA_SYNC" ]]; then
  IPOD_SIG="$(python3 "$AQUARIA_SYNC" identify --mount "$IPOD_MOUNT" --devname "${IPOD_DEVNAME:-}")"
  log "iPod signature: $IPOD_SIG"
  [[ -n "${IPOD_STATE_FILE:-}" ]] && echo "IPOD_SIG=$IPOD_SIG" >> "$IPOD_STATE_FILE"
  set +e
  WHO="$(python3 "$AQUARIA_SYNC" seen --mount "$IPOD_MOUNT" --sig "$IPOD_SIG" 2>>"$LOGFILE")"
  rc=$?
  set -e
  case $rc in
    0)  MODE="aquaria"
        log "Linked in Aquaria: $WHO — syncing its saved plan."
        python3 "$AQUARIA_SYNC" report --sig "$IPOD_SIG" --status syncing || true ;;
    10) log "This iPod isn't linked to an Aquaria account yet."
        log "Open the web UI → My iPod → \"Link to my account\", then reconnect it."
        emit_event sync warning "Unlinked iPod connected — link it in Aquaria (My iPod) to sync" "$IPOD_SIG"
        exit 0 ;;
    *)  warn "Aquaria unreachable (exit $rc) — falling back to legacy full-library sync." ;;
  esac
else
  log "Aquaria not configured ($AQUARIA_ENV) — legacy full-library sync."
fi

# Report failures of an Aquaria-mode sync back to the web UI (status + activity feed).
on_exit() {
  local rc=$?
  if [[ $rc -ne 0 && "$MODE" == "aquaria" ]]; then
    python3 "$AQUARIA_SYNC" report --sig "$IPOD_SIG" --status error \
      --message "Sync failed (exit $rc) — see $(basename "$LOGFILE") on the server" || true
  fi
  [[ -n "${STAGE:-}" ]] && rm -rf "$STAGE"
}
trap on_exit EXIT

emit_event sync info "iPod sync started" "$(basename "$LOGFILE")"

# ══════════════════════════════════════════════════════════════════════════════
# 3. SYNC MUSIC  (server → iPod, server is authoritative)
# ══════════════════════════════════════════════════════════════════════════════
section "Syncing music: server → iPod"

# rsync exit codes 23/24 (skipped / vanished files) are non-fatal; 25 = --max-delete hit.
SKIPPED=0
rsync_soft_fail() {
  local code=$1
  if [[ $code -eq 23 || $code -eq 24 ]]; then
    sleep 1                                   # let the stderr tee finish writing
    SKIPPED="$(grep -c '^rsync: .*failed' "$LOGFILE" || true)"; SKIPPED="${SKIPPED:-1}"
    warn "rsync finished with ${SKIPPED} file(s) not copied (code $code) — see the 'rsync:' lines above."
    warn "Likely cause: names the iPod's FAT filesystem can't store (: ? * \" < > | or a"
    warn "trailing dot/space). Rename those files on the server."
  elif [[ $code -eq 25 ]]; then
    die "rsync stopped: it would delete more than $IPOD_MAX_DELETE files from the iPod. If you just shrank what this iPod syncs, set IPOD_MAX_DELETE higher in config.env for one sync."
  else
    die "rsync failed with code $code — aborting."
  fi
}

if [[ "$MODE" == "aquaria" ]]; then
  # 3a. Playlists made on the iPod itself → the owner's account (before the plan is built)
  python3 "$AQUARIA_SYNC" pull --mount "$IPOD_MOUNT" --sig "$IPOD_SIG" 2>&1 | tee -a "$LOGFILE"

  # 3b. Stage the plan as a symlink tree laid out exactly as the iPod should look
  STAGE="$(mktemp -d /tmp/ipod-sync-stage.XXXXXX)"
  python3 "$AQUARIA_SYNC" stage --mount "$IPOD_MOUNT" --sig "$IPOD_SIG" \
    --music-root "$MUSIC_DIR" --out "$STAGE" 2>&1 | tee -a "$LOGFILE"

  log "Source:      $STAGE/music (-> $MUSIC_DIR)"
  log "Destination: $IPOD_MUSIC_DIR"
  # --copy-links: send what the symlinks point at (whole album folders / artwork files)
  rsync \
    --recursive \
    --copy-links \
    --times \
    --modify-window=2 \
    --delete \
    --max-delete="$IPOD_MAX_DELETE" \
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
    "$STAGE/music/" \
    "$IPOD_MUSIC_DIR/" \
    2> >(tee -a "$LOGFILE" >&2) \
    || rsync_soft_fail $?
  log "Music sync complete."

  # 3c. Playlists: exactly the plan's set (iPod-made ones were imported in 3a)
  section "Syncing playlists"
  rsync \
    --recursive \
    --checksum \
    --delete \
    --human-readable \
    --log-file="$LOGFILE" \
    --log-file-format='%t [playlists] %o %f' \
    "$STAGE/playlists/" \
    "$IPOD_PLAYLIST_DIR/" \
    | tee -a "$LOGFILE"
  cp "$STAGE/pushed.json" "$IPOD_MOUNT/.aquaria/pushed.json"
  log "Playlist sync complete."
else


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

# --max-delete=200 : safety cap. If a source glitch (e.g. drive hiccup that slipped
#                    past the mount guard) makes the library look empty, rsync aborts
#                    rather than deleting the whole iPod. Raise if you legitimately
#                    remove >200 files at once.
rsync \
  --recursive \
  --times \
  --modify-window=2 \
  --delete \
  --max-delete="$IPOD_MAX_DELETE" \
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
  2> >(tee -a "$LOGFILE" >&2) \
  || rsync_soft_fail $?

log "Music sync complete."

# ══════════════════════════════════════════════════════════════════════════════
# 4. SYNC PLAYLISTS  (legacy mode: bidirectional — server wins on conflict)
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
fi   # legacy mode

# ══════════════════════════════════════════════════════════════════════════════
# 5. SCROBBLE
# ══════════════════════════════════════════════════════════════════════════════
section "Scrobbling .scrobbler.log → Last.fm"

# Aquaria mode scrobbles to the iPod owner's own Last.fm account (if they connected
# one and left scrobbling on); legacy mode uses LASTFM_USERNAME from config.env.
SCROBBLE_ARGS=()
SKIP_SCROBBLE=0
if [[ "$MODE" == "aquaria" ]]; then
  if [[ -f "$STAGE/scrobble.env" ]]; then
    source "$STAGE/scrobble.env"
    export LASTFM_SESSION_KEY_OVERRIDE
    SCROBBLE_ARGS=(--username "$LASTFM_SYNC_USER")
  else
    log "Scrobbling is off for this iPod (or its owner hasn't connected Last.fm)."
    SKIP_SCROBBLE=1
  fi
fi

if [[ $SKIP_SCROBBLE -eq 1 ]]; then
  :
elif [[ -f "$IPOD_SCROBBLER_LOG" ]]; then
  log "Found scrobbler log: $IPOD_SCROBBLER_LOG"
  if [[ -x "$SCROBBLE_SCRIPT" ]]; then
    python3 "$SCROBBLE_SCRIPT" \
      --log "$IPOD_SCROBBLER_LOG" \
      --config "$CONFIG_FILE" \
      "${SCROBBLE_ARGS[@]}" \
      2>&1 | tee -a "$LOGFILE"
  else
    warn "scrobble.py not found or not executable at $SCROBBLE_SCRIPT — skipping."
  fi
else
  log "No .scrobbler.log found on iPod — nothing to scrobble."
fi

# ══════════════════════════════════════════════════════════════════════════════
# 6. SYNC COMPLETE
# ══════════════════════════════════════════════════════════════════════════════
section "Sync complete"
log "Log saved to: $LOGFILE"
# Under ipod-attach.sh the mount is unmounted right after this — it announces "safe".
if [[ "${IPOD_MANAGED_MOUNT:-}" == 1 ]]; then
  log "Unmounting next — wait for \"safe to unplug\"."
  emit_event sync success "iPod sync complete — unmounting" "$(basename "$LOGFILE")"
else
  log "iPod is safe to unplug."
  emit_event sync success "iPod sync complete — safe to unplug" "$(basename "$LOGFILE")"
fi
if [[ "$MODE" == "aquaria" ]]; then
  if (( SKIPPED )); then
    python3 "$AQUARIA_SYNC" report --sig "$IPOD_SIG" --status warning \
      --message "Synced, but $SKIPPED file(s) couldn't be copied (names the iPod can't store) — see $(basename "$LOGFILE")" \
      --stats "$(cat "$STAGE/plan.json")" || true
  else
    python3 "$AQUARIA_SYNC" report --sig "$IPOD_SIG" --status ok \
      --message "Synced" --stats "$(cat "$STAGE/plan.json")" || true
  fi
fi

# Optional: desktop notification if running in a graphical session
notify-send "iPod Sync" "Sync complete. Safe to unplug." 2>/dev/null || true
