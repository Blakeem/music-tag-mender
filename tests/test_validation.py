"""Tests for the shared engine argument checks."""

from __future__ import annotations

import re

import pytest

from tagmend.engine.validation import require_choice, validate_file_pairs


def test_require_choice_message_format() -> None:
    expected = "unknown tier: 'bogus' (expected one of high, low, medium)"

    with pytest.raises(ValueError, match=f"^{re.escape(expected)}$"):
        require_choice("tier", "bogus", {"high", "low", "medium"})


def test_require_choice_accepts_none() -> None:
    require_choice("tier", None, {"high"})
    require_choice("tier", "high", {"high"})


def _text(value: object) -> str:
    if not isinstance(value, str):
        message = "value must be a string"
        raise ValueError(message)  # noqa: TRY004 - the checks under test raise ValueError
    return value


def test_validate_file_pairs_narrows_each_pair_in_order() -> None:
    pairs = validate_file_pairs([(2, "b"), (1, "a")], value_name="value", check_value=_text)

    assert pairs == [(2, "b"), (1, "a")]


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        pytest.param([{"file_id": 1}], "entry 0: expected a (file_id, value) pair, got dict"),
        pytest.param([(1, "a", "b")], "entry 0: expected a (file_id, value) pair, got 3 items"),
        pytest.param([(True, "a")], "entry 0: file_id must be an integer, got bool"),
        pytest.param([(1, 2)], "entry 0 (file_id=1): value must be a string"),
        pytest.param([(1, "a"), (1, "b")], "entry 1: duplicate file_id=1 in batch"),
    ],
)
def test_validate_file_pairs_names_the_entry_index(entries: list[object], expected: str) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(expected)}$"):
        validate_file_pairs(entries, value_name="value", check_value=_text)
