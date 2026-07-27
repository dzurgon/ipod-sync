"""
Aquaria Music Manager — path → canonical metadata parser.

This is the heart of the "unify it under some schema" requirement.

The server library on disk stays exactly as Ben keeps it:

    /mnt/data/media/music/FLAC/0_GENRE/AlbumArtist/Album (YEAR)/NN - Title.flac

...but the *meaning* of a path is NEVER inferred from its position in the tree.
Instead every path is parsed into a canonical record:

    ParsedTrack(genre, artist, album, year, track_no, title, ...)

and downstream everything (Library views, playlists, iPod projections) is built
from those records, not from "first folder = artist" guesses. That guess is the
bug that turns "0_FOLK" into an artist and "Pete Seeger" into an album.

Supported physical layouts under the music root
------------------------------------------------
1. Genre-first, artist folder present (Ben's default):
       0_POP/Charli XCX/BRAT (2025)/01 - 360.flac
2. Genre-first, artist-less album folder (small/obscure artists):
       0_POP/Passion Pit - Gossamer (2013)/01. Little Secrets.flac
   -> split on the FIRST " - ": artist="Passion Pit", album="Gossamer (2013)"
3. No genre layer (robustness / non-genre libraries, e.g. roommate's flat setup):
       Charli XCX/BRAT (2025)/01 - 360.flac
       Passion Pit - Gossamer (2013)/01. Little Secrets.flac

Track filename styles seen in the wild (all handled):
       01 - 360.flac
       01. x w x.flac
       01. sulky baby.flac
       1-01 Title.flac        (disc-track)
       360.flac               (no track number)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional

AUDIO_EXTS = {".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac", ".alac", ".wav", ".aiff"}

# ── Regexes ────────────────────────────────────────────────────────────────────

# Genre bucket: "0_POP", "12_Electronic", "0_HIP-HOP" -> order + genre label.
_GENRE_RE = re.compile(r"^(?P<order>\d+)_(?P<genre>.+)$")

# Trailing "(YYYY)" year on an album folder name. Kept anchored to the end so a
# year that appears mid-title (rare) is not mistaken for the release year.
_YEAR_RE = re.compile(r"\s*\((?P<year>\d{4})\)\s*$")

# Artist-less album folder: "Artist - Album (Year)". Split on the FIRST " - ".
# The spaces around the dash are required (per the spec) so hyphenated names
# like "Jay-Z" or "Godspeed You! Black Emperor - ..." are not split incorrectly.
_ARTIST_ALBUM_RE = re.compile(r"^(?P<artist>.+?) - (?P<album>.+)$")

# Track filename leading number(s): "01 - ", "01. ", "1-01 ", "01 " ...
# Captures optional disc, the track number, and the remaining title.
_TRACK_RE = re.compile(
    r"""^
        (?:(?P<disc>\d+)[-.])?          # optional "1-" disc prefix
        (?P<track>\d{1,3})              # track number
        \s*[-.]?\s+                     # separator: " - ", ". ", " "
        (?P<title>.+)$                  # the rest is the title
    """,
    re.VERBOSE,
)

# Legacy/messy playlist album strings, e.g. "(2023) yeule - softscars [FLAC]".
# Year leads, "[FLAC]"/"[16-44]" junk tags trail. We normalise these so old
# playlists still resolve against the re-structured library.
_LEADING_YEAR_RE = re.compile(r"^\s*\((?P<year>\d{4})\)\s*")
_JUNK_TAG_RE = re.compile(r"\s*\[[^\]]*\]\s*$")


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class ParsedTrack:
    """Canonical identity of a single audio file, independent of disk layout."""
    rel_path: str                 # path relative to the music root (posix)
    genre: Optional[str] = None   # human genre label, "0_" prefix stripped
    genre_order: Optional[int] = None  # the numeric prefix, for sort order
    artist: str = ""              # album artist (folder or parsed)
    album: str = ""               # album title WITHOUT the "(Year)" suffix
    year: Optional[int] = None
    disc_no: Optional[int] = None
    track_no: Optional[int] = None
    title: str = ""
    ext: str = ""
    # True when artist came from splitting "Artist - Album", i.e. there was no
    # dedicated artist folder on disk. Useful for the Library views.
    artist_inferred: bool = False


# ── Helpers ──────────────────────────────────────────────────────────────────

def is_audio(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() in AUDIO_EXTS


def parse_genre(folder: str) -> tuple[Optional[str], Optional[int]]:
    """'0_POP' -> ('POP', 0). A non-genre folder returns (None, None)."""
    m = _GENRE_RE.match(folder)
    if not m:
        return None, None
    return m.group("genre"), int(m.group("order"))


def split_year(name: str) -> tuple[str, Optional[int]]:
    """'BRAT (2025)' -> ('BRAT', 2025). 'Untitled' -> ('Untitled', None)."""
    m = _YEAR_RE.search(name)
    if not m:
        return name.strip(), None
    year = int(m.group("year"))
    return name[: m.start()].strip(), year


def split_artist_album(folder: str) -> tuple[Optional[str], str]:
    """
    Artist-less album folder -> (artist, album-with-year).
    'Passion Pit - Gossamer (2013)' -> ('Passion Pit', 'Gossamer (2013)').
    No ' - ' present -> (None, folder).
    """
    m = _ARTIST_ALBUM_RE.match(folder)
    if not m:
        return None, folder
    return m.group("artist").strip(), m.group("album").strip()


def parse_track_filename(filename: str) -> tuple[Optional[int], Optional[int], str]:
    """
    'Charli XCX/.../01 - 360.flac' filename part ->
        (disc_no, track_no, title).
    Falls back to (None, None, stem) when there is no leading number.
    """
    stem = PurePosixPath(filename).stem
    m = _TRACK_RE.match(stem)
    if not m:
        return None, None, stem.strip()
    disc = int(m.group("disc")) if m.group("disc") else None
    track = int(m.group("track"))
    return disc, track, m.group("title").strip()


def normalize_legacy_album(text: str) -> tuple[Optional[str], str, Optional[int]]:
    """
    Normalise a messy legacy playlist album string.
    '(2023) yeule - softscars [FLAC]' -> (artist='yeule', album='softscars', 2023)
    Returns (artist_or_None, album, year_or_None).
    """
    year = None
    m = _LEADING_YEAR_RE.match(text)
    if m:
        year = int(m.group("year"))
        text = text[m.end():]
    text = _JUNK_TAG_RE.sub("", text).strip()
    # also strip a trailing "(YYYY)" if present instead of a leading one
    text, y2 = split_year(text)
    year = year or y2
    artist, album = split_artist_album(text)
    return artist, (album if artist else text), year


# ── Top-level: parse a full relative path into a ParsedTrack ──────────────────

def parse_rel_path(rel_path: str) -> Optional[ParsedTrack]:
    """
    Parse a music-root-relative file path into a ParsedTrack.
    Returns None if the file is not audio.

    The parser is layout-tolerant. It walks the path parts and classifies:
      - a leading "N_Genre" part  -> genre
      - a part containing " - " with NO deeper album dir under it -> artist-less
      - otherwise: <artist>/<album (year)>/<file>
    """
    rel = PurePosixPath(rel_path.strip("/"))
    if not is_audio(rel.name):
        return None

    parts = list(rel.parts)
    filename = parts[-1]
    dirs = parts[:-1]  # everything above the file

    genre = genre_order = None
    # Peel a leading genre bucket if present.
    if dirs:
        g, order = parse_genre(dirs[0])
        if g is not None:
            genre, genre_order = g, order
            dirs = dirs[1:]

    artist = ""
    album_raw = ""
    artist_inferred = False

    if len(dirs) >= 2:
        # Standard: <artist>/<album (year)>/file
        artist = dirs[0]
        album_raw = dirs[1]
    elif len(dirs) == 1:
        # Single folder above the file: either an artist-less "Artist - Album"
        # OR (rarely) a bare album folder with no artist info at all.
        parsed_artist, album_raw = split_artist_album(dirs[0])
        if parsed_artist is not None:
            artist = parsed_artist
            artist_inferred = True
        else:
            album_raw = dirs[0]
    else:
        # File sits directly under the genre/root — treat filename stem as album.
        album_raw = PurePosixPath(filename).stem

    album, year = split_year(album_raw)
    disc_no, track_no, title = parse_track_filename(filename)

    return ParsedTrack(
        rel_path=str(rel),
        genre=genre,
        genre_order=genre_order,
        artist=artist,
        album=album,
        year=year,
        disc_no=disc_no,
        track_no=track_no,
        title=title,
        ext=rel.suffix.lower(),
        artist_inferred=artist_inferred,
    )


def album_display_all_albums(track: ParsedTrack) -> str:
    """
    'All Albums' list-mode label: 'ArtistFolderName - Album Name (Year)'.
    Per spec, combine artist + album under the ' - ' convention.
    """
    year = f" ({track.year})" if track.year else ""
    artist = track.artist or "Unknown Artist"
    return f"{artist} - {track.album}{year}"
