r"""Pure folder and filename parsing for album grounding.

:func:`parse_folder` recovers an album guess from a release folder name.
:func:`parse_filename_track` reads the track number and title out of an
``Artist - Album - NN - Title`` track filename. :func:`fold_contains` checks that a parsed album
token appears in the folder's own filenames, so a caller can corroborate a guess.

The two folder patterns are fixed: ``Artist - Year - Album`` and ``Artist - Album``, with the year
``(19|20)\d\d``. Code only validates a parsed candidate against the folder's own filenames. It
never authors a pattern. The fold key is :func:`tagmend.engine.text_keys.alnum_key`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tagmend.engine.text_keys import alnum_key

# The folder/filename field separator: space-hyphen-space.
_FOLDER_YEAR_RE: Final = re.compile(r"^(?P<artist>.+?) - (?P<year>(?:19|20)\d\d) - (?P<album>.+)$")
_FOLDER_RE: Final = re.compile(r"^(?P<artist>.+?) - (?P<album>.+)$")
_FILENAME_RE: Final = re.compile(
    r"^(?P<artist>.+?) - (?P<album>.+?) - (?P<track>\d+) - (?P<title>.+)$",
)


@dataclass(frozen=True, slots=True)
class ParsedFilename:
    """The track number and title of an ``Artist - Album - NN - Title`` track filename."""

    track: str
    title: str


def _parse_artist_year_album(name: str) -> str | None:
    r"""Return the album of an ``Artist - Year - Album`` folder name (year ``(19|20)\d\d``).

    The album is everything after the year separator (it may itself contain ``" - "``).
    """
    match = _FOLDER_YEAR_RE.match(name.strip())
    if match is None:
        return None
    return match["album"].strip()


def _parse_artist_album(name: str) -> str | None:
    """Return the album of an ``Artist - Album`` folder name, or ``None`` with no ``" - "``."""
    match = _FOLDER_RE.match(name.strip())
    if match is None:
        return None
    return match["album"].strip()


def parse_folder(name: str) -> str | None:
    """Return the album of a folder name, trying ``Artist - Year - Album``, then ``Artist - Album``.

    Returns ``None`` for a name that matches neither fixed pattern (e.g. a junk/single-token
    folder). A regex hit alone is not corroboration. Callers must still validate the parsed
    album against the folder's filenames via :func:`fold_contains`.
    """
    return _parse_artist_year_album(name) or _parse_artist_album(name)


def parse_filename_track(filename: str) -> ParsedFilename | None:
    """Parse an ``Artist - Album - NN - Title`` filename (extension stripped), or ``None``.

    The trailing extension is dropped via :attr:`pathlib.Path.stem` before matching. The
    track segment is the first run of digits between the album and the title.
    ``detect_mismatches`` reads every filename through this pattern, so a change to it moves the
    path-staging gate.
    """
    stem = Path(filename).stem
    match = _FILENAME_RE.match(stem.strip())
    if match is None:
        return None
    return ParsedFilename(track=match["track"], title=match["title"].strip())


def fold_contains(text: str, token: str) -> bool:
    """Return whether *token*'s fold-key is a non-empty substring of *text*'s fold-key.

    The corroboration primitive: fold both sides (lowercase + strip non-``[a-z0-9]``) and
    test substring containment, so spacing/punctuation/case differences between an album
    token and a filename never defeat the match. An empty folded *token* never matches.
    """
    folded_token = alnum_key(token)
    if not folded_token:
        return False
    return folded_token in alnum_key(text)
