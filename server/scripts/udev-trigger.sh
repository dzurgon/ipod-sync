#!/usr/bin/env bash
# udev-trigger.sh — thin wrapper launched by udev (runs as root, minimal env)
# Hands off to ipod-sync.sh running as the regular user via systemd-run.
#
# Deploy to: /usr/local/lib/ipod-sync/udev-trigger.sh
# Must be executable and owned by root.

# Wait a moment for the filesystem to settle after the block device appears.
sleep 3

# Resolve the sync user from config (falls back to jimin). udev runs with almost
# no environment, so we read the value directly rather than sourcing the whole file.
CONFIG_FILE="/etc/ipod-sync/config.env"
SYNC_USER="$(sed -n 's/^SYNC_USER=//p' "$CONFIG_FILE" 2>/dev/null | tr -d '\"'\''' | head -n1)"
SYNC_USER="${SYNC_USER:-jimin}"

# Run the real sync script as the regular user with a full environment.
# systemd-run gives it a proper environment, cgroup, and journal logging.
/usr/bin/systemd-run \
  --unit=ipod-sync \
  --uid="$SYNC_USER" \
  --gid="$SYNC_USER" \
  --setenv=CONFIG_FILE="$CONFIG_FILE" \
  --pipe \
  --collect \
  /usr/local/lib/ipod-sync/ipod-sync.sh
