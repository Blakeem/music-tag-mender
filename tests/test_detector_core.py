"""Tests for the shared core of the ``detect_*`` family."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tagmend.engine import detector_core


@dataclass(frozen=True, slots=True)
class _Item:
    folder: str
    name: str


@dataclass(frozen=True, slots=True)
class _Row:
    file_id: int
    folder: str
    tier: str
    filename: str = "a.mp3"
    field: str = "date"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7/12", 7),
        ("07", 7),
        (" 3 ", 3),
        ("²", None),
        ("A1", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_position(value: str | None, expected: int | None) -> None:
    assert detector_core.parse_position(value) == expected


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        (r"C:\m\Band\Singles", True),
        (r"C:\m\Band\E.P.", True),
        (r"C:\m\Band\Live!", True),
        (r"C:\m\Band\Live at Wembley", False),
        (r"C:\m\Band\Album", False),
    ],
)
def test_is_non_album_folder(folder: str, expected: bool) -> None:  # noqa: FBT001
    assert detector_core.is_non_album_folder(folder) is expected


def test_validate_tier_rejects_an_unknown_tier() -> None:
    detector_core.validate_tier(None)
    detector_core.validate_tier("high")
    with pytest.raises(ValueError, match="unknown tier: 'bogus'"):
        detector_core.validate_tier("bogus")


def test_group_by_folder_preserves_first_seen_order() -> None:
    items = [_Item("b", "1"), _Item("a", "2"), _Item("b", "3"), _Item("a", "4")]

    grouped = detector_core.group_by_folder(items)

    assert list(grouped) == ["b", "a"]
    assert [i.name for i in grouped["b"]] == ["1", "3"]
    assert [i.name for i in grouped["a"]] == ["2", "4"]


def test_tiers_by_file_counts_each_file_once_at_its_worst_tier() -> None:
    rows = [_Row(1, "a", "low"), _Row(1, "a", "high"), _Row(2, "a", "medium")]

    assert detector_core.tiers_by_file(rows) == {"high": 1, "medium": 1}


def test_regroup_matches_rows_to_groups_by_the_given_key() -> None:
    groups = [_Item("a", "x"), _Item("a", "y"), _Item("b", "x")]
    rows = [_Item("a", "y"), _Item("b", "x"), _Item("b", "x")]

    def refold(group: _Item, group_rows: list[_Item]) -> _Item:
        return _Item(group.folder, f"{group.name}{len(group_rows)}")

    regrouped = detector_core.regroup(groups, rows, refold, key=lambda i: (i.folder, i.name))

    assert regrouped == [_Item("a", "y1"), _Item("b", "x2")]


@pytest.mark.parametrize(
    ("albumartist", "compilation", "artist", "expected"),
    [
        (" Band ", "1", "Guest", "Band"),
        (None, "1", "Guest", "Various Artists"),
        ("", "yes", "Guest", "Guest"),
        (None, None, " ", "[Unknown Artist]"),
    ],
)
def test_display_album_artist_falls_back_in_server_order(
    albumartist: str | None,
    compilation: str | None,
    artist: str | None,
    expected: str,
) -> None:
    assert detector_core.display_album_artist(albumartist, compilation, artist) == expected


def test_album_identity_is_the_release_id_else_the_folded_names_and_date() -> None:
    by_id = detector_core.album_identity(" rel-1 ", "Band", "Album", "2001")
    by_name = detector_core.album_identity(None, "BAND", "Album", " 2001 ")

    assert by_id == ("release", "rel-1")
    assert by_name == detector_core.album_identity("", "band", "album", "2001")
    assert by_name != detector_core.album_identity(None, "band", "album", "2001-01-01")


def test_release_date_falls_back_to_a_raw_year() -> None:
    assert detector_core.release_date({"date": " ", "year": "1999"}) == "1999"
    assert detector_core.release_date({"date": "2001", "year": "1999"}) == "2001"
