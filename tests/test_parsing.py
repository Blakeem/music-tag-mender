"""Tests for pure folder/filename parsing (:mod:`tagmend.engine.parsing`).

Exhaustive pattern-matrix tests over the real measured folder/filename shapes from the
live library (verbatim as cases): the ``Artist - Year - Album`` and ``Artist - Album``
folder patterns, the ``Artist - Album - NN - Title`` filename pattern, and the fold-based
token-containment corroboration primitive.
"""

from __future__ import annotations

from tagmend.engine import parsing

# --- folder: Artist - Year - Album --------------------------------------------------


def test_parse_artist_year_album_bradley_nowell() -> None:
    parsed = parsing.parse_artist_year_album("Sublime - 1998 - Acoustic Bradley Nowell and Friends")
    assert parsed is not None
    assert parsed.artist == "Sublime"
    assert parsed.year == "1998"
    assert parsed.album == "Acoustic Bradley Nowell and Friends"


def test_parse_artist_year_album_rejects_non_year_middle() -> None:
    # A two-field name has no year in the middle -> the year pattern must not match.
    assert parsing.parse_artist_year_album("Sublime - Sinsemilla") is None
    # A middle token that is not a 1900/2000-era year is not a year.
    assert parsing.parse_artist_year_album("Sublime - 1899 - Old") is None


# --- folder: Artist - Album ---------------------------------------------------------


def test_parse_artist_album_two_field() -> None:
    parsed = parsing.parse_artist_album("Sublime - Sinsemilla")
    assert parsed is not None
    assert parsed.artist == "Sublime"
    assert parsed.album == "Sinsemilla"
    assert parsed.year is None


def test_parse_artist_album_youtube_is_only_a_regex_hit() -> None:
    # "Maphra - YouTube" parses structurally; corroboration (elsewhere) is what rejects it.
    parsed = parsing.parse_artist_album("Maphra - YouTube")
    assert parsed is not None
    assert parsed.artist == "Maphra"
    assert parsed.album == "YouTube"


def test_parse_artist_album_keeps_separators_in_album() -> None:
    parsed = parsing.parse_artist_album("Sublime - Misc LIVE+Acoustic+Extras")
    assert parsed is not None
    assert parsed.artist == "Sublime"
    assert parsed.album == "Misc LIVE+Acoustic+Extras"


# --- parse_folder: year first, then plain -------------------------------------------


def test_parse_folder_prefers_year_pattern() -> None:
    parsed = parsing.parse_folder("Sublime - 1998 - Acoustic Bradley Nowell and Friends")
    assert parsed is not None
    assert parsed.year == "1998"
    assert parsed.album == "Acoustic Bradley Nowell and Friends"


def test_parse_folder_falls_back_to_two_field() -> None:
    parsed = parsing.parse_folder("Sublime - Sinsemilla")
    assert parsed is not None
    assert parsed.year is None
    assert parsed.album == "Sinsemilla"


def test_parse_folder_junk_names_return_none() -> None:
    assert parsing.parse_folder("Sublime") is None
    assert parsing.parse_folder("RandomFolder") is None
    assert parsing.parse_folder("Artist-Album") is None  # needs the ' - ' separator


# --- filename: Artist - Album - NN - Title ------------------------------------------


def test_parse_filename_track_strips_extension_and_splits() -> None:
    parsed = parsing.parse_filename_track(
        "Sublime - Acoustic Bradley Nowell and Friends - 01 - Waiting for Bud.mp3",
    )
    assert parsed is not None
    assert parsed.artist == "Sublime"
    assert parsed.album == "Acoustic Bradley Nowell and Friends"
    assert parsed.track == "01"
    assert parsed.title == "Waiting for Bud"


def test_parse_filename_track_rejects_non_matching() -> None:
    assert parsing.parse_filename_track("01 - Waiting for Bud.mp3") is None
    assert parsing.parse_filename_track("just a title.mp3") is None


# --- fold-based token containment ---------------------------------------------------


def test_fold_contains_matches_across_case_and_punctuation() -> None:
    assert parsing.fold_contains(
        "Sublime - Acoustic Bradley Nowell and Friends - 01 - Song.mp3",
        "Acoustic Bradley Nowell and Friends",
    )
    # Case / spacing / punctuation differences never defeat the match.
    assert parsing.fold_contains("The B-Sides Collection", "b sides")


def test_fold_contains_rejects_absent_token_and_empty() -> None:
    assert parsing.fold_contains("Maphra - Some Song.mp3", "YouTube") is False
    # An empty (or punctuation-only) token never matches.
    assert parsing.fold_contains("anything at all", "") is False
    assert parsing.fold_contains("anything at all", "   ") is False
