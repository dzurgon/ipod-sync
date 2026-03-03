#!/usr/bin/env bash
# udev-trigger.sh — thin wrapper launched by udev (runs as root, minimal env)
# Hands off to ipod-sync.sh running as the regular user via systemd-run.
#
# Deploy to: /usr/local/lib/ipod-sync/udev-trigger.sh
# Must be executable and owned by root.

# Wait a moment for the filesystem to settle after the block device appears
sleep 3

# Run the real sync script as user 'ben' with a full login environment.
# systemd-run ensures it gets a proper environment, cgroup, and journal logging.
/usr/bin/systemd-run \
  --unit=ipod-sync \
  --uid=ben \
  --gid=ben \
  --pipe \
  --collect \
  /usr/local/lib/ipod-sync/ipod-sync.sh
