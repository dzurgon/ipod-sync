# ipod-sync

Automated music management for iPod Classic (Rockbox) + Linux home server + Mac.

Plug in your iPod — everything else happens automatically.

---

## What it does

| Trigger | Action |
|---------|--------|
| New music lands on server | beets auto-tags, renames folders, fetches cover art |
| iPod plugged into server via USB | Music synced from server → iPod |
| iPod plugged in | Playlists synced (bidirectional) |
| iPod plugged in | `.scrobbler.log` submitted to Last.fm, marked to prevent re-submission |
| Mac qBittorrent finishes a "Music" download | Files copied to Nextcloud → server pipeline picks them up |

---

## Stack

```
NUC (Ubuntu)                        Mac
├── beets          auto-tagger       └── Nextcloud desktop client
├── inotifywait    file watcher          ~/Nextcloud/FLAC  ←→  server
├── udev           iPod detection    └── qBittorrent
├── systemd        service manager       torrent-finish.sh hook
├── FastAPI+HTMX   playlist manager
└── python3        Last.fm scrobbler

iPod Classic (Rockbox)
├── FLAC/          music library
├── Playlists/     .m3u files
└── .scrobbler.log play history
```

---

## File structure

```
ipod-sync/
├── config.env.example          template for secrets (copy → /etc/ipod-sync/config.env)
│
├── server/
│   ├── udev/
│   │   └── 99-ipod-rockbox.rules     udev rule: fires on iPod USB connect
│   ├── scripts/
│   │   ├── udev-trigger.sh           thin root→user bridge via systemd-run
│   │   ├── ipod-sync.sh              main sync script (mount → rsync → playlists → scrobble)
│   │   └── scrobble.py               Last.fm scrobbler (parses Rockbox log format)
│   ├── beets/
│   │   └── config.yaml               fully silent auto-tagger config
│   ├── watcher/
│   │   ├── music-watcher.sh          inotifywait loop → beets → Nextcloud rescan
│   │   └── music-watcher.service     systemd unit
│   └── web/
│       ├── app.py                    FastAPI playlist manager (port 8337)
│       ├── requirements.txt
│       ├── playlist-manager.service  systemd unit
│       ├── static/style.css
│       └── templates/                HTMX-powered UI
│
├── mac/
│   └── torrent-finish.sh             qBittorrent post-download hook (Path A)
│
└── docs/
    └── setup.md                      step-by-step install guide
```

---

## Music ingest paths

**Path A — Mac qBittorrent:**
```
~/Torrents/Music  →[torrent-finish.sh]→  ~/Nextcloud/FLAC
  →[Nextcloud sync]→  /mnt/data/media/music/FLAC
    →[inotify watcher]→  beets auto-tags  →  Nextcloud rescan
```

**Path B — Server qBittorrent:**
```
/mnt/data/media/music/FLAC  →[inotify watcher]→  beets auto-tags
  →[Nextcloud rescan]→  ~/Nextcloud/FLAC (Mac)
```

Both paths converge at the inotify watcher. beets is idempotent.

---

## Folder naming convention

beets renames every album folder to:

```
AlbumName - AlbumArtist (Year)/
  01 - Track Title.flac
  cover.jpg
```

This is the format Rockbox and the iPod FLAC/ directory both expect.

---

## Playlist format

Playlists are `.m3u` files with **relative paths**:

```m3u
#EXTM3U
AlbumName - AlbumArtist (Year)/01 - Track.flac
AlbumName - AlbumArtist (Year)/02 - Track.flac
```

Relative paths work on both the server and the iPod without any rewriting.

---

## Requirements

### Server (Ubuntu)
```bash
sudo apt install beets inotify-tools python3 python3-pip rsync
pip3 install beets[fetchart,lastgenre,embedart,scrub]
pip3 install fastapi uvicorn jinja2 python-multipart
```

### Mac
- [Nextcloud desktop client](https://nextcloud.com/install/#install-clients)
- [qBittorrent](https://www.qbittorrent.org/)

### Accounts
- [Last.fm API key](https://www.last.fm/api/account/create) (free)

---

## Quick setup

Full instructions in [docs/setup.md](docs/setup.md). Short version:

```bash
# 1. Config
sudo mkdir -p /etc/ipod-sync
sudo cp config.env.example /etc/ipod-sync/config.env
sudo nano /etc/ipod-sync/config.env   # fill in paths, passwords, API keys

# 2. Deploy scripts
sudo mkdir -p /usr/local/lib/ipod-sync
sudo cp server/scripts/*.{sh,py} server/watcher/music-watcher.sh /usr/local/lib/ipod-sync/
sudo chmod +x /usr/local/lib/ipod-sync/*.sh /usr/local/lib/ipod-sync/*.py

# 3. beets config
cp server/beets/config.yaml ~/.config/beets/config.yaml

# 4. Systemd services
sudo cp server/watcher/music-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now music-watcher

# 5. udev rule (iPod auto-sync on USB)
sudo cp server/udev/99-ipod-rockbox.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules

# 6. Last.fm auth (interactive, one-time)
python3 /usr/local/lib/ipod-sync/scrobble.py --auth --config /etc/ipod-sync/config.env

# 7. Playlist manager web app
cd server/web && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo cp playlist-manager.service /etc/systemd/system/
sudo systemctl enable --now playlist-manager
# → http://your-nuc-ip:8337
```

---

## Secrets & security

All credentials live in `/etc/ipod-sync/config.env` on the server — this file is **never committed**. The repo only contains `config.env.example` with placeholder values.

The Last.fm session key is stored in `~/.config/ipod-sync/lastfm-session` (mode `600`).

---

## Log locations

| Component | Log |
|-----------|-----|
| iPod sync | `/var/log/ipod-sync/ipod-sync-YYYYMMDD-HHMMSS.log` |
| beets | `/var/log/ipod-sync/beets-import.log` |
| Music watcher | `/var/log/ipod-sync/watcher.log` · `journalctl -u music-watcher` |
| Playlist manager | `journalctl -u playlist-manager` |
| Mac torrent hook | `~/Library/Logs/ipod-sync/torrent-finish.log` |

---

## Troubleshooting

**iPod not detected:**
```bash
udevadm monitor --environment --udev   # plug iPod in, watch for events
lsusb | grep -i apple                  # confirm vendor:product IDs
```
If `idProduct` differs from `1261`, update [server/udev/99-ipod-rockbox.rules](server/udev/99-ipod-rockbox.rules).

**beets not matching an album:**
```bash
cat /var/log/ipod-sync/beets-import.log   # shows unmatched files
beet import -t /path/to/album             # re-run interactively for that album
```

**Scrobbles not submitting:**
```bash
python3 /usr/local/lib/ipod-sync/scrobble.py \
  --log /media/ben/IPOD/.scrobbler.log \
  --config /etc/ipod-sync/config.env \
  --dry-run
```

**Nextcloud not syncing:**
```bash
docker exec nextcloud php occ files:scan --path="ben/files/FLAC"
```

---

## Tested on

- Ubuntu 22.04 (server)
- macOS Sonoma (Mac client)
- iPod Classic 5th gen, Rockbox 3.15
