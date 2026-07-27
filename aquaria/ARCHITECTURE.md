# Aquaria Music Manager — Architecture

This is the v2 iteration of `ipod-sync`: a Docker-run web service for managing a
shared FLAC library and syncing per-user iPods, served over your LAN/Tailscale.
This document covers the core engine built in this pass and the decisions behind
it, then lays out the roadmap for the remaining features.

## The central idea: meaning never comes from folder position

The bug in the old app — `0_FOLK` showing up as an artist, "Pete Seeger" showing
up as an album, `0_FOLK` bleeding into the green subtext — comes from a single
assumption in `scan_library()`: *the first folder is the artist*. Once you added
the `0_GENRE/` root that assumption broke everywhere.

Aquaria removes that assumption. Every audio file's path is parsed into a
canonical record and **stored in a SQLite index**:

```
ParsedTrack(genre, genre_order, artist, artist_inferred, album, year,
            disc_no, track_no, title, ext, rel_path)
```

Every screen (all four Library views, playlists, iPod projections, search) is
built from those records, never from where a file happens to sit in the tree.
This directly serves your BIG DECISION: **you keep uploading into `0_GENRE/…` on
the server, and the app presents a clean, uniform schema regardless.** The disk
layout is an input to the parser, not a source of truth for the UI.

### Parsing rules (`app/parser.py`)

- Genre bucket: a top-level folder matching `^\d+_(.+)` → genre label with the
  numeric prefix kept only for sort order (`0_POP` → `POP`, order `0`).
- Standard album: `…/Artist/Album (YEAR)/NN - Title.ext`. Year is taken only from
  a trailing `(YYYY)`.
- Artist-less album (small/obscure artists with no artist folder): a single
  folder above the file whose name contains `" - "` is split on the **first**
  `" - "` → `Passion Pit - Gossamer (2013)` becomes artist `Passion Pit`, album
  `Gossamer (2013)`, and the track is flagged `artist_inferred`. The spaces
  around the dash are required so hyphenated names (`Jay-Z`) aren't mis-split.
- Track filenames handle every style in your library: `01 - 360`, `01. x w x`,
  `02. sulky baby`, disc-prefixed `1-02 Paranoid Android`, and bare `Xtal`.
- No-genre / flat libraries are also parsed correctly, which is what lets your
  roommate run a simple structure-agnostic library on the same instance.

All of this is covered by 20 passing tests (`tests/`), including an explicit
regression test for the Pete Seeger case and one that resolves a messy legacy
playlist line against the restructured library.

## The four Library views (`app/library.py`)

The Library tab is a projection of one indexed table. `GET /api/library?view=…`:

- **`0_Genre`** — genre buckets at the front, each holding its albums. This is
  folder-parity: what you'd see `cd`-ing the server. Tracks with no genre are
  surfaced under a `(no genre)` bucket so nothing silently disappears.
- **`Artist &/or Album`** — one tile per artist, rendered as a **circular avatar**
  (a head icon, or `artist.*` image if present in the artist folder) rather than
  a square album cover. Artist-less albums appear here under their parsed artist,
  so `Passion Pit` shows up as an artist even though there's no `Passion Pit/`
  folder on disk. Click an artist → their albums (square tiles).
- **`All Albums`** — a flat **list** (not tiles) of every album labelled
  `Artist - Album (Year)`, per your combine rule.

A note on "4 ways": the spec says four but names three. The engine is built so a
fourth (e.g. a flat all-tracks list, or "Albums only" without the artist layer)
is a few lines in `library.py` — tell me which fourth you want and I'll wire it.

Artist avatars are stored where you asked: case-insensitive `artist.*` inside the
artist folder. The scanner caches their path; the frontend circle-crops via CSS.

## Smart search / "currently adding" (`GET /api/search`)

Backed by an FTS5 index over title/album/artist/genre. Returns results grouped as
artists, albums, and tracks, so the floating "editing" bubble can let you add a
single track, a whole album, or everything by an artist. The demo UI wires the
bottom-left bubble → 🔍 → live search exactly as described; adding-to-playlist is
the next step once the playlist write-path lands.

## Playlists — the "unity" requirement (`app/playlists.py`)

Playlists stay as text files of paths, but each line is also **resolved to a
canonical track id** in the index via three tiers: exact `rel_path`, then a
normalized `(album, title[, year])` match, then a fuzzy FTS fallback. This is
what reconciles your messy legacy format —
`(2023) yeule - softscars [FLAC]/01. x w x.flac` — with the restructured library:
the leading year and `[FLAC]` junk are stripped, the album/title are normalized,
and the line resolves to the real track even though that folder no longer exists.

Because playlists resolve to track ids, an iPod's copy of a playlist can be
**re-projected** into whatever path layout that iPod uses (see below).

## Users, iPods, activity (schema in `app/db.py`)

Per your spec, each user has a username (no password), personal settings
(backdrop, default iPod layout), personal playlists, and linked iPods. Users
share the library and, by default, their playlists are global (visible to
others) unless toggled off.

An iPod is linked by its stable hardware **signature** (serial / FireWire GUID),
plus a friendly name and metadata, so the frontend can show the live "connected &
stable" indicator and list a user's iPods as subtext under their name. Crucially,
each iPod carries a `layout` field:

- `genre` → mirror your server (`0_GENRE/Artist/Album (Year)/…`), which is the
  structure you've wanted to replicate on your own iPod.
- `flat` → drop the genre bucket (`Artist/Album (Year)/…`) for a simple,
  structure-agnostic setup — this is your roommate's iPod.

`ipod_path()` already projects any track to the right path for either layout, so
the same library serves both iPods from one source. The **Activity Feed is
per-user iPod history** (connect/disconnect/sync/playlist/scrobble), stored in the
`activity` table.

## Answers to your inline (◊) questions

**Repo location — `code/` vs `docker/`:** put the deployable service in `docker/`
with your other compose stacks (e.g. `docker/aquaria/`) so `docker compose up -d`
stays uniform across services. Keep hacking on it wherever you like in `code/`;
the canonical deploy tree should live beside your other containers. The music
library is bind-mounted **read-only** — Aquaria never mutates your FLAC files.

**Server vs iPod structure:** keep `0_GENRE/Artist/Album (Year)/` on the server.
The app normalizes it into the index and *generates* each iPod's layout on demand.
Same source, different projection — you don't maintain two trees.

**Albums without an artist folder:** not a problem; handled by the `" - "` split
and flagged `artist_inferred` so the Artist view still surfaces them cleanly.

**How abstracted should the iPod view be:** default to a clean logical view
(albums / playlists / tracks + a live sync-status indicator), with an optional
"raw tree" toggle for Finder-parity when you want it. Rockbox extras (Last.fm
scrobble, `.scrobbler.log`) belong in the "My iPod" module as optional setup — on
if you want it, invisible if you don't. The new **My iPod** tab sits between
Activity and Playlists as you specified.

## Frontend recommendation

You left the stack to me. **Recommendation: React + Tailwind + Vite, built as a
static bundle and served by the same FastAPI container.** The spec is genuinely
SPA-shaped — live iPod connection indicators, the hover-reveal "currently adding"
search, circle-cropped artist uploads, per-user animated aquamarine backdrops,
smooth view transitions. HTMX can do a lot, but this much stateful interactivity
is where a component model pays for itself, and a Vite build compiled into the
image keeps deployment to a single container with no runtime Node.

For **this** core-engine pass I shipped a dependency-free single-file demo UI
(`app/static/index.html`) so you can see the four Library views, the user row with
iPod subtext, the tab bar, and the floating search working against the real API
today. It's the throwaway scaffold; the React app replaces it when we build the
full frontend.

## Deployment

```bash
cd docker/aquaria         # (once relocated from code/)
docker compose up -d --build
# open http://<server>:8337
# first run: POST /api/scan to index the library
```

The compose file bind-mounts `/mnt/data/media/music/FLAC` read-only, mounts
`Playlists/` read-write, and keeps the index DB on a named volume. iPod USB
passthrough (libgpod/udev) is commented in the compose file and lands with the
sync feature.

## Roadmap (what this pass intentionally left as scaffold)

1. **iPod integration** — libgpod/udev detection, signature capture, the live
   connection indicator, the "My iPod" module and raw-tree toggle.
2. **Playlist write-path** — create/edit in the UI, `.m3u`/`.txt` round-trip,
   global-vs-private toggle, "add from search while editing".
3. **Sync engine** — project resolved playlists + albums onto an iPod per its
   layout; reuse the existing `server/scripts` rsync/scrobble logic as callable
   helpers.
4. **React frontend** — the full aquamarine app with settings/backdrops and
   artist-image upload.
5. **Incremental scanning** — mtime-based rescans on file-watcher events.
```
