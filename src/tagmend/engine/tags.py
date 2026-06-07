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

from tagmend.log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

# Raw (already-lowercased) key -> canonical key. Kept deliberately small.
_ALIASES: Final[dict[str, str]] = {
    "album artist": "albumartist",
    "band": "albumartist",
}

# The deliberately narrow set of tags TagMend is allowed to write/revert. Keeping it
# small means snapshots stay tiny and a write/revert can never damage unrelated
# metadata (title, track, art). ``artist`` is included so revert can restore it; the
# classify/write layer (M2/M4) decides whether to *modify* it (PLAN.md §11 "cautious").
MANAGED_TAGS: Final[frozenset[str]] = frozenset(
    {"genre", "albumartist", "artist", "musicbrainz_artistid"},
)


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
