"""Read and surgically write the normalized tag set via mutagen (M1 read + M3 write).

Tags are read in mutagen's "easy" mode and normalized into a single canonical,
lowercase namespace so the rest of the engine never has to care about format-specific
key spellings (ID3 vs Vorbis vs MP4). A Vorbis spelling map collapses the Picard names
mutagen leaves raw. Everything else passes through lowercased unchanged.

The write path (:func:`write_managed_tags`, M3) touches only the narrow
:data:`MANAGED_TAGS` set and writes atomically (temp copy + ``os.replace``) so a
dropped NAS connection mid-write cannot corrupt the original (PLAN.md §7 and §11).
Before the swap it verifies the temp copy, and refuses with :class:`TagWriteError` when the
save changed anything outside the target. A container the verifier cannot check is refused
before any copy, and :func:`ensure_writable` lets staging refuse it before a change is queued.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import shutil
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, BinaryIO, Final

import mutagen

# The only shared base of every Vorbis-comment container (FLAC, OggVorbis, OggOpus, ...).
# mutagen 1.47 exposes no public predicate for "are these Vorbis comments", and naming the
# concrete subclasses instead would silently miss the ones not listed.
from mutagen._vorbis import VCommentDict
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4, EasyMP4Tags
from mutagen.flac import FLAC
from mutagen.id3 import (  # type: ignore[attr-defined]
    ID3,
    ID3FileType,
    ID3NoHeaderError,
    MakeID3v1,
    ParseID3v1,
)
from mutagen.mp4 import MP4, MP4Tags
from mutagen.ogg import OggFileType

from tagmend.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from mutagen._file import FileType

logger = get_logger(__name__)

# ``originaldate`` (the original/first-release year) is native on ID3 (``TDOR``) and Vorbis
# (``ORIGINALDATE``) via mutagen's easy mode, but MP4 has no built-in easy mapping. Register
# it ONCE at module load so read and write agree on the iTunes freeform atom — never ``©day``
# (that is ``date``, the reissue year). The atom name is matched CASE-SENSITIVELY and Picard
# writes it lowercase, so an uppercase registration reads nothing on a Picard-tagged file and
# writes a second, contradicting atom beside it. Registration is idempotent; importing this
# module is the single place it happens.
EasyMP4Tags.RegisterFreeformKey("originaldate", "originaldate")  # type: ignore[no-untyped-call]

# EasyID3 maps ``albumartistsort`` to ``TXXX:ALBUMARTISTSORT``, a frame Picard does not write:
# it uses the iTunes-compatible ``TSO2`` instead. Measured on this library, 88% of a 400-file
# MP3 sample carried ``TSO2`` and none carried the ``TXXX`` frame, so without this the tag reads
# as absent on ~7,960 files and writing it creates a second value the rest of the world ignores.
EasyID3.RegisterTextKey("albumartistsort", "TSO2")  # type: ignore[no-untyped-call]

# Neither easy layer maps the multi-value ``artists`` list. Picard writes it to ``TXXX:ARTISTS``
# on ID3 and to the uppercase ``ARTISTS`` freeform atom on MP4, the only spellings this library
# carries (8,015 MP3s and 5 M4As). Vorbis carries ``ARTISTS`` natively.
EasyID3.RegisterTXXXKey("artists", "ARTISTS")  # type: ignore[no-untyped-call]
EasyMP4Tags.RegisterFreeformKey("artists", "ARTISTS")  # type: ignore[no-untyped-call]

# The two MusicBrainz ids in :data:`MANAGED_TAGS` that EasyMP4 has no built-in mapping for
# (the other four — album/albumartist/artist/track ids + album type — are native). Register
# them here on the SAME iTunes freeform atom names Picard writes (verified against a real
# Picard-tagged ``.m4a``: ``----:com.apple.iTunes:MusicBrainz Release Group Id`` /
# ``MusicBrainz Release Track Id``), so read and write agree. EasyID3/Vorbis carry both
# natively.
EasyMP4Tags.RegisterFreeformKey(  # type: ignore[no-untyped-call]
    "musicbrainz_releasegroupid",
    "MusicBrainz Release Group Id",
)
EasyMP4Tags.RegisterFreeformKey(  # type: ignore[no-untyped-call]
    "musicbrainz_releasetrackid",
    "MusicBrainz Release Track Id",
)

# The release-stamp fields MP4 needs mapped. ``organization`` is registered for READING only
# (it is not in the managed set — see the note beside _VORBIS_SPELLINGS), which costs nothing
# and beats leaving the atom invisible. All but ``releasecountry`` simply have no built-in
# EasyMP4 entry; ``releasecountry`` HAS one and it points at the wrong atom — mutagen says
# ``MusicBrainz Release Country`` while Picard writes ``MusicBrainz Album Release Country``, one
# word apart and invisible without this override. Every name here was read off a real
# Picard-tagged ``.m4a`` in the library except ``CATALOGNUMBER``, which no sample carried and
# which follows the uppercase convention the other five share.
for _key, _atom in (
    ("organization", "LABEL"),
    ("media", "MEDIA"),
    ("barcode", "BARCODE"),
    ("catalognumber", "CATALOGNUMBER"),
    ("isrc", "ISRC"),
    ("asin", "ASIN"),
    ("releasecountry", "MusicBrainz Album Release Country"),
):
    EasyMP4Tags.RegisterFreeformKey(_key, _atom)  # type: ignore[no-untyped-call]

# Canonical key -> the name Picard uses for the same concept in a Vorbis comment. mutagen has
# no "easy" layer for Vorbis (``mutagen.File(path, easy=True)`` hands back a plain ``FLAC`` /
# ``OggVorbis``), so unlike ID3 and MP4 these names are NOT normalized for us and every one
# that differs from the canonical key has to be mapped here, in both directions. An entry is
# needed ONLY where the two names differ.
_VORBIS_SPELLINGS: Final[Mapping[str, str]] = {
    "musicbrainz_albumtype": "releasetype",
    "musicbrainz_albumstatus": "releasestatus",
}

# NOT here, deliberately: ``organization`` <-> ``label``. Measured on this library, 479 FLACs
# carry BOTH Vorbis names and on 245 of them the values DIFFER (an original label in
# ORGANIZATION, the reissue label in LABEL). Collapsing them would pick one and let a later
# write delete the other, with the baseline holding only the survivor — irreversible. The two
# above have zero such overlap, so collapsing them is lossless. Until the label pair has a
# decided rule, both names stay unmapped and unmanaged.
# The same holds for ``BAND`` and ``ALBUM ARTIST`` against ``ALBUMARTIST``: library FLACs carry a
# different value in each, and none carries either without ``ALBUMARTIST``.

# Derived, never hand-written twice: a second literal could drift out of step with the map above.
_VORBIS_TO_CANONICAL: Final[Mapping[str, str]] = {v: k for k, v in _VORBIS_SPELLINGS.items()}

# Two canonical keys sharing one Vorbis name would silently collapse the reverse map, making one
# of them unreadable. Fail at import rather than at some file months later.
if len(_VORBIS_TO_CANONICAL) != len(_VORBIS_SPELLINGS):  # pragma: no cover - import-time guard
    _DUPLICATE_VORBIS_NAME = "_VORBIS_SPELLINGS maps two canonical keys to one Vorbis name"
    raise AssertionError(_DUPLICATE_VORBIS_NAME)

# The five tags TagMend managed BEFORE the mismatch-fix widening: managed-set version 1 in
# :data:`MANAGED_SETS`. A revision stamped version 1 governed exactly these, so revert
# deletes only these when its snapshot omits them and preserves everything wider.
ORIGINAL_MANAGED_TAGS: Final[frozenset[str]] = frozenset(
    {
        "genre",
        "albumartist",
        "artist",
        "musicbrainz_artistid",
        "originaldate",
    },
)

# The 13 identity/MusicBrainz fields the mismatch-fix flow adds: the full wrong-release
# "stamp" a tagger (Picard) leaves when it matches a track against the wrong MusicBrainz
# release — identity (``title``/``album``/``date``/``tracknumber``/``discnumber``/the two
# sort names) plus the album type and the five remaining MB ids. EasyID3/Vorbis carry them
# natively; EasyMP4 needs the two freeform registrations above.
_WIDENED_MANAGED_TAGS: Final[frozenset[str]] = frozenset(
    {
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

# The release/recording provenance stamp: WHICH PRESSING a file's tags came from. An identity
# fix rewrites artist/album/title but leaves this block behind, so a rebound file reads
# "Alice in Chains - Greatest Hits" while its albumstatus still says "bootleg" and its country
# "RU" — from the Russian bootleg it was wrongly matched to. Managed so the fix flow can clear
# or replace it in the same commit, and so revert governs it like everything else.
RELEASE_STAMP_TAGS: Final[frozenset[str]] = frozenset(
    {
        "musicbrainz_albumstatus",
        "media",
        "releasecountry",
        "barcode",
        "catalognumber",
        "isrc",
        "asin",
    },
)

# The multi-value list a library server builds its artist entities from when a file carries
# it, ``artist`` then being only the display credit. Managed so an artist-name fix renames both.
_ARTIST_LIST_TAGS: Final[frozenset[str]] = frozenset({"artists"})

# The set of tags TagMend is allowed to write/revert (26 = 5 original + 13 identity + 7 release
# stamp + the artists list). A CLOSED set: anything outside it (``comment``/``composer``/art…)
# is never read, written, or deleted, and every key here MUST be provably writable on all four
# formats. The mismatch-fix flow can repair a poisoned release in one commit, and revert restores
# every field the target revision's own managed set governed (see
# :func:`tagmend.engine.versioning._revert_target_tags`).
# ``date`` (reissue year, MP4 ``©day``) and ``originaldate`` (original year, MP4 freeform) are
# BOTH managed and kept distinct.
MANAGED_TAGS: Final[frozenset[str]] = (
    ORIGINAL_MANAGED_TAGS | _WIDENED_MANAGED_TAGS | RELEASE_STAMP_TAGS | _ARTIST_LIST_TAGS
)

# Which managed set governed a given revision, so revert can tell "this tag was empty then"
# from "this tag was not tracked then". Version 1 is the pre-widening five-tag set, version 2
# adds the thirteen identity fields, version 3 the seven release-stamp fields, version 4 the
# ``artists`` list. Every new revision is stamped with :data:`MANAGED_SET_VERSION`, and
# :func:`governed_tags` looks a stamp up here. Widening the set again means a new entry and a
# bump, never editing an existing entry, since stored revisions point at it.
MANAGED_SET_VERSION: Final = 4

MANAGED_SETS: Final[Mapping[int, frozenset[str]]] = {
    1: ORIGINAL_MANAGED_TAGS,
    2: ORIGINAL_MANAGED_TAGS | _WIDENED_MANAGED_TAGS,
    3: ORIGINAL_MANAGED_TAGS | _WIDENED_MANAGED_TAGS | RELEASE_STAMP_TAGS,
    4: MANAGED_TAGS,
}


def governed_tags(managed_set: int) -> frozenset[str]:
    """Return the tags a revision stamped *managed_set* governed.

    An unknown stamp falls back to the pre-widening set: preserve rather than delete, since
    deleting is the unrecoverable direction.
    """
    return MANAGED_SETS.get(managed_set, ORIGINAL_MANAGED_TAGS)


# Which reader produced a snapshot row, so an incremental scan can spot rows left behind by
# an older one and re-read them exactly once. BUMP THIS IN THE SAME COMMIT as any change to
# what :func:`read_tags` produces (the managed set, a Vorbis spelling, a format registration), or
# every already-scanned file keeps serving the old reader's output to every detector.
TAG_READER_VERSION: Final = 7


@dataclass(frozen=True, slots=True)
class TrackTags:
    """A file's tags: canonical lowercase name -> ordered list of string values."""

    tags: dict[str, list[str]]


def _vorbis_field(key: str) -> str:
    """Return the Vorbis comment field name to write *key* under.

    Uppercase because that is what Picard emits and what the rest of a Picard-tagged file
    already uses. Field names are case-insensitive per the Vorbis spec, so this is convention
    rather than correctness: it keeps one file from carrying a mix of cases.
    """
    return _VORBIS_SPELLINGS.get(key, key).upper()


def _probe_id3_frames() -> dict[str, str]:
    """Map each managed key to the ID3v2 frame key the easy layer writes it under."""
    spellings: dict[str, str] = {}
    for key in MANAGED_TAGS:
        frames: Any = ID3()  # type: ignore[no-untyped-call]
        EasyID3.Set[key](frames, key, ["1"])
        (spellings[key],) = frames.keys()
    return spellings


def _probe_mp4_atoms() -> dict[str, str]:
    """Map each managed key to the MP4 atom the easy layer writes it under."""
    spellings: dict[str, str] = {}
    for key in MANAGED_TAGS:
        atoms: Any = MP4Tags()  # type: ignore[no-untyped-call]
        EasyMP4Tags.Set[key](atoms, key, ["1"])
        (spellings[key],) = atoms.keys()
    return spellings


# The raw entry each managed key occupies, read off the easy layers themselves, so the write
# verifier and the writer can never disagree about what "managed" covers.
_ID3_FRAMES: Final[Mapping[str, str]] = _probe_id3_frames()
_MP4_ATOMS: Final[Mapping[str, str]] = _probe_mp4_atoms()
_VORBIS_FIELDS: Final[frozenset[str]] = frozenset(
    {_vorbis_field(key) for key in MANAGED_TAGS} | {key.upper() for key in _VORBIS_SPELLINGS},
)

# The older frames mutagen's v2.4 upgrade removes. It folds these into a successor only when
# that successor is absent and the value parses, and pops them unseen otherwise.
_ID3_FOLDED: Final = ("TYER", "TDAT", "TIME", "TORY", "IPLS")
# These it deletes with no successor. TSIZ is left out because the v2.4 spec itself drops it.
_ID3_DROPPED: Final = ("RVAD", "EQUA", "TRDA")

_ISO_DATE_PREFIX: Final = re.compile(r"(\d{4}-\d{2}-\d{2})")
_MUTAGEN_TYER: Final = re.compile(r"[0-9]{4}(-[0-9]{2}-[0-9]{2})?\Z")
_TWO_PAIRS: Final = re.compile(r"([0-9]{2})([0-9]{2})\Z")


def _first_iso_date(tyer_texts: Iterable[str]) -> list[str]:
    """Return the ``YYYY-MM-DD`` prefix of the first ``TYER`` value that has one."""
    for text in tyer_texts:
        match = _ISO_DATE_PREFIX.match(text)
        if match is not None:
            return [match.group(1)]
    return []


def _unconverted_tyer_date(path: Path) -> list[str]:
    """Return the ``YYYY-MM-DD`` prefix of a v2.3 ``TYER`` mutagen's v2.4 upgrade discarded.

    mutagen converts only a bare year or ``YYYY-MM-DD`` and silently drops anything longer, such
    as ``2013-10-04T07:00:00Z``, so the date would otherwise read as absent and vanish on save.
    """
    try:
        frames: Any = ID3(path, translate=False, load_v1=False)  # type: ignore[no-untyped-call]
    except ID3NoHeaderError:
        return []
    return _first_iso_date(str(text) for frame in frames.getall("TYER") for text in frame.text)


def read_tags(path: Path) -> TrackTags:
    """Read and normalize the tags on *path*.

    Returns an empty :class:`TrackTags` when mutagen cannot identify the file or it
    carries no tags. Lets :class:`mutagen.MutagenError` propagate so the caller can
    decide how to record a read failure. An ID3v2.3 ``TYER`` that starts with a full
    ``YYYY-MM-DD`` date mutagen cannot convert reads as that ``date`` when no other date exists.
    """
    return _normalized_tags(path, mutagen.File(path, easy=True))  # type: ignore[attr-defined]


def _normalized_tags(path: Path, audio: FileType | None) -> TrackTags:
    """Normalize the tags of *audio*, opened in easy mode from *path*, as :func:`read_tags` does.

    A write's temp copy is opened by the class identified from the original, because mutagen
    weighs the file name when it sniffs and the temp name carries no audio extension.
    """
    # Input
    tags: Any = None if audio is None else audio.tags
    if tags is None:
        return TrackTags({})

    # Process — a file can carry both spellings of one concept (a tagger wrote the Vorbis name,
    # an older TagMend wrote the canonical one). The Vorbis name is what every other reader
    # looks at, so it wins regardless of iteration order.
    normalized: dict[str, list[str]] = {}
    from_vorbis_spelling: set[str] = set()
    for raw_key, raw_values in tags.items():
        lowered = str(raw_key).lower()
        key = _VORBIS_TO_CANONICAL.get(lowered, lowered)
        native = lowered in _VORBIS_TO_CANONICAL
        if key in from_vorbis_spelling and not native:
            continue
        normalized[key] = [str(value) for value in raw_values]
        if native:
            from_vorbis_spelling.add(key)
    if isinstance(tags, EasyID3) and "date" not in normalized:
        fallback = _unconverted_tyer_date(path)
        if fallback:
            normalized["date"] = fallback

    # Output
    return TrackTags(normalized)


class TagWriteError(ValueError):
    """A tag write refused as unverifiable, or because it changed more than its target.

    Raised before the atomic replace, so the original file is untouched.
    """

    def __init__(self, path: Path, violations: list[str]) -> None:
        """Name *path* and every violation in the message."""
        self.path = path
        self.violations = violations
        super().__init__(f"refusing to write {path}: {'; '.join(violations)}")


class _Container(StrEnum):
    """The container families the write verifier knows the layout of."""

    ID3 = "id3"  # an ID3v2-prefixed stream such as MP3
    FLAC = "flac"
    MP4 = "mp4"
    OGG = "ogg"
    OTHER = "other"


# The suffixes that open as a container the verifier knows, so a caller holding only a filename
# can tell which files staging would refuse. WAV, AIFF, WMA and raw AAC open as OTHER.
VERIFIABLE_SUFFIXES: Final[frozenset[str]] = frozenset({".mp3", ".flac", ".m4a", ".ogg", ".opus"})


# The containers whose hashed payload holds every byte the decoder reads. MP4 decoding also reads
# the moov chunk offsets a save rewrites, and Ogg hashes no payload.
_PAYLOAD_DECIDES_AUDIO: Final = frozenset({_Container.ID3, _Container.FLAC})


@dataclass(frozen=True, slots=True)
class TagWriteResult:
    """What :func:`write_managed_tags` did to the file.

    ``audio_proven`` holds after a write whose verified payload hash decides the decoded audio,
    so a fingerprint taken before the write still describes the file.
    """

    written: bool
    audio_proven: bool


def _container_of(audio: FileType) -> _Container:
    """Return the container family of an opened *audio* file."""
    if isinstance(audio, ID3FileType):
        return _Container.ID3
    if isinstance(audio, FLAC):
        return _Container.FLAC
    if isinstance(audio, MP4):
        return _Container.MP4
    if isinstance(audio, OggFileType):
        return _Container.OGG
    return _Container.OTHER


@dataclass(frozen=True, slots=True)
class _ContainerSnapshot:
    """What a tag write must leave alone: the audio bytes, unmanaged entries and extra blocks."""

    audio_digest: str | None
    entries: dict[str, tuple[str, ...]]
    has_id3v1: bool
    has_apev2: bool


@dataclass(frozen=True, slots=True)
class _Trailer:
    """The tag blocks that may follow the audio: an APEv2 tag, then a 128-byte ID3v1 tag."""

    has_id3v1: bool
    has_apev2: bool
    audio_end: int


_ID3V1_SIZE: Final = 128
_PAYLOAD_NOT_LOCATED: Final = "the audio payload could not be located to verify the write"
_ID3V2_HEADER_SIZE: Final = 10
_ID3V2_FOOTER_FLAG: Final = 0x10
_APE_FOOTER_SIZE: Final = 32
_APE_HAS_HEADER: Final = 0x80000000
_FLAC_LAST_BLOCK: Final = 0x80
_MP4_ATOM_HEADER: Final = 8
_MP4_LARGE_SIZE: Final = 1
_MP4_TO_EOF: Final = 0
_HASH_CHUNK: Final = 1 << 20


def _digest(data: bytes | str) -> str:
    """Return a SHA-256 hex digest of *data*, text encoded as UTF-8."""
    raw = data.encode("utf-8", "surrogatepass") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def _read_trailer(handle: BinaryIO, size: int) -> _Trailer:
    """Locate the ID3v1 and APEv2 blocks at the end of the file open in *handle*."""
    end = size
    has_id3v1 = False
    if size >= _ID3V1_SIZE:
        handle.seek(size - _ID3V1_SIZE)
        has_id3v1 = handle.read(3) == b"TAG"
    if has_id3v1:
        end -= _ID3V1_SIZE

    has_apev2 = False
    if end >= _APE_FOOTER_SIZE:
        handle.seek(end - _APE_FOOTER_SIZE)
        footer = handle.read(_APE_FOOTER_SIZE)
        has_apev2 = footer[:8] == b"APETAGEX"
        if has_apev2:
            tag_size = int.from_bytes(footer[12:16], "little")
            flags = int.from_bytes(footer[20:24], "little")
            end -= tag_size + (_APE_FOOTER_SIZE if flags & _APE_HAS_HEADER else 0)
    return _Trailer(has_id3v1=has_id3v1, has_apev2=has_apev2, audio_end=end)


def _id3v2_end(handle: BinaryIO) -> int:
    """Return the offset just past a leading ID3v2 tag (its footer included), else 0."""
    handle.seek(0)
    header = handle.read(_ID3V2_HEADER_SIZE)
    if len(header) < _ID3V2_HEADER_SIZE or header[:3] != b"ID3":
        return 0
    size = 0
    for byte in header[6:10]:
        size = (size << 7) | (byte & 0x7F)
    footer = _ID3V2_HEADER_SIZE if header[5] & _ID3V2_FOOTER_FLAG else 0
    return _ID3V2_HEADER_SIZE + size + footer


def _flac_audio_start(handle: BinaryIO) -> int | None:
    """Return the offset just past the last FLAC metadata block, or ``None`` if not found."""
    position = _id3v2_end(handle)
    handle.seek(position)
    if handle.read(4) != b"fLaC":
        return None
    position += 4
    while True:
        handle.seek(position)
        header = handle.read(4)
        if len(header) < 4:  # noqa: PLR2004 - a FLAC metadata block header is four bytes
            return None
        position += 4 + int.from_bytes(header[1:4], "big")
        if header[0] & _FLAC_LAST_BLOCK:
            return position


def _mp4_mdat_ranges(handle: BinaryIO, size: int) -> list[tuple[int, int]]:
    """Return the ``[start, end)`` payload range of every top-level ``mdat`` atom."""
    ranges: list[tuple[int, int]] = []
    position = 0
    while position + _MP4_ATOM_HEADER <= size:
        handle.seek(position)
        header = handle.read(_MP4_ATOM_HEADER)
        atom_size = int.from_bytes(header[:4], "big")
        header_size = _MP4_ATOM_HEADER
        if atom_size == _MP4_LARGE_SIZE:
            atom_size = int.from_bytes(handle.read(8), "big")
            header_size += 8
        elif atom_size == _MP4_TO_EOF:
            atom_size = size - position
        if atom_size < header_size:
            break
        if header[4:8] == b"mdat":
            ranges.append((position + header_size, position + atom_size))
        position += atom_size
    return ranges


def _hash_ranges(handle: BinaryIO, ranges: list[tuple[int, int]]) -> str:
    """Return one SHA-256 hex digest over every ``[start, end)`` byte range, in order."""
    digest = hashlib.sha256()
    for start, end in ranges:
        handle.seek(start)
        remaining = end - start
        while remaining > 0:
            chunk = handle.read(min(_HASH_CHUNK, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _audio_ranges(
    handle: BinaryIO, container: _Container, trailer: _Trailer
) -> list[tuple[int, int]] | None:
    """Return the byte ranges holding the audio payload, or ``None`` when not locatable."""
    if container is _Container.ID3:
        return [(_id3v2_end(handle), trailer.audio_end)]
    if container is _Container.FLAC:
        start = _flac_audio_start(handle)
        return None if start is None else [(start, trailer.audio_end)]
    if container is _Container.MP4:
        return _mp4_mdat_ranges(handle, trailer.audio_end) or None
    # An Ogg stream interleaves its comment packets with audio pages and a save renumbers the
    # pages, so no byte range holds the audio alone.
    return None


@dataclass(frozen=True, slots=True)
class _Successors:
    """What an upgraded ID3 tag holds for the older frames the v2.4 upgrade folds away.

    ``dates`` are the ``TDRC`` values, or the ``TYER`` date :func:`read_tags` reports when there
    is no ``TDRC``. ``original_dates`` are the ``TDOR`` values, ``people`` each ``TIPL`` list.
    """

    dates: tuple[str, ...]
    original_dates: tuple[str, ...]
    people: tuple[str, ...]


def _upgrade_keeps(frame_id: str, value: str, successors: _Successors) -> bool:
    """Return whether one *value* of the older frame *frame_id* survives the v2.4 upgrade."""
    # ID3v2.3 stores TDAT as DDMM and TIME as HHMM.
    pairs = _TWO_PAIRS.match(value)
    if frame_id == "TYER":
        iso = _first_iso_date([value])
        prefix = value if _MUTAGEN_TYER.match(value) else next(iter(iso), None)
        return prefix is not None and any(date.startswith(prefix) for date in successors.dates)
    if frame_id == "TDAT":
        month_day = f"-{pairs[2]}-{pairs[1]}" if pairs else None
        return any(date[4:10] == month_day for date in successors.dates)
    if frame_id == "TIME":
        # Skips the date/time separator, which mutagen's timestamps write as a space.
        clock = f"{pairs[1]}:{pairs[2]}" if pairs else None
        return any(date[11:16] == clock for date in successors.dates)
    if frame_id == "TORY":
        return any(date.startswith(value) for date in successors.original_dates)
    if frame_id == "IPLS":
        return value in successors.people
    return False


def _id3_entries(source: Path | BinaryIO) -> dict[str, list[str]]:
    """Return the unmanaged ID3v2 frames of *source* as ``frame key -> value digests``.

    *source* is a path or a file object, so :func:`ensure_writable` can read an in-memory save.

    Loaded untranslated so a frame the v2.4 upgrade removes is still seen, then upgraded so a
    v2.3 frame compares equal to the v2.4 successor a save writes in its place. An older frame
    whose value the upgrade does not carry into a successor is kept under its own id, so the
    saved copy, which lacks it, reads as dropping it. ID3v1 is left out: the snapshot compares
    only its presence.
    """
    try:
        frames: Any = ID3(source, translate=False, load_v1=False)  # type: ignore[no-untyped-call]
    except ID3NoHeaderError:
        return {}
    entries: dict[str, list[str]] = {}
    id_size, header_size = (3, 6) if frames.version < (2, 3, 0) else (4, 10)
    for data in frames.unknown_frames:
        key = f"unknown frame {bytes(data[:id_size]).decode('latin-1')}"
        entries.setdefault(key, []).append(_digest(bytes(data[header_size:])))
    older = [
        (frame_id, frame)
        for frame_id in (*_ID3_FOLDED, *_ID3_DROPPED)
        for frame in frames.getall(frame_id)
    ]
    tyer_date = _first_iso_date(
        str(text) for frame_id, frame in older if frame_id == "TYER" for text in frame.text
    )
    frames.update_to_v24()
    dates = [str(stamp) for frame in frames.getall("TDRC") for stamp in frame.text]
    successors = _Successors(
        dates=tuple(dates or tyer_date),
        original_dates=tuple(str(stamp) for frame in frames.getall("TDOR") for stamp in frame.text),
        people=tuple(repr(frame.people) for frame in frames.getall("TIPL")),
    )
    for frame_id, frame in older:
        kept = False
        if frame_id == "IPLS":
            kept = _upgrade_keeps(frame_id, repr(frame.people), successors)
        elif frame_id in _ID3_FOLDED:
            kept = all(_upgrade_keeps(frame_id, str(text), successors) for text in frame.text)
        if not kept:
            entries.setdefault(frame_id, []).append(_digest(repr(frame)))
    managed = frozenset(_ID3_FRAMES.values())
    for key, frame in frames.items():
        if key not in managed:
            entries.setdefault(key, []).append(_digest(repr(frame)))
    return entries


def _mutagen_entries(
    path: Path,
    container: _Container,
    kind: type[FileType],
) -> dict[str, list[str]]:
    """Return the unmanaged Vorbis comments or MP4 atoms (plus FLAC pictures) of *path*.

    *kind* is the easy class the original opened as. MP4 swaps in the raw class, because the
    easy layer renames the atoms.
    """
    entries: dict[str, list[str]] = {}
    raw_kind = MP4 if issubclass(kind, EasyMP4) else kind
    audio: Any = raw_kind(path)
    is_mp4 = container is _Container.MP4
    if audio.tags is not None:
        pairs = audio.tags.items() if is_mp4 else audio.tags
        managed = frozenset(_MP4_ATOMS.values()) if is_mp4 else _VORBIS_FIELDS
        for name, value in pairs:
            # MP4 atom names are case-sensitive, Vorbis field names are not.
            key = str(name) if is_mp4 else str(name).upper()
            if key not in managed:
                entries.setdefault(key, []).append(_digest(repr(value)))
    if container is _Container.FLAC:
        for picture in audio.pictures:
            entries.setdefault("FLAC picture block", []).append(_digest(picture.write()))
    return entries


def _unmanaged_entries(
    path: Path,
    container: _Container,
    kind: type[FileType],
) -> dict[str, tuple[str, ...]]:
    """Return every tag entry outside the managed set's raw spellings, as value digests."""
    if container is _Container.ID3:
        entries = _id3_entries(path)
    else:
        entries = _mutagen_entries(path, container, kind)
    return {key: tuple(values) for key, values in entries.items()}


def _snapshot(path: Path, container: _Container, kind: type[FileType]) -> _ContainerSnapshot:
    """Capture what a tag write on *path*, opened as the easy class *kind*, must preserve."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        trailer = _read_trailer(handle, size)
        ranges = _audio_ranges(handle, container, trailer)
        audio_digest = None if ranges is None else _hash_ranges(handle, ranges)
    return _ContainerSnapshot(
        audio_digest=audio_digest,
        entries=_unmanaged_entries(path, container, kind),
        has_id3v1=trailer.has_id3v1,
        has_apev2=trailer.has_apev2,
    )


def _snapshot_violations(before: _ContainerSnapshot, after: _ContainerSnapshot) -> list[str]:
    """Describe every way *after* differs from *before*."""
    violations: list[str] = []
    if before.audio_digest != after.audio_digest:
        violations.append("audio payload changed")
    for key in sorted(before.entries.keys() | after.entries.keys()):
        if key not in after.entries:
            violations.append(f"dropped {key}")
        elif key not in before.entries:
            violations.append(f"added {key}")
        elif before.entries[key] != after.entries[key]:
            violations.append(f"changed {key}")
    for name, had, has in (
        ("ID3v1", before.has_id3v1, after.has_id3v1),
        ("APEv2", before.has_apev2, after.has_apev2),
    ):
        if had != has:
            violations.append(f"{name} block {'removed' if had else 'added'}")
    return violations


def _readback_violations(
    path: Path,
    kind: type[FileType],
    managed: Mapping[str, list[str]],
) -> list[str]:
    """Describe every managed key on *path*, opened as *kind*, not reading back as *managed*."""
    read_back = _normalized_tags(path, kind(path)).tags
    violations: list[str] = []
    for key in sorted(MANAGED_TAGS):
        wanted = list(managed.get(key) or [])
        got = read_back.get(key, [])
        if got != wanted:
            violations.append(f"{key} reads back {got!r}, wanted {wanted!r}")
    return violations


def _plan_changes(
    path: Path,
    audio: FileType,
    managed: dict[str, list[str]],
) -> list[tuple[str, list[str] | None]]:
    """Return the ``(written key, values)`` edits that turn *audio* into *managed*.

    ``None`` values mean delete. A key whose current values already equal the target is left
    out, because the easy layers build a fresh frame on assignment and would re-encode an
    unchanged one. Sorted because a Vorbis assignment appends the field, so set order would
    shuffle them.
    """
    vorbis = isinstance(audio.tags, VCommentDict)
    # read_tags reports the date of a v2.3 TYER the easy view lost, so a target without a date
    # must plan its deletion too, or the commit keeps a date its diff removed.
    has_tyer_date = (
        _container_of(audio) is _Container.ID3
        and "date" not in audio
        and bool(_unconverted_tyer_date(path))
    )
    changes: list[tuple[str, list[str] | None]] = []
    for key in sorted(MANAGED_TAGS):
        written = _vorbis_field(key) if vorbis else key
        values = managed.get(key)
        present = written in audio or (key == "date" and has_tyer_date)
        current = [str(value) for value in audio[written]] if written in audio else []
        if values and current != list(values):
            changes.append((written, list(values)))
        elif not values and present:
            changes.append((written, None))
        # Drop the other spelling of the same concept so the file never carries two values
        # for one tag, contradicting whichever reader picks the other name. Only Vorbis has
        # a second spelling to drop: on ID3 and MP4 the easy layer owns the frame/atom name,
        # and reaching a foreign one (a TXXX:ALBUMARTISTSORT some other tagger wrote) would
        # need raw container access the easy layer does not expose.
        if vorbis and key in _VORBIS_SPELLINGS and key in audio:
            changes.append((key, None))
    return changes


def _read_id3v1(path: Path) -> dict[str, object] | None:
    """Return the frames of the ID3v1 block ending *path*, or ``None`` when it has none."""
    with path.open("rb") as handle:
        trailer = _read_trailer(handle, path.stat().st_size)
        if not trailer.has_id3v1:
            return None
        handle.seek(-_ID3V1_SIZE, os.SEEK_END)
        raw = handle.read(_ID3V1_SIZE)
        frames: dict[str, object] | None = ParseID3v1(raw)  # type: ignore[no-untyped-call]
    return frames or {}


def _rewrite_id3v1(path: Path, original: Mapping[str, object], touched: frozenset[str]) -> None:
    """Rebuild the ID3v1 block from the ID3v2 tag, keeping the old values v2 does not hold.

    mutagen rebuilds ID3v1 from ID3v2 alone, which blanks a field only ID3v1 carried. The old
    comment rides under the bare ``COMM`` key, the only one mutagen's builder reads.
    """
    try:
        tag: Any = ID3(path, load_v1=False)  # type: ignore[no-untyped-call]
        current: dict[str, object] = dict(tag.items())
    except ID3NoHeaderError:
        current = {}
    kept = {key: frame for key, frame in original.items() if key not in touched}
    block: bytes = MakeID3v1({**kept, **current})  # type: ignore[no-untyped-call]
    with path.open("r+b") as handle:
        handle.seek(-_ID3V1_SIZE, os.SEEK_END)
        handle.write(block)


def _apply_id3(path: Path, changes: list[tuple[str, list[str] | None]]) -> None:
    """Apply *changes* to the ID3v2 tag of *path* without promoting ID3v1-only values.

    mutagen's easy MP3 loader merges ID3v1 into the ID3v2 frames it holds, and a save then
    writes those truncated values into ID3v2. Loading ID3v2 alone and setting keys through
    the easy layer's own setters keeps the ID3v2 footprint to the planned keys.
    """
    original_v1 = _read_id3v1(path)
    try:
        frames: Any = ID3(path, load_v1=False)  # type: ignore[no-untyped-call]
    except ID3NoHeaderError:
        frames = ID3()  # type: ignore[no-untyped-call]
    for written, values in changes:
        if values is None:
            # A value that lived only in ID3v1 has no ID3v2 frame to delete.
            with contextlib.suppress(KeyError):
                EasyID3.Delete[written](frames, written)
        else:
            EasyID3.Set[written](frames, written, values)
    frames.save(path)
    if original_v1 is not None:
        touched = frozenset(_ID3_FRAMES[written] for written, _ in changes)
        _rewrite_id3v1(path, original_v1, touched)


def _apply_changes(
    path: Path,
    container: _Container,
    kind: type[FileType],
    changes: list[tuple[str, list[str] | None]],
) -> None:
    """Apply the planned *changes* to *path*, opened as the easy class *kind*, and save it."""
    if container is _Container.ID3:
        _apply_id3(path, changes)
        return
    audio: Any = kind(path)
    for written, values in changes:
        if values is None:
            del audio[written]
        else:
            audio[written] = values
    audio.save()


def _require_verifiable(path: Path, audio: FileType) -> _Container:
    """Return *audio*'s container, or raise :class:`TagWriteError` when a write is unverifiable."""
    container = _container_of(audio)
    if container is _Container.OTHER:
        kind = type(audio).__name__
        raise TagWriteError(path, [f"the write verifier has no layout for the {kind} container"])
    # Ogg is the one container whose payload is never located, so a miss anywhere else means
    # a write could change audio without the verifier seeing it.
    if container is not _Container.OGG:
        size = path.stat().st_size
        with path.open("rb") as handle:
            ranges = _audio_ranges(handle, container, _read_trailer(handle, size))
        if ranges is None:
            raise TagWriteError(path, [_PAYLOAD_NOT_LOCATED])
    return container


def _require_lossless_id3_save(path: Path) -> None:
    """Raise :class:`TagWriteError` when saving *path*'s ID3 tag as v2.4 drops an unmanaged frame.

    Every ID3 write saves v2.4, which discards v2.3-only frames such as ``RVAD`` and unknown
    frames, so the verifier refuses every write of such a file whatever its changes.
    """
    try:
        frames: Any = ID3(path, load_v1=False)  # type: ignore[no-untyped-call]
    except ID3NoHeaderError:
        return
    saved = io.BytesIO()
    frames.save(saved)
    saved.seek(0)
    violations = _snapshot_violations(_id3_entries_snapshot(path), _id3_entries_snapshot(saved))
    if violations:
        raise TagWriteError(path, violations)


def _id3_entries_snapshot(source: Path | BinaryIO) -> _ContainerSnapshot:
    """Snapshot only the unmanaged ID3 frames of *source*, every other part held equal."""
    entries = {key: tuple(values) for key, values in _id3_entries(source).items()}
    return _ContainerSnapshot(audio_digest=None, entries=entries, has_id3v1=False, has_apev2=False)


def ensure_writable(path: Path) -> None:
    """Raise when :func:`write_managed_tags` would refuse *path* before reading its changes.

    Staging calls this so a change on a file that can never be written is refused up front
    rather than failing on every commit.
    """
    audio = mutagen.File(path, easy=True)  # type: ignore[attr-defined]
    if audio is None:
        message = f"mutagen could not identify {path} for writing"
        raise ValueError(message)
    if _require_verifiable(path, audio) is _Container.ID3:
        _require_lossless_id3_save(path)


def write_managed_tags(path: Path, managed: dict[str, list[str]]) -> TagWriteResult:
    """Surgically write the managed-tag set on *path*, leaving all other tags intact.

    For each key in :data:`MANAGED_TAGS`: a non-empty value list in *managed* is written
    (replacing any existing values); a key absent from *managed* (or mapped to an empty
    list) is deleted from the file, so reverting to a baseline that lacked a tag removes
    a later-added one. Keys outside :data:`MANAGED_TAGS` are never read, written, or
    removed. Passing one raises :class:`ValueError` (a caller bug). Only the keys whose
    values differ are touched, so an unchanged frame keeps its encoding. On an MP3, a value
    held only in ID3v1 stays there unless the target changes it.

    Returns ``written=False`` without touching the file when nothing differs. After a write it
    returns ``written=True``, and ``audio_proven`` for an ID3 or FLAC file. The write is
    atomic: tags are applied to a sibling temp copy which then atomically replaces the
    original via :meth:`Path.replace`, so an interrupted write leaves the original file
    untouched (PLAN.md §11). Lets :class:`mutagen.MutagenError` / ``OSError``
    propagate, mirroring :func:`read_tags`.

    The temp copy is verified before the swap: the audio payload hash (not for Ogg, whose
    pages a save renumbers), every unmanaged tag entry, the presence of ID3v1 and APEv2 blocks,
    and a re-read of every managed key against *managed*. Any difference deletes the temp copy
    and raises :class:`TagWriteError` listing each one. A container the verifier has no layout
    for, or a non-Ogg file whose audio payload cannot be located, is refused before the copy.
    Measured on a 10 MB MP3 on a local SSD, the verification costs about 18 ms of a 34 ms write
    (two SHA-256 passes and two tag parses).
    """
    # Input / validation
    unknown = set(managed) - MANAGED_TAGS
    if unknown:
        message = f"refusing to write non-managed tags: {sorted(unknown)}"
        raise ValueError(message)
    original = mutagen.File(path, easy=True)  # type: ignore[attr-defined]
    if original is None:
        message = f"mutagen could not identify {path} for writing"
        raise ValueError(message)

    # Process: plan against the original (the merged view a reader sees), so a no-op never
    # copies or rewrites the file.
    changes = _plan_changes(path, original, managed)
    if not changes:
        return TagWriteResult(written=False, audio_proven=False)
    container = _require_verifiable(path, original)
    kind = type(original)
    before = _snapshot(path, container, kind)

    # Output: apply the plan to a temp copy, verify it, then atomically swap it in.
    tmp = path.with_name(f"{path.name}.tagmend.tmp")
    shutil.copy2(path, tmp)
    replaced = False
    try:
        _apply_changes(tmp, container, kind, changes)
        violations = _snapshot_violations(before, _snapshot(tmp, container, kind))
        violations += _readback_violations(tmp, kind, managed)
        if violations:
            raise TagWriteError(path, violations)
        tmp.replace(path)
        replaced = True
    finally:
        if not replaced:
            tmp.unlink(missing_ok=True)
    return TagWriteResult(written=True, audio_proven=container in _PAYLOAD_DECIDES_AUDIO)
