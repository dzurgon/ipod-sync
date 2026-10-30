#!/usr/bin/env bash
# ipod-attach.sh — own the iPod's mount for a sync: clear stale mounts, mount the
# partition that was just plugged in, run ipod-sync.sh as the sync user, then flush
# and unmount so unplugging is safe. Runs as root.
#
# Deploy to: /usr/local/lib/ipod-sync/ipod-attach.sh
# Started by udev-trigger.sh through systemd-run, because systemd-udevd runs RUN+=
# programs with PrivateMounts=yes — a mount made there would never be visible outside.
#
#   ipod-attach.sh attach /dev/sdX2   iPod's FAT partition appeared: mount → sync → unmount
#   ipod-attach.sh detach /dev/sdX2   it disappeared: lazily drop any mount still pointing at it

set -uo pipefail

ACTION="${1:-}"
DEV="${2:-}"
CONFIG_FILE="/etc/ipod-sync/config.env"
LIB="/usr/local/lib/ipod-sync"

cfg() { sed -n "s/^$1=//p" "$CONFIG_FILE" 2>/dev/null | tr -d '"'\''' | head -n1; }
SYNC_USER="$(cfg SYNC_USER)"; SYNC_USER="${SYNC_USER:-jimin}"
BASE="$(cfg IPOD_MOUNT_PATH)"; BASE="${BASE:-/mnt/ipod}"

log() { echo "[ipod-attach] $*"; }   # → journal (journalctl -u 'ipod-attach-*')

# Set from what ipod-sync.sh writes to $STATE: its log file and the iPod's signature.
IPOD_LOGFILE=""; IPOD_SIG=""
# Log to the journal and append to the sync's own log, so that log ends with the outcome.
note() { log "$*"; [[ -n "$IPOD_LOGFILE" ]] && echo "[$(date '+%F %T')] $*" >> "$IPOD_LOGFILE"; }
# Tell Aquaria (if this iPod is linked) what's happening: unmounting | safe | warning.
aq_report() {
  [[ -n "$IPOD_SIG" && -r /etc/ipod-sync/aquaria.env ]] || return 0
  ( set -a; . /etc/ipod-sync/aquaria.env; set +a
    python3 "$LIB/aquaria_sync.py" report --sig "$IPOD_SIG" "$@" ) >/dev/null 2>&1 || true
}

# Unmount whatever is stacked at $1 while the top mount is dead: its device is gone
# (unplugged without unmounting) or it errors on access. Returns 1 if a live mount
# remains (another iPod is using this path), 0 once the path is free.
clear_stale() {
  local mp="$1" src
  while mountpoint -q "$mp"; do
    src="$(findmnt -n -o SOURCE "$mp" | tail -n1)"        # last line = top of the stack
    if [[ ! -b "$src" ]] || ! timeout 5 ls "$mp" >/dev/null 2>&1; then
      log "dropping stale mount of $src at $mp"
      umount -l "$mp" || return 1
    else
      return 1
    fi
  done
  return 0
}

case "$ACTION" in
  detach)
    while read -r mp; do
      [[ -n "$mp" ]] && umount -l "$mp" && log "iPod $DEV removed — dropped its mount at $mp"
    done < <(findmnt -n -o TARGET --source "$DEV" 2>/dev/null)
    exit 0
    ;;
  attach) ;;
  *) echo "usage: $0 attach|detach /dev/sdXN" >&2; exit 2 ;;
esac

[[ -b "$DEV" ]] || { log "$DEV is gone — nothing to do"; exit 0; }
sleep 2   # let the device settle

# Already mounted by something else (e.g. a desktop automounter)? Sync it there, leave it.
MP="$(findmnt -n -o TARGET --source "$DEV" | head -n1)"
if [[ -n "$MP" ]] && ! timeout 5 ls "$MP" >/dev/null 2>&1; then
  log "dropping dead mount at $MP (left over from an earlier $DEV)"
  umount -l "$MP"; MP=""                 # same device name as a previous, unplugged iPod
fi
OWN=0
if [[ -z "$MP" ]]; then
  MP="$BASE"
  if ! clear_stale "$MP"; then
    MP="${BASE}-$(basename "$DEV")"       # another live iPod holds the usual path
    clear_stale "$MP" || { log "both $BASE and $MP are busy — giving up"; exit 1; }
  fi
  mkdir -p "$MP"
  # Repair first: an iPod unplugged mid-write (or never unmounted) is left dirty, and a
  # dirty FAT can lose files or misreport sizes. -a fixes what it can automatically.
  fsck_out="$(fsck.vfat -a "$DEV" 2>&1)"; rc=$?
  case $rc in
    0) log "filesystem check of $DEV: clean" ;;
    1) log "filesystem check of $DEV: repaired errors"; log "$fsck_out" ;;
    *) log "WARN: filesystem check of $DEV failed (exit $rc) — mounting anyway"; log "$fsck_out" ;;
  esac
  # utf8: vfat long names are UTF-16 on disk; utf8 maps them faithfully (iso8859-1 can't).
  # flush: write data out as it goes, so the wait at unmount stays short.
  if ! mount -t vfat -o "uid=$(id -u "$SYNC_USER"),gid=$(id -g "$SYNC_USER"),umask=022,utf8,shortname=mixed,flush,errors=remount-ro" \
       "$DEV" "$MP"; then
    log "could not mount $DEV at $MP"
    exit 1
  fi
  OWN=1
  log "mounted $DEV at $MP"
fi

STATE="$(mktemp /tmp/ipod-attach-state.XXXXXX)"
chown "$SYNC_USER" "$STATE"
trap 'rm -f "$STATE"' EXIT

runuser -u "$SYNC_USER" -- env \
  HOME="$(getent passwd "$SYNC_USER" | cut -d: -f6)" \
  CONFIG_FILE="$CONFIG_FILE" \
  IPOD_DEVNAME="$DEV" \
  IPOD_MOUNT_OVERRIDE="$MP" \
  IPOD_MANAGED_MOUNT=1 \
  IPOD_STATE_FILE="$STATE" \
  "$LIB/ipod-sync.sh"
rc=$?
IPOD_LOGFILE="$(sed -n 's/^IPOD_LOGFILE=//p' "$STATE")"
IPOD_SIG="$(sed -n 's/^IPOD_SIG=//p' "$STATE")"

aq_report --status unmounting
note "writing the last changes to the iPod — don't unplug yet"
sync
if (( OWN )); then
  umount "$MP"; urc=$?
  if [[ ! -b "$DEV" ]]; then
    # The device vanished while we were still writing: umount "succeeds" on a dead device.
    note "WARN: the iPod was unplugged before writes finished — plug it back in; it'll be checked, repaired and re-synced"
    aq_report --status warning --message "Unplugged before writes finished — plug it back in to repair and re-sync"
    rc=1
  elif (( urc == 0 )); then
    note "unmounted $MP — safe to unplug"
    aq_report --status safe
    "$LIB/ipod-event.sh" safe || true
  else
    note "WARN: could not unmount $MP (busy?) — run 'sudo umount $MP' before unplugging"
  fi
else
  [[ -b "$DEV" ]] && aq_report --status safe   # someone else's mount: synced + flushed
fi
exit "$rc"
