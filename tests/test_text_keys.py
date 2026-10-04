"""Tests for the comparison keys (:mod:`tagmend.engine.text_keys`)."""

from __future__ import annotations

import unicodedata

from tagmend.engine import text_keys


def test_alnum_key_strips_case_space_and_punctuation() -> None:
    assert text_keys.alnum_key("Synth Pop") == "synthpop"
    assert text_keys.alnum_key("synth-pop") == "synthpop"
    assert text_keys.alnum_key("R&B") == "rb"
    assert text_keys.alnum_key("rnb") == "rnb"  # does NOT fold to "rb", which is why aliases exist
    assert text_keys.alnum_key("8-Bit") == "8bit"


def test_alnum_ascii_key_folds_ligatures_and_diacritics_equal() -> None:
    assert text_keys.alnum_ascii_key("Leæther Strip") == text_keys.alnum_ascii_key("Leaether Strip")
    assert text_keys.alnum_ascii_key("Dååth") == text_keys.alnum_ascii_key("Daath")
    assert text_keys.alnum_ascii_key("Röyksopp") == text_keys.alnum_ascii_key("Royksopp")


def test_alnum_ascii_key_strips_case_space_punctuation() -> None:
    assert text_keys.alnum_ascii_key("  Ozzy  Osbourne! ") == "ozzyosbourne"
    assert text_keys.alnum_ascii_key("A.B. & C") == "abc"


def test_alnum_script_key_folds_latin_and_keeps_other_scripts() -> None:
    assert text_keys.alnum_script_key("Dååth") == text_keys.alnum_script_key("Daath")
    assert text_keys.alnum_script_key("Leæther Strip") == "leaetherstrip"
    assert text_keys.alnum_script_key("Love Song (Live)") == "lovesonglive"
    assert text_keys.alnum_script_key("Часть 1") != text_keys.alnum_script_key("Глава 1")
    assert text_keys.alnum_script_key("か") != text_keys.alnum_script_key("が")
    assert text_keys.alnum_script_key("ｶﾞ") == text_keys.alnum_script_key("ガ")
    assert text_keys.alnum_script_key("+") == ""


def test_display_key_ignores_casing_spacing_and_typographic_quotes() -> None:
    curly = "Don\N{RIGHT SINGLE QUOTATION MARK}t  Stop"
    assert text_keys.display_key(curly) == text_keys.display_key("don't stop")


def test_display_key_keeps_other_punctuation_significant() -> None:
    assert text_keys.display_key("Part: One") != text_keys.display_key("Part - One")


def test_keys_disagree_where_documented() -> None:
    assert text_keys.alnum_key("Röyksopp") != text_keys.alnum_ascii_key("Röyksopp")
    assert text_keys.display_key("The Crow: City") != text_keys.display_key("The Crow- City")
    assert text_keys.alnum_ascii_key("The Crow: City") == text_keys.alnum_ascii_key(
        "The Crow- City"
    )
    assert text_keys.artist_name_key("Static\u2010X") == text_keys.artist_name_key("Static X")
    assert text_keys.artist_name_key("Static\u2010X") == text_keys.artist_name_key("static x")
    assert text_keys.artist_name_key("It\u201as") == text_keys.artist_name_key("It's")
    assert text_keys.title_key("東京事変") != ""


def test_artist_name_key_equates_both_byte_forms_of_one_accent() -> None:
    composed = unicodedata.normalize("NFC", "Björk")
    decomposed = unicodedata.normalize("NFD", "Björk")

    assert composed != decomposed
    assert text_keys.artist_name_key(decomposed) == text_keys.artist_name_key(composed)
