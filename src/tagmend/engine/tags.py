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
from mutagen.easymp4 import EasyMP4Tags

from tagmend.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

logger = get_logger(__name__)

# ``originaldate`` (the original/first-release year) is native on ID3 (``TDOR``) and Vorbis
# (``ORIGINALDATE``) via mutagen's easy mode, but MP4 has no built-in easy mapping. Register
# it ONCE at module load so read and write agree on the iTunes freeform atom
# ``----:com.apple.iTunes:ORIGINALDATE`` — never ``©day`` (that is ``date``, the reissue
# year). Registration is idempotent; importing this module is the single place it happens.
EasyMP4Tags.RegisterFreeformKey("originaldate", "ORIGINALDATE")  # type: ignore[no-untyped-call]

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

# Raw (already-lowercased) key -> canonical key. Kept deliberately small.
_ALIASES: Final[dict[str, str]] = {
    "album artist": "albumartist",
    "band": "albumartist",
}

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

# The set of tags TagMend is allowed to write/revert (18 = the 5 original + 13 widened). A
# CLOSED set: anything outside it (``comment``/``composer``/art…) is never read, written, or
# deleted, and every key here MUST be provably writable on all four formats. The mismatch-fix
# flow can repair a poisoned release in one commit, and revert restores every field the
# target revision's own managed set governed (see
# :func:`tagmend.engine.versioning._revert_target_tags`).
# ``date`` (reissue year, MP4 ``©day``) and ``originaldate`` (original year, MP4 freeform) are
# BOTH managed and kept distinct.
MANAGED_TAGS: Final[frozenset[str]] = ORIGINAL_MANAGED_TAGS | _WIDENED_MANAGED_TAGS

# Which managed set governed a given revision, so revert can tell "this tag was empty then"
# from "this tag was not tracked then". Version 1 is the pre-widening five-tag set, version 2
# the current eighteen. Every new revision is stamped with :data:`MANAGED_SET_VERSION`;
# :func:`tagmend.engine.versioning._revert_target_tags` looks the stamp up here. Widening the
# set again means a new entry and a bump — never editing an existing entry, since stored
# revisions point at it.
MANAGED_SET_VERSION: Final = 2

MANAGED_SETS: Final[Mapping[int, frozenset[str]]] = {
    1: ORIGINAL_MANAGED_TAGS,
    2: MANAGED_TAGS,
}

# Which reader produced a snapshot row, so an incremental scan can spot rows left behind by
# an older one and re-read them exactly once. BUMP THIS IN THE SAME COMMIT as any change to
# what :func:`read_tags` produces — the managed set, an alias, a format registration — or
# every already-scanned file keeps serving the old reader's output to every detector.
TAG_READER_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class TrackTags:
    """A file's tags: canonical lowercase name -> ordered list of string values."""

    tags: dict[str, list[str]]


def _canonical_key(raw_key: str) -> str:
    """Lowercase *raw_key* and resolve it through the alias map."""
    lowered = raw_key.lower()
    return _ALIASES.get(lowered, lowered)


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

    # Process
    normalized: dict[str, list[str]] = {}
    for raw_key, raw_values in audio.tags.items():
        key = _canonical_key(str(raw_key))
        normalized[key] = [str(value) for value in raw_values]

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
        for key in MANAGED_TAGS:
            values = managed.get(key)
            if values:
                audio[key] = list(values)
            elif key in audio:
                del audio[key]
        audio.save()
        tmp.replace(path)
        replaced = True
    finally:
        if not replaced:
            tmp.unlink(missing_ok=True)
