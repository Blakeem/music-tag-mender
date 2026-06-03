"""Unit tests for the normalized tag read path (:mod:`tagmend.engine.tags`)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mutagen
import pytest
from mutagen.flac import FLAC

from conftest import make_track
from tagmend.engine.tags import read_tags

if TYPE_CHECKING:
    from pathlib import Path

_ALL_FORMATS = [".mp3", ".flac", ".m4a", ".ogg"]
# Only these templates round-trip to an empty tag set; .flac/.ogg carry an `encoder`
# Vorbis comment baked into the template, so they cannot assert an exactly-empty map.
_TAGFREE_FORMATS = [".mp3", ".m4a"]


@pytest.mark.parametrize("suffix", _ALL_FORMATS)
def test_round_trips_canonical_tags(tmp_path: Path, suffix: str) -> None:
    track = make_track(
        tmp_path / f"track{suffix}",
        {
            "artist": ["A"],
            "album": ["Alb"],
            "albumartist": ["AA"],
            "genre": ["Synthwave", "Darksynth"],
            "musicbrainz_artistid": ["mbid-1"],
        },
    )

    tags = read_tags(track).tags

    assert tags["artist"] == ["A"]
    assert tags["album"] == ["Alb"]
    assert tags["albumartist"] == ["AA"]
    assert tags["musicbrainz_artistid"] == ["mbid-1"]
    # genre stays a 2-element list in the written order.
    assert tags["genre"] == ["Synthwave", "Darksynth"]


@pytest.mark.parametrize("suffix", _TAGFREE_FORMATS)
def test_untagged_file_reads_empty(tmp_path: Path, suffix: str) -> None:
    track = make_track(tmp_path / f"empty{suffix}")
    assert read_tags(track).tags == {}


def test_alias_band_maps_to_albumartist(tmp_path: Path) -> None:
    track = make_track(tmp_path / "band.flac")
    audio = FLAC(track)
    audio["band"] = ["The Band"]  # raw vorbis comment, not the canonical key
    audio.save()

    tags = read_tags(track).tags
    assert tags["albumartist"] == ["The Band"]


def test_alias_album_artist_maps_to_albumartist(tmp_path: Path) -> None:
    track = make_track(tmp_path / "aa.flac")
    audio = FLAC(track)
    audio["album artist"] = ["The AA"]  # raw vorbis comment with a space
    audio.save()

    tags = read_tags(track).tags
    assert tags["albumartist"] == ["The AA"]


def test_corrupt_mp3_raises_mutagen_error(tmp_path: Path) -> None:
    # EMPIRICAL: feeding garbage bytes through a .mp3 path makes mutagen raise
    # MutagenError ("can't sync to MPEG frame") rather than returning an empty set.
    bad = tmp_path / "broken.mp3"
    bad.write_bytes(b"not an audio file")
    with pytest.raises(mutagen.MutagenError):  # type: ignore[attr-defined]
        read_tags(bad)


def test_unidentifiable_file_reads_empty(tmp_path: Path) -> None:
    # A file mutagen cannot identify (unknown signature, non-audio suffix) makes
    # mutagen.File return None, which read_tags normalizes to an empty tag set.
    unknown = tmp_path / "mystery.dat"
    unknown.write_bytes(b"\x00\x01\x02\x03")
    assert read_tags(unknown).tags == {}
