"""The shared core of the ``detect_*`` family: tiers, folder buckets, fold keys and positions."""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from tagmend.engine.validation import require_choice

if TYPE_CHECKING:
    from collections.abc import Iterable


class Tier(StrEnum):
    """Confidence tier for a flagged row (most to least severe)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


TIER_RANK: Final = {Tier.HIGH: 0, Tier.MEDIUM: 1, Tier.LOW: 2}
TIERS: Final = frozenset(t.value for t in Tier)

# Leaf folder names that are not normal albums: a guest or other artist here is legitimate,
# so such a folder holds several releases by design.
NON_ALBUM_FOLDERS: Final = frozenset(
    {"singles", "featured", "remixes", "bonus", "live", "ep"},
)

# Ligature/eszett map applied after casefold (which already folds ``ß`` → ``ss`` and
# ``Æ`` → ``æ`` etc.), covering the compatibility cases NFKD does not decompose.
_LIGATURES: Final = {
    "æ": "ae",
    "ø": "o",
    "œ": "oe",
    "ł": "l",
    "þ": "th",
    "ð": "d",
}
_LIGATURE_TABLE: Final = str.maketrans(_LIGATURES)

_NON_ALNUM: Final = re.compile(r"[^a-z0-9]+")

# A tracknumber/discnumber may be stored as "7" or as the "7/12" slash form. Only the part
# before the slash is the position.
_SLASH: Final = "/"


class _HasFolder(Protocol):
    """Anything that sits in one library folder."""

    @property
    def folder(self) -> str: ...


def validate_tier(tier: str | None) -> None:
    """Raise :class:`ValueError` for a *tier* outside :data:`TIERS`. ``None`` passes."""
    require_choice("tier", tier, TIERS)


def fold(s: str) -> str:
    """Return the detector's fold-key for *s*: casefold + Unicode/ligature fold + strip.

    Casefold, translate the residual ligatures NFKD leaves intact (``æ`` → ``ae`` …),
    NFKD-decompose and drop combining marks (diacritics), then strip everything outside
    ``[a-z0-9]``. Deliberately a **superset** of :func:`tagmend.engine.classify.fold` (the
    genre fold-key, which is casefold + strip only): the detector additionally needs the
    Unicode/ligature folding so ``Leæther Strip`` == ``Leaether Strip`` and ``Dååth`` ==
    ``Daath``. A match and compare key only, never written to disk.
    """
    translated = s.casefold().translate(_LIGATURE_TABLE)
    decomposed = unicodedata.normalize("NFKD", translated)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_ALNUM.sub("", without_marks)


# Punctuation-insensitive, because a folder named ``E.P.`` or ``Live!`` holds an EP or a live set.
_NON_ALBUM_KEYS: Final = frozenset(fold(name) for name in NON_ALBUM_FOLDERS)


def is_non_album_folder(folder: str) -> bool:
    """Return whether *folder*'s leaf name marks a collection rather than one album."""
    return fold(Path(folder).name) in _NON_ALBUM_KEYS


def group_by_folder[T: _HasFolder](items: Iterable[T]) -> dict[str, list[T]]:
    """Bucket *items* by their folder, preserving first-seen order within each folder."""
    grouped: dict[str, list[T]] = {}
    for item in items:
        grouped.setdefault(item.folder, []).append(item)
    return grouped


def parse_position(value: str | None) -> int | None:
    """Return the position of an ``n`` / ``n/total`` tag value, or ``None`` if unusable."""
    if not value:
        return None
    head = value.split(_SLASH, 1)[0].strip()
    # isdecimal, not isdigit: isdigit accepts superscripts and enclosed digits that int()
    # rejects, and one such tag would abort the whole run.
    return int(head) if head.isdecimal() else None
