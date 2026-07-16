#!/usr/bin/env bash
# common.sh — shared helpers sourced by the server-side scripts.
#
# Deploy to: /usr/local/lib/ipod-sync/lib/common.sh
# Source it after loading config.env, e.g.:
#
#   source "$CONFIG_FILE"
#   source "$(dirname "$0")/lib/common.sh"     # or /usr/local/lib/ipod-sync/lib/common.sh
#
# Provides: log / warn / die / section, emit_event, require_healthy_mount.

# ── Defaults (config.env overrides these) ───────────────────────────────────────
LOG_DIR="${LOG_DIR:-/var/log/ipod-sync}"
EVENTS_LOG="${EVENTS_LOG:-$LOG_DIR/events.jsonl}"
DATA_MOUNT="${DATA_MOUNT:-/mnt/data}"
mkdir -p "$LOG_DIR" 2>/dev/null || true

# LOGFILE may be set by the caller before sourcing; if not, log to a default.
: "${LOGFILE:=$LOG_DIR/ipod-sync.log}"

# ── Logging ──────────────────────────────────────────────────────────────────────
log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"; }
warn() { log "WARN: $*"; }
die()  { log "ERROR: $*"; emit_event "${EVENT_SOURCE:-sync}" error "$*" 2>/dev/null || true; exit 1; }

section() {
  log ""
  log "════════════════════════════════════════"
  log "  $*"
  log "════════════════════════════════════════"
}

# ── Structured events (JSONL, shown in the web UI) ───────────────────────────────
# emit_event <source> <kind> <message> [detail]
#   source: cd | upload | ipod | sync | beets | playlist | mount
#   kind:   info | success | warning | error
emit_event() {
  local ev_source="${1:-sync}" kind="${2:-info}" message="${3:-}" detail="${4:-}"
  local path="${EVENTS_LOG:-$LOG_DIR/events.jsonl}"
  mkdir -p "$(dirname "$path")" 2>/dev/null || true
  python3 - "$ev_source" "$kind" "$message" "$detail" "$path" <<'PY' 2>/dev/null || true
import json, sys, time
src, kind, message, detail, path = sys.argv[1:6]
rec = {"ts": int(time.time()), "source": src, "kind": kind,
       "message": message, "detail": detail}
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY
}

# ── Mount health guard ───────────────────────────────────────────────────────────
# require_healthy_mount <mountpoint>
# Aborts (die) unless the mountpoint is mounted, responds without I/O error, and
# carries the .mounted_ok sentinel that proves the real volume is present.
# This is what stops a dropped 4TB drive from letting rsync --delete wipe the iPod.
require_healthy_mount() {
  local mp="${1:-$DATA_MOUNT}"
  mountpoint -q "$mp" \
    || die "$mp is not mounted — aborting before any destructive sync."
  # A stale/dropped device stays "mounted" but throws EIO on access — catch it here:
  timeout 5 ls "$mp" >/dev/null 2>&1 \
    || die "$mp is mounted but not responding (I/O error) — drive likely dropped. Aborting."
  [[ -f "$mp/.mounted_ok" ]] \
    || die "$mp is missing its .mounted_ok sentinel — refusing to treat it as the real volume."
}
