"""Tests for the shared core of the ``detect_*`` family."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tagmend.engine import detector_core


@dataclass(frozen=True, slots=True)
class _Item:
    folder: str
    name: str


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
