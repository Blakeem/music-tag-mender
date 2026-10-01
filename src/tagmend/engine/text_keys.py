"""Comparison keys. Each one folds away the differences its comparison treats as cosmetic.

* :func:`alnum_key` treats casing and every character outside ``[a-z0-9]`` as cosmetic.
* :func:`alnum_ascii_key` also treats ligatures and diacritics as cosmetic.
* :func:`display_key` treats casing, typographic character choice and whitespace runs as cosmetic.
* :func:`artist_name_key` also treats a dash written for a word break as cosmetic.
* :func:`loose_key` treats Unicode compatibility forms, casing and whitespace as cosmetic.
* :func:`title_key` is :func:`alnum_key`, or :func:`loose_key` for a title it empties.

A key decides whether two spellings are the same. It is never written to disk.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

_NON_ALNUM: Final = re.compile(r"[^a-z0-9]+")

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

# Typographic characters a server folds to ASCII before grouping. Deliberately NOT
# :func:`alnum_ascii_key`, which strips every non-alphanumeric character: that would erase
# ``The Crow: City of Angels`` against ``The Crow- City Of Angels``, which really are two
# albums downstream and are exactly what the album-conflict detector exists to find. Case
# and surrounding or repeated whitespace are cosmetic. Punctuation is not.
DISPLAY_TYPOGRAPHY: Final[Mapping[str, str]] = {
    chr(codepoint): plain
    for codepoints, plain in (
        ((0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212), "-"),
        ((0x2018, 0x2019), "'"),
        ((0x201C, 0x201D), '"'),
        ((0x00A0, 0x2009, 0x202F), " "),
    )
    for codepoint in codepoints
}

# Characters that separate the same name into different spellings. MusicBrainz writes real
# typography (``Static\u2010X`` carries U+2010, not a hyphen-minus) while taggers and
# filesystems substitute ASCII, and a word break is written as a dash by one source and a
# space by another (``Mindless Self\u2010Indulgence`` against MusicBrainz's spaced
# spelling). Folding decides SAMENESS only, and only against names MusicBrainz records for
# the id the file already carries, so it can never merge two different artists. The staged
# value is always MusicBrainz's own spelling.
ARTIST_NAME_TYPOGRAPHY: Final[Mapping[str, str]] = {
    "-": " ",
    "\u2010": " ",
    "\u2011": " ",
    "\u2012": " ",
    "\u2013": " ",
    "\u2014": " ",
    "\u2212": " ",
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u00a0": " ",
    "\u2009": " ",
    "\u202f": " ",
}


def alnum_key(s: str) -> str:
    """Return the fold-key for *s*: lowercase, then strip everything non-``[a-z0-9]``.

    The canonical definition from ``docs/genre-tagging-spec.md`` §3. Used only to compare
    spelling, spacing and punctuation variants as equal.
    """
    return _NON_ALNUM.sub("", s.lower())


def alnum_ascii_key(s: str) -> str:
    """Return the detector's fold-key for *s*: casefold + Unicode/ligature fold + strip.

    Casefold, translate the residual ligatures NFKD leaves intact (``æ`` → ``ae`` …),
    NFKD-decompose and drop combining marks (diacritics), then strip everything outside
    ``[a-z0-9]``. Deliberately a **superset** of :func:`alnum_key` (casefold + strip only):
    the detectors also need the Unicode/ligature folding so ``Leæther Strip`` ==
    ``Leaether Strip`` and ``Dååth`` == ``Daath``.
    """
    translated = s.casefold().translate(_LIGATURE_TABLE)
    decomposed = unicodedata.normalize("NFKD", translated)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_ALNUM.sub("", without_marks)


def display_key(value: str) -> str:
    """Return the comparison key for an album or artist string as a server displays it.

    Casing, typographic character choice and whitespace runs are cosmetic. Every other
    difference separates two albums for anything reading these tags. The album-conflict and
    disagreement detectors share it, so they agree on what is cosmetic.
    """
    # NFC first, so the two byte-forms of one accented string compare equal. Deliberately not
    # NFKD-with-marks-stripped like :func:`alnum_ascii_key`: that folds an accent away, and
    # an accent really does separate two albums for anything reading these tags.
    folded = "".join(DISPLAY_TYPOGRAPHY.get(ch, ch) for ch in unicodedata.normalize("NFC", value))
    return " ".join(folded.casefold().split())


def artist_name_key(value: str) -> str:
    """Fold *value* to a key ignoring casing, typography and dash-vs-space word breaks.

    Used only to decide whether two spellings are the same name. Never used as a value.
    """
    folded = "".join(ARTIST_NAME_TYPOGRAPHY.get(ch, ch) for ch in value)
    return " ".join(folded.casefold().split())


def loose_key(value: str) -> str:
    """Return an NFKC casefold key with whitespace removed, for titles :func:`alnum_key` empties.

    :func:`alnum_key` strips everything outside ``[a-z0-9]``, so a title written wholly in a
    non-Latin script or in symbols (``Спутник``, ``東京事変``, ``+``) folds to ``""`` and could
    never match itself. This keeps those characters instead of dropping them.
    """
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def title_key(title: str) -> str:
    """Return *title*'s comparison key: its :func:`alnum_key`, or :func:`loose_key` when empty."""
    return alnum_key(title) or loose_key(title)
