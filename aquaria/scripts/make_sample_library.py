#!/usr/bin/env python3
"""
Generate a fake FLAC tree that exercises every parsing edge case, so the engine
can be developed/tested without touching the real /mnt/data. Files are empty
placeholders — only their names/paths matter to the parser.

Usage:  python scripts/make_sample_library.py [target_dir]
Default target: aquaria/sample_library
"""
import sys
from pathlib import Path

TRACKS = [
    # standard: genre / artist folder / album (year) / NN - Title
    "0_POP/Charli XCX/BRAT (2025)/01 - 360.flac",
    "0_POP/Charli XCX/BRAT (2025)/02 - Club classics.flac",
    "0_POP/Charli XCX/how i'm feeling now (2020)/01 - pink diamond.flac",
    # artist-less album folder under a genre (small artist, " - " split)
    "0_POP/Passion Pit - Gossamer (2013)/01. Take a Walk.flac",
    "0_POP/Passion Pit - Gossamer (2013)/02. I'll Be Alright.flac",
    # THE BUG CASE: genre=FOLK, artist=Pete Seeger (not artist=0_FOLK/album=Pete Seeger)
    "0_FOLK/Pete Seeger/We Shall Overcome (1963)/01. If I Had a Hammer.flac",
    "0_FOLK/Pete Seeger/We Shall Overcome (1963)/02. Guantanamera.flac",
    # dotted track numbers + lowercase quirky titles
    "0_ELECTRONIC/yeule/softscars (2023)/01. x w x.flac",
    "0_ELECTRONIC/yeule/softscars (2023)/02. sulky baby.flac",
    # artist-less obscure jazz with " - " numbering
    "0_JAZZ/The Nowhere Trio - Live at Nowhere (1978)/01 - Opening.flac",
    # disc-track prefix
    "0_ROCK/Radiohead/OK Computer (1997)/1-01 Airbag.flac",
    "0_ROCK/Radiohead/OK Computer (1997)/1-02 Paranoid Android.flac",
    # flat, no genre bucket (roommate-style simple library) — robustness
    "Aphex Twin/Selected Ambient Works 85-92 (1992)/01. Xtal.flac",
    "Boards of Canada - Music Has the Right to Children (1998)/01. Wildlife Analysis.flac",
]

# artist avatar image (case-insensitive artist.*)
IMAGES = [
    "0_POP/Charli XCX/artist.jpg",
    "0_ELECTRONIC/yeule/Artist.PNG",
]


def main():
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent / "sample_library"
    for rel in TRACKS + IMAGES:
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    # add a cover in one album to test /art
    (target / "0_POP/Charli XCX/BRAT (2025)/cover.jpg").touch()
    print(f"Created {len(TRACKS)} tracks + {len(IMAGES)} artist images under {target}")


if __name__ == "__main__":
    main()
