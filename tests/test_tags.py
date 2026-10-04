"""Unit tests for the normalized tag read path (:mod:`tagmend.engine.tags`)."""

from __future__ import annotations

import contextlib
import io
import os
import re
import stat
import wave
from typing import TYPE_CHECKING

import mutagen
import pytest
from mutagen.apev2 import APEv2
from mutagen.flac import FLAC
from mutagen.id3 import (  # type: ignore[attr-defined]
    ID3,
    IPLS,
    TALB,
    TCON,
    TDAT,
    TDOR,
    TDRC,
    TIME,
    TIPL,
    TIT2,
    TORY,
    TPE1,
    TRCK,
    TRDA,
    TSO2,
    TXXX,
    TYER,
    Frame,
    MakeID3v1,
    ParseID3v1,
)
from mutagen.mp4 import MP4, Atoms  # type: ignore[attr-defined]
from mutagen.oggvorbis import OggVorbis

from conftest import make_droppable_frames_mp3, make_track
from tagmend.engine import tags
from tagmend.engine.scan import TEMP_SUFFIX

# Import tags so its module-load RegisterFreeformKey runs before make_track writes any
# ``originaldate`` via raw mutagen easy mode (the M4A freeform atom must be registered).
from tagmend.engine.tags import (
    MANAGED_SET_VERSION,
    MANAGED_SETS,
    MANAGED_TAGS,
    ORIGINAL_MANAGED_TAGS,
    TAG_READER_VERSION,
    TagWriteError,
    TagWriteResult,
    read_tags,
    write_managed_tags,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mutagen._file import FileType

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


# The five original fields, the 13 widened ones (the six MB ids, identity title/album/date,
# track/disc numbers, and the two sort names), the seven release-stamp fields and the
# ``artists`` list. ``date``/``originaldate`` need valid year values on MP3 (EasyID3 silently
# drops an unparseable TDRC), so the round-trip uses realistic values per field.
_EXPECTED_MANAGED = frozenset(
    {
        "genre",
        "albumartist",
        "artist",
        "artists",
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
        "musicbrainz_albumstatus",
        "media",
        "releasecountry",
        "barcode",
        "catalognumber",
        "isrc",
        "asin",
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


def test_managed_tags_is_exactly_the_declared_set() -> None:
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


@pytest.mark.parametrize("raw_key", ["band", "album artist"])
def test_albumartist_lookalikes_pass_through_unmapped(tmp_path: Path, raw_key: str) -> None:
    # Library FLACs hold a different value here than in ALBUMARTIST, so the pair is never merged.
    track = make_track(tmp_path / "aa.flac")
    audio = FLAC(track)
    audio[raw_key] = ["Other"]
    audio["albumartist"] = ["Real"]
    audio.save()

    tags = read_tags(track).tags
    assert tags["albumartist"] == ["Real"]
    assert tags[raw_key] == ["Other"]


class _StubTags:
    """A tag container whose ``items()`` order the test controls (Vorbis order is hash order)."""

    def __init__(self, pairs: list[tuple[str, list[str]]]) -> None:
        self._pairs = pairs

    def items(self) -> list[tuple[str, list[str]]]:
        return list(self._pairs)


class _StubAudio:
    def __init__(self, pairs: list[tuple[str, list[str]]]) -> None:
        self.tags = _StubTags(pairs)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    ("pairs", "want"),
    [
        # A look-alike never competes with the canonical key, whichever comes first.
        ([("ALBUMARTIST", ["Real"]), ("BAND", [""])], ("albumartist", ["Real"])),
        (
            [("ALBUMARTIST", ["Tattooed Corpse"]), ("ALBUM ARTIST", ["Various"])],
            ("albumartist", ["Tattooed Corpse"]),
        ),
        # The native Vorbis spelling beats the canonical name.
        (
            [("RELEASETYPE", ["album"]), ("MUSICBRAINZ_ALBUMTYPE", ["single"])],
            ("musicbrainz_albumtype", ["album"]),
        ),
    ],
)
def test_read_ignores_iteration_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pairs: list[tuple[str, list[str]]],
    want: tuple[str, list[str]],
    *,
    reverse: bool,
) -> None:
    ordered = list(reversed(pairs)) if reverse else pairs
    monkeypatch.setattr(mutagen, "File", lambda *_a, **_k: _StubAudio(ordered))

    key, expected = want
    assert read_tags(tmp_path / "any.flac").tags[key] == expected


def test_write_keeps_albumartist_beside_a_blank_band(tmp_path: Path) -> None:
    # BAND='' beside a real ALBUMARTIST is a live-library shape. A write that changes only ARTIST
    # must leave both raw fields exactly as they were.
    track = make_track(tmp_path / "band.flac")
    audio = FLAC(track)
    audio["BAND"] = [""]
    audio["ALBUMARTIST"] = ["Smashing Pumpkins"]
    audio.save()

    target = read_tags(track).tags
    target = {k: v for k, v in target.items() if k in MANAGED_TAGS}
    target["artist"] = ["The Smashing Pumpkins"]
    write_managed_tags(track, target)

    raw = FLAC(track)
    assert raw["ALBUMARTIST"] == ["Smashing Pumpkins"]
    assert raw["BAND"] == [""]
    assert raw["ARTIST"] == ["The Smashing Pumpkins"]


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


# --- managed-set version 3: the release/recording provenance stamp -------------------
# After an identity fix rewrites artist/album/title, the WRONG release's provenance block
# stays behind: a file reading "Alice in Chains - Greatest Hits" while its albumstatus says
# "bootleg" and its country says "RU" is still wrong. These eight fields make that block
# fixable and revertible instead of invisible.

_RELEASE_STAMP = {
    "musicbrainz_albumstatus": ["official"],
    "media": ["CD"],
    "releasecountry": ["US"],
    "barcode": ["0123456789012"],
    "catalognumber": ["CAT-001"],
    "isrc": ["USRC17607839"],
    "asin": ["B000001"],
}


@pytest.mark.parametrize("field", sorted(_RELEASE_STAMP))
def test_release_stamp_fields_are_managed(field: str) -> None:
    assert field in MANAGED_TAGS


@pytest.mark.parametrize("suffix", _ALL_FORMATS)
def test_release_stamp_round_trips(tmp_path: Path, suffix: str) -> None:
    # The closed-set rule: every managed key must be provably writable on all four containers.
    track = make_track(tmp_path / f"stamp{suffix}")
    write_managed_tags(track, dict(_RELEASE_STAMP))

    tags = read_tags(track).tags
    for field, expected in _RELEASE_STAMP.items():
        assert tags.get(field) == expected, f"{field} on {suffix}"


def test_label_and_organization_stay_distinct_on_flac(tmp_path: Path) -> None:
    # 245 FLACs in the real library carry a DIFFERENT value in each, so collapsing them
    # would pick one and let a later write delete the other. Both stay their own tag.
    track = make_track(tmp_path / "picard.flac")
    audio = FLAC(track)
    audio["ORGANIZATION"] = ["Gashed!"]
    audio["LABEL"] = ["Metropolis Records"]
    audio.save()

    tags = read_tags(track).tags
    assert tags.get("organization") == ["Gashed!"]
    assert tags.get("label") == ["Metropolis Records"]
    assert "organization" not in MANAGED_TAGS
    assert "label" not in MANAGED_TAGS


def test_reads_releasestatus_as_albumstatus_on_flac(tmp_path: Path) -> None:
    track = make_track(tmp_path / "picard.flac")
    audio = FLAC(track)
    audio["RELEASESTATUS"] = ["bootleg"]
    audio.save()

    assert read_tags(track).tags.get("musicbrainz_albumstatus") == ["bootleg"]


def test_reads_releasecountry_from_picard_atom_on_m4a(tmp_path: Path) -> None:
    # mutagen maps releasecountry to the atom "MusicBrainz Release Country"; Picard writes
    # "MusicBrainz Album Release Country". One word apart, and invisible without the override.
    track = make_track(tmp_path / "picard.m4a")
    raw = MP4(track)  # type: ignore[no-untyped-call]
    raw["----:com.apple.iTunes:MusicBrainz Album Release Country"] = [b"GB"]
    raw.save()  # type: ignore[no-untyped-call]

    assert read_tags(track).tags.get("releasecountry") == ["GB"]


_TWO_ARTISTS = ["Bryan EL", "Guest"]


def _write_picard_artists(track: Path, values: list[str]) -> None:
    """Write the ``artists`` list under the raw name Picard uses on *track*'s container."""
    if track.suffix == ".mp3":
        frames = ID3(track)  # type: ignore[no-untyped-call]
        frames.add(TXXX(encoding=3, desc="ARTISTS", text=values))  # type: ignore[no-untyped-call]
        frames.save()
    elif track.suffix == ".m4a":
        atoms = MP4(track)  # type: ignore[no-untyped-call]
        atoms["----:com.apple.iTunes:ARTISTS"] = [value.encode() for value in values]
        atoms.save()  # type: ignore[no-untyped-call]
    else:
        audio = mutagen.File(track)  # type: ignore[attr-defined]
        audio["ARTISTS"] = values
        audio.save()


def _raw_artists_entries(track: Path) -> list[str]:
    """Return every raw entry name on *track* that spells the ``artists`` list, in any case."""
    if track.suffix == ".mp3":
        frames = ID3(track)  # type: ignore[no-untyped-call]
        return sorted(key for key in frames if key.upper() == "TXXX:ARTISTS")
    if track.suffix == ".m4a":
        atoms = MP4(track)  # type: ignore[no-untyped-call]
        return sorted(key for key in atoms if key.upper().endswith(":ARTISTS"))
    audio = mutagen.File(track)  # type: ignore[attr-defined]
    return sorted({key for key, _ in audio.tags if key.upper() == "ARTISTS"})


_NATIVE_ARTISTS_ENTRY = {
    ".mp3": ["TXXX:ARTISTS"],
    ".m4a": ["----:com.apple.iTunes:ARTISTS"],
    ".flac": ["ARTISTS"],
    ".ogg": ["ARTISTS"],
}


@pytest.mark.parametrize("suffix", _ALL_FORMATS)
def test_reads_the_picard_artists_list(tmp_path: Path, suffix: str) -> None:
    track = make_track(tmp_path / f"picard{suffix}", {"title": ["T"]})
    _write_picard_artists(track, _TWO_ARTISTS)

    assert read_tags(track).tags.get("artists") == _TWO_ARTISTS


@pytest.mark.parametrize("suffix", _ALL_FORMATS)
def test_artists_list_round_trips_in_order_on_its_native_entry(
    tmp_path: Path,
    suffix: str,
) -> None:
    track = make_track(tmp_path / f"track{suffix}", {"title": ["T"]})
    _write_picard_artists(track, ["Old One", "Old Two"])

    write_managed_tags(track, {**_managed(track), "artists": _TWO_ARTISTS})

    assert read_tags(track).tags["artists"] == _TWO_ARTISTS
    assert _raw_artists_entries(track) == _NATIVE_ARTISTS_ENTRY[suffix]


def test_managed_set_version_4_registered() -> None:
    assert MANAGED_SET_VERSION == 4
    assert MANAGED_SETS[4] == MANAGED_TAGS
    # Older stamps must stay frozen: stored revisions point at them.
    assert MANAGED_SETS[1] == ORIGINAL_MANAGED_TAGS
    assert len(MANAGED_SETS[2]) == 18
    assert MANAGED_SETS[3] == MANAGED_TAGS - {"artists"}
    assert TAG_READER_VERSION == 8


def test_write_leaves_unchanged_frames_untouched(tmp_path: Path) -> None:
    # The easy layer builds a fresh UTF-8 frame on every assignment, so rewriting an unchanged
    # value would flip its encoding. Only the changed frame may move.
    track = make_track(tmp_path / "latin1.mp3", {"genre": ["Rock"]})
    raw = ID3(track)  # type: ignore[no-untyped-call]
    raw.add(TRCK(encoding=0, text=["3/10"]))  # type: ignore[no-untyped-call]
    raw.save()
    managed = read_tags(track).tags

    written = write_managed_tags(track, {**managed, "genre": ["Jazz"]})

    after = ID3(track)  # type: ignore[no-untyped-call]
    assert written.written is True
    assert after["TRCK"].encoding == 0
    assert after["TRCK"].text == ["3/10"]
    assert read_tags(track).tags["genre"] == ["Jazz"]


@pytest.mark.parametrize("suffix", _ALL_FORMATS)
def test_write_with_no_change_does_not_touch_the_file(tmp_path: Path, suffix: str) -> None:
    track = make_track(tmp_path / f"same{suffix}", {"genre": ["Rock"], "title": ["Song"]})
    managed = {key: values for key, values in read_tags(track).tags.items() if key in MANAGED_TAGS}
    before_bytes = track.read_bytes()
    before_mtime = track.stat().st_mtime_ns

    written = write_managed_tags(track, managed)

    assert written == TagWriteResult(written=False, audio_proven=False)
    assert track.read_bytes() == before_bytes
    assert track.stat().st_mtime_ns == before_mtime
    assert not list(tmp_path.glob("*.tagmend.tmp"))


@pytest.mark.parametrize(
    ("suffix", "audio_proven"),
    [(".mp3", True), (".flac", True), (".m4a", False), (".ogg", False)],
)
def test_write_reports_whether_the_payload_hash_proves_the_audio(
    tmp_path: Path, suffix: str, *, audio_proven: bool
) -> None:
    track = make_track(tmp_path / f"proof{suffix}", {"genre": ["Rock"]})

    result = write_managed_tags(track, {"genre": ["Jazz"]})

    assert result == TagWriteResult(written=True, audio_proven=audio_proven)


def _managed(track: Path) -> dict[str, list[str]]:
    return {key: values for key, values in read_tags(track).tags.items() if key in MANAGED_TAGS}


def test_write_refuses_to_drop_a_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    track = make_track(tmp_path / "keep.mp3", {"genre": ["Rock"]})
    raw = ID3(track)  # type: ignore[no-untyped-call]
    raw.add(TXXX(encoding=3, desc="Foo", text=["bar"]))  # type: ignore[no-untyped-call]
    raw.save()
    before = track.read_bytes()
    real_apply = tags._apply_changes

    def lossy(
        path: Path,
        container: tags._Container,
        kind: type[FileType],
        changes: list[tuple[str, list[str] | None]],
    ) -> None:
        real_apply(path, container, kind, changes)
        frames = ID3(path)  # type: ignore[no-untyped-call]
        frames.delall("TXXX:Foo")  # type: ignore[no-untyped-call]
        frames.save()

    monkeypatch.setattr(tags, "_apply_changes", lossy)

    with pytest.raises(TagWriteError, match="dropped TXXX:Foo"):
        write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]})

    assert track.read_bytes() == before
    assert not list(tmp_path.glob("*.tagmend.tmp"))


def test_write_refuses_a_container_the_verifier_cannot_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(tmp_path / "other.mp3", {"genre": ["Rock"]})
    before = track.read_bytes()
    monkeypatch.setattr(tags, "_container_of", lambda _audio: tags._Container.OTHER)

    with pytest.raises(TagWriteError, match=r"no layout for the \w+ container"):
        write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]})

    assert track.read_bytes() == before
    assert not list(tmp_path.glob("*.tagmend.tmp"))


def test_write_refuses_when_the_audio_payload_cannot_be_located(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(tmp_path / "lost.flac", {"genre": ["Rock"]})
    before = track.read_bytes()
    monkeypatch.setattr(tags, "_audio_ranges", lambda *_args: None)

    with pytest.raises(TagWriteError, match="audio payload could not be located"):
        write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]})

    assert track.read_bytes() == before
    assert not list(tmp_path.glob("*.tagmend.tmp"))


def test_mp4_without_an_mdat_atom_has_no_locatable_payload() -> None:
    ftyp = (16).to_bytes(4, "big") + b"ftypM4A " + bytes(4)
    trailer = tags._Trailer(has_id3v1=False, has_apev2=False, audio_end=len(ftyp))

    assert tags._audio_ranges(io.BytesIO(ftyp), tags._Container.MP4, trailer) is None


def _payload_byte(path: Path, container: tags._Container) -> int:
    """Return the offset of one audio payload byte in *path*, located by its *container*."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if container is tags._Container.ID3:
            start = tags._id3v2_end(handle)
            end = tags._read_trailer(handle, size).audio_end
            return (start + end) // 2
        if container is tags._Container.MP4:
            atoms = Atoms(handle)
            mdat = next(atom for atom in atoms.atoms if atom.name == b"mdat")
            return int(mdat.offset + mdat.length - 1)
    # The FLAC template carries no ID3v1 or APEv2 trailer, so its last byte is audio.
    return size - 1


@pytest.mark.parametrize("suffix", [".mp3", ".flac", ".m4a"])
def test_write_refuses_when_audio_payload_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    track = make_track(tmp_path / f"audio{suffix}", {"genre": ["Rock"]})
    before = track.read_bytes()
    real_apply = tags._apply_changes

    def corrupting(
        path: Path,
        container: tags._Container,
        kind: type[FileType],
        changes: list[tuple[str, list[str] | None]],
    ) -> None:
        real_apply(path, container, kind, changes)
        data = bytearray(path.read_bytes())
        data[_payload_byte(path, container)] ^= 0xFF
        path.write_bytes(bytes(data))

    monkeypatch.setattr(tags, "_apply_changes", corrupting)

    with pytest.raises(TagWriteError, match="audio payload changed"):
        write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]})

    assert track.read_bytes() == before
    assert not list(tmp_path.glob("*.tagmend.tmp"))


def test_write_refuses_to_drop_an_mp4_atom(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(tmp_path / "keep.m4a", {"genre": ["Rock"]})
    raw = MP4(track)  # type: ignore[no-untyped-call]
    raw["----:com.apple.iTunes:FOO"] = [b"bar"]
    raw.save()  # type: ignore[no-untyped-call]
    before = track.read_bytes()
    real_apply = tags._apply_changes

    def lossy(
        path: Path,
        container: tags._Container,
        kind: type[FileType],
        changes: list[tuple[str, list[str] | None]],
    ) -> None:
        real_apply(path, container, kind, changes)
        atoms = MP4(path)  # type: ignore[no-untyped-call]
        del atoms["----:com.apple.iTunes:FOO"]  # type: ignore[no-untyped-call]
        atoms.save()  # type: ignore[no-untyped-call]

    monkeypatch.setattr(tags, "_apply_changes", lossy)

    with pytest.raises(TagWriteError, match=re.escape("dropped ----:com.apple.iTunes:FOO")):
        write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]})

    assert track.read_bytes() == before
    assert not list(tmp_path.glob("*.tagmend.tmp"))


def test_write_to_a_read_only_file_leaves_no_temp_copy(tmp_path: Path) -> None:
    track = make_track(tmp_path / "locked.mp3", {"genre": ["Rock"]})
    track.chmod(stat.S_IREAD)
    try:
        # POSIX renames onto a read-only file, while Windows refuses the swap.
        with contextlib.suppress(PermissionError):
            write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]})
        assert not track.with_name(track.name + TEMP_SUFFIX).exists()
    finally:
        track.chmod(stat.S_IREAD | stat.S_IWRITE)

    assert write_managed_tags(track, {**_managed(track), "genre": ["Blues"]}).written is True
    assert read_tags(track).tags["genre"] == ["Blues"]


def test_write_replaces_a_read_only_leftover_temp_copy(tmp_path: Path) -> None:
    track = make_track(tmp_path / "left.mp3", {"genre": ["Rock"]})
    leftover = track.with_name(track.name + TEMP_SUFFIX)
    leftover.write_bytes(b"stale")
    leftover.chmod(stat.S_IREAD)

    assert write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]}).written is True

    assert read_tags(track).tags["genre"] == ["Jazz"]
    assert not leftover.exists()


def test_write_flushes_the_temp_copy_before_the_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(tmp_path / "flush.mp3", {"genre": ["Rock"]})
    flushed: list[int] = []
    monkeypatch.setattr(os, "fsync", flushed.append)

    assert write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]}).written is True

    assert len(flushed) == 1


def test_write_opens_the_temp_copy_as_the_original_class(tmp_path: Path) -> None:
    # An ID3-prefixed FLAC sniffs as FLAC only by its extension, which the temp copy lacks.
    track = make_track(tmp_path / "prefixed.flac", {"genre": ["Rock"]})
    prefix = io.BytesIO()
    frames = ID3()  # type: ignore[no-untyped-call]
    frames.add(TIT2(encoding=3, text=["Prefix"]))  # type: ignore[no-untyped-call]
    frames.save(prefix)
    track.write_bytes(prefix.getvalue() + track.read_bytes())

    assert write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]}).written is True

    assert read_tags(track).tags["genre"] == ["Jazz"]
    assert track.read_bytes().startswith(b"ID3")
    assert not list(tmp_path.glob("*.tagmend.tmp"))


def test_v23_iso_tyer_survives_a_write(tmp_path: Path) -> None:
    track = make_track(tmp_path / "iso.mp3")
    frames = ID3()  # type: ignore[no-untyped-call]
    frames.add(TYER(encoding=0, text=["2013-10-04T07:00:00Z"]))  # type: ignore[no-untyped-call]
    frames.add(TCON(encoding=0, text=["Rock"]))  # type: ignore[no-untyped-call]
    frames.save(track, v2_version=3)

    assert read_tags(track).tags["date"] == ["2013-10-04"]

    write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]})

    after = read_tags(track).tags
    assert after["date"] == ["2013-10-04"]
    assert after["genre"] == ["Jazz"]


def _v23_track(path: Path, frames: list[Frame]) -> Path:
    track = make_track(path)
    tag = ID3()  # type: ignore[no-untyped-call]
    tag.add(TCON(encoding=0, text=["Rock"]))  # type: ignore[no-untyped-call]
    for frame in frames:
        tag.add(frame)  # type: ignore[no-untyped-call]
    tag.save(track, v2_version=3)
    return track


@pytest.mark.parametrize(
    ("frames", "dropped"),
    [
        pytest.param(
            [
                TYER(encoding=0, text=["2013"]),  # type: ignore[no-untyped-call]
                TRDA(encoding=0, text=["4th-7th June 2013"]),  # type: ignore[no-untyped-call]
            ],
            "TRDA",
            id="trda-has-no-successor",
        ),
        pytest.param(
            [TDAT(encoding=0, text=["0410"])],  # type: ignore[no-untyped-call]
            "TDAT",
            id="tdat-without-a-year",
        ),
        pytest.param(
            [
                TORY(encoding=0, text=["1998"]),  # type: ignore[no-untyped-call]
                TDOR(encoding=0, text=["1999"]),  # type: ignore[no-untyped-call]
            ],
            "TORY",
            id="tory-beside-a-different-tdor",
        ),
        pytest.param(
            [
                IPLS(encoding=0, people=[["producer", "X"]]),  # type: ignore[no-untyped-call]
                TIPL(encoding=0, people=[["mix", "Y"]]),  # type: ignore[no-untyped-call]
            ],
            "IPLS",
            id="ipls-beside-a-tipl",
        ),
    ],
)
def test_write_refuses_a_v23_frame_the_upgrade_drops(
    tmp_path: Path, frames: list[Frame], dropped: str
) -> None:
    track = _v23_track(tmp_path / "v23.mp3", frames)
    before = track.read_bytes()

    with pytest.raises(TagWriteError, match=f"dropped {dropped}"):
        write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]})

    assert track.read_bytes() == before
    assert not list(tmp_path.glob("*.tagmend.tmp"))


def test_write_carries_v23_frames_the_upgrade_folds(tmp_path: Path) -> None:
    track = _v23_track(
        tmp_path / "v23.mp3",
        [
            TYER(encoding=0, text=["2013"]),  # type: ignore[no-untyped-call]
            TDAT(encoding=0, text=["0410"]),  # type: ignore[no-untyped-call]
            TIME(encoding=0, text=["0730"]),  # type: ignore[no-untyped-call]
            TORY(encoding=0, text=["1999"]),  # type: ignore[no-untyped-call]
            IPLS(encoding=0, people=[["producer", "X"]]),  # type: ignore[no-untyped-call]
        ],
    )

    assert write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]}).written is True

    after = read_tags(track).tags
    assert after["date"] == ["2013-10-04 07:30:00"]
    assert after["originaldate"] == ["1999"]
    assert ID3(track)["TIPL"].people == [["producer", "X"]]  # type: ignore[no-untyped-call]


_BOTH_FRAMES = frozenset({"RVAD", "NCON"})


def test_write_drops_the_frames_the_caller_names(
    tmp_path: Path, tagmend_warnings: pytest.LogCaptureFixture
) -> None:
    track = make_droppable_frames_mp3(tmp_path / "loud.mp3")

    result = write_managed_tags(
        track, {**_managed(track), "genre": ["Jazz"]}, droppable_frames=_BOTH_FRAMES
    )

    assert result.written is True
    assert result.dropped_frames == ("NCON", "RVAD")
    after = read_tags(track).tags
    assert after["genre"] == ["Jazz"]
    assert after["title"] == ["Loud"]
    raw = ID3(track, translate=False)  # type: ignore[no-untyped-call]
    assert raw.getall("RVAD") == []  # type: ignore[no-untyped-call]
    assert raw.unknown_frames == []
    messages = [record.getMessage() for record in tagmend_warnings.records]
    assert len(messages) == 1
    assert "NCON, RVAD" in messages[0]
    assert str(track) in messages[0]


def test_a_write_dropping_nothing_reports_no_dropped_frames(tmp_path: Path) -> None:
    track = make_track(tmp_path / "plain.mp3", {"genre": ["Rock"]})

    result = write_managed_tags(
        track, {**_managed(track), "genre": ["Jazz"]}, droppable_frames=_BOTH_FRAMES
    )

    assert result.written is True
    assert result.dropped_frames == ()


def test_a_frame_left_unnamed_still_refuses_the_write_and_the_stage_check(
    tmp_path: Path,
) -> None:
    track = make_droppable_frames_mp3(tmp_path / "loud.mp3")
    before = track.read_bytes()
    only_rvad = frozenset({"RVAD"})

    with pytest.raises(TagWriteError) as written:
        write_managed_tags(
            track, {**_managed(track), "genre": ["Jazz"]}, droppable_frames=only_rvad
        )
    with pytest.raises(TagWriteError) as staged:
        tags.ensure_writable(track, droppable_frames=only_rvad)

    assert written.value.violations == ["dropped unknown frame NCON"]
    assert staged.value.violations == ["dropped unknown frame NCON"]
    assert track.read_bytes() == before
    assert not list(tmp_path.glob("*.tagmend.tmp"))


def test_the_stage_check_accepts_a_file_once_every_dropped_frame_is_named(
    tmp_path: Path,
) -> None:
    track = make_droppable_frames_mp3(tmp_path / "loud.mp3")

    with pytest.raises(TagWriteError, match="dropped RVAD"):
        tags.ensure_writable(track)

    tags.ensure_writable(track, droppable_frames=_BOTH_FRAMES)


def test_named_frames_still_refuse_a_write_that_changes_another_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = make_droppable_frames_mp3(tmp_path / "loud.mp3")
    # Untranslated, so the v2.3 save keeps the RVAD frame a v2.4 upgrade deletes.
    raw = ID3(track, translate=False)  # type: ignore[no-untyped-call]
    raw.add(TXXX(encoding=3, desc="Foo", text=["bar"]))  # type: ignore[no-untyped-call]
    raw.save(v2_version=3)
    assert sorted(tags._id3_entries(track)) == ["RVAD", "TXXX:Foo", "unknown frame NCON"]
    before = track.read_bytes()
    real_apply = tags._apply_changes

    def rewrite_foo(
        path: Path,
        container: tags._Container,
        kind: type[FileType],
        changes: list[tuple[str, list[str] | None]],
    ) -> None:
        real_apply(path, container, kind, changes)
        frames = ID3(path)  # type: ignore[no-untyped-call]
        frames.add(TXXX(encoding=3, desc="Foo", text=["baz"]))  # type: ignore[no-untyped-call]
        frames.save()

    monkeypatch.setattr(tags, "_apply_changes", rewrite_foo)

    with pytest.raises(TagWriteError) as refused:
        write_managed_tags(
            track, {**_managed(track), "genre": ["Jazz"]}, droppable_frames=_BOTH_FRAMES
        )

    assert refused.value.violations == ["changed TXXX:Foo"]
    assert track.read_bytes() == before


@pytest.mark.parametrize(
    ("before_entries", "after_entries", "violation"),
    [
        pytest.param({"RVAD": ("a",)}, {"RVAD": ("b",)}, "changed RVAD", id="changed"),
        pytest.param({}, {"RVAD": ("a",)}, "added RVAD", id="added"),
        pytest.param({"EQUA": ("a",)}, {}, "dropped EQUA", id="dropped-unnamed"),
    ],
)
def test_a_named_frame_id_excuses_only_its_drop(
    before_entries: dict[str, tuple[str, ...]],
    after_entries: dict[str, tuple[str, ...]],
    violation: str,
) -> None:
    def snapshot(entries: dict[str, tuple[str, ...]]) -> tags._ContainerSnapshot:
        return tags._ContainerSnapshot(
            audio_digest=None, entries=entries, has_id3v1=False, has_apev2=False
        )

    violations = tags._snapshot_violations(
        snapshot(before_entries), snapshot(after_entries), droppable_frames=frozenset({"RVAD"})
    )

    assert violations == [violation]


def test_droppable_frames_are_ignored_off_id3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = make_track(tmp_path / "vorbis.flac", {"genre": ["Rock"], "rvad": ["x"]})
    before = track.read_bytes()
    real_apply = tags._apply_changes

    def drop_rvad(
        path: Path,
        container: tags._Container,
        kind: type[FileType],
        changes: list[tuple[str, list[str] | None]],
    ) -> None:
        real_apply(path, container, kind, changes)
        audio = FLAC(path)
        del audio["rvad"]
        audio.save()

    monkeypatch.setattr(tags, "_apply_changes", drop_rvad)

    with pytest.raises(TagWriteError, match="dropped RVAD"):
        write_managed_tags(
            track, {**_managed(track), "genre": ["Jazz"]}, droppable_frames=frozenset({"RVAD"})
        )

    assert track.read_bytes() == before


@pytest.mark.parametrize("with_id3v1", [False, True])
def test_write_deletes_a_v23_iso_tyer_date(tmp_path: Path, *, with_id3v1: bool) -> None:
    track = make_track(tmp_path / "iso.mp3")
    frames = ID3()  # type: ignore[no-untyped-call]
    frames.add(TYER(encoding=0, text=["2013-10-04T07:00:00Z"]))  # type: ignore[no-untyped-call]
    frames.save(track, v2_version=3, v1=0)
    if with_id3v1:
        year = TYER(encoding=0, text=["2013"])  # type: ignore[no-untyped-call]
        with track.open("ab") as handle:
            handle.write(MakeID3v1({"TYER": year}))  # type: ignore[no-untyped-call]
    target = _managed(track)
    assert target.pop("date") == ["2013-10-04"]

    assert write_managed_tags(track, target).written is True

    assert "date" not in read_tags(track).tags
    assert (track.read_bytes()[-128:-125] == b"TAG") is with_id3v1


def test_write_keeps_id3v1_and_apev2_presence(tmp_path: Path) -> None:
    with_v1 = make_track(tmp_path / "v1.mp3", {"genre": ["Rock"], "title": ["Song"]})
    ID3(with_v1).save(v1=2)  # type: ignore[no-untyped-call]
    with_ape = make_track(tmp_path / "ape.mp3", {"genre": ["Rock"]})
    ape = APEv2()  # type: ignore[no-untyped-call]
    ape["Foo"] = "bar"
    ape.save(with_ape)

    for track in (with_v1, with_ape):
        write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]})

    assert with_v1.read_bytes()[-128:-125] == b"TAG"
    assert str(APEv2(with_ape)["Foo"]) == "bar"  # type: ignore[no-untyped-call]
    assert read_tags(with_v1).tags["genre"] == ["Jazz"]
    assert read_tags(with_ape).tags["genre"] == ["Jazz"]


def _id3v1_title_beside_genre(dest: Path) -> Path:
    """Write an MP3 whose ID3v2 tag holds only a genre and whose ID3v1 block holds a title."""
    track = make_track(dest)
    frames = ID3()  # type: ignore[no-untyped-call]
    frames.add(TCON(encoding=3, text=["Rock"]))  # type: ignore[no-untyped-call]
    frames.save(track, v1=0)
    title = TIT2(encoding=0, text=["Short Title"])  # type: ignore[no-untyped-call]
    with track.open("ab") as handle:
        handle.write(MakeID3v1({"TIT2": title}))  # type: ignore[no-untyped-call]
    return track


def _id3v1_only(dest: Path) -> Path:
    """Write an MP3 with no ID3v2 tag and an ID3v1 block holding a title, artist, album and year."""
    track = make_track(dest)
    ID3(track).delete()  # type: ignore[no-untyped-call]
    block = MakeID3v1(  # type: ignore[no-untyped-call]
        {
            "TIT2": TIT2(encoding=0, text=["Short Title"]),  # type: ignore[no-untyped-call]
            "TPE1": TPE1(encoding=0, text=["Band"]),  # type: ignore[no-untyped-call]
            "TALB": TALB(encoding=0, text=["Record"]),  # type: ignore[no-untyped-call]
            "TDRC": TDRC(encoding=0, text=["1999"]),  # type: ignore[no-untyped-call]
        },
    )
    with track.open("ab") as handle:
        handle.write(block)
    return track


def test_read_ignores_id3v1_beside_an_id3v2_frame(tmp_path: Path) -> None:
    track = _id3v1_title_beside_genre(tmp_path / "beside.mp3")

    assert read_tags(track).tags == {"genre": ["Rock"]}


def test_read_falls_back_to_id3v1_without_an_id3v2_frame(tmp_path: Path) -> None:
    track = _id3v1_only(tmp_path / "v1only.mp3")

    assert read_tags(track).tags == {
        "album": ["Record"],
        "artist": ["Band"],
        "date": ["1999"],
        "title": ["Short Title"],
    }


def test_write_creating_id3v2_writes_every_value(tmp_path: Path) -> None:
    track = _id3v1_only(tmp_path / "v1only.mp3")
    target = {**_managed(track), "genre": ["Jazz"]}

    assert write_managed_tags(track, target).written is True

    v2 = ID3(track, load_v1=False)  # type: ignore[no-untyped-call]
    assert sorted(v2.keys()) == ["TALB", "TCON", "TDRC", "TIT2", "TPE1"]  # type: ignore[no-untyped-call]
    assert read_tags(track).tags == target
    assert track.read_bytes()[-128:-125] == b"TAG"


def test_write_of_an_id3v1_only_file_skips_a_no_op(tmp_path: Path) -> None:
    track = _id3v1_only(tmp_path / "v1only.mp3")
    before = track.read_bytes()

    assert write_managed_tags(track, _managed(track)).written is False

    assert track.read_bytes() == before


def test_write_keeps_an_id3v1_only_field_out_of_id3v2(tmp_path: Path) -> None:
    track = _id3v1_title_beside_genre(tmp_path / "beside.mp3")

    write_managed_tags(track, {**_managed(track), "genre": ["Jazz"]})

    v2 = ID3(track, load_v1=False)  # type: ignore[no-untyped-call]
    assert "TIT2" not in v2
    assert v2["TCON"].text == ["Jazz"]
    v1 = ParseID3v1(track.read_bytes()[-128:])  # type: ignore[no-untyped-call]
    assert v1 is not None
    assert v1["TIT2"].text == ["Short Title"]


def _syncsafe(size: int) -> bytes:
    return bytes((size >> shift) & 0x7F for shift in (21, 14, 7, 0))


def _raw_id3v2_beside_id3v1_title(dest: Path, major: int, frames: bytes) -> Path:
    """Write an MP3 whose ID3v2 tag holds *frames* under a v2.*major* header and an ID3v1 title."""
    track = make_track(dest)
    ID3(track).delete()  # type: ignore[no-untyped-call]
    padding = bytes(64)
    header = b"ID3" + bytes((major, 0, 0)) + _syncsafe(len(frames) + len(padding))
    title = TIT2(encoding=0, text=["Short Title"])  # type: ignore[no-untyped-call]
    block = MakeID3v1({"TIT2": title})  # type: ignore[no-untyped-call]
    track.write_bytes(header + frames + padding + track.read_bytes() + block)
    return track


def test_read_falls_back_to_id3v1_behind_a_frameless_id3v2_header(tmp_path: Path) -> None:
    track = _raw_id3v2_beside_id3v1_title(tmp_path / "frameless.mp3", 4, b"")

    assert read_tags(track).tags == {"title": ["Short Title"]}


def test_read_counts_an_unknown_frame_as_an_id3v2_frame(tmp_path: Path) -> None:
    frame = b"ZZZZ" + _syncsafe(1) + b"\x00\x00" + b"x"
    track = _raw_id3v2_beside_id3v1_title(tmp_path / "unknown.mp3", 4, frame)

    assert read_tags(track).tags == {}


def test_read_falls_back_to_id3v1_behind_an_unsupported_id3v2_version(tmp_path: Path) -> None:
    track = _raw_id3v2_beside_id3v1_title(tmp_path / "v25.mp3", 5, b"")

    assert read_tags(track).tags == {"title": ["Short Title"]}


def test_read_ignores_id3v1_beside_an_id3v23_frame(tmp_path: Path) -> None:
    track = make_track(tmp_path / "v23.mp3")
    frames = ID3()  # type: ignore[no-untyped-call]
    frames.add(TPE1(encoding=0, text=["Band"]))  # type: ignore[no-untyped-call]
    frames.save(track, v2_version=3, v1=0)
    title = TIT2(encoding=0, text=["Short Title"])  # type: ignore[no-untyped-call]
    with track.open("ab") as handle:
        handle.write(MakeID3v1({"TIT2": title}))  # type: ignore[no-untyped-call]

    assert read_tags(track).tags == {"artist": ["Band"]}


def test_write_deletes_every_value_of_an_id3v1_only_file(tmp_path: Path) -> None:
    track = _id3v1_only(tmp_path / "v1only.mp3")

    assert write_managed_tags(track, {}).written is True

    assert read_tags(track).tags == {}
    assert track.read_bytes()[-128:-125] == b"TAG"


def test_write_refuses_to_empty_id3v2_over_id3v1_values(tmp_path: Path) -> None:
    track = _id3v1_title_beside_genre(tmp_path / "beside.mp3")
    before = track.read_bytes()

    with pytest.raises(TagWriteError, match="emptying ID3v2 exposes the values only ID3v1 holds"):
        write_managed_tags(track, {})

    assert track.read_bytes() == before


@pytest.mark.parametrize("suffix", _ALL_FORMATS)
def test_a_verifiable_suffix_opens_as_a_container_staging_accepts(
    tmp_path: Path, suffix: str
) -> None:
    track = make_track(tmp_path / f"track{suffix}")

    tags.ensure_writable(track)

    assert suffix in tags.VERIFIABLE_SUFFIXES


def test_a_wav_suffix_is_not_verifiable(tmp_path: Path) -> None:
    clip = tmp_path / "clip.wav"
    with wave.open(str(clip), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(bytes(1600))

    with pytest.raises(TagWriteError, match="no layout for the WAVE container"):
        tags.ensure_writable(clip)

    assert ".wav" not in tags.VERIFIABLE_SUFFIXES
