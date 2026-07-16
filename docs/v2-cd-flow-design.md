# v2 Design — CD Ripping Flow + Homeserver Integration

**Status:** Proposal for review. No code changes yet — this document is the plan we'll
build from. Nothing here is deployed.

**Goal:** Make ripping a CD hassle-free. Insert disc → import in Apple Music → it
lands on the home server → it's on the iPod next time it's plugged in. Plus: one
always-on webpage where you can browse the library and read verbose logs (CD imports,
file uploads, every iPod plug/unplug) from your phone.

---

## 1. What already exists (and stays)

The current system, which we are **extending, not replacing**:

- **beets** auto-tags/renames new music that lands in `/mnt/data/media/music/FLAC`.
- **music-watcher** (systemd + inotifywait) fires beets when files appear there.
- **ipod-sync.sh** (udev on USB connect) rsyncs `FLAC` → `/mnt/data/ipod/FLAC`, syncs
  playlists, and scrobbles `.scrobbler.log` to Last.fm.
- **Playlist Manager** — FastAPI + HTMX web app on `:8337`, reachable via Tailscale
  `100.94.158.95:8337`.
- **Mac ingest** today is qBittorrent → `~/Nextcloud/FLAC` → Nextcloud sync → server.

The CD flow is a **new, third ingest path** that converges on the same beets watcher,
so everything downstream (iPod sync, playlists, scrobbling) works unchanged.

Server user is **`jimin`**; server music authority is **`/mnt/data/media/music/FLAC`**;
iPod copy is **`/mnt/data/ipod/`**. These are unchanged.

---

## 2. The new CD ingest path (end to end)

```
┌─ MAC ──────────────────────────────────────────────────────────────┐
│  1. Insert CD into Apple SuperDrive                                 │
│  2. Apple Music imports it →                                        │
│     /Users/ben/Music/Music/Media.localized/Music/<Artist>/<Album>/  │
│        *.m4a                                                         │
│  3. launchd agent (WatchPaths) notices new files, debounces,        │
│     rsync-over-SSH pushes the new album folder →                    │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  rsync over Tailscale SSH
                               ▼
┌─ NUC (jimin) ──────────────────────────────────────────────────────┐
│  4. Lands in /mnt/data/media/music/FLAC/<Artist>/<Album>/           │
│  5. music-watcher (inotify) sees new audio → debounce → beets       │
│  6. beets tags, renames to "Album - Artist (Year)/", fetches cover  │
│  7. (optional) transcode step: ALAC .m4a → .flac                    │
│  8. Event logged: "CD import: <Album> (N tracks)"                   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  next USB connect
                               ▼
┌─ iPod (Rockbox) ───────────────────────────────────────────────────┐
│  9. ipod-sync.sh rsyncs FLAC → /mnt/data/ipod/FLAC                  │
│ 10. Plug/unplug + sync events logged and visible in the web UI      │
└─────────────────────────────────────────────────────────────────────┘
```

The only genuinely new pieces are steps **3** (Mac watcher + push) and the
**logging/UI** work in steps 8–10. Steps 4–7 reuse the existing watcher/beets.

---

## 3. Audio format decision

You picked "decide for me," so here's the recommendation and why.

**Default: keep the native format Apple Music produces (`.m4a`), don't transcode.**

- Apple Music rips to `.m4a` regardless of codec. The `.m4a` is a container; inside it's
  either **ALAC** (lossless) or **AAC** (lossy), set by Music → Settings → Files →
  Import Settings. We'll confirm which yours is set to during the build phase.
- **Rockbox on the iPod Classic plays both ALAC and AAC natively**, so nothing needs
  converting just to play on the iPod.
- **beets tags `.m4a` fine** and files them into the same `Artist/Album (Year)/` layout,
  so they sit happily beside the existing FLAC.
- Transcoding **AAC → FLAC is pointless** (you can't recover detail lost by a lossy
  encoder; you'd just make bigger files of the same audio).
- Transcoding **ALAC → FLAC is lossless** but only buys you uniformity — both are
  lossless, both play everywhere you care about. Not worth the CPU per rip by default.

**The one wrinkle — the folder is named `FLAC` but will hold `.m4a` too.** That's cosmetic.
Two options, your call during build:

- **(a)** Leave it. `FLAC` becomes "the lossless-ish music library" by convention. Zero
  churn, no path changes anywhere.
- **(b)** Rename the library dir to `Music` and update `MUSIC_DIR` in `config.env`, beets
  `directory:`, the watcher, and iPod-sync. One-time, touches a handful of config lines,
  and iPod re-syncs into `/mnt/data/ipod/FLAC` (or we rename that too). Cleaner long-term.

**Optional transcode toggle (build it, default off):** a `CD_TRANSCODE_TO_FLAC=false`
switch in `config.env`. When true, a beets `convert`-plugin step (or a small ffmpeg wrapper)
turns ALAC → FLAC after import and leaves AAC untouched. Off by default per the reasoning
above; there if you ever want a pure-FLAC library.

---

## 4. Mac → server sync (auto folder-watch)

You picked **auto folder-watch via launchd**. Design:

- A **launchd LaunchAgent** (`com.ben.cd-sync.plist`) with `WatchPaths` pointing at the
  Apple Music folder. launchd relaunches the agent whenever that tree changes.
- The agent runs `mac/cd-sync.sh`, which:
  1. **Debounces** — waits for the folder to go quiet (a full album import writes many
     files); no push mid-import.
  2. **rsync over SSH** to the NUC over Tailscale:
     `rsync -rt --modify-window=2 --exclude junk … "$MUSIC_APP_DIR/" jimin@100.94.158.95:/mnt/data/media/music/FLAC/`
  3. Pushes **only new/changed files** (rsync diff). **No `--delete`** on this hop —
     the Mac is a *source*, not the authority, so deleting a CD from Apple Music must not
     wipe it from the server.
  4. Logs to `~/Library/Logs/ipod-sync/cd-sync.log` **and** posts a structured event to
     the web app's log endpoint (see §6) so "CD import: X" shows up on the webpage.

**Why not Nextcloud for this?** The Apple Music library lives at
`/Users/ben/Music/Music/Media.localized/Music/`, which is *not* your `~/Nextcloud/FLAC`
folder, and you don't want Apple Music's whole library folder inside Nextcloud (it churns
constantly). A dedicated one-way rsync push is cleaner and matches how the rest of the repo
already moves music. qBittorrent → Nextcloud path stays exactly as-is.

**Prereqs to set up during build:** an SSH key from Mac → `jimin@nuc` (Tailscale),
`fswatch` or pure-launchd `WatchPaths` (no extra dep), and a guard so the push **aborts if
`/mnt/data` isn't healthily mounted on the server** (see §7).

**Caveat on Apple Music internals:** Apple Music may keep files inside its managed library
and can move/rename on edits. We treat the Media folder as read-only *source* and only ever
copy *out* of it. If "Keep Media folder organized" or "Copy files to library" is off, we'll
adjust the watch path during build — easy to confirm then.

---

## 5. Server side — reuse the watcher, add CD awareness

Almost no new server code for ingest itself:

- New files arriving via rsync trigger the **existing** `music-watcher` inotify loop →
  beets → cover art → (optional transcode). beets is idempotent, so re-pushes are safe.
- We tag CD-origin imports so the log reads "CD import" vs "torrent import." Simplest
  approach: the Mac `cd-sync.sh` posts the event with a `source=cd` label after a
  successful push, rather than trying to infer origin server-side.

**Bug to fix here (you flagged it):** `music-watcher.sh` has a debounce that never fires
reliably. The inotify read-loop runs in one subshell (right side of the `|` pipe) and the
flush timer runs in a *second* backgrounded subshell. Each subshell gets its **own copy** of
`PENDING_DIRS` and `LAST_EVENT_TIME`, so the timer never sees the events the reader recorded.
Fix in build phase: collapse to a single process (e.g. `inotifywait` writing to a coprocess/
FIFO that one loop both reads and time-checks, or a `read -t $DEBOUNCE` timeout on the pipe so
the same shell that collects events also flushes them). This is what's been blocking
hands-free beets.

---

## 6. Web UI v2 — always-up, verbose, phone-friendly

Your three goals map to concrete changes on the existing FastAPI app.

**Goal 1 — always up.** It already runs under systemd (`playlist-manager.service`,
`Restart=on-failure`). We'll harden it: `Restart=always`, `After=network-online.target`, and
add a `/healthz` endpoint. Access stays direct via Tailscale `100.94.158.95:8337` — per your
homeserver CLAUDE.md, internal services are **not** put behind Caddy, so no reverse-proxy
block. (If you ever want a nicer hostname, AdGuard/Tailscale MagicDNS can map one without
touching Caddy.)

**Goal 2 — verbose, browsable logs for CD imports, file uploads, and iPod plug/unplug.**
The current "Activity Feed" is an **in-memory ring buffer** — it's wiped on restart and only
knows about playlist edits. We replace it with a **persistent structured event log**:

- An append-only **JSONL** file, e.g. `/var/log/ipod-sync/events.jsonl`, one event per line:
  `{ts, source, kind, message, detail}` where `source ∈ {cd, upload, ipod, sync, beets,
  playlist}` and `kind ∈ {info, success, warning, error}`.
- Everything writes to it: `cd-sync.sh` (via an HTTP POST to a new `/events` endpoint or a
  direct file append over SSH), `music-watcher.sh` (beets start/finish/counts), `ipod-sync.sh`
  (sync stats), and **new udev plug/unplug hooks** (§ below).
- The web app tails and renders it with **filters by source** and full-text search, plus the
  existing per-run `ipod-sync-*.log` viewer. Auto-refresh via the HTMX poll you already have.
- Mobile: the CSS is already dark/responsive; we add a compact log view and make the album
  grid + playlist list comfortable on a phone so you can "scroll playlists, music, albums…
  on my homeserver while on my iPhone."

**iPod plug/unplug events (new).** Today udev only fires on `add`. We add a matching
`remove` rule and log both, so the feed shows "iPod connected 14:02" / "iPod removed 14:19"
independent of whether a sync ran. Two lines in the udev rules + a tiny logger script.

**"File uploads" logging.** Any file landing in `MUSIC_DIR` (Nextcloud, scp, rsync) is
already seen by inotify — we just emit an event per detected album/file so uploads show up
in the feed too, not only beets results.

---

## 7. The `/mnt/data` I/O error (your aside) — diagnosis + prevention

What you saw:

```
$ ls /mnt/data
ls: reading directory '.': Input/output error
$ sudo mount -a      # didn't help
$ cd /mnt; ls        # data still listed, but broken
```

**What this almost certainly is.** `Input/output error` (EIO) on `ls` of a mounted
directory is not a "not mounted" problem — it's the **underlying block device dropping off
the bus or throwing read errors while still mounted**. The mount entry is stale: the kernel
thinks the filesystem is there, but the physical 4TB drive stopped responding (loose/failing
SATA or USB cable, USB auto-suspend on the enclosure, drive spin-down/power issue, or early
drive failure). `mount -a` does nothing because the device is *already* mounted — it never
tries to remount a live mount. That's why only a full unmount+remount (or reboot) clears it.

**How to confirm (run these when it happens or now):**

```bash
dmesg -T | tail -50                      # look for I/O errors, "reset", USB disconnects, ATA errors
journalctl -k | grep -iE 'ata|usb|reset|I/O error|EXT4-fs error'
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT,STATE,MODEL
sudo smartctl -a /dev/sdX                # drive health (install smartmontools); check Reallocated_Sector_Ct, pending sectors
```

If `dmesg` shows USB resets/disconnects → it's cabling/enclosure/power or USB auto-suspend.
If it shows ATA errors or SMART shows reallocated/pending sectors → the drive itself is
degrading and should be backed up and replaced.

**Prevention — three layers:**

1. **Hardware/OS reliability**
   - If it's a USB enclosure: disable USB auto-suspend for it
     (`usbcore.autosuspend=-1` kernel param, or a udev `power/control=on` rule for that device),
     and prefer a powered enclosure / known-good cable. USB auto-suspend is the single most
     common cause of a media drive "vanishing" on a NUC.
   - Consider moving the drive to internal SATA if the NUC allows it — far more stable than USB
     for an always-on media library.

2. **Mount configuration that fails safe** (`/etc/fstab`)
   - Mount by **UUID**, not `/dev/sdX` (device letters reorder across reboots):
     ```
     UUID=<uuid>  /mnt/data  ext4  defaults,nofail,x-systemd.device-timeout=30,x-systemd.mount-timeout=30  0  2
     ```
   - `nofail` → boot doesn't hang if the drive is absent. `x-systemd.device-timeout` → don't
     wait forever. Optionally `x-systemd.automount` so it mounts on first access and can
     recover after a drop without a reboot.
   - If the fs is ext4, keep periodic `fsck` enabled and don't ignore EXT4-fs errors in dmesg.

3. **Guard rails so the sync pipeline never runs on a broken mount** (this is the important
   one for *this* project). A dropped `/mnt/data` while `ipod-sync.sh` runs with `--delete`
   could, worst case, see an empty/erroring source and start deleting the iPod copy, or write
   into the root filesystem under an empty mountpoint. Every script gets a **preflight health
   check** before doing anything destructive:

   ```bash
   require_healthy_mount() {           # add to a shared lib sourced by all scripts
     local mp="$1"
     mountpoint -q "$mp"            || die "$mp is not mounted — aborting."
     # prove the device actually responds (EIO shows up here, not in mountpoint):
     timeout 5 ls "$mp" >/dev/null 2>&1 || die "$mp is mounted but not responding (I/O error) — aborting."
     # sentinel file that must exist on the real volume:
     [[ -f "$mp/.mounted_ok" ]]    || die "$mp missing sentinel — refusing to treat as authoritative."
   }
   ```

   - Drop a `.mounted_ok` sentinel file on the real 4TB volume once. If the drive is absent,
     the bare mountpoint under `/mnt/data` won't have it → scripts abort instead of syncing
     garbage. This single check protects music-watcher, cd-sync (server side), and ipod-sync.
   - **rsync gets `--delete` protection too:** add rsync's own guard
     (`--delete` only after confirming a non-empty, sentinel-bearing source), and consider
     `rsync --delete-after` + a max-delete cap (`--max-delete=100`) so a source glitch can't
     nuke the whole iPod in one run.

4. **(Optional) a watchdog** — a tiny systemd timer that runs `require_healthy_mount` every
   few minutes and, on failure, tries a `umount -l && mount` cycle and logs an event to the
   feed so you *see* "⚠ /mnt/data went unhealthy 03:14" on the webpage instead of discovering
   it by accident.

---

## 8. Bugs to fix while we're in here

Found while reading the repo — all small, all rolled into the build phase:

| File | Bug | Fix |
|------|-----|-----|
| `server/scripts/udev-trigger.sh` | Launches sync as `--uid=ben --gid=ben`, but the server user is **`jimin`** — the auto-trigger would fail. | Change to `jimin` (or read `SYNC_USER` from config). |
| `server/watcher/music-watcher.sh` | Debounce never fires: reader loop and timer loop are separate subshells with separate variable copies (the "beets doesn't flush" issue in your workflow doc). | Single-process debounce (`read -t`, coprocess, or FIFO). |
| `mac/torrent-finish.sh` | Line `--exclude='*.jpg' \   # comment` — a comment after a `\` line-continuation breaks the whole rsync command. | Move comment to its own line. |
| repo root | `config.env.example` is referenced by README, setup.md, and every service, but **doesn't exist** in the repo (`.gitignore` even has a `!config.env.example` un-ignore for it). | Add it with every var the scripts read (`MUSIC_DIR`, `PLAYLIST_DIR`, `IPOD_MOUNT_PATH`, `LOG_DIR`, `NEXTCLOUD_*`, `LASTFM_*`, plus new `MUSIC_APP_DIR`, `SYNC_USER`, `CD_TRANSCODE_TO_FLAC`, `NUC_HOST`). |
| `server/udev/99-ipod-rockbox.rules` | `add` only; no `remove`. Comment says product `1261` but rule uses `1209`. | Add `remove` rule for plug/unplug logging; reconcile the product-ID comment. |

---

## 9. How this fits the homeserver (Docker) repo

- The playlist manager, watcher, udev, and mount watchdog are **host systemd + udev units**,
  not containers — they need raw device/USB and `/mnt/data` access, so they stay outside
  Docker, exactly as they are now. No change to your compose stack.
- **No Caddy block** — per your homeserver `CLAUDE.md`, internal services are reached
  directly on the Tailscale IP (`100.94.158.95:8337`). Port `8337` doesn't clash with the
  service port table (Jellyfin 8096, Nextcloud 8080, etc.).
- Nextcloud rescan hook already `docker exec nextcloud php occ files:scan` — unchanged.
- Everything the webpage needs (`/mnt/data/media/music`, `/var/log/ipod-sync`) is already on
  the shared media volume / host, so it survives across your Mac↔server commits.

---

## 10. Build plan (once you approve this)

Phased so each step is independently testable:

1. **Foundations & fixes** — add `config.env.example`; extract a shared
   `lib/common.sh` (logging + `require_healthy_mount`); fix the three bugs in §8.
2. **Mount hardening** — fstab UUID/`nofail`/timeout guidance, `.mounted_ok` sentinel,
   preflight guards wired into every script, optional watchdog timer.
3. **CD ingest** — `mac/cd-sync.sh` + `com.ben.cd-sync.plist` launchd agent, SSH key setup,
   confirm Apple Music import format & watch path, optional ALAC→FLAC transcode toggle.
4. **Structured events** — `events.jsonl`, `/events` POST + `/healthz` endpoints, emit events
   from cd-sync, watcher, ipod-sync.
5. **udev plug/unplug** — `remove` rule + logger.
6. **Web UI v2** — persistent filterable/searchable event log, mobile-friendly library &
   playlist views, `Restart=always`.
7. **Docs** — update README, setup.md, workflow.md for the CD path and new logging.

Each phase is a commit you can pull to the NUC and test before the next.

---

## 11. Open questions to settle at build time

- **Library folder name:** keep `FLAC`, or rename to `Music` (§3)? Recommend keeping `FLAC`
  unless you want the tidy rename.
- **Apple Music import codec:** ALAC or AAC? (Determines whether the transcode toggle is ever
  worth flipping — we'll read your Music import settings.)
- **Event delivery from Mac:** HTTP POST to the web app over Tailscale, vs. rsync the event
  line into `events.jsonl` alongside the music push. POST is cleaner; needs the web app
  reachable from the Mac (it is, via Tailscale).
- **SSH:** confirm Mac → `jimin@100.94.158.95` key auth is set up (or set it up in phase 3).
