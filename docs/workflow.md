# How It All Works

This document explains what every part of the system is doing, why it exists,
and what actually happens — step by step — when you interact with it.

---

## The Big Picture

There are three locations that need to stay in sync:

```
/mnt/data/media/music/FLAC    ← server music library (the authority)
/mnt/data/media/music/Playlists

/mnt/data/ipod/FLAC           ← iPod (copy of the library)
/mnt/data/ipod/Playlists

~/Nextcloud/FLAC              ← Mac (synced from server via Nextcloud)
~/Nextcloud/Playlists
```

The server is always the authority. Everything flows out from it.

---

## What Each Component Does

### beets
A command-line music tagger. When new music lands on the server, beets:
- Looks up the album on MusicBrainz (a public music database)
- Writes correct metadata tags into the files (artist, album, year, track number, genre)
- Renames the folder to the standard format: `AlbumName - AlbumArtist (Year)/`
- Downloads `cover.jpg` from the Cover Art Archive if it's missing

**Why it matters:** Without beets, folders named `new album` or `Artist - 2024` would
land on the iPod with inconsistent naming. beets enforces a single clean structure
that Rockbox, Jellyfin, and the playlist manager all understand.

**Where it runs:** On the server, triggered automatically by the music watcher.

**Config:** `server/beets/config.yaml`

---

### music-watcher (systemd service)
A background service that watches `/mnt/data/media/music/FLAC` for new files using
`inotifywait`. When a new audio file appears (via SCP, Nextcloud sync, or qBittorrent):

1. It waits 30 seconds after the last file event (debounce — so a 50-track album
   triggers beets once, not 50 times)
2. Runs `beet import -q` on the affected folder
3. Triggers a Nextcloud rescan so the Mac's Nextcloud client picks up the changes

**Status:** `systemctl status music-watcher`
**Logs:** `/var/log/ipod-sync/watcher.log`

---

### ipod-sync.sh
The main sync script. Runs on USB connect (via udev) or manually. Does four things
in sequence — see full walkthrough below.

**Logs:** `/var/log/ipod-sync/ipod-sync-YYYYMMDD-HHMMSS.log`

---

### scrobble.py
Parses the `.scrobbler.log` file that Rockbox writes on the iPod. Every time you
listen to a track in Rockbox, it logs: artist, album, title, timestamp, duration.
When the iPod syncs, this script reads those entries and submits them to Last.fm
so your listening history is tracked.

To prevent double-submissions, each submitted line gets a `# SUBMITTED` marker
appended to it. On the next sync, those lines are skipped.

---

### Playlist Manager (web app)
A web interface running on the server at port `8337`. Accessible from any browser
on your LAN or via Tailscale.

You can:
- Browse all albums in the library
- Create and edit `.m3u` playlist files
- View a live activity feed of sync events
- Read the raw logs from past iPod syncs

Playlists are stored as `.m3u` files at `/mnt/data/media/music/Playlists/` on the
server. When the iPod syncs, these are pushed to `/mnt/data/ipod/Playlists/`.

Playlist paths use a relative format (`../FLAC/Album/track.flac`) so the same file
works correctly in both locations without any rewriting.

**Start:** `systemctl status playlist-manager`
**Access:** `http://192.168.1.65:8337` or `http://100.94.158.95:8337` (Tailscale)

---

### udev rule
A Linux kernel rule in `/etc/udev/rules.d/99-ipod-rockbox.rules`. It watches for
USB device events matching your iPod's vendor ID (`05ac`) and product ID (`1209`).

When the iPod is plugged in, udev fires `udev-trigger.sh`, which uses `systemd-run`
to launch `ipod-sync.sh` as a proper background process with its own journal entry.

**Why systemd-run?** udev rules run as root in a minimal environment with no home
directory, no PATH, and a strict execution timeout. `systemd-run` escapes that
context and gives the script a full environment to work in.

---

## Intended Workflow (once fully set up)

Once the udev rule is deployed and the fstab mount permissions are fixed, the
entire pipeline is hands-free:

```
1. You plug your iPod into the NUC via USB.

2. Linux kernel detects the USB device (vendor 05ac, product 1209).

3. udev fires 99-ipod-rockbox.rules → runs udev-trigger.sh.

4. udev-trigger.sh waits 3 seconds (filesystem settle), then launches
   ipod-sync.sh via systemd-run as user jimin.

5. ipod-sync.sh runs automatically — see full breakdown below.

6. You get a log file at /var/log/ipod-sync/ipod-sync-YYYYMMDD-HHMMSS.log.

7. Unplug the iPod when the log says "iPod is safe to unplug."
```

For new music arriving on the server:

```
1. You SCP a folder, or it arrives via Nextcloud from your Mac.

2. inotifywait detects the new .flac/.mp3 file.

3. After 30 seconds of quiet (debounce), music-watcher.sh runs beets on it.

4. beets renames the folder, fixes tags, downloads cover.jpg.

5. Nextcloud rescans its library so the Mac client picks up the change.

6. Next time you plug in the iPod, the new album syncs automatically.
```

---

## Current Manual Workflow

Because the udev auto-trigger isn't firing yet (permissions issue on the vfat
mount — `root` owns it instead of `jimin`), you run the sync manually:

```bash
sudo CONFIG_FILE=/etc/ipod-sync/config.env /usr/local/lib/ipod-sync/ipod-sync.sh
```

Here is exactly what happens when you run that command:

---

### Step 1 — Wait for iPod mount

```
[02:39:57]   Waiting for iPod mount
[02:39:57] iPod mounted at: /mnt/data/ipod
```

The script checks `IPOD_MOUNT_PATH` from your config (`/mnt/data/ipod`).
It calls `mountpoint` to confirm it's actually mounted. If nothing is mounted
there within 60 seconds, it exits with an error.

Once found, it derives three paths from the mount:
```
/mnt/data/ipod/FLAC         ← music destination
/mnt/data/ipod/Playlists    ← playlist destination
/mnt/data/ipod/.scrobbler.log
```

---

### Step 2 — rsync music: server → iPod

```
[02:39:57]   Syncing music: server → iPod
Source:      /mnt/data/media/music/FLAC
Destination: /mnt/data/ipod/FLAC
```

rsync compares the server library against the iPod using **timestamps** (not full
checksums — that was too slow with 5.8GB of music). The `--modify-window=2` flag
handles vfat's 2-second timestamp granularity so files don't get recopied
unnecessarily.

What rsync does for each file:
- **File on server, not on iPod** → copies it across
- **File on both, same timestamp** → skips it (fast)
- **File on iPod, not on server** → deletes it from iPod (`--delete`)
- **Different content or timestamp** → overwrites the iPod copy

Files excluded: `.DS_Store`, `._*` (macOS junk), `.Spotlight-*`, `.Trashes`, `*.tmp`

At the end of this step you'll see an rsync stats summary:
```
Number of files: 4,200
Number of created files: 12
Number of deleted files: 0
Total transferred file size: 287.4 MB
```

---

### Step 3 — Sync playlists (bidirectional)

```
[02:xx:xx]   Syncing playlists
```

This step runs in two passes:

**Pass A — Pull from iPod → server (new playlists only)**
Any `.m3u` file on the iPod that doesn't exist on the server gets copied to
`/mnt/data/media/music/Playlists/`. This is how playlists you create directly
in Rockbox make it back to the server.

**Pass B — Push from server → iPod (server is authoritative)**
All server playlists are rsync'd to `/mnt/data/ipod/Playlists/`. If you edited
a playlist in the web UI or via Nextcloud on your Mac, those changes get pushed
to the iPod here.

Playlist files use relative paths (`../FLAC/AlbumName - Artist (Year)/track.flac`)
so Rockbox can resolve them correctly from `/mnt/data/ipod/Playlists/`.

---

### Step 4 — Scrobble to Last.fm

```
[02:xx:xx]   Scrobbling .scrobbler.log → Last.fm
```

The script checks for `/mnt/data/ipod/.scrobbler.log`. Rockbox writes a line
to this file every time you finish a track (or get 50% through it). Format:

```
ARTIST    ALBUM    TITLE    TRACKNUM    DURATION    RATING    TIMESTAMP    MBID
```

`scrobble.py` reads each unsubmitted line (no `# SUBMITTED` marker), sends them
to Last.fm's API in batches of 50, and then marks submitted lines:

```
The Fall    Hex Enduction Hour    The Classical    ...    # SUBMITTED
```

On the next sync, those lines are skipped. Tracks rated `S` (skipped in Rockbox)
are never submitted. Tracks older than 14 days are also skipped (Last.fm rejects them).

---

### Step 5 — Done

```
[02:xx:xx]   Sync complete
[02:xx:xx] Log saved to: /var/log/ipod-sync/ipod-sync-20260309-023957.log
[02:xx:xx] iPod is safe to unplug.
```

The full log is saved with a timestamp. The last 30 logs are kept; older ones
are automatically cleaned up.

---

## Viewing Sync Activity in the Web UI

Open `http://192.168.1.65:8337` in any browser.

The **Activity Feed** on the main page polls every 10 seconds and shows events
generated by the playlist manager (playlist created, track added, etc.).

The **Recent Syncs** table at the bottom links to each `ipod-sync-*.log` file.
Click any log to read the full rsync output and scrobble results from that session.

The **Library** section shows every album folder in `/mnt/data/media/music/FLAC`
with track counts. Use the search box to filter.

The **Playlists** section lets you create new `.m3u` files, add tracks from the
library browser, remove tracks, and delete playlists. Changes take effect
immediately on the server — they'll reach the iPod on the next sync.

---

## What Still Needs Fixing

| Issue | Status | Fix |
|-------|--------|-----|
| udev auto-trigger on USB | Pending | Fix vfat mount ownership via fstab so `jimin` can write; then udev fires hands-free |
| beets debounce timer | Pending | Variable scoping bug in watcher — beets events fire but flush doesn't always trigger |
| Nextcloud → server path | Pending | Confirm Nextcloud data volume maps to `/mnt/data/media/music/FLAC` or add bridge |
| Last.fm auth | Pending | Run `python3 /usr/local/lib/ipod-sync/scrobble.py --auth --config /etc/ipod-sync/config.env` once interactively |
