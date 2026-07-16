#!/usr/bin/env bash
# mount-watchdog.sh — periodically verify /mnt/data is healthy; try to recover if not.
#
# Deploy to: /usr/local/lib/ipod-sync/mount-watchdog.sh
# Run by:    mount-watchdog.timer (systemd) every few minutes.
#
# What it catches: the 4TB drive dropping off the bus while still "mounted" (EIO on
# access). `mount -a` can't fix that — the device is already mounted — so we do a
# lazy unmount + remount cycle and log the event so it shows up in the web UI.

set -uo pipefail

CONFIG_FILE="${CONFIG_FILE:-/etc/ipod-sync/config.env}"
[[ -f "$CONFIG_FILE" ]] && source "$CONFIG_FILE"

LOG_DIR="${LOG_DIR:-/var/log/ipod-sync}"
LOGFILE="$LOG_DIR/mount-watchdog.log"
EVENT_SOURCE="mount"
DATA_MOUNT="${DATA_MOUNT:-/mnt/data}"
mkdir -p "$LOG_DIR"

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"
[[ -f "$LIB_DIR/common.sh" ]] || LIB_DIR="/usr/local/lib/ipod-sync/lib"
# shellcheck source=/dev/null
source "$LIB_DIR/common.sh"

healthy() {
  mountpoint -q "$DATA_MOUNT" || return 1
  timeout 5 ls "$DATA_MOUNT" >/dev/null 2>&1 || return 1
  [[ -f "$DATA_MOUNT/.mounted_ok" ]] || return 1
  return 0
}

if healthy; then
  # Quiet on the happy path — no event spam. Uncomment to log every check:
  # log "OK: $DATA_MOUNT healthy."
  exit 0
fi

log "UNHEALTHY: $DATA_MOUNT failed health check — attempting recovery."
emit_event mount warning "$DATA_MOUNT went unhealthy — attempting remount"

# Lazy unmount detaches the stale mount even while a process holds it; then remount
# from fstab (must be defined there by UUID, with nofail — see docs).
umount -l "$DATA_MOUNT" 2>>"$LOGFILE"
sleep 2
mount "$DATA_MOUNT" 2>>"$LOGFILE"
sleep 2

if healthy; then
  log "RECOVERED: $DATA_MOUNT remounted successfully."
  emit_event mount success "$DATA_MOUNT remounted successfully"
  exit 0
else
  log "FAILED: $DATA_MOUNT still unhealthy after remount. Check dmesg / smartctl / cabling."
  emit_event mount error "$DATA_MOUNT still unhealthy after remount — check dmesg/smartctl"
  exit 1
fi
