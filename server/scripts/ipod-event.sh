#!/usr/bin/env bash
# ipod-event.sh — log iPod connect/disconnect as a structured event.
#
# Deploy to: /usr/local/lib/ipod-sync/ipod-event.sh
# Called by udev (99-ipod-rockbox.rules) on both add and remove, with $1 = action.
# This is independent of whether a full sync runs — so the web UI always shows
# "iPod connected" / "iPod removed" even if the sync itself is skipped or fails.

ACTION="${1:-unknown}"

CONFIG_FILE="/etc/ipod-sync/config.env"
LOG_DIR="$(sed -n 's/^LOG_DIR=//p' "$CONFIG_FILE" 2>/dev/null | tr -d '"'\''' | head -n1)"
LOG_DIR="${LOG_DIR:-/var/log/ipod-sync}"
EVENTS_LOG="$(sed -n 's/^EVENTS_LOG=//p' "$CONFIG_FILE" 2>/dev/null | tr -d '"'\''' | head -n1)"
EVENTS_LOG="${EVENTS_LOG:-$LOG_DIR/events.jsonl}"
mkdir -p "$LOG_DIR" 2>/dev/null || true

case "$ACTION" in
  add|connect)    KIND=success; MSG="iPod connected" ;;
  remove|disconnect) KIND=info;  MSG="iPod removed" ;;
  *)              KIND=info;     MSG="iPod event: $ACTION" ;;
esac

python3 - "$KIND" "$MSG" "$EVENTS_LOG" <<'PY' 2>/dev/null || true
import json, sys, time
kind, message, path = sys.argv[1:4]
rec = {"ts": int(time.time()), "source": "ipod", "kind": kind, "message": message, "detail": ""}
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY
