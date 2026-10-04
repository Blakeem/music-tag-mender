"""Comparison keys. Each one folds away the differences its comparison treats as cosmetic.

* :func:`alnum_key` treats casing and every character outside ``[a-z0-9]`` as cosmetic.
* :func:`alnum_ascii_key` treats casing, ligatures and diacritics as cosmetic by folding each
  letter to its base letter, then drops every character outside ``[a-z0-9]``.
* :func:`alnum_script_key` is :func:`alnum_ascii_key` that keeps every letter with no ASCII form.
* :func:`display_key` treats casing, typographic character choice and whitespace runs as cosmetic.
* :func:`artist_name_key` also treats a dash written for a word break as cosmetic.
* :func:`loose_key` treats Unicode compatibility forms, casing and whitespace as cosmetic.
* :func:`title_key` is :func:`alnum_key`, or :func:`loose_key` for a title it empties.
* :func:`year_key` treats everything after a date's leading four-digit year as cosmetic.

A key decides whether two spellings are the same. It is never written to disk.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

_NON_ALNUM: Final = re.compile(r"[^a-z0-9]+")
_ASCII_ALNUM: Final = re.compile(r"[a-z0-9]+")
_SCRIPT_CATEGORIES: Final = frozenset({"L", "M", "N"})
_YEAR_LEN: Final = 4

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

# Typographic characters a server folds to ASCII before grouping. Punctuation outside this
# table is never folded, because it really does separate two albums downstream.
TYPOGRAPHIC: Final[Mapping[str, str]] = {
    chr(codepoint): plain
    for codepoints, plain in (
        ((0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212), "-"),
        ((0x2018, 0x2019, 0x201A), "'"),
        ((0x201C, 0x201D), '"'),
        ((0x00A0, 0x2009, 0x202F), " "),
    )
    for codepoint in codepoints
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
    ``[a-z0-9]``. Unlike :func:`alnum_key`, which lowercases and drops every non-ASCII letter,
    this folds each one to its base letter, so neither key's equivalences contain the other's.
    The detectors need ``Leæther Strip`` == ``Leaether Strip`` and ``Dååth`` == ``Daath``.
    """
    translated = s.casefold().translate(_LIGATURE_TABLE)
    decomposed = unicodedata.normalize("NFKD", translated)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_ALNUM.sub("", without_marks)


def alnum_script_key(s: str) -> str:
    """Return :func:`alnum_ascii_key` for *s*, keeping each letter that has no ASCII form.

    A base character folds together with the marks after it. That cluster becomes its ASCII
    form when the form is all ``[a-z0-9]``. It stays as written when its base is a letter, mark
    or digit of another script, and drops otherwise. So ``Dååth`` == ``Daath``, while
    ``Часть 1`` and ``Глава 1`` stay apart and a kana voicing mark survives.
    """
    folded = unicodedata.normalize("NFKC", s).casefold().translate(_LIGATURE_TABLE)
    clusters: list[str] = []
    for ch in folded:
        if clusters and unicodedata.category(ch).startswith("M"):
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return "".join(_fold_cluster(cluster) for cluster in clusters)


def _fold_cluster(cluster: str) -> str:
    """Return *cluster*'s ASCII form, else *cluster* when its base is a letter, else ``""``."""
    decomposed = unicodedata.normalize("NFKD", cluster)
    ascii_form = "".join(ch for ch in decomposed if not unicodedata.category(ch).startswith("M"))
    if _ASCII_ALNUM.fullmatch(ascii_form):
        return ascii_form
    if unicodedata.category(cluster[0])[0] in _SCRIPT_CATEGORIES:
        return cluster
    return ""


def display_key(value: str) -> str:
    """Return the comparison key for an album or artist string as a server displays it.

    Casing, typographic character choice and whitespace runs are cosmetic. Every other
    difference separates two albums for anything reading these tags. The album-conflict and
    disagreement detectors share it, so they agree on what is cosmetic.
    """
    # NFC first, so the two byte-forms of one accented string compare equal. Deliberately not
    # NFKD-with-marks-stripped like :func:`alnum_ascii_key`: that folds an accent away, and
    # an accent really does separate two albums for anything reading these tags.
    folded = "".join(TYPOGRAPHIC.get(ch, ch) for ch in unicodedata.normalize("NFC", value))
    return " ".join(folded.casefold().split())


def artist_name_key(value: str) -> str:
    """Fold *value* to a key ignoring casing, typography and dash-vs-space word breaks.

    Used only to decide whether two spellings are the same name. Never used as a value.
    """
    # MusicBrainz writes a word break as a dash where taggers write a space.
    return " ".join(display_key(value).replace("-", " ").split())


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


def year_key(value: str | None) -> str | None:
    """Return the four-digit year leading *value*, or ``None`` when it carries none.

    ASCII digits only, so two years of equal length compare correctly as strings.
    """
    head = (value or "").strip()[:_YEAR_LEN]
    if len(head) == _YEAR_LEN and head.isascii() and head.isdigit():
        return head
    return None
