"""Tests for the shared engine argument checks."""

from __future__ import annotations

import re

import pytest

from tagmend.engine.validation import require_choice


def test_require_choice_message_format() -> None:
    expected = "unknown tier: 'bogus' (expected one of high, low, medium)"

    with pytest.raises(ValueError, match=f"^{re.escape(expected)}$"):
        require_choice("tier", "bogus", {"high", "low", "medium"})


def test_require_choice_accepts_none() -> None:
    require_choice("tier", None, {"high"})
    require_choice("tier", "high", {"high"})
