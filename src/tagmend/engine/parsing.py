r"""Pure folder/filename parsing for album grounding (no store/settings/axis wiring).

Standalone, side-effect-free helpers that recover an ``(artist, album)`` guess from a
release *folder* name or an ``Artist - Album - NN - Title`` track *filename*, plus a
fold-based token-containment check used to self-corroborate a parsed album against the
folder's own filenames. Deliberately free of any ledger/axis/status wiring so the
album-gap detector, the no-identity worklist, and a future song-axis fill can all import
it directly (decision-r2 sub-decision (c)).

The two folder patterns are **fixed** (``Artist - Year - Album`` and ``Artist - Album``,
year ``(19|20)\d\d``); code only ever *validates* a parsed candidate against known-good
rows (sibling values / filename self-corroboration) — it never authors patterns. The
fold-key reuses :func:`tagmend.engine.classify.fold` (lowercase + strip non-``[a-z0-9]``)
so a match/dedup key stays a single definition across the engine; it is never written to
disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tagmend.engine.classify import fold

# The folder/filename field separator: space-hyphen-space.
_FOLDER_YEAR_RE: Final = re.compile(r"^(?P<artist>.+?) - (?P<year>(?:19|20)\d\d) - (?P<album>.+)$")
_FOLDER_RE: Final = re.compile(r"^(?P<artist>.+?) - (?P<album>.+)$")
_FILENAME_RE: Final = re.compile(
    r"^(?P<artist>.+?) - (?P<album>.+?) - (?P<track>\d+) - (?P<title>.+)$",
)


@dataclass(frozen=True, slots=True)
class ParsedFolder:
    """A release folder name parsed into ``artist`` / ``album`` (+ ``year`` when present)."""

    artist: str
    album: str
    year: str | None


@dataclass(frozen=True, slots=True)
class ParsedFilename:
    """An ``Artist - Album - NN - Title`` track filename parsed into its four fields."""

    artist: str
    album: str
    track: str
    title: str


def parse_artist_year_album(name: str) -> ParsedFolder | None:
    r"""Parse an ``Artist - Year - Album`` folder name (year ``(19|20)\d\d``), or ``None``.

    The album is everything after the year separator (it may itself contain ``" - "``).
    """
    match = _FOLDER_YEAR_RE.match(name.strip())
    if match is None:
        return None
    return ParsedFolder(
        artist=match["artist"].strip(),
        album=match["album"].strip(),
        year=match["year"],
    )


def parse_artist_album(name: str) -> ParsedFolder | None:
    """Parse an ``Artist - Album`` folder name (no year), or ``None`` when it has no ``" - "``."""
    match = _FOLDER_RE.match(name.strip())
    if match is None:
        return None
    return ParsedFolder(artist=match["artist"].strip(), album=match["album"].strip(), year=None)


def parse_folder(name: str) -> ParsedFolder | None:
    """Parse a folder name, trying ``Artist - Year - Album`` first, then ``Artist - Album``.

    Returns ``None`` for a name that matches neither fixed pattern (e.g. a junk/single-token
    folder). A *regex* hit alone is not corroboration — callers must still validate the
    parsed album against the folder's filenames via :func:`fold_contains`.
    """
    return parse_artist_year_album(name) or parse_artist_album(name)


def parse_filename_track(filename: str) -> ParsedFilename | None:
    """Parse an ``Artist - Album - NN - Title`` filename (extension stripped), or ``None``.

    The trailing extension is dropped via :attr:`pathlib.Path.stem` before matching; the
    track segment is the first run of digits between the album and the title.
    """
    stem = Path(filename).stem
    match = _FILENAME_RE.match(stem.strip())
    if match is None:
        return None
    return ParsedFilename(
        artist=match["artist"].strip(),
        album=match["album"].strip(),
        track=match["track"],
        title=match["title"].strip(),
    )


def fold_contains(text: str, token: str) -> bool:
    """Return whether *token*'s fold-key is a non-empty substring of *text*'s fold-key.

    The corroboration primitive: fold both sides (lowercase + strip non-``[a-z0-9]``) and
    test substring containment, so spacing/punctuation/case differences between an album
    token and a filename never defeat the match. An empty folded *token* never matches.
    """
    folded_token = fold(token)
    if not folded_token:
        return False
    return folded_token in fold(text)
