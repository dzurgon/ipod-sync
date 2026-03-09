# Setup Guide

Step-by-step installation for the iPod music management system.

---

## Prerequisites

### NUC (Ubuntu)
```
sudo apt update
sudo apt install -y \
  beets \
  inotify-tools \
  python3 python3-pip python3-venv \
  rsync \
  udevadm
```

Install beets plugins:
```
pip3 install beets[fetchart,lastgenre,embedart,scrub]
```

### Mac
- Nextcloud desktop client (syncing to `~/Nextcloud/`)
- qBittorrent

---

## 1. Config file

```bash
sudo mkdir -p /etc/ipod-sync
sudo cp config.env.example /etc/ipod-sync/config.env
sudo nano /etc/ipod-sync/config.env   # fill in real values
sudo chmod 640 /etc/ipod-sync/config.env
sudo chown root:ben /etc/ipod-sync/config.env
```

---

## 2. Beets

```bash
mkdir -p ~/.config/beets
cp server/beets/config.yaml ~/.config/beets/config.yaml
```

Test on a single album:
```bash
beet import -q /mnt/data/media/music/FLAC/SomeAlbum
```

---

## 3. Music watcher (systemd)

```bash
sudo mkdir -p /usr/local/lib/ipod-sync
sudo cp server/watcher/music-watcher.sh /usr/local/lib/ipod-sync/
sudo chmod +x /usr/local/lib/ipod-sync/music-watcher.sh

sudo cp server/watcher/music-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now music-watcher

# Check it's running:
sudo systemctl status music-watcher
journalctl -u music-watcher -f
```

---

## 4. iPod udev rule

**Find your iPod's USB IDs:**
```bash
lsusb | grep -i apple
# e.g.: Bus 001 Device 004: ID 05ac:1261 Apple, Inc. iPod Classic
```

Edit `server/udev/99-ipod-rockbox.rules` if `idProduct` differs from `1261`.

```bash
sudo cp server/udev/99-ipod-rockbox.rules /etc/udev/rules.d/
sudo cp server/scripts/udev-trigger.sh /usr/local/lib/ipod-sync/
sudo cp server/scripts/ipod-sync.sh    /usr/local/lib/ipod-sync/
sudo cp server/scripts/scrobble.py     /usr/local/lib/ipod-sync/

sudo chmod +x \
  /usr/local/lib/ipod-sync/udev-trigger.sh \
  /usr/local/lib/ipod-sync/ipod-sync.sh \
  /usr/local/lib/ipod-sync/scrobble.py

sudo udevadm control --reload-rules
sudo udevadm trigger
```

**Test:** Plug in iPod and watch:
```bash
journalctl -f -t ipod-sync
# or:
tail -f /var/log/ipod-sync/ipod-sync-*.log
```

---

## 5. Last.fm authentication

Get an API key at https://www.last.fm/api/account/create

Add to `/etc/ipod-sync/config.env`:
```
LASTFM_API_KEY=your-key
LASTFM_API_SECRET=your-secret
LASTFM_USERNAME=blgondzur
```

Run auth once (interactive — prompts for password):
```bash
python3 /usr/local/lib/ipod-sync/scrobble.py \
  --auth \
  --config /etc/ipod-sync/config.env
```

Session key is saved to `~/.config/ipod-sync/lastfm-session` and reused forever.

Test with dry-run:
```bash
python3 /usr/local/lib/ipod-sync/scrobble.py \
  --log /media/ben/IPOD/.scrobbler.log \
  --config /etc/ipod-sync/config.env \
  --dry-run
```

---

## 6. Playlist Manager web app

```bash
cd server/web
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Copy to deploy location
sudo cp -r server/web /usr/local/lib/ipod-sync/web
sudo cp server/web/playlist-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now playlist-manager
```

Access at: `http://192.168.1.65:8337` or `http://100.94.158.95:8337` (Tailscale)

---

## 7. Mac — qBittorrent hook (Path A)

```bash
chmod +x mac/torrent-finish.sh
```

In qBittorrent → Preferences → Downloads:
- Check "Run external program on torrent completion"
- Set command:
  ```
  /Users/ben/Code/personal-scripts/ipod/mac/torrent-finish.sh "%D" "%N" "%L"
  ```

Label music torrents with category **"Music"** in qBittorrent for the hook to fire.

---

## Log locations

| Log | Location |
|-----|----------|
| iPod sync runs | `/var/log/ipod-sync/ipod-sync-YYYYMMDD-HHMMSS.log` |
| beets import | `/var/log/ipod-sync/beets-import.log` |
| Music watcher | `/var/log/ipod-sync/watcher.log` + `journalctl -u music-watcher` |
| Playlist manager | `journalctl -u playlist-manager` |
| Mac torrent hook | `~/Library/Logs/ipod-sync/torrent-finish.log` |

---

## Troubleshooting

**iPod not detected by udev:**
```bash
udevadm monitor --environment --udev | grep -i ipod
udevadm info --name=/dev/sdX --attribute-walk | grep -E 'idVendor|idProduct|ID_FS_LABEL'
```

**beets not matching albums:**
Check `/var/log/ipod-sync/beets-import.log` — unmatched files are logged there.
You can manually correct them with `beet modify` or lower `strong_rec_thresh` further.

**Scrobbles not submitting:**
```bash
python3 /usr/local/lib/ipod-sync/scrobble.py \
  --log /media/ben/IPOD/.scrobbler.log \
  --config /etc/ipod-sync/config.env \
  --dry-run
```

**Nextcloud not picking up changes:**
```bash
docker exec nextcloud php occ files:scan --path="ben/files/FLAC"
```
