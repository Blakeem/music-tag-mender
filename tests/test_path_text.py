"""Tests for the path value rule (:mod:`tagmend.engine.path_text`)."""

from __future__ import annotations

import unicodedata

import pytest

from tagmend.engine.path_text import clean_value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('AC/DC: <Live> "Best" \\ Of | 1? *', "ACDC Live Best Of 1"),
        ("Tab\there\nnew\x00line\x7f\x85", "Tabherenewline"),
        ("  spaced    out\u00a0words  ", "spaced out words"),
        ("Wait...", "Wait..."),
        ("", ""),
        ("?*:", ""),
    ],
)
def test_clean_value_deletes_forbidden_characters_and_collapses_whitespace(
    raw: str,
    expected: str,
) -> None:
    assert clean_value(raw) == expected


def test_clean_value_returns_nfc_even_after_a_deletion() -> None:
    decomposed = unicodedata.normalize("NFD", "Röyksopp Café")

    assert clean_value(decomposed) == "Röyksopp Café"
    assert clean_value("e:\u0301") == "é"


def test_clean_value_is_idempotent() -> None:
    raw = unicodedata.normalize("NFD", ' Mélodie: A.M. / "Remix"? ')

    once = clean_value(raw)

    assert clean_value(once) == once
