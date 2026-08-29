"""Unit tests for the normalized tag read path (:mod:`tagmend.engine.tags`)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mutagen
import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TSO2  # type: ignore[attr-defined]
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis

from conftest import make_track

# Import tags so its module-load RegisterFreeformKey runs before make_track writes any
# ``originaldate`` via raw mutagen easy mode (the M4A freeform atom must be registered).
from tagmend.engine.tags import (
    MANAGED_TAGS,
    TAG_READER_VERSION,
    read_tags,
    write_managed_tags,
)

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


def test_originaldate_is_managed() -> None:
    assert "originaldate" in MANAGED_TAGS


@pytest.mark.parametrize("suffix", _ALL_FORMATS)
def test_originaldate_round_trips_and_leaves_date_untouched(
    tmp_path: Path,
    suffix: str,
) -> None:
    # ``date`` (the reissue year) and ``originaldate`` (the original year) are BOTH managed
    # and independent: a write carrying both preserves both, distinct storage each format
    # (the M4A ``©day`` vs ORIGINALDATE freeform split is the critical guard).
    track = make_track(tmp_path / f"track{suffix}", {"date": ["2015"]})
    write_managed_tags(
        track,
        {"originaldate": ["1970"], "genre": ["Heavy Metal"], "date": ["2015"]},
    )

    tags = read_tags(track).tags
    assert tags["originaldate"] == ["1970"]
    assert tags["date"] == ["2015"]  # the reissue year is preserved


def test_originaldate_writes_to_freeform_atom_on_m4a(tmp_path: Path) -> None:
    track = make_track(tmp_path / "track.m4a", {"date": ["2015"]})
    write_managed_tags(track, {"originaldate": ["1970"], "date": ["2015"]})

    raw = MP4(track)  # type: ignore[no-untyped-call]
    # originaldate lands in the iTunes freeform atom Picard uses (lowercase — the atom name is
    # matched case-sensitively), never in the ©day (date) atom.
    assert "----:com.apple.iTunes:originaldate" in raw
    assert raw["©day"] == ["2015"]


# The full wrong-release "stamp" the mismatch-fix flow repairs: the five original fields plus
# the 13 widened ones (the six MB ids, identity title/album/date, track/disc numbers, and the
# two sort names). ``date``/``originaldate`` need valid year values on MP3 (EasyID3 silently
# drops an unparseable TDRC), so the round-trip uses realistic values per field.
_EXPECTED_MANAGED = frozenset(
    {
        "genre",
        "albumartist",
        "artist",
        "musicbrainz_artistid",
        "originaldate",
        "title",
        "album",
        "date",
        "tracknumber",
        "discnumber",
        "artistsort",
        "albumartistsort",
        "musicbrainz_albumtype",
        "musicbrainz_albumartistid",
        "musicbrainz_albumid",
        "musicbrainz_releasegroupid",
        "musicbrainz_releasetrackid",
        "musicbrainz_trackid",
    },
)
_NEW_FIELD_VALUES: dict[str, list[str]] = {
    "title": ["A Song"],
    "album": ["An Album"],
    "date": ["2015"],
    "tracknumber": ["3/12"],
    "discnumber": ["1/2"],
    "artistsort": ["Osbourne, Ozzy"],
    "albumartistsort": ["Osbourne, Ozzy"],
    "musicbrainz_albumtype": ["album"],
    "musicbrainz_albumartistid": ["mb-aa-id"],
    "musicbrainz_albumid": ["mb-al-id"],
    "musicbrainz_releasegroupid": ["mb-rg-id"],
    "musicbrainz_releasetrackid": ["mb-rt-id"],
    "musicbrainz_trackid": ["mb-tr-id"],
}


def test_managed_tags_is_exactly_the_widened_set() -> None:
    assert MANAGED_TAGS == _EXPECTED_MANAGED
    # Each newly-managed field is a member (the fix flow + revert can touch all of them).
    for field in _NEW_FIELD_VALUES:
        assert field in MANAGED_TAGS


@pytest.mark.parametrize("suffix", _ALL_FORMATS)
def test_new_managed_fields_round_trip(tmp_path: Path, suffix: str) -> None:
    # Every widened field must be provably writable + readable on all four formats — the
    # EasyID3/EasyMP4 write path raises on an unregistered key, so this is not assumable.
    track = make_track(tmp_path / f"track{suffix}")
    write_managed_tags(track, dict(_NEW_FIELD_VALUES))

    tags = read_tags(track).tags
    for field, expected in _NEW_FIELD_VALUES.items():
        # The "n/total" slash form for tracknumber/discnumber round-trips literally on all
        # four containers (Vorbis stores the string; EasyID3/EasyMP4 reconstruct it).
        assert tags.get(field) == expected, field


def test_release_ids_use_picard_freeform_atoms_on_m4a(tmp_path: Path) -> None:
    # The two MB ids EasyMP4 has no native mapping for must land on the exact iTunes
    # freeform atom names Picard writes, so a Picard-tagged file round-trips through us.
    track = make_track(tmp_path / "ids.m4a")
    write_managed_tags(
        track,
        {"musicbrainz_releasegroupid": ["rg-1"], "musicbrainz_releasetrackid": ["rt-1"]},
    )

    raw = MP4(track)  # type: ignore[no-untyped-call]
    assert "----:com.apple.iTunes:MusicBrainz Release Group Id" in raw
    assert "----:com.apple.iTunes:MusicBrainz Release Track Id" in raw


def test_vorbis_separate_tracknumber_and_total_reads_number_only(tmp_path: Path) -> None:
    # Accepted v1 behavior (documented): a Vorbis file tagged with separate TRACKNUMBER +
    # TRACKTOTAL reads the managed `tracknumber` back as the bare number; the total lives in
    # the unmanaged `tracktotal` (never managed — EasyID3 has no such key, writing it would
    # crash MP3 commits), and a managed slash-form write leaves that total untouched.
    track = make_track(tmp_path / "sep.flac")
    audio = FLAC(track)
    audio["tracknumber"] = ["3"]
    audio["tracktotal"] = ["12"]
    audio.save()

    assert read_tags(track).tags["tracknumber"] == ["3"]

    write_managed_tags(track, {"tracknumber": ["5/12"]})
    tags = read_tags(track).tags
    assert tags["tracknumber"] == ["5/12"]  # slash form stored literally
    assert tags["tracktotal"] == ["12"]  # unmanaged total preserved


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


# --- format-native spelling: TagMend's canonical namespace vs what Picard actually writes ---
# `read_tags` promises one canonical namespace so the rest of the engine never sees a
# format-specific spelling. These reproduce the two places that promise is broken against a
# genuinely Picard-tagged file (not a file TagMend itself wrote).


def test_reads_originaldate_from_picard_lowercase_atom_on_m4a(tmp_path: Path) -> None:
    # Picard writes ----:com.apple.iTunes:originaldate in LOWERCASE; MP4 freeform atom names
    # are matched case-sensitively, so a uppercase-only registration cannot see it.
    track = make_track(tmp_path / "picard.m4a", {"date": ["2011"]})
    raw = MP4(track)  # type: ignore[no-untyped-call]
    raw["----:com.apple.iTunes:originaldate"] = [b"1979"]
    raw.save()  # type: ignore[no-untyped-call]

    assert read_tags(track).tags.get("originaldate") == ["1979"]


def test_write_leaves_one_originaldate_atom_on_m4a(tmp_path: Path) -> None:
    # Writing over a Picard-tagged file must not leave two contradictory original dates.
    track = make_track(tmp_path / "picard.m4a", {"date": ["2011"]})
    raw = MP4(track)  # type: ignore[no-untyped-call]
    raw["----:com.apple.iTunes:originaldate"] = [b"2011-11-11"]
    raw.save()  # type: ignore[no-untyped-call]

    write_managed_tags(track, {"originaldate": ["1979"], "date": ["2011"]})

    after = MP4(track)  # type: ignore[no-untyped-call]
    atoms = [k for k in after if k.lower() == "----:com.apple.itunes:originaldate"]
    assert len(atoms) == 1, atoms
    assert read_tags(track).tags["originaldate"] == ["1979"]


def test_reads_releasetype_as_albumtype_on_flac(tmp_path: Path) -> None:
    # Vorbis comments pass through raw (mutagen has no easy layer for FLAC), so Picard's
    # Vorbis spelling RELEASETYPE never reaches the canonical managed key.
    track = make_track(tmp_path / "picard.flac")
    audio = FLAC(track)
    audio["releasetype"] = ["album"]
    audio.save()

    assert read_tags(track).tags.get("musicbrainz_albumtype") == ["album"]


def test_write_leaves_one_albumtype_spelling_on_flac(tmp_path: Path) -> None:
    # musicbrainz_albumtype is MANAGED, so this is a live corruption path: writing it onto a
    # Picard-tagged FLAC must not leave RELEASETYPE and MUSICBRAINZ_ALBUMTYPE contradicting.
    track = make_track(tmp_path / "picard.flac")
    audio = FLAC(track)
    audio["releasetype"] = ["compilation"]
    audio.save()

    write_managed_tags(track, {"musicbrainz_albumtype": ["album"]})

    raw = FLAC(track)
    present = [k for k in ("releasetype", "musicbrainz_albumtype") if raw.get(k)]
    assert len(present) == 1, {k: raw.get(k) for k in present}
    assert read_tags(track).tags["musicbrainz_albumtype"] == ["album"]


def test_reads_albumartistsort_from_picard_tso2_frame_on_mp3(tmp_path: Path) -> None:
    # Picard writes the iTunes-compatible TSO2 frame; EasyID3's default map points at
    # TXXX:ALBUMARTISTSORT, which Picard never writes.
    track = make_track(tmp_path / "picard.mp3", {"artist": ["311"]})
    raw = ID3(track)  # type: ignore[no-untyped-call]
    raw.add(TSO2(encoding=3, text=["311"]))  # type: ignore[no-untyped-call]
    raw.save()

    assert read_tags(track).tags.get("albumartistsort") == ["311"]


def test_write_albumartistsort_targets_tso2_and_adds_no_txxx(tmp_path: Path) -> None:
    track = make_track(tmp_path / "picard.mp3", {"artist": ["311"]})
    raw = ID3(track)  # type: ignore[no-untyped-call]
    raw.add(TSO2(encoding=3, text=["311"]))  # type: ignore[no-untyped-call]
    raw.save()

    write_managed_tags(track, {"albumartistsort": ["Three Eleven"]})

    after = ID3(track)  # type: ignore[no-untyped-call]
    assert after["TSO2"].text == ["Three Eleven"]
    assert not [k for k in after if k.upper().endswith("ALBUMARTISTSORT")]


def test_reads_releasetype_as_albumtype_on_ogg(tmp_path: Path) -> None:
    # OggVorbis is the second raw-Vorbis container and must not be forgotten.
    track = make_track(tmp_path / "picard.ogg")
    audio = OggVorbis(track)  # type: ignore[no-untyped-call]
    audio["releasetype"] = ["album"]
    audio.save()

    assert read_tags(track).tags.get("musicbrainz_albumtype") == ["album"]


def test_write_leaves_one_albumtype_spelling_on_ogg(tmp_path: Path) -> None:
    track = make_track(tmp_path / "picard.ogg")
    audio = OggVorbis(track)  # type: ignore[no-untyped-call]
    audio["musicbrainz_albumtype"] = ["single"]
    audio.save()

    write_managed_tags(track, {"musicbrainz_albumtype": ["album"]})

    raw = OggVorbis(track)  # type: ignore[no-untyped-call]
    present = [k for k in ("releasetype", "musicbrainz_albumtype") if raw.get(k)]  # type: ignore[no-untyped-call]
    assert present == ["releasetype"]
    assert raw["releasetype"] == ["album"]


def test_vorbis_native_spelling_wins_when_both_present(tmp_path: Path) -> None:
    # Whichever value the rest of the world reads is the one we must report, regardless of
    # the order mutagen happens to yield the two comment fields in.
    track = make_track(tmp_path / "both.flac")
    audio = FLAC(track)
    audio["musicbrainz_albumtype"] = ["single"]
    audio["releasetype"] = ["album"]
    audio.save()

    assert read_tags(track).tags["musicbrainz_albumtype"] == ["album"]


def test_reader_version_bumped_with_the_reader_change() -> None:
    # The three registration/mapping fixes change what read_tags produces for ~90% of the
    # library, so already-scanned rows must be re-read exactly once.
    assert TAG_READER_VERSION == 2


def test_vorbis_write_uses_uppercase_field_names(tmp_path: Path) -> None:
    # Picard uppercases Vorbis field names, so writing lowercase left a Picard-tagged file
    # carrying a mix of cases for no reason. Names are case-insensitive per the spec, so this
    # is about not churning bytes in a library another tagger also manages.
    track = make_track(tmp_path / "case.flac")
    audio = FLAC(track)
    audio["RELEASETYPE"] = ["album"]
    audio["ARTIST"] = ["A"]
    audio.save()

    write_managed_tags(track, {"musicbrainz_albumtype": ["album"], "artist": ["A"]})

    # Iterating the tag object yields the RAW stored names; .items() case-folds them.
    stored = {name for name, _ in FLAC(track).tags}  # type: ignore[union-attr]
    assert "RELEASETYPE" in stored
    assert "ARTIST" in stored
    assert "releasetype" not in stored
    assert "artist" not in stored
