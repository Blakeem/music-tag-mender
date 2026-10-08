"""Comparison helpers for matching a file's tags against a MusicBrainz release's tracklist.

They live apart from :mod:`tagmend.engine.release_disagreements` so that every caller matching
a file to a release track agrees on what counts as the same title, track number and disc.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from tagmend.engine.detector_core import parse_position, position_head
from tagmend.engine.text_keys import display_key

if TYPE_CHECKING:
    from tagmend.engine.musicbrainz import MBMedium, MBRelease, MBTrack

# Picard omits ``discnumber`` on a single-medium release, so only a longer release proposes one.
_SINGLE_MEDIUM: Final = 1


def album_status(release: MBRelease) -> str:
    """Return *release*'s status as Picard writes ``musicbrainz_albumstatus``.

    MusicBrainz answers the display form (``Official``) and Picard writes it lowercase, so a
    proposal and a stamp spell it the way the rest of the library already does.
    """
    return release.status.lower()


def position(value: str | None) -> str:
    """Return the position part of an ``n`` / ``n/total`` tag value, without leading zeros.

    A non-decimal head such as the vinyl ``A1`` comes back verbatim, so a side designation
    still compares.
    """
    number = parse_position(value)
    if number is not None:
        return str(number)
    return position_head(value)


def track_number_agrees(have: str, track: MBTrack) -> bool:
    """Return whether the file's track number names this track, in either spelling.

    A vinyl medium numbers its tracks by side (``A1``, ``B7``) while Picard writes the
    sequential position, so the two strings differ on every file of such a release without
    anything being wrong. Either spelling is a defensible reading, so either is accepted.
    """
    return display_key(have) in {
        display_key(position(track.number)),
        display_key(position(str(track.position))),
    }


def medium_holding(release: MBRelease, track: MBTrack) -> MBMedium | None:
    """Return the medium of *release* holding *track*, or None if none holds it.

    Identity, not equality: :class:`MBTrack` is a frozen dataclass, so two equal-valued tracks
    on different media would otherwise resolve to whichever medium came first.
    """
    return next((m for m in release.media if any(t is track for t in m.tracks)), None)


def medium_of(release: MBRelease, track: MBTrack) -> int:
    """Return the 1-based disc position of the medium holding *track*, or 0 if unknown."""
    medium = medium_holding(release, track)
    return 0 if medium is None else medium.position


def disc_expectation(release: MBRelease, track: MBTrack) -> str:
    """Return the disc number this track should carry, or empty when there is nothing to say.

    A single-medium release says nothing: Picard routinely omits ``discnumber`` there, and
    proposing 1 on every such file would bury the report. A medium whose position did not
    parse says nothing either, because disc zero is not an answer.
    """
    if len(release.media) <= _SINGLE_MEDIUM:
        return ""
    disc = medium_of(release, track)
    return str(disc) if disc > 0 else ""
