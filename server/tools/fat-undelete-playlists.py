#!/usr/bin/env python3
"""
fat-undelete-playlists.py — recover deleted playlists from a (Rockbox) iPod's FAT32 disk.

READ-ONLY: opens the raw partition for reading and never writes to it. Run it on the
UNMOUNTED partition, before anything else writes to the iPod — a deleted file's data
survives only until its clusters are reused.

    sudo python3 fat-undelete-playlists.py /dev/sdX2 --out ~/recovered-playlists
    sudo python3 fat-undelete-playlists.py /dev/sdX2 --out DIR --dir Playlists --ext .m3u8 .m3u

Stage 1 (instant) reads deleted directory entries in --dir: each still records the
file's first cluster and size, so small files (one cluster) come back exactly.
Stage 2 (--carve, slow: reads all free space) looks for playlist-like text in free
clusters, for files whose directory entries were overwritten since.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

DELETED = 0xE5
ATTR_LFN, ATTR_DIR, ATTR_VOLUME = 0x0F, 0x10, 0x08
AUDIO = (b".flac", b".mp3", b".m4a", b".aiff", b".aif", b".wav", b".ogg", b".opus", b".wma")


class Fat32:
    def __init__(self, dev: str):
        self.f = open(dev, "rb")                                   # read-only, always
        bs = self.f.read(512)
        if bs[82:90] != b"FAT32   ":
            sys.exit(f"{dev}: not a FAT32 filesystem (type field {bs[82:90]!r})")
        self.bps = struct.unpack_from("<H", bs, 11)[0]
        self.spc = bs[13]
        reserved = struct.unpack_from("<H", bs, 14)[0]
        nfats = bs[16]
        self.fat_sectors = struct.unpack_from("<I", bs, 36)[0]
        self.root = struct.unpack_from("<I", bs, 44)[0]
        total = struct.unpack_from("<I", bs, 32)[0]
        self.csize = self.bps * self.spc
        self.fat_off = reserved * self.bps
        self.data_off = (reserved + nfats * self.fat_sectors) * self.bps
        self.nclusters = (total * self.bps - self.data_off) // self.csize + 2
        self._fat = None

    def fat(self) -> bytes:
        if self._fat is None:
            self.f.seek(self.fat_off)
            self._fat = self.f.read(self.fat_sectors * self.bps)
        return self._fat

    def next(self, c: int) -> int:
        return struct.unpack_from("<I", self.fat(), c * 4)[0] & 0x0FFFFFFF

    def is_free(self, c: int) -> bool:
        return self.next(c) == 0

    def read_cluster(self, c: int, n: int = 1) -> bytes:
        self.f.seek(self.data_off + (c - 2) * self.csize)
        return self.f.read(self.csize * n)

    def chain(self, c: int) -> bytes:
        out, seen = bytearray(), set()
        while 2 <= c < 0x0FFFFFF8 and c not in seen:
            seen.add(c)
            out += self.read_cluster(c)
            c = self.next(c)
        return bytes(out)


def lfn_part(e: bytes) -> str:
    raw = e[1:11] + e[14:26] + e[28:32]
    return raw.decode("utf-16-le", "replace").split("\x00")[0].replace("￿", "")


def entries(dirdata: bytes):
    """Yield (name, attr, first_cluster, size, deleted) for each file/dir entry."""
    lfn: list[bytes] = []
    for i in range(0, len(dirdata), 32):
        e = dirdata[i:i + 32]
        if e[0] == 0x00:
            lfn = []                       # free slot; keep scanning (deleted data may follow)
            continue
        attr = e[11]
        if attr == ATTR_LFN:
            lfn.append(e)
            continue
        if attr & ATTR_VOLUME:
            lfn = []
            continue
        deleted = e[0] == DELETED
        # LFN parts precede their short entry in reverse order; only trust parts with the
        # same deleted-ness as the short entry (a reused slot breaks the run).
        parts = [p for p in lfn if (p[0] == DELETED) == deleted]
        name = "".join(lfn_part(p) for p in reversed(parts)) if parts else ""
        if not name:
            base = e[0:8].decode("ascii", "replace").rstrip()
            ext = e[8:11].decode("ascii", "replace").rstrip()
            name = ("_" + base[1:] if deleted else base) + ("." + ext if ext else "")
        cluster = struct.unpack_from("<H", e, 20)[0] << 16 | struct.unpack_from("<H", e, 26)[0]
        size = struct.unpack_from("<I", e, 28)[0]
        yield name, attr, cluster, size, deleted
        lfn = []


def find_dir(fs: Fat32, path: str) -> int:
    c = fs.root
    for part in [p for p in path.strip("/").split("/") if p]:
        for name, attr, cl, _, deleted in entries(fs.chain(c)):
            if not deleted and attr & ATTR_DIR and name.casefold() == part.casefold():
                c = cl
                break
        else:
            sys.exit(f"directory not found on the iPod: /{path}")
    return c


def looks_like_playlist(data: bytes) -> bool:
    lines = [l.strip() for l in data.splitlines() if l.strip()]
    if not lines:
        return False
    tracks = [l for l in lines if not l.startswith(b"#")]
    return bool(tracks) and sum(l.lower().endswith(AUDIO) for l in tracks) >= 0.8 * len(tracks)


def save(out: Path, name: str, data: bytes) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    safe = "".join("_" if c in '/\\:*?"<>|' else c for c in name) or "recovered.m3u8"
    p, n = out / safe, 1
    while p.exists():
        n += 1
        p = out / f"{Path(safe).stem} ({n}){Path(safe).suffix}"
    p.write_bytes(data)
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("device", help="the iPod's FAT32 partition, e.g. /dev/sdd2 (unmounted)")
    ap.add_argument("--out", required=True, help="folder to write recovered playlists into")
    ap.add_argument("--dir", default="Playlists", help="folder on the iPod (default: Playlists)")
    ap.add_argument("--ext", nargs="+", default=[".m3u8", ".m3u"])
    ap.add_argument("--carve", action="store_true",
                    help="also scan all free space for playlist text (slow: reads the whole free area)")
    a = ap.parse_args()
    exts = tuple(x.lower() for x in a.ext)
    out = Path(a.out).expanduser()

    fs = Fat32(a.device)
    print(f"FAT32: {fs.csize // 1024} KiB clusters, {fs.nclusters:,} clusters")
    dc = find_dir(fs, a.dir)
    found = 0
    print(f"\n/{a.dir} — live files:")
    for name, attr, cl, size, deleted in entries(fs.chain(dc)):
        if not deleted and not attr & ATTR_DIR:
            print(f"  {name}  ({size} bytes)")

    print(f"\n/{a.dir} — deleted entries:")
    seen_clusters = set()
    for name, attr, cl, size, deleted in entries(fs.chain(dc)):
        if not deleted or attr & ATTR_DIR or not name.lower().endswith(exts) or name.startswith("._"):
            continue                                 # ._* = macOS metadata, not a playlist
        if cl < 2 or cl >= fs.nclusters or size == 0:
            print(f"  {name}: no data recorded (size {size}) — skipped")
            continue
        n = -(-size // fs.csize)
        data = fs.read_cluster(cl, n)[:size]
        state = "free" if all(fs.is_free(cl + i) for i in range(n)) else "REUSED by another file"
        ok = looks_like_playlist(data)
        print(f"  {name}: {size} bytes at cluster {cl} ({state}) — "
              f"{'looks like a playlist' if ok else 'contents do NOT look like a playlist'}")
        if ok:
            p = save(out, name, data)
            seen_clusters.add(cl)
            found += 1
            print(f"    → {p}")

    if a.carve:
        print("\nCarving free clusters for playlist text (Ctrl-C to stop)…")
        fat = fs.fat()
        for c in range(2, fs.nclusters):
            if c % 200000 == 0:
                print(f"  … {100 * c / fs.nclusters:.1f}%", flush=True)
            if struct.unpack_from("<I", fat, c * 4)[0] & 0x0FFFFFFF or c in seen_clusters:
                continue
            data = fs.read_cluster(c)
            if not data.strip(b"\x00"):
                continue
            text = data.split(b"\x00", 1)[0]
            if len(text) > 20 and looks_like_playlist(text):
                p = save(out, f"carved-cluster-{c}.m3u8", text)
                found += 1
                print(f"  cluster {c}: {len(text)} bytes → {p}")

    print(f"\n{found} playlist(s) recovered into {out}" if found else "\nNothing recovered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
