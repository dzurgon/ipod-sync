#!/usr/bin/env bash
# udev-trigger.sh — thin wrapper launched by udev (runs as root, minimal env)
# Hands the iPod's partition to ipod-attach.sh in its own systemd unit.
#
# Deploy to: /usr/local/lib/ipod-sync/udev-trigger.sh
# Must be executable and owned by root.
#
#   udev-trigger.sh attach   (on add)     mount → ipod-sync.sh as the sync user → unmount
#   udev-trigger.sh detach   (on remove)  drop mounts left by an unplugged iPod
#
# Why systemd-run: systemd-udevd runs RUN+= programs with PrivateMounts=yes and kills
# them after a short timeout, so mounts made here would be invisible and long syncs
# would die. The transient unit runs in the host's mount namespace with no time limit;
# its name is per device, so a duplicate event can't start a second sync.

MODE="${1:-attach}"
DEV="${DEVNAME:-}"
[[ -n "$DEV" ]] || exit 0

exec /usr/bin/systemd-run \
  --unit="ipod-${MODE}-$(basename "$DEV")" \
  --collect \
  --quiet \
  /usr/local/lib/ipod-sync/ipod-attach.sh "$MODE" "$DEV"
