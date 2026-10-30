#!/usr/bin/env python3
"""
aquaria_sync.py — host-side helper that lets ipod-sync.sh sync per-iPod, per-user plans
saved in Aquaria (the web app), instead of mirroring everything to every iPod.

Deploy to: /usr/local/lib/ipod-sync/aquaria_sync.py   (stdlib only)

Reads AQUARIA_URL (default http://127.0.0.1:8337) and AQUARIA_HOST_TOKEN from the
environment (ipod-sync.sh sources /etc/ipod-sync/aquaria.env). Subcommands:

  identify  --mount M [--devname D]   print the iPod's signature: USB serial, else the
                                      .aquaria/id marker on the iPod, else FS UUID, else
                                      a new marker. Writes metadata to M/.aquaria/host.json
  seen      --mount M --sig S         register with Aquaria. exit 0 linked · 10 not linked
                                      · 20 Aquaria unreachable / host API disabled
  pull      --mount M --sig S         import playlists created on the iPod (not ones we pushed)
  stage     --mount M --sig S --music-root R --out DIR
            builds DIR/music      symlink tree: <iPod path> -> <server file/folder>
                   DIR/playlists  the .m3u files this iPod should hold
                   DIR/pushed.json  names we pushed (copied to the iPod after a good push)
                   DIR/scrobble.env  Last.fm user + session for the owner (mode 600), if on
  report    --sig S --status syncing|ok|warning|error|unmounting|safe [--message TEXT] [--stats JSON]
                                      ok/warning/error are results; syncing/unmounting/safe only
                                      update the live state shown on the My iPod page
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

URL = os.environ.get("AQUARIA_URL", "http://127.0.0.1:8337").rstrip("/")
TOKEN = os.environ.get("AQUARIA_HOST_TOKEN", "")
STATE_DIR = ".aquaria"            # on the iPod: id marker, pushed.json, host.json


class Unreachable(Exception):
    pass


def api(method: str, path: str, body: dict | None = None, ok404: bool = False):
    req = urllib.request.Request(
        URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Aquaria-Token": TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return None
        raise Unreachable(f"{method} {path}: HTTP {e.code} {e.read()[:200]!r}")
    except (urllib.error.URLError, OSError) as e:
        raise Unreachable(f"{method} {path}: {e}")


def sig_path(sig: str) -> str:
    return "/api/host/ipods/" + urllib.parse.quote(sig, safe="")


# ── identify ─────────────────────────────────────────────────────────────────

def udev_props(dev: str) -> dict:
    try:
        out = subprocess.run(["udevadm", "info", "--query=property", f"--name={dev}"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


def rockbox_info(mount: Path) -> dict:
    info = {}
    try:
        for line in (mount / ".rockbox" / "rockbox-info.txt").read_text(errors="replace").splitlines():
            k, _, v = line.partition(":")
            info[k.strip().lower()] = v.strip()
    except OSError:
        pass
    return info


def cmd_identify(a) -> int:
    mount = Path(a.mount)
    dev = a.devname
    if not dev:
        try:
            dev = subprocess.run(["findmnt", "-n", "-o", "SOURCE", str(mount)],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            dev = ""
    props = udev_props(dev) if dev else {}
    state = mount / STATE_DIR
    marker = state / "id"
    if props.get("ID_SERIAL_SHORT"):
        sig = props["ID_SERIAL_SHORT"]
    elif marker.is_file() and marker.read_text().strip():
        sig = marker.read_text().strip()
    elif props.get("ID_FS_UUID"):
        sig = "fs-" + props["ID_FS_UUID"]
    else:
        sig = "aq-" + uuid.uuid4().hex[:16]
    state.mkdir(exist_ok=True)
    if not marker.is_file():
        marker.write_text(sig + "\n")

    rb = rockbox_info(mount)
    du = shutil.disk_usage(mount)
    meta = {
        "model": rb.get("target") or props.get("ID_MODEL", "").replace("_", " ") or None,
        "rockbox": rb.get("version"),
        "capacity": f"{du.total / 1e9:.0f} GB",
        "free": f"{du.free / 1e9:.1f} GB",
        "usb_serial": props.get("ID_SERIAL_SHORT"),
        "fs_uuid": props.get("ID_FS_UUID"),
    }
    (state / "host.json").write_text(json.dumps({k: v for k, v in meta.items() if v}, indent=1))
    print(sig)
    return 0


def _meta(mount: Path) -> dict:
    try:
        return json.loads((mount / STATE_DIR / "host.json").read_text())
    except (OSError, ValueError):
        return {}


# ── seen / pull / stage / report ─────────────────────────────────────────────

def cmd_seen(a) -> int:
    if not TOKEN:
        print("AQUARIA_HOST_TOKEN not set", file=sys.stderr)
        return 20
    try:
        r = api("POST", sig_path(a.sig) + "/seen", {"metadata": _meta(Path(a.mount))})
    except Unreachable as e:
        print(e, file=sys.stderr)
        return 20
    if not r.get("linked"):
        return 10
    print(f"{r['name']} (owner: {r['owner']})")
    return 0


def cmd_pull(a) -> int:
    mount = Path(a.mount)
    pdir = mount / "Playlists"
    pushed_file = mount / STATE_DIR / "pushed.json"
    try:
        pushed = set(json.loads(pushed_file.read_text()))
    except (OSError, ValueError):
        pushed = set()
    plan = api("GET", sig_path(a.sig) + "/plan")
    known = {p["name"].casefold() for p in plan["playlists"]} | {n.casefold() for n in pushed}
    n = 0
    for f in sorted(pdir.glob("*.m3u")) if pdir.is_dir() else []:
        if f.stem.casefold() in known:
            continue
        lines = [l.strip() for l in f.read_text(errors="replace").splitlines()
                 if l.strip() and not l.startswith("#")]
        r = api("POST", sig_path(a.sig) + "/playlists", {"name": f.stem, "lines": lines})
        print(f"  imported playlist from iPod: {f.name} -> {r['name']}")
        n += 1
    print(f"  {n} new playlist(s) imported from the iPod.")
    return 0


def _safe_name(name: str) -> str:
    return "".join("_" if c in '/\\:*?"<>|' else c for c in name).strip() or "playlist"


def cmd_stage(a) -> int:
    root, out = Path(a.music_root), Path(a.out)
    plan = api("GET", sig_path(a.sig) + "/plan")
    music, plsdir = out / "music", out / "playlists"
    music.mkdir(parents=True)
    plsdir.mkdir()

    linked: set[str] = set()
    for item in sorted(plan["music"], key=lambda m: m["dst"].count("/")):
        dst = item["dst"]
        if any(dst.startswith(p + "/") for p in linked):
            continue                                   # already inside a linked folder
        src = root / item["src"]
        if not src.exists():
            print(f"  missing on server, skipped: {item['src']}", file=sys.stderr)
            continue
        target = music / dst
        # Never create anything through a symlink — that would write into the real library.
        if any(p.is_symlink() for p in target.parents if p != music and music in p.parents):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            continue                                   # flat-layout collision: first wins
        target.symlink_to(src)
        linked.add(dst)

    names = []
    for p in plan["playlists"]:
        fname = _safe_name(p["name"]) + ".m3u"
        (plsdir / fname).write_text(p["m3u"], encoding="utf-8")
        names.append(Path(fname).stem)
    (out / "pushed.json").write_text(json.dumps(names))
    (out / "plan.json").write_text(json.dumps(plan["summary"]))

    sc = plan["scrobble"]
    if sc["enabled"]:
        env = out / "scrobble.env"
        env.touch(mode=0o600)
        env.write_text(f"LASTFM_SYNC_USER={sc['lastfm_user']}\nLASTFM_SESSION_KEY_OVERRIDE={sc['session_key']}\n")
    s = plan["summary"]
    print(f"  plan for {plan['ipod']['name']} ({plan['owner']}, {plan['ipod']['layout']} layout): "
          f"{s['albums']} albums, {s['tracks']} tracks, {s['bytes'] / 1e9:.1f} GB, "
          f"{s['playlists']} playlists, scrobble {'on' if sc['enabled'] else 'off'}")
    return 0


def cmd_report(a) -> int:
    try:
        api("POST", sig_path(a.sig) + "/report",
            {"status": a.status, "message": a.message, "stats": json.loads(a.stats or "{}")})
    except Unreachable as e:
        print(e, file=sys.stderr)
        return 20
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("identify"); p.add_argument("--mount", required=True); p.add_argument("--devname", default="")
    p = sub.add_parser("seen"); p.add_argument("--mount", required=True); p.add_argument("--sig", required=True)
    p = sub.add_parser("pull"); p.add_argument("--mount", required=True); p.add_argument("--sig", required=True)
    p = sub.add_parser("stage"); p.add_argument("--mount", required=True); p.add_argument("--sig", required=True)
    p.add_argument("--music-root", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("report"); p.add_argument("--sig", required=True)
    p.add_argument("--status", required=True, choices=["syncing", "ok", "warning", "error", "unmounting", "safe"])
    p.add_argument("--message", default=""); p.add_argument("--stats", default="")
    a = ap.parse_args()
    try:
        return {"identify": cmd_identify, "seen": cmd_seen, "pull": cmd_pull,
                "stage": cmd_stage, "report": cmd_report}[a.cmd](a)
    except Unreachable as e:
        print(f"Aquaria error: {e}", file=sys.stderr)
        return 20


if __name__ == "__main__":
    sys.exit(main())
