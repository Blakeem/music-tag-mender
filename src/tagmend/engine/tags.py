"""Read and surgically write the normalized tag set via mutagen.

Tags are read in mutagen's "easy" mode and normalized into a single canonical,
lowercase namespace so the rest of the engine never has to care about format-specific
key spellings (ID3 vs Vorbis vs MP4). A Vorbis spelling map collapses the Picard names
mutagen leaves raw. Everything else passes through lowercased unchanged.

The write path (:func:`write_managed_tags`) touches only the narrow
:data:`MANAGED_TAGS` set and writes atomically (temp copy + ``os.replace``) so a
dropped NAS connection mid-write cannot corrupt the original.
Before the swap it verifies the temp copy, and refuses with :class:`TagWriteError` when the
save changed anything outside the target. A container the verifier cannot check is refused
before any copy, and :func:`ensure_writable` lets staging refuse it before a change is queued.
An ID3 frame the caller names in ``droppable_frames`` is the one entry a save may drop.

:func:`read_pictures` lists a file's embedded pictures, each with its bytes and their SHA-256.
:func:`read_picture_data` lists them with every attribute their container stores, and
:func:`write_pictures` sets that list through the same verified temp copy.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import stat
from collections import Counter
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, BinaryIO, Final, Protocol

import mutagen

# The only shared base of every Vorbis-comment container (FLAC, OggVorbis, OggOpus, ...).
# mutagen 1.47 exposes no public predicate for "are these Vorbis comments", and naming the
# concrete subclasses instead would silently miss the ones not listed.
from mutagen._vorbis import VCommentDict
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4, EasyMP4Tags
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (  # type: ignore[attr-defined]
    APIC,
    ID3,
    ID3FileType,
    ID3NoHeaderError,
    ID3UnsupportedVersionError,
    MakeID3v1,
    ParseID3v1,
)
from mutagen.mp4 import MP4, MP4Cover, MP4Tags
from mutagen.ogg import OggFileType

from tagmend.engine.scan import TEMP_SUFFIX
from tagmend.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

    from mutagen._file import FileType

logger = get_logger(__name__)

# EasyMP4 has no ``originaldate`` mapping, and ``©day`` is ``date``, the reissue year. Picard
# writes the freeform atom lowercase, and an atom name matches case-sensitively.
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

# Navidrome shows the union of ``ALBUMARTIST`` and these two aliases, so each alias is managed
# and a stage can leave one spelling. ID3 holds each in a ``TXXX`` frame, MP4 in a freeform atom.
_ALBUMARTIST_ALIAS_FIELDS: Final[Mapping[str, str]] = {
    "album artist": "ALBUM ARTIST",
    "album_artist": "ALBUM_ARTIST",
}
_ALBUMARTIST_ALIAS_FRAMES: Final[frozenset[str]] = frozenset(
    f"TXXX:{field}" for field in _ALBUMARTIST_ALIAS_FIELDS.values()
)


def _alias_frame_of(frame_key: str) -> str | None:
    """Return the albumartist alias ``TXXX`` frame *frame_key* names in any ASCII case, or ``None``.

    TagLib upper-cases the ASCII letters of a ``TXXX`` description, so Navidrome reads
    ``TXXX:album artist`` as the ``album artist`` alias.
    """
    folded = frame_key.upper() if frame_key.isascii() else None
    return folded if folded in _ALBUMARTIST_ALIAS_FRAMES else None


def _register_alias_txxx_key(key: str, desc: str) -> None:
    """Register *key* on every ``TXXX`` frame whose description is *desc* in any ASCII case.

    A read joins every case variant, as TagLib does, and a write or a clear removes each one, so
    the file keeps one spelling.
    """
    frame_key = f"TXXX:{desc}"
    EasyID3.RegisterTXXXKey(key, desc)  # type: ignore[no-untyped-call]
    add_frame = EasyID3.Set[key]

    def variants(id3: ID3) -> list[str]:
        held = list(id3.keys())  # type: ignore[no-untyped-call]
        return [name for name in held if _alias_frame_of(name) == frame_key]

    def getter(id3: ID3, _key: str) -> list[str]:
        held = variants(id3)
        if not held:
            raise KeyError(frame_key)
        return [str(text) for variant in held for text in id3[variant]]

    def deleter(id3: ID3, _key: str) -> None:
        held = variants(id3)
        if not held:
            raise KeyError(frame_key)
        for variant in held:
            del id3[variant]  # type: ignore[no-untyped-call]

    def setter(id3: ID3, key: str, value: list[str]) -> None:
        for variant in variants(id3):
            del id3[variant]  # type: ignore[no-untyped-call]
        add_frame(id3, key, value)

    EasyID3.RegisterKey(key, getter, setter, deleter)  # type: ignore[no-untyped-call]


for _alias, _field in _ALBUMARTIST_ALIAS_FIELDS.items():
    _register_alias_txxx_key(_alias, _field)
    EasyMP4Tags.RegisterFreeformKey(_alias, _field)  # type: ignore[no-untyped-call]

# The two MusicBrainz ids in :data:`MANAGED_TAGS` that EasyMP4 has no built-in mapping for
# (the album, albumartist, artist and track ids and the album type are native). Register
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

# EasyMP4 lacks these atoms, and its own ``releasecountry`` atom is a word off from Picard's.
# Each name is Picard's (no sample held CATALOGNUMBER). ``organization`` is read, never managed.
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

# Canonical key -> Picard's Vorbis name, only where the two differ. Vorbis has no easy layer, so
# mutagen leaves these names raw in both directions.
_VORBIS_SPELLINGS: Final[Mapping[str, str]] = {
    "musicbrainz_albumtype": "releasetype",
    "musicbrainz_albumstatus": "releasestatus",
}

# Left unmapped: 245 of 479 library FLACs holding both ``organization`` and ``label`` differ,
# and ``BAND`` can differ from ``ALBUMARTIST``. Collapsing either pair loses data. ``BAND`` stays
# unmanaged, since Navidrome does not read it as an album artist.

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

# The 13 identity and MusicBrainz fields of a wrong-release stamp. Vorbis spells the album type
# ``releasetype``, ID3 needs the ``TSO2`` override and MP4 the two freeform registrations above.
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

# The provenance stamp of a file's release, which an identity fix would otherwise leave behind
# (a bootleg status, a foreign country). Managed so the fix clears it in the same commit.
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

# The albumartist aliases, each its own key so that staging can clear the ones a change leaves.
ALBUMARTIST_ALIASES: Final[frozenset[str]] = frozenset(_ALBUMARTIST_ALIAS_FIELDS)

# The 28 tags TagMend writes and reverts, each writable on all four formats. A key outside the
# set is never written or deleted. :func:`read_tags` reports every key the file holds, and
# ``versioning.managed_subset`` narrows a read to this set.
MANAGED_TAGS: Final[frozenset[str]] = (
    ORIGINAL_MANAGED_TAGS
    | _WIDENED_MANAGED_TAGS
    | RELEASE_STAMP_TAGS
    | _ARTIST_LIST_TAGS
    | ALBUMARTIST_ALIASES
)

# Which managed set governed a given revision, so revert can tell "this tag was empty then"
# from "this tag was not tracked then". Version 1 is the pre-widening five-tag set, version 2
# adds the thirteen identity fields, version 3 the seven release-stamp fields, version 4 the
# ``artists`` list, version 5 the two albumartist aliases. Every new revision is stamped with
# :data:`MANAGED_SET_VERSION`, and :func:`governed_tags` looks a stamp up here. Widening the set
# again means a new entry and a bump, never editing an existing entry, since stored revisions
# point at it.
MANAGED_SET_VERSION: Final = 5

MANAGED_SETS: Final[Mapping[int, frozenset[str]]] = {
    1: ORIGINAL_MANAGED_TAGS,
    2: ORIGINAL_MANAGED_TAGS | _WIDENED_MANAGED_TAGS,
    3: ORIGINAL_MANAGED_TAGS | _WIDENED_MANAGED_TAGS | RELEASE_STAMP_TAGS,
    4: ORIGINAL_MANAGED_TAGS | _WIDENED_MANAGED_TAGS | RELEASE_STAMP_TAGS | _ARTIST_LIST_TAGS,
    5: MANAGED_TAGS,
}


def governed_tags(managed_set: int) -> frozenset[str]:
    """Return the tags a revision stamped *managed_set* governed.

    An unknown stamp falls back to the pre-widening set: preserve rather than delete, since
    deleting is the unrecoverable direction.
    """
    return MANAGED_SETS.get(managed_set, ORIGINAL_MANAGED_TAGS)


# Which reader produced a snapshot row, so an incremental scan can spot rows left behind by
# an older one and re-read them exactly once. BUMP THIS IN THE SAME COMMIT as any change to
# what :func:`read_tags` produces (a Vorbis spelling, a format registration), or
# every already-scanned file keeps serving the old reader's output to every detector.
# The scan stores each file's ``file_pictures`` on the same read, so version 11 re-reads every
# file once to fill them.
TAG_READER_VERSION: Final = 11


@dataclass(frozen=True, slots=True)
class TrackTags:
    """A file's tags: canonical lowercase name -> ordered list of string values."""

    tags: dict[str, list[str]]


class _EasyTags(Protocol):
    """The mapping face of mutagen's easy tags and Vorbis comments, which it leaves untyped."""

    def __contains__(self, key: object, /) -> bool: ...

    def __getitem__(self, key: str, /) -> Sequence[object]: ...


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
    frames: Any = _raw_id3v2(path)
    if frames is None:
        return []
    return _first_iso_date(str(text) for frame in frames.getall("TYER") for text in frame.text)


def _raw_id3v2(source: Path | BinaryIO) -> ID3 | None:
    """Return the untranslated ID3v2 tag of *source*, or ``None`` when it has no readable one.

    mutagen's merged loader falls back to ID3v1 on an unsupported version as on a missing header.
    """
    try:
        return ID3(source, translate=False, load_v1=False)  # type: ignore[no-untyped-call]
    except (ID3NoHeaderError, ID3UnsupportedVersionError):
        return None


def _id3v2_holds_frames(path: Path) -> bool:
    """Whether the ID3v2 tag of *path* holds a frame, the test TagLib's ``isEmpty`` applies.

    TagLib keeps unknown and obsolete frames in its frame list, so they count.
    """
    frames: Any = _raw_id3v2(path)
    return frames is not None and (bool(frames.keys()) or bool(frames.unknown_frames))


def _taglib_view(path: Path, tags: _EasyTags | None) -> _EasyTags:
    """Return the part of the easy *tags* read from *path* that a TagLib reader sees.

    mutagen's easy MP3 loader fills each field ID3v2 lacks from ID3v1. TagLib, which Navidrome
    reads through, reads an ID3v2 tag that holds any frame alone and reads ID3v1 only otherwise.
    TagLib also reads an APEv2 tag before ID3v1, which this view does not model.
    """
    if tags is None:
        return {}
    if not isinstance(tags, EasyID3) or not _id3v2_holds_frames(path):
        return tags
    id3v2_only = EasyID3()  # type: ignore[no-untyped-call]
    id3v2_only.load(path, load_v1=False)
    return id3v2_only


def _easy_id3_view(frames: ID3) -> dict[str, list[str]]:
    """Read a raw ID3 tag through the getters the easy MP3 layer registers, in its key order.

    mutagen has no easy class for WAV or AIFF and hands back raw frames, so without this an MP3
    and a WAV holding one frame would read under two keys.
    """
    view: dict[str, list[str]] = {}
    for pattern, getter in EasyID3.Get.items():
        lister = EasyID3.List.get(pattern)
        keys = [pattern] if lister is None else lister(frames, pattern)
        for key in keys:
            with contextlib.suppress(KeyError):
                view[key] = getter(frames, key)
    return view


def read_tags(path: Path) -> TrackTags:
    """Read and normalize the tags on *path*.

    Returns an empty :class:`TrackTags` when mutagen cannot identify the file or it
    carries no tags. Lets :class:`mutagen.MutagenError` propagate so the caller can
    decide how to record a read failure. An ID3v2.3 ``TYER`` that starts with a full
    ``YYYY-MM-DD`` date mutagen cannot convert reads as that ``date`` when no other date exists.
    An MP3 reads ID3v2 alone when it holds a frame, else ID3v1, as TagLib reads it apart from APEv2.
    A WAV or AIFF ID3 tag reads under the MP3 keys. Any other value that is not a list of
    strings, such as a WMA attribute, is skipped.
    """
    return _normalized_tags(path, mutagen.File(path, easy=True))  # type: ignore[attr-defined]


def _normalized_tags(path: Path, audio: FileType | None) -> TrackTags:
    """Normalize the tags of *audio*, opened in easy mode from *path*, as :func:`read_tags` does.

    A write's temp copy is opened by the class identified from the original, because mutagen
    weighs the file name when it sniffs and the temp name carries no audio extension.
    """
    # Input
    raw_tags: Any = None if audio is None else audio.tags
    tags: Any = (
        _easy_id3_view(raw_tags) if isinstance(raw_tags, ID3) else _taglib_view(path, raw_tags)
    )

    # Process: a file can carry both spellings of one concept (a tagger wrote the Vorbis name,
    # an older TagMend wrote the canonical one). The Vorbis name is what every other reader
    # looks at, so it wins regardless of iteration order.
    normalized: dict[str, list[str]] = {}
    from_vorbis_spelling: set[str] = set()
    for raw_key, raw_values in tags.items():
        if not isinstance(raw_values, list) or not all(isinstance(v, str) for v in raw_values):
            continue
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


_MP4_COVER_MIMES: Final = {MP4Cover.FORMAT_JPEG: "image/jpeg", MP4Cover.FORMAT_PNG: "image/png"}

# The Vorbis comment an Ogg stream embeds each picture in, as a base64 FLAC picture block.
_VORBIS_PICTURE_FIELD: Final = "METADATA_BLOCK_PICTURE"

_PICTURE_DECODE_ERRORS: Final = (ValueError, mutagen.MutagenError)  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class EmbeddedPicture:
    """One picture embedded in an audio file.

    ``ordinal`` is its position in the container's picture list. ``picture_type`` is the ID3 or
    FLAC picture type, ``None`` for an MP4 ``covr`` item, which has none.
    """

    ordinal: int
    picture_type: int | None
    mime: str
    description: str
    size_bytes: int
    sha256: str
    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class PictureData:
    """One embedded picture with every attribute its container stores, so a restore writes it equal.

    ``picture_type`` is the ID3 or FLAC picture type, ``None`` for an MP4 ``covr`` item. An MP4
    item stores only ``image_format``, which its ``mime`` is read from. ``encoding`` is the ID3
    text encoding of the description. ``width``, ``height``, ``depth`` and ``colors`` belong to
    the FLAC picture block, which Ogg also embeds. A field the container does not store holds
    ``None`` or 0.
    """

    picture_type: int | None
    mime: str
    description: str
    data: bytes = field(repr=False)
    encoding: int | None = None
    width: int = 0
    height: int = 0
    depth: int = 0
    colors: int = 0
    image_format: int | None = None

    def to_json(self) -> str:
        """Return every attribute but the image bytes as a JSON object, keys sorted."""
        attributes = {name: getattr(self, name) for name in _PICTURE_ATTRIBUTES}
        return json.dumps(attributes, sort_keys=True)

    @classmethod
    def from_json(cls, attributes: str, data: bytes) -> PictureData:
        """Rebuild a picture from its :meth:`to_json` *attributes* and its image *data*.

        Raises :class:`ValueError` when *attributes* is not a JSON object naming exactly the
        attributes :meth:`to_json` writes.
        """
        values = json.loads(attributes)
        if not isinstance(values, dict) or set(values) != set(_PICTURE_ATTRIBUTES):
            message = f"picture attributes must name exactly {sorted(_PICTURE_ATTRIBUTES)}"
            raise ValueError(message)
        return cls(data=bytes(data), **values)


_PICTURE_ATTRIBUTES: Final = tuple(item.name for item in fields(PictureData) if item.name != "data")


def _block_picture(block: Picture) -> PictureData:
    """Return the :class:`PictureData` of a FLAC picture block."""
    return PictureData(
        picture_type=int(block.type),
        mime=block.mime,
        description=block.desc,
        data=bytes(block.data),
        width=block.width,
        height=block.height,
        depth=block.depth,
        colors=block.colors,
    )


def _decode_vorbis_picture(value: str) -> Picture:
    """Decode one Ogg ``metadata_block_picture`` value as a FLAC picture block."""
    return Picture(base64.b64decode(value))  # type: ignore[no-untyped-call]


def _vorbis_picture_data(path: Path, values: Sequence[str]) -> list[tuple[int, PictureData]]:
    """Decode each Ogg ``metadata_block_picture`` value of *path*, with its container position.

    A value that does not decode is skipped, and the next keeps its own container position.
    """
    pictures: list[tuple[int, PictureData]] = []
    for ordinal, value in enumerate(values):
        try:
            block = _decode_vorbis_picture(value)
        except _PICTURE_DECODE_ERRORS as exc:
            logger.warning("skipped undecodable picture %d of %s: %s", ordinal, path, exc)
            continue
        pictures.append((ordinal, _block_picture(block)))
    return pictures


def _indexed_picture_data(path: Path, audio: FileType | None) -> list[tuple[int, PictureData]]:
    """Return each picture *audio*, opened from *path*, embeds, with its container position."""
    if audio is None:
        return []
    if isinstance(audio, FLAC):
        return list(enumerate(_block_picture(block) for block in audio.pictures))
    tags: Any = audio.tags
    if isinstance(tags, ID3):
        frames: Any = tags.getall("APIC")  # type: ignore[no-untyped-call]
        return [
            (
                ordinal,
                PictureData(
                    picture_type=int(frame.type),
                    mime=frame.mime,
                    description=frame.desc,
                    data=bytes(frame.data),
                    encoding=int(frame.encoding),
                ),
            )
            for ordinal, frame in enumerate(frames)
        ]
    if isinstance(tags, MP4Tags):
        items: Any = tags.get("covr") or []  # type: ignore[no-untyped-call]
        return [
            (
                ordinal,
                PictureData(
                    picture_type=None,
                    mime=_MP4_COVER_MIMES.get(item.imageformat, ""),
                    description="",
                    data=bytes(item),
                    image_format=int(item.imageformat),
                ),
            )
            for ordinal, item in enumerate(items)
        ]
    if isinstance(tags, VCommentDict):
        values: Any = tags.get(_VORBIS_PICTURE_FIELD, [])  # type: ignore[no-untyped-call]
        return _vorbis_picture_data(path, values)
    return []


def read_picture_data(path: Path) -> list[PictureData]:
    """Return the pictures embedded in *path* with every attribute their container stores.

    The list holds the pictures :func:`read_pictures` reports, in the same order. Raises as
    :func:`read_tags` does for an unreadable file.
    """
    audio: FileType | None = mutagen.File(path)  # type: ignore[attr-defined]
    return [picture for _, picture in _indexed_picture_data(path, audio)]


def read_pictures(path: Path) -> list[EmbeddedPicture]:
    """Return the pictures embedded in *path*, in its container's order.

    They are the ID3v2 ``APIC`` frames, the FLAC picture blocks, the MP4 ``covr`` items or the
    Ogg ``metadata_block_picture`` comments. mutagen loads an ID3v2.2 ``PIC`` frame as ``APIC``.
    A file mutagen cannot identify holds none. Raises as :func:`read_tags` does for an
    unreadable file.
    """
    audio: FileType | None = mutagen.File(path)  # type: ignore[attr-defined]
    return [
        EmbeddedPicture(
            ordinal=ordinal,
            picture_type=picture.picture_type,
            mime=picture.mime,
            description=picture.description,
            size_bytes=len(picture.data),
            sha256=hashlib.sha256(picture.data).hexdigest(),
            data=picture.data,
        )
        for ordinal, picture in _indexed_picture_data(path, audio)
    ]


def has_embedded_picture(path: Path) -> bool:
    """Whether *path* holds a picture Navidrome can show as its album's cover.

    Raises as :func:`read_tags` does for an unreadable file.
    """
    return bool(read_pictures(path))


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
    """What :func:`write_managed_tags` or :func:`write_pictures` did to the file.

    ``audio_proven`` holds after a write whose verified payload hash decides the decoded audio,
    so a fingerprint taken before the write still describes the file. ``dropped_frames`` holds
    the sorted ID3 frame ids the write removed under ``droppable_frames``.
    """

    written: bool
    audio_proven: bool
    dropped_frames: tuple[str, ...] = ()


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


# mutagen keeps a frame it cannot parse as raw bytes, so its entry key carries only its id.
_UNKNOWN_FRAME_PREFIX: Final = "unknown frame "


def _frame_id(entry_key: str) -> str:
    """Return the ID3 frame id an :func:`_id3_entries` key names."""
    return entry_key.removeprefix(_UNKNOWN_FRAME_PREFIX)


def _id3_entries(source: Path | BinaryIO) -> dict[str, list[str]]:
    """Return the unmanaged ID3v2 frames of *source* as ``frame key -> value digests``.

    *source* is a path or a file object, so :func:`ensure_writable` can read an in-memory save.

    Loaded untranslated so a frame the v2.4 upgrade removes is still seen, then upgraded so a
    v2.3 frame compares equal to the v2.4 successor a save writes in its place. An older frame
    whose value the upgrade does not carry into a successor is kept under its own id, so the
    saved copy, which lacks it, reads as dropping it. ID3v1 is left out: the snapshot compares
    only its presence.
    """
    frames: Any = _raw_id3v2(source)
    if frames is None:
        return {}
    entries: dict[str, list[str]] = {}
    id_size, header_size = (3, 6) if frames.version < (2, 3, 0) else (4, 10)
    for data in frames.unknown_frames:
        key = f"{_UNKNOWN_FRAME_PREFIX}{bytes(data[:id_size]).decode('latin-1')}"
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
        if key not in managed and _alias_frame_of(key) is None:
            entries.setdefault(key, []).append(_digest(repr(frame)))
    return entries


# The unmanaged entry each container keeps the pictures :func:`write_pictures` sets under. ID3
# keys each ``APIC`` frame by its description, so its entry is a key prefix.
_ID3_PICTURE_PREFIX: Final = "APIC:"
_FLAC_PICTURE_ENTRY: Final = "FLAC picture block"
_PICTURE_ENTRIES: Final[Mapping[_Container, str]] = {
    _Container.FLAC: _FLAC_PICTURE_ENTRY,
    _Container.MP4: "covr",
    _Container.OGG: _VORBIS_PICTURE_FIELD,
}


def _raw_kind(kind: type[FileType]) -> type[FileType]:
    """Return the class that opens a non-ID3 file with the entries of the easy class *kind* raw.

    MP4 swaps in the raw class, because the easy layer renames the atoms.
    """
    return MP4 if issubclass(kind, EasyMP4) else kind


def _mutagen_entries(
    path: Path,
    container: _Container,
    kind: type[FileType],
) -> dict[str, list[str]]:
    """Return the unmanaged Vorbis comments or MP4 atoms (plus FLAC pictures) of *path*.

    *kind* is the easy class the original opened as.
    """
    entries: dict[str, list[str]] = {}
    audio: Any = _raw_kind(kind)(path)
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
            entries.setdefault(_FLAC_PICTURE_ENTRY, []).append(_digest(picture.write()))
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


def _dropped_keys(before: _ContainerSnapshot, after: _ContainerSnapshot) -> set[str]:
    """Return the unmanaged entry keys *before* holds and *after* lacks."""
    return before.entries.keys() - after.entries.keys()


def _snapshot_violations(
    before: _ContainerSnapshot,
    after: _ContainerSnapshot,
    *,
    droppable_frames: frozenset[str] = frozenset(),
) -> list[str]:
    """Describe every way *after* differs from *before*, except a drop *droppable_frames* names."""
    allowed = {key for key in _dropped_keys(before, after) if _frame_id(key) in droppable_frames}
    violations: list[str] = []
    if before.audio_digest != after.audio_digest:
        violations.append("audio payload changed")
    for key in sorted((before.entries.keys() | after.entries.keys()) - allowed):
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
    container: _Container,
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
    if violations and container is _Container.ID3 and not _id3v2_holds_frames(path):
        violations.append("emptying ID3v2 exposes the values only ID3v1 holds")
    return violations


def _plan_changes(
    path: Path,
    container: _Container,
    tags: _EasyTags,
    managed: dict[str, list[str]],
) -> list[tuple[str, list[str] | None]]:
    """Return the ``(written key, values)`` edits that turn the easy *tags* into *managed*.

    ``None`` values mean delete. A key whose current values already equal the target is left
    out, because the easy layers build a fresh frame on assignment and would re-encode an
    unchanged one. Sorted because a Vorbis assignment appends the field, so set order would
    shuffle them.
    """
    vorbis = isinstance(tags, VCommentDict)
    # read_tags reports the date of a v2.3 TYER the easy view lost, so a target without a date
    # must plan its deletion too, or the commit keeps a date its diff removed.
    has_tyer_date = (
        container is _Container.ID3 and "date" not in tags and bool(_unconverted_tyer_date(path))
    )
    changes: list[tuple[str, list[str] | None]] = []
    for key in sorted(MANAGED_TAGS):
        written = _vorbis_field(key) if vorbis else key
        values = managed.get(key)
        present = written in tags or (key == "date" and has_tyer_date)
        current = [str(value) for value in tags[written]] if written in tags else []
        if values and current != list(values):
            changes.append((written, list(values)))
        elif not values and present:
            changes.append((written, None))
        # Drop the other spelling of the same concept so the file never carries two values
        # for one tag, contradicting whichever reader picks the other name. Only Vorbis has
        # a second spelling to drop: on ID3 and MP4 the easy layer owns the frame/atom name,
        # and reaching a foreign one (a TXXX:ALBUMARTISTSORT some other tagger wrote) would
        # need raw container access the easy layer does not expose.
        if vorbis and key in _VORBIS_SPELLINGS and key in tags:
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


@contextlib.contextmanager
def _edited_id3v2(path: Path, touched: frozenset[str]) -> Iterator[Any]:
    """Yield the ID3v2 tag of *path* loaded alone, then save it and rebuild ID3v1 around *touched*.

    mutagen's easy MP3 loader merges ID3v1 into the ID3v2 frames it holds, and a save then
    writes those truncated values into ID3v2. *touched* names the frames whose old ID3v1 value
    the rebuild must not keep.
    """
    original_v1 = _read_id3v1(path)
    try:
        frames: Any = ID3(path, load_v1=False)  # type: ignore[no-untyped-call]
    except ID3NoHeaderError:
        frames = ID3()  # type: ignore[no-untyped-call]
    yield frames
    frames.save(path)
    if original_v1 is not None:
        _rewrite_id3v1(path, original_v1, touched)


def _apply_id3(path: Path, changes: list[tuple[str, list[str] | None]]) -> None:
    """Apply *changes* to the ID3v2 tag of *path*, writing only the planned keys.

    Setting keys through the easy layer's own setters on the ID3v2 tag alone keeps the ID3v2
    footprint to the planned keys.
    """
    touched = frozenset(_ID3_FRAMES[written] for written, _ in changes)
    with _edited_id3v2(path, touched) as frames:
        for written, values in changes:
            if values is None:
                # A value that lived only in ID3v1 has no ID3v2 frame to delete.
                with contextlib.suppress(KeyError):
                    EasyID3.Delete[written](frames, written)
            else:
                EasyID3.Set[written](frames, written, values)


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


def _discard_temp(tmp: Path) -> None:
    """Remove a write's temp copy, logging a failure so the caller's own exception propagates.

    Windows refuses to unlink a read-only file, and ``shutil.copy2`` copies that bit.
    """
    try:
        with contextlib.suppress(FileNotFoundError):
            tmp.chmod(tmp.stat().st_mode | stat.S_IWRITE)
            tmp.unlink()
    except OSError as exc:
        logger.warning("could not remove temp copy %s: %s", tmp, exc)


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


def _require_lossless_id3_save(path: Path, *, droppable_frames: frozenset[str]) -> None:
    """Raise :class:`TagWriteError` when saving *path*'s ID3 tag as v2.4 drops an unmanaged frame.

    Every ID3 write saves v2.4, which discards v2.3-only frames such as ``RVAD`` and unknown
    frames, so the verifier refuses every write of such a file whatever its changes. A frame
    whose id *droppable_frames* names is the exception.
    """
    try:
        frames: Any = ID3(path, load_v1=False)  # type: ignore[no-untyped-call]
    except ID3NoHeaderError:
        return
    saved = io.BytesIO()
    frames.save(saved)
    saved.seek(0)
    violations = _snapshot_violations(
        _id3_entries_snapshot(path),
        _id3_entries_snapshot(saved),
        droppable_frames=droppable_frames,
    )
    if violations:
        raise TagWriteError(path, violations)


def _id3_entries_snapshot(source: Path | BinaryIO) -> _ContainerSnapshot:
    """Snapshot only the unmanaged ID3 frames of *source*, every other part held equal."""
    entries = {key: tuple(values) for key, values in _id3_entries(source).items()}
    return _ContainerSnapshot(audio_digest=None, entries=entries, has_id3v1=False, has_apev2=False)


def _open_for_writing(path: Path) -> FileType:
    """Open *path* in easy mode, raising :class:`ValueError` when mutagen cannot identify it."""
    audio: FileType | None = mutagen.File(path, easy=True)  # type: ignore[attr-defined]
    if audio is None:
        message = f"mutagen could not identify {path} for writing"
        raise ValueError(message)
    return audio


def _writable_audio(path: Path, *, droppable_frames: frozenset[str]) -> FileType:
    """Open *path*, raising where every write of it would be refused whatever its changes."""
    audio = _open_for_writing(path)
    if _require_verifiable(path, audio) is _Container.ID3:
        _require_lossless_id3_save(path, droppable_frames=droppable_frames)
    return audio


def _require_decodable_pictures(path: Path, audio: FileType) -> None:
    """Raise :class:`TagWriteError` when an Ogg picture of *path* does not decode.

    A picture write rebuilds every ``metadata_block_picture`` value from the decoded pictures,
    so it would drop the one that does not decode.
    """
    tags: Any = audio.tags
    if _container_of(audio) is not _Container.OGG or tags is None:
        return
    violations: list[str] = []
    for ordinal, value in enumerate(tags.get(_VORBIS_PICTURE_FIELD, [])):
        try:
            _decode_vorbis_picture(value)
        except _PICTURE_DECODE_ERRORS:
            violations.append(f"picture {ordinal} does not decode, and a picture write drops it")
    if violations:
        raise TagWriteError(path, violations)


def ensure_writable(path: Path, *, droppable_frames: frozenset[str] = frozenset()) -> None:
    """Raise when :func:`write_managed_tags` would refuse *path* before reading its changes.

    Staging calls this so a change on a file that can never be written is refused up front
    rather than failing on every commit. *droppable_frames* is the set the write is given.
    """
    _writable_audio(path, droppable_frames=droppable_frames)


def ensure_pictures_writable(
    path: Path,
    *,
    droppable_frames: frozenset[str] = frozenset(),
) -> None:
    """Raise when :func:`write_pictures` would refuse *path* before reading its pictures.

    It refuses what :func:`ensure_writable` refuses, and an Ogg picture that does not decode.
    *droppable_frames* is the set the write is given.
    """
    audio = _writable_audio(path, droppable_frames=droppable_frames)
    _require_decodable_pictures(path, audio)


def check_round_trip(path: Path, managed: Mapping[str, list[str]]) -> None:
    """Raise :class:`ValueError` for a value in *managed* the container of *path* alters.

    The ID3 and MP4 easy layers parse what they store, such as a timestamp or a track pair, and
    :func:`read_tags` reads it back reformatted, so the write verifier would refuse every commit.
    The values pass through the same easy setters and getters the writer and the reader use.
    """
    audio = mutagen.File(path, easy=True)  # type: ignore[attr-defined]
    container = None if audio is None else _container_of(audio)
    probe: Any
    easy: Any
    if container is _Container.ID3:
        probe, easy = ID3(), EasyID3  # type: ignore[no-untyped-call]
    elif container is _Container.MP4:
        probe, easy = MP4Tags(), EasyMP4Tags  # type: ignore[no-untyped-call]
    else:
        return
    for key, values in sorted(managed.items()):
        if not values:
            continue
        try:
            easy.Set[key](probe, key, list(values))
            stored = [str(value) for value in easy.Get[key](probe, key)]
        except ValueError as exc:
            message = f"{key}={values!r} cannot be stored in {container} tags: {exc}"
            raise ValueError(message) from exc
        if stored != list(values):
            message = f"{key}={values!r} reads back from {container} tags as {stored!r}"
            raise ValueError(message)


def _write_verified_copy(  # noqa: PLR0913 - the steps each writer supplies, keyword-only
    path: Path,
    container: _Container,
    *,
    snapshot: Callable[[Path], _ContainerSnapshot],
    apply: Callable[[Path], None],
    verify: Callable[[Path], list[str]],
    droppable_frames: frozenset[str],
) -> TagWriteResult:
    """Apply a change to a sibling temp copy of *path*, verify it, then flush and swap it in.

    *apply* changes the copy. *snapshot* captures what the change must leave alone, and *verify*
    lists what else the copy got wrong. A difference raises :class:`TagWriteError` and leaves the
    original untouched. An ID3 frame whose id *droppable_frames* names may be dropped. It is
    listed in ``dropped_frames`` and a warning names it, since no revert restores it.
    """
    # Input: a Vorbis field or an MP4 atom can share its name with an ID3 frame id.
    droppable = droppable_frames if container is _Container.ID3 else frozenset()
    before = snapshot(path)
    tmp = path.with_name(path.name + TEMP_SUFFIX)
    dropped_frames: tuple[str, ...] = ()
    replaced = False

    # Process
    _discard_temp(tmp)
    try:
        shutil.copy2(path, tmp)
        tmp.chmod(tmp.stat().st_mode | stat.S_IWRITE)
        apply(tmp)
        after = snapshot(tmp)
        violations = _snapshot_violations(before, after, droppable_frames=droppable) + verify(tmp)
        if violations:
            raise TagWriteError(path, violations)
        dropped_frames = tuple(sorted({_frame_id(key) for key in _dropped_keys(before, after)}))
        # The swap frees the original's data at once, so the new bytes must be on disk first.
        # Windows flushes only a handle opened for writing.
        with tmp.open("r+b") as handle:
            os.fsync(handle.fileno())
        tmp.replace(path)
        replaced = True
    finally:
        if not replaced:
            _discard_temp(tmp)

    # Output
    if dropped_frames:
        logger.warning(
            "dropped ID3 frame(s) %s from %s, and no revert can restore them",
            ", ".join(dropped_frames),
            path,
        )
    return TagWriteResult(
        written=True,
        audio_proven=container in _PAYLOAD_DECIDES_AUDIO,
        dropped_frames=dropped_frames,
    )


def write_managed_tags(
    path: Path,
    managed: dict[str, list[str]],
    *,
    droppable_frames: frozenset[str] = frozenset(),
) -> TagWriteResult:
    """Surgically write the managed-tag set on *path*, leaving all other tags intact.

    For each key in :data:`MANAGED_TAGS`, a non-empty value list in *managed* is written, and a
    key absent from *managed* or mapped to an empty list is deleted. Keys outside
    :data:`MANAGED_TAGS` are never written or removed, and passing one raises
    :class:`ValueError`. Only keys whose values differ from what :func:`read_tags` reports are
    touched, so an unchanged frame keeps its encoding. On an MP3, a write that creates the first
    ID3v2 frame writes every value of *managed* into ID3v2, since TagLib then stops reading ID3v1.

    The tags go to a sibling temp copy that is verified, flushed and swapped in with
    :meth:`Path.replace`, so an interrupted write leaves the original untouched. A container the
    verifier has no layout for, or a non-Ogg file whose audio payload cannot be located, is
    refused before the copy. A verification difference raises :class:`TagWriteError`. An ID3
    frame whose id *droppable_frames* names may be dropped. It is listed in ``dropped_frames``
    and a warning names it, since no revert restores it. :class:`mutagen.MutagenError` and
    ``OSError`` propagate.

    Returns ``written=False`` without touching the file when nothing differs, else
    ``written=True`` with ``audio_proven`` set for an ID3 or FLAC file.
    """
    # Input / validation
    unknown = set(managed) - MANAGED_TAGS
    if unknown:
        message = f"refusing to write non-managed tags: {sorted(unknown)}"
        raise ValueError(message)
    original = _open_for_writing(path)

    # Process: plan against what read_tags reports, so a no-op never copies or rewrites the file.
    container = _container_of(original)
    changes = _plan_changes(path, container, _taglib_view(path, original.tags), managed)
    if changes and container is _Container.ID3 and not _id3v2_holds_frames(path):
        # TagLib stops reading ID3v1 once ID3v2 holds a frame. A deletion still goes through,
        # since rebuilding ID3v1 blanks each touched field.
        deletions = [change for change in changes if change[1] is None]
        changes = _plan_changes(path, container, {}, managed) + deletions
    if not changes:
        return TagWriteResult(written=False, audio_proven=False)
    _require_verifiable(path, original)
    kind = type(original)

    # Output
    return _write_verified_copy(
        path,
        container,
        snapshot=lambda source: _snapshot(source, container, kind),
        apply=lambda tmp: _apply_changes(tmp, container, kind, changes),
        verify=lambda tmp: _readback_violations(tmp, container, kind, managed),
        droppable_frames=droppable_frames,
    )


def _is_picture_entry(container: _Container, key: str) -> bool:
    """Whether the unmanaged entry *key* of *container* holds the pictures of a picture write."""
    if container is _Container.ID3:
        return key.startswith(_ID3_PICTURE_PREFIX)
    return key == _PICTURE_ENTRIES.get(container)


def _without_pictures(snapshot: _ContainerSnapshot, container: _Container) -> _ContainerSnapshot:
    """Return *snapshot* without the unmanaged entries holding the pictures of *container*."""
    entries = {
        key: values
        for key, values in snapshot.entries.items()
        if not _is_picture_entry(container, key)
    }
    return replace(snapshot, entries=entries)


def _unfit_pictures(container: _Container, pictures: Sequence[PictureData]) -> list[int]:
    """Return the positions of *pictures* lacking an attribute *container* stores per picture."""
    unfit: list[int] = []
    for position, picture in enumerate(pictures):
        if container is _Container.ID3:
            fits = picture.encoding is not None and picture.picture_type is not None
        elif container is _Container.MP4:
            fits = picture.image_format is not None
        else:
            fits = picture.picture_type is not None
        if not fits:
            unfit.append(position)
    return unfit


def _picture_view(path: Path, container: _Container, kind: type[FileType]) -> FileType:
    """Open *path*, identified as the easy class *kind*, with its pictures reachable.

    The easy ID3 tag hides ``APIC`` frames, so an ID3 file loads its raw tag class instead.
    """
    if container is _Container.ID3:
        return kind(path, ID3=ID3)
    return _raw_kind(kind)(path)


def _view_pictures(path: Path, container: _Container, kind: type[FileType]) -> list[PictureData]:
    """Return the pictures of *path*, opened as :func:`_picture_view` opens it."""
    audio = _picture_view(path, container, kind)
    return [picture for _, picture in _indexed_picture_data(path, audio)]


def _same_pictures(
    container: _Container,
    left: Sequence[PictureData],
    right: Sequence[PictureData],
) -> bool:
    """Whether *left* and *right* are one picture list of *container*.

    ID3 compares them as a multiset, since an ID3 save orders its frames by size and key.
    """
    if container is _Container.ID3:
        return Counter(left) == Counter(right)
    return list(left) == list(right)


def _picture_labels(pictures: Sequence[PictureData]) -> str:
    """Label each picture by its attributes and the SHA-256 of its bytes."""
    labels = [f"{picture.to_json()} sha256 {_digest(picture.data)}" for picture in pictures]
    return f"[{', '.join(labels)}]"


def _picture_violations(
    path: Path,
    container: _Container,
    kind: type[FileType],
    wanted: Sequence[PictureData],
) -> list[str]:
    """Describe how the pictures of *path*, opened as *kind*, differ from *wanted*."""
    got = _view_pictures(path, container, kind)
    if _same_pictures(container, got, wanted):
        return []
    return [f"pictures read back as {_picture_labels(got)}, wanted {_picture_labels(wanted)}"]


def _picture_block(picture: PictureData) -> Picture:
    """Build the FLAC picture block that stores *picture*."""
    block: Any = Picture()  # type: ignore[no-untyped-call]
    block.type = picture.picture_type
    block.mime = picture.mime
    block.desc = picture.description
    block.width = picture.width
    block.height = picture.height
    block.depth = picture.depth
    block.colors = picture.colors
    block.data = picture.data
    return block  # type: ignore[no-any-return]


def _entry_values(container: _Container, pictures: Sequence[PictureData]) -> list[object]:
    """Return the MP4 ``covr`` items or the Ogg comment values that store *pictures*."""
    if container is _Container.MP4:
        return [
            MP4Cover(picture.data, imageformat=picture.image_format)  # type: ignore[no-untyped-call]
            for picture in pictures
        ]
    blocks = [_picture_block(picture).write() for picture in pictures]  # type: ignore[no-untyped-call]
    return [base64.b64encode(block).decode("ascii") for block in blocks]


def _apply_pictures(
    path: Path,
    container: _Container,
    kind: type[FileType],
    pictures: Sequence[PictureData],
) -> None:
    """Set the picture list of *path*, identified as the easy class *kind*, and save it."""
    if container is _Container.ID3:
        # read_tags reports the date of a v2.3 TYER the v2.4 save discards, so it lands as TDRC.
        tyer_date = _unconverted_tyer_date(path)
        with _edited_id3v2(path, frozenset()) as frames:
            if tyer_date and not frames.getall("TDRC"):
                EasyID3.Set["date"](frames, "date", tyer_date)
            frames.delall("APIC")
            for picture in pictures:
                frames.add(
                    APIC(  # type: ignore[no-untyped-call]
                        encoding=picture.encoding,
                        mime=picture.mime,
                        type=picture.picture_type,
                        desc=picture.description,
                        data=picture.data,
                    ),
                )
        return
    audio: Any = _picture_view(path, container, kind)
    if container is _Container.FLAC:
        audio.clear_pictures()
        for picture in pictures:
            audio.add_picture(_picture_block(picture))
    elif pictures:
        if audio.tags is None:
            audio.add_tags()
        audio.tags[_PICTURE_ENTRIES[container]] = _entry_values(container, pictures)
    elif audio.tags is not None:
        with contextlib.suppress(KeyError):
            del audio.tags[_PICTURE_ENTRIES[container]]
    audio.save()


def write_pictures(
    path: Path,
    pictures: Sequence[PictureData],
    *,
    droppable_frames: frozenset[str] = frozenset(),
) -> TagWriteResult:
    """Set the pictures embedded in *path* to exactly *pictures*, in order, leaving all else intact.

    The pictures are the entries :func:`read_picture_data` reads. An ID3 save orders its
    ``APIC`` frames by size and key, so on ID3 only the multiset of *pictures* is set. A picture
    lacking an attribute the container stores per picture raises :class:`ValueError`.

    The write runs through the verified temp copy :func:`write_managed_tags` uses, and refuses
    what it refuses. The verifier compares every unmanaged entry but the pictures, reads the
    managed tags back unchanged and reads the pictures back as *pictures*. It also refuses an Ogg
    file holding a picture that does not decode, since the rewrite would drop it.

    Returns ``written=False`` without touching the file when the list already equals
    *pictures*, else ``written=True`` with ``audio_proven`` set for an ID3 or FLAC file.
    """
    # Input / validation
    original = _open_for_writing(path)
    container = _container_of(original)
    kind = type(original)
    target = list(pictures)
    unfit = _unfit_pictures(container, target)
    if unfit:
        message = f"pictures {unfit} lack an attribute the {container} container stores"
        raise ValueError(message)

    # Process: compare with the current list, so a no-op never copies or rewrites the file.
    if _same_pictures(container, _view_pictures(path, container, kind), target):
        return TagWriteResult(written=False, audio_proven=False)
    _require_verifiable(path, original)
    _require_decodable_pictures(path, original)
    managed = _normalized_tags(path, original).tags

    # Output
    return _write_verified_copy(
        path,
        container,
        snapshot=lambda source: _without_pictures(_snapshot(source, container, kind), container),
        apply=lambda tmp: _apply_pictures(tmp, container, kind, target),
        verify=lambda tmp: (
            _readback_violations(tmp, container, kind, managed)
            + _picture_violations(tmp, container, kind, target)
        ),
        droppable_frames=droppable_frames,
    )
