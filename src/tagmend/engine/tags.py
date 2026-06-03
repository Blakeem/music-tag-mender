"""Read the normalized tag set via mutagen (M1 read path; write lands in M3).

Tags are read in mutagen's "easy" mode and normalized into a single canonical,
lowercase namespace so the rest of the engine never has to care about format-specific
key spellings (ID3 vs Vorbis vs MP4). A small alias map collapses a few well-known
synonyms; everything else passes through lowercased unchanged.

The write path (atomic temp+rename of the narrow managed-tag set) is intentionally
deferred to M3 — see PLAN.md §7 and §11.
"""

from __future__ import annotations

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
