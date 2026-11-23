#!/usr/bin/env bash
# sync-request.sh — run Aquaria's "Sync now" requests (root; started by
# aquaria-sync-request.path when the request folder isn't empty).
#
# Aquaria writes <request dir>/<iPod signature>. Each request is consumed, and if that
# iPod is plugged in (ipod-attach.sh keeps /run/ipod-sync/<sdXN>.sig) and not already
# syncing, the same attach + sync a plug-in runs is started for it.
set -uo pipefail

REQ_DIR="${1:?usage: sync-request.sh <request dir>}"
LIB=/usr/local/lib/ipod-sync

report() {   # report <sig> <status> — tell Aquaria (state only)
  [[ -r /etc/ipod-sync/aquaria.env ]] || return 0
  ( set -a; . /etc/ipod-sync/aquaria.env; set +a
    python3 "$LIB/aquaria_sync.py" report --sig "$1" --status "$2" ) >/dev/null 2>&1 || true
}

for req in "$REQ_DIR"/*; do
  [[ -f "$req" ]] || continue
  sig="$(basename "$req")"; rm -f "$req"
  [[ "$sig" =~ ^[A-Za-z0-9._-]+$ ]] || continue
  dev=""
  for m in /run/ipod-sync/*.sig; do
    [[ -f "$m" && "$(cat "$m")" == "$sig" ]] && dev="/dev/$(basename "$m" .sig)"
  done
  if [[ -z "$dev" || ! -b "$dev" ]]; then
    echo "Sync now: iPod $sig isn't plugged in"
    report "$sig" removed
  elif systemctl is-active --quiet "ipod-attach-$(basename "$dev")"; then
    echo "Sync now: $dev is already syncing"
  else
    echo "Sync now: starting a sync of $dev ($sig)"
    DEVNAME="$dev" "$LIB/udev-trigger.sh" attach
  fi
done
