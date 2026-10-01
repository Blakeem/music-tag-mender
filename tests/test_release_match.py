"""Unit tests for the shared release-matching helpers (``engine/release_match.py``)."""

from __future__ import annotations

import pytest

from tagmend.engine import release_match
from tagmend.engine.musicbrainz import MBMedium, MBRelease, MBTrack


def _track(number: str, position: int, title: str = "Song") -> MBTrack:
    return MBTrack(
        position=position,
        number=number,
        title=title,
        release_track_mbid=f"rt-{number}",
        recording_mbid=f"rec-{number}",
        artist_credit="Band",
        artist_sort="Band",
        artist_mbids=("artist-1",),
    )


def _medium(position: int, *tracks: MBTrack) -> MBMedium:
    return MBMedium(
        position=position, title="", format="CD", track_count=len(tracks), tracks=tracks
    )


def _release(*media: MBMedium) -> MBRelease:
    return MBRelease(
        mbid="rel-1",
        title="Album",
        artist_credit="Band",
        artist_sort="Band",
        artist_mbids=("artist-1",),
        date="1997",
        country="US",
        status="Official",
        barcode="",
        media=media,
    )


def test_text_key_ignores_casing_spacing_and_typographic_quotes() -> None:
    curly = "Don\N{RIGHT SINGLE QUOTATION MARK}t  Stop"
    assert release_match.text_key(curly) == release_match.text_key("don't stop")


def test_text_key_keeps_other_punctuation_significant() -> None:
    assert release_match.text_key("Part: One") != release_match.text_key("Part - One")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("07", "7"), ("3/12", "3"), ("A1", "A1"), (" B2 /9", "B2"), ("", ""), (None, "")],
)
def test_position_strips_the_total_and_leading_zeros(value: str | None, expected: str) -> None:
    assert release_match.position(value) == expected


def test_track_number_agrees_on_the_sequential_position_or_the_side_number() -> None:
    track = _track("A2", 2)
    assert release_match.track_number_agrees("2", track)
    assert release_match.track_number_agrees("a2", track)
    assert not release_match.track_number_agrees("3", track)


def test_medium_of_finds_the_disc_by_identity_not_equality() -> None:
    first = _track("1", 1)
    twin = _track("1", 1)  # equal-valued, on another disc
    release = _release(_medium(1, first), _medium(2, twin))
    assert release_match.medium_of(release, twin) == 2
    assert release_match.medium_of(release, first) == 1
    assert release_match.medium_of(release, _track("9", 9)) == 0


def test_disc_expectation_is_silent_on_a_single_medium_release() -> None:
    track = _track("1", 1)
    assert release_match.disc_expectation(_release(_medium(1, track)), track) == ""


def test_disc_expectation_names_the_disc_on_a_multi_medium_release() -> None:
    track = _track("1", 1)
    release = _release(_medium(1, _track("1", 1)), _medium(2, track))
    assert release_match.disc_expectation(release, track) == "2"


def test_disc_expectation_is_silent_on_an_unparsed_medium_position() -> None:
    track = _track("1", 1)
    release = _release(_medium(1, _track("1", 1)), _medium(0, track))
    assert release_match.disc_expectation(release, track) == ""
