"""Parser unit tests — cover every layout edge case the spec calls out."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.parser import (
    parse_rel_path, parse_genre, split_year, split_artist_album,
    parse_track_filename, normalize_legacy_album, album_display_all_albums,
)


def test_genre_prefix():
    assert parse_genre("0_POP") == ("POP", 0)
    assert parse_genre("12_Electronic") == ("Electronic", 12)
    assert parse_genre("Charli XCX") == (None, None)


def test_split_year():
    assert split_year("BRAT (2025)") == ("BRAT", 2025)
    assert split_year("softscars (2023)") == ("softscars", 2023)
    assert split_year("Untitled") == ("Untitled", None)


def test_split_artist_album():
    assert split_artist_album("Passion Pit - Gossamer (2013)") == ("Passion Pit", "Gossamer (2013)")
    assert split_artist_album("BRAT (2025)") == (None, "BRAT (2025)")


def test_track_filename():
    assert parse_track_filename("01 - 360.flac") == (None, 1, "360")
    assert parse_track_filename("01. x w x.flac") == (None, 1, "x w x")
    assert parse_track_filename("02. sulky baby.flac") == (None, 2, "sulky baby")
    assert parse_track_filename("1-02 Paranoid Android.flac") == (1, 2, "Paranoid Android")
    assert parse_track_filename("Xtal.flac") == (None, None, "Xtal")


def test_standard_layout():
    t = parse_rel_path("0_POP/Charli XCX/BRAT (2025)/01 - 360.flac")
    assert t.genre == "POP" and t.genre_order == 0
    assert t.artist == "Charli XCX" and not t.artist_inferred
    assert t.album == "BRAT" and t.year == 2025
    assert t.track_no == 1 and t.title == "360"


def test_artist_less_album():
    t = parse_rel_path("0_POP/Passion Pit - Gossamer (2013)/01. Take a Walk.flac")
    assert t.genre == "POP"
    assert t.artist == "Passion Pit" and t.artist_inferred
    assert t.album == "Gossamer" and t.year == 2013
    assert t.title == "Take a Walk"


def test_the_pete_seeger_bug():
    """Regression: 0_FOLK must NOT become the artist, Pete Seeger must NOT be the album."""
    t = parse_rel_path("0_FOLK/Pete Seeger/We Shall Overcome (1963)/01. If I Had a Hammer.flac")
    assert t.genre == "FOLK"
    assert t.artist == "Pete Seeger"
    assert t.album == "We Shall Overcome" and t.year == 1963
    assert t.title == "If I Had a Hammer"


def test_flat_no_genre():
    t = parse_rel_path("Aphex Twin/Selected Ambient Works 85-92 (1992)/01. Xtal.flac")
    assert t.genre is None
    assert t.artist == "Aphex Twin"
    assert t.album == "Selected Ambient Works 85-92" and t.year == 1992


def test_flat_artist_less():
    t = parse_rel_path("Boards of Canada - Music Has the Right to Children (1998)/01. Wildlife Analysis.flac")
    assert t.artist == "Boards of Canada" and t.artist_inferred
    assert t.album == "Music Has the Right to Children" and t.year == 1998


def test_all_albums_label():
    t = parse_rel_path("0_POP/Charli XCX/BRAT (2025)/01 - 360.flac")
    assert album_display_all_albums(t) == "Charli XCX - BRAT (2025)"


def test_legacy_playlist_normalise():
    artist, album, year = normalize_legacy_album("(2023) yeule - softscars [FLAC]")
    assert artist == "yeule" and album == "softscars" and year == 2023


def test_non_audio_ignored():
    assert parse_rel_path("0_POP/Charli XCX/BRAT (2025)/cover.jpg") is None
