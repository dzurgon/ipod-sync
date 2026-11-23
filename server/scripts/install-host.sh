#!/usr/bin/env bash
# install-host.sh — install/refresh the host-side iPod sync (scripts, udev rule, Aquaria link).
#
# Run on the server from a clone of this repo:
#   sudo bash server/scripts/install-host.sh
#
# Copies server/scripts → /usr/local/lib/ipod-sync, the udev rule → /etc/udev/rules.d,
# and writes /etc/ipod-sync/aquaria.env (root:<sync user>, 640) with the host token
# shared with the Aquaria container's .env, so a connected iPod syncs its saved plan.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIB=/usr/local/lib/ipod-sync
CONFIG=/etc/ipod-sync/config.env
SYNC_USER="$(sed -n 's/^SYNC_USER=//p' "$CONFIG" 2>/dev/null | tr -d '"'\''' | head -n1)"
SYNC_USER="${SYNC_USER:-${SUDO_USER:-jimin}}"
AQUARIA_DIR="${AQUARIA_DIR:-$(getent passwd "$SYNC_USER" | cut -d: -f6)/docker/aquaria}"

install -d -m 755 "$LIB" "$LIB/lib"
install -m 755 "$REPO"/server/scripts/{ipod-sync.sh,ipod-attach.sh,udev-trigger.sh,ipod-event.sh,scrobble.py,aquaria_sync.py,sync-request.sh} "$LIB/"
install -m 755 "$REPO/server/scripts/lib/common.sh" "$LIB/lib/"
install -m 644 "$REPO/server/udev/99-ipod-rockbox.rules" /etc/udev/rules.d/99-ipod-rockbox.rules
echo "Installed scripts → $LIB, udev rule → /etc/udev/rules.d/99-ipod-rockbox.rules"

TOKEN="$(sed -n 's/^AQUARIA_HOST_TOKEN=//p' "$AQUARIA_DIR/.env" 2>/dev/null | head -n1)"
if [[ -n "$TOKEN" ]]; then
  ( umask 027
    printf 'AQUARIA_URL=http://127.0.0.1:8337\nAQUARIA_HOST_TOKEN=%s\n' "$TOKEN" > /etc/ipod-sync/aquaria.env )
  chown root:"$SYNC_USER" /etc/ipod-sync/aquaria.env
  chmod 640 /etc/ipod-sync/aquaria.env
  echo "Linked to Aquaria: /etc/ipod-sync/aquaria.env (token from $AQUARIA_DIR/.env)"
else
  echo "No AQUARIA_HOST_TOKEN in $AQUARIA_DIR/.env — iPods will use the legacy full-library sync."
fi

# Aquaria's "Sync now": the container writes requests into its data volume; a path unit
# here starts the sync for the plugged-in iPod.
REQ_DIR="$AQUARIA_DIR/data/sync-requests"
install -d -m 755 -o "$SYNC_USER" -g "$SYNC_USER" "$REQ_DIR"
for u in aquaria-sync-request.path aquaria-sync-request.service; do
  sed "s|@REQ_DIR@|$REQ_DIR|g" "$REPO/server/systemd/$u" > "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now aquaria-sync-request.path >/dev/null
echo "Sync now: watching $REQ_DIR (aquaria-sync-request.path)"

udevadm control --reload-rules
echo "udev rules reloaded. Sync runs as: $SYNC_USER"
