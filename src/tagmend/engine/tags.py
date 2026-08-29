"""Read and surgically write the normalized tag set via mutagen (M1 read + M3 write).

Tags are read in mutagen's "easy" mode and normalized into a single canonical,
lowercase namespace so the rest of the engine never has to care about format-specific
key spellings (ID3 vs Vorbis vs MP4). A small alias map collapses a few well-known
synonyms; everything else passes through lowercased unchanged.

The write path (:func:`write_managed_tags`, M3) touches only the narrow
:data:`MANAGED_TAGS` set and writes atomically (temp copy + ``os.replace``) so a
dropped NAS connection mid-write cannot corrupt the original — see PLAN.md §7 and §11.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import mutagen

# The only shared base of every Vorbis-comment container (FLAC, OggVorbis, OggOpus, ...).
# mutagen 1.47 exposes no public predicate for "are these Vorbis comments", and naming the
# concrete subclasses instead would silently miss the ones not listed.
from mutagen._vorbis import VCommentDict
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4Tags

from tagmend.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

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

# Raw (already-lowercased) key -> canonical key. Kept deliberately small.
_ALIASES: Final[dict[str, str]] = {
    "album artist": "albumartist",
    "band": "albumartist",
}

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
_RELEASE_STAMP_TAGS: Final[frozenset[str]] = frozenset(
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

# The set of tags TagMend is allowed to write/revert (25 = 5 original + 13 identity + 7 release
# stamp). A CLOSED set: anything outside it (``comment``/``composer``/art…) is never read,
# written, or deleted, and every key here MUST be provably writable on all four formats. The
# mismatch-fix flow can repair a poisoned release in one commit, and revert restores every field
# the target revision's own managed set governed (see
# :func:`tagmend.engine.versioning._revert_target_tags`).
# ``date`` (reissue year, MP4 ``©day``) and ``originaldate`` (original year, MP4 freeform) are
# BOTH managed and kept distinct.
MANAGED_TAGS: Final[frozenset[str]] = (
    ORIGINAL_MANAGED_TAGS | _WIDENED_MANAGED_TAGS | _RELEASE_STAMP_TAGS
)

# Which managed set governed a given revision, so revert can tell "this tag was empty then"
# from "this tag was not tracked then". Version 1 is the pre-widening five-tag set, version 2
# adds the thirteen identity fields, version 3 the seven release-stamp fields. Every new
# revision is stamped with :data:`MANAGED_SET_VERSION`;
# :func:`tagmend.engine.versioning._revert_target_tags` looks the stamp up here. Widening the
# set again means a new entry and a bump — never editing an existing entry, since stored
# revisions point at it.
MANAGED_SET_VERSION: Final = 3

MANAGED_SETS: Final[Mapping[int, frozenset[str]]] = {
    1: ORIGINAL_MANAGED_TAGS,
    2: ORIGINAL_MANAGED_TAGS | _WIDENED_MANAGED_TAGS,
    3: MANAGED_TAGS,
}

# Which reader produced a snapshot row, so an incremental scan can spot rows left behind by
# an older one and re-read them exactly once. BUMP THIS IN THE SAME COMMIT as any change to
# what :func:`read_tags` produces — the managed set, an alias, a format registration — or
# every already-scanned file keeps serving the old reader's output to every detector.
TAG_READER_VERSION: Final = 4


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


def _canonical_key(lowered_key: str) -> str:
    """Resolve an already-lowercased raw key through the Vorbis and alias maps."""
    canonical = _VORBIS_TO_CANONICAL.get(lowered_key)
    if canonical is not None:
        return canonical
    return _ALIASES.get(lowered_key, lowered_key)


def read_tags(path: Path) -> TrackTags:
    """Read and normalize the tags on *path*.

    Returns an empty :class:`TrackTags` when mutagen cannot identify the file or it
    carries no tags. Lets :class:`mutagen.MutagenError` propagate so the caller can
    decide how to record a read failure.
    """
    # Input
    audio = mutagen.File(path, easy=True)  # type: ignore[attr-defined]
    if audio is None or audio.tags is None:
        return TrackTags({})

    # Process — a file can carry both spellings of one concept (a tagger wrote the Vorbis name,
    # an older TagMend wrote the canonical one). The Vorbis name is what every other reader
    # looks at, so it wins regardless of iteration order.
    normalized: dict[str, list[str]] = {}
    from_vorbis_spelling: set[str] = set()
    for raw_key, raw_values in audio.tags.items():
        lowered = str(raw_key).lower()
        key = _canonical_key(lowered)
        native = lowered in _VORBIS_TO_CANONICAL
        if key in from_vorbis_spelling and not native:
            continue
        normalized[key] = [str(value) for value in raw_values]
        if native:
            from_vorbis_spelling.add(key)

    # Output
    return TrackTags(normalized)


def write_managed_tags(path: Path, managed: dict[str, list[str]]) -> None:
    """Surgically write the managed-tag set on *path*, leaving all other tags intact.

    For each key in :data:`MANAGED_TAGS`: a non-empty value list in *managed* is written
    (replacing any existing values); a key absent from *managed* (or mapped to an empty
    list) is deleted from the file, so reverting to a baseline that lacked a tag removes
    a later-added one. Keys outside :data:`MANAGED_TAGS` are never read, written, or
    removed; passing one raises :class:`ValueError` (a caller bug).

    The write is atomic: tags are applied to a sibling temp copy which then atomically
    replaces the original via :meth:`Path.replace`, so an interrupted write leaves the
    original file untouched (PLAN.md §11). Lets :class:`mutagen.MutagenError` / ``OSError``
    propagate, mirroring :func:`read_tags`.
    """
    # Input / validation
    unknown = set(managed) - MANAGED_TAGS
    if unknown:
        message = f"refusing to write non-managed tags: {sorted(unknown)}"
        raise ValueError(message)

    # Process — apply to a temp copy, then atomically swap it in.
    tmp = path.with_name(f"{path.name}.tagmend.tmp")
    shutil.copy2(path, tmp)
    replaced = False
    try:
        audio = mutagen.File(tmp, easy=True)  # type: ignore[attr-defined]
        if audio is None:
            message = f"mutagen could not identify {path} for writing"
            raise ValueError(message)
        vorbis = isinstance(audio.tags, VCommentDict)
        for key in MANAGED_TAGS:
            written = _vorbis_field(key) if vorbis else key
            values = managed.get(key)
            if values:
                audio[written] = list(values)
            elif written in audio:
                del audio[written]
            # Drop the other spelling of the same concept so the file never carries two values
            # for one tag, contradicting whichever reader picks the other name. Only Vorbis has
            # a second spelling to drop: on ID3 and MP4 the easy layer owns the frame/atom name,
            # and reaching a foreign one (a TXXX:ALBUMARTISTSORT some other tagger wrote) would
            # need raw container access the easy layer does not expose.
            if vorbis and key in _VORBIS_SPELLINGS and key in audio:
                del audio[key]
        audio.save()
        tmp.replace(path)
        replaced = True
    finally:
        if not replaced:
            tmp.unlink(missing_ok=True)
