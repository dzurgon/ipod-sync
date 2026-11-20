"""
Track metadata for scrobbling: the file's own tags, falling back to the index.

The index is built from folder and file names, which can be messy (a rip named
"01.-Them Bones.flac" in "Alice In Chains - Dirt (1992) (Dutch PBTHAL LP 24-96)
[FLAC]"). Tags are what the release actually says, so Last.fm gets "Them Bones"
on "Dirt" and matches the real track.
"""

from __future__ import annotations

from pathlib import Path


def _first(tags, key: str) -> str:
    v = tags.get(key) if tags else None
    return (v[0] if isinstance(v, list) and v else v or "").strip() if v else ""


def scrobble_meta(path: Path, fallback: dict) -> dict:
    """{artist, track, album, trackNumber} for Last.fm. `fallback` is the index row."""
    meta = {"artist": fallback["artist"], "track": fallback["title"],
            "album": fallback["album"], "trackNumber": fallback["track_no"]}
    try:
        import mutagen
        tags = mutagen.File(path, easy=True)
    except Exception:                       # missing, unreadable, or mutagen not installed
        tags = None
    if tags is not None and tags.tags:
        artist = _first(tags, "artist") or _first(tags, "albumartist")
        title = _first(tags, "title")
        if artist and title:                # only trust tags that name the song
            meta.update(artist=artist, track=title, album=_first(tags, "album") or meta["album"])
            num = _first(tags, "tracknumber").split("/")[0]
            meta["trackNumber"] = int(num) if num.isdigit() else meta["trackNumber"]
    return meta
