#!/usr/bin/env bash
# torrent-finish.sh — qBittorrent "Run external program on torrent completion" hook
#
# In qBittorrent preferences → Downloads → "Run external program on torrent completion":
#   /Users/ben/Code/personal-scripts/ipod/mac/torrent-finish.sh "%D" "%N" "%L"
#
# Arguments supplied by qBittorrent:
#   %D = save path (directory where files landed)
#   %N = torrent name
#   %L = category/label
#
# What this does:
#   1. Only acts on torrents in the "Music" category (label)
#   2. Copies the completed files into ~/Nextcloud/FLAC/
#   3. Nextcloud desktop client picks them up and syncs to server
#   4. Server inotify watcher fires → beets runs → pipeline continues

set -euo pipefail

TORRENT_PATH="${1:-}"    # %D: full save path
TORRENT_NAME="${2:-}"    # %N: torrent name
TORRENT_LABEL="${3:-}"   # %L: category label

# ── Config ────────────────────────────────────────────────────────────────────

MUSIC_LABEL="Music"                         # qBittorrent category to act on
NEXTCLOUD_FLAC="$HOME/Nextcloud/FLAC"       # Nextcloud sync folder
LOG_FILE="$HOME/Library/Logs/ipod-sync/torrent-finish.log"

mkdir -p "$(dirname "$LOG_FILE")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# ── Gate: only handle music torrents ─────────────────────────────────────────

log "Torrent finished: '$TORRENT_NAME' (label='$TORRENT_LABEL', path='$TORRENT_PATH')"

if [[ "$TORRENT_LABEL" != "$MUSIC_LABEL" ]]; then
  log "Label is not '$MUSIC_LABEL' — skipping."
  exit 0
fi

if [[ -z "$TORRENT_PATH" || ! -e "$TORRENT_PATH" ]]; then
  log "ERROR: torrent path not found: '$TORRENT_PATH'"
  exit 1
fi

# ── Copy to Nextcloud FLAC folder ─────────────────────────────────────────────
# Use rsync so re-downloads or re-seeds don't cause issues.
# We copy (not move) so the torrent can continue seeding from ~/Torrents/Music.

mkdir -p "$NEXTCLOUD_FLAC"

log "Copying '$TORRENT_PATH' → '$NEXTCLOUD_FLAC/'"

rsync \
  --archive \
  --human-readable \
  --exclude='.DS_Store' \
  --exclude='._*' \
  --exclude='*.nfo' \
  --exclude='*.txt' \
  --exclude='*.sfv' \
  --exclude='*.jpg' \   # beets will fetch cover art; don't copy torrent junk art
  --stats \
  "$TORRENT_PATH/" \
  "$NEXTCLOUD_FLAC/$TORRENT_NAME/" \
  >> "$LOG_FILE" 2>&1

log "Copy complete. Nextcloud will sync to server automatically."

# ── macOS notification ────────────────────────────────────────────────────────
osascript -e "display notification \"Copied to Nextcloud: $TORRENT_NAME\" \
  with title \"iPod Sync\" subtitle \"Music queued for server beets pipeline\"" 2>/dev/null || true
