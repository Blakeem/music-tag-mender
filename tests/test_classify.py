"""Unit tests for the genre classifier (``engine/classify.py``).

Covers :func:`load_vocabulary` against the real bundled YAML (fold/alias/overlay matching,
single-valued index, collision handling) and the pure :func:`classify_genres` pipeline on
the real band examples from ``docs/genre-tagging-spec.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from tagmend.config import Settings
from tagmend.engine.classify import Vocabulary, classify_genres, load_vocabulary
from tagmend.engine.lastfm import Tag
from tagmend.engine.text_keys import alnum_key

# A sane lower bound for the generated MusicBrainz vocabulary (about 2,156 genres, plus
# aliases and the overlay). Keeps the assertion robust to refreshes that grow the list.
_MIN_GENRE_COUNT = 2000


@pytest.fixture
def captured_warnings(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Capture ``tagmend`` warnings.

    The ``tagmend`` logger sets ``propagate=False`` (so MCP stdout stays clean), so the
    root-attached ``caplog`` handler never sees its records. Attach the capture handler to
    the ``tagmend`` logger directly for the duration of the test, then detach it.
    """
    logger = logging.getLogger("tagmend")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="tagmend"):
            yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def _settings(*, min_weight: int = 2, max_count: int | None = None) -> Settings:
    """A Settings with only the genre knobs that ``classify_genres`` reads set.

    ``classify_genres`` never touches ``db_path``, so a throwaway path keeps it simple.
    """
    return Settings(
        music_path=None,
        lastfm_api_key=None,
        db_path=Path("unused.sqlite3"),
        genre_min_weight=min_weight,
        genre_max_count=max_count,
    )


# --- load_vocabulary (real bundled files) --------------------------------------------


def test_load_vocabulary_real_files_resolves_known_genres() -> None:
    vocab = load_vocabulary()

    # Overlay-only bare genres MusicBrainz omits.
    assert vocab.match("alternative") == "alternative"
    assert vocab.match("indie") == "indie"

    # Overlay extra spellings for an existing genre.
    assert vocab.match("8-bit") == "chiptune"
    assert vocab.match("8bit") == "chiptune"

    # Folding alone (no alias needed) handles spacing/punctuation variants.
    assert vocab.match("synthpop") == "synth-pop"
    assert vocab.match("synth pop") == "synth-pop"

    # Alias that folding can't reach.
    assert vocab.match("RnB") == "r&b"

    # A non-genre tag matches nothing.
    assert vocab.match("2013") is None
    assert vocab.match("favourite albums") is None


def test_load_vocabulary_index_is_single_valued_and_large() -> None:
    vocab = load_vocabulary()
    # The frozen index is keyed by fold-key; a dict is single-valued by definition, but
    # assert it is genuinely populated to a sane size.
    assert isinstance(vocab, Vocabulary)
    assert len(vocab) > _MIN_GENRE_COUNT
    # Every fold-key maps to a value whose own fold matches the key's canonical (sanity).
    for key, name in vocab.index.items():
        assert key == alnum_key(key)  # keys are already folded
        assert isinstance(name, str)


# --- classify_genres: real band examples ----------------------------------------------


def test_classify_genres_thermostatic_artist_only() -> None:
    vocab = load_vocabulary()
    artist_tags = [
        Tag("synthpop", 100),
        Tag("electronic", 71),
        Tag("bitpop", 38),
        Tag("electropop", 38),
        Tag("swedish", 19),  # not a vocab genre → dropped
        Tag("8-bit", 7),  # → chiptune via overlay
        Tag("electronica", 4),
    ]

    result = classify_genres(
        artist_tags, None, vocab, _settings(min_weight=2), lookup_artist="Thermostatic"
    )

    # Weight-ordered desc, name asc on ties. bitpop(38) and electropop(38) tie → name asc.
    assert result == [
        "synth-pop",  # 100
        "electronic",  # 71
        "bitpop",  # 38
        "electropop",  # 38
        "chiptune",  # 7 (from 8-bit)
        "electronica",  # 4
    ]


def test_classify_genres_daft_punk_artist_plus_album_merge() -> None:
    vocab = load_vocabulary()
    # From spec §6.1 (Daft Punk / Random Access Memories), min_weight=2.
    artist_tags = [
        Tag("electronic", 100),
        Tag("house", 63),
        Tag("dance", 36),
        Tag("techno", 26),
        Tag("electronica", 11),
        Tag("electro", 2),
    ]
    album_tags = [
        Tag("2013", 100),  # not in vocab → dropped
        Tag("electronic", 87),
        Tag("disco", 43),  # album-only genre
        Tag("funk", 32),  # album-only genre
        Tag("house", 12),
        Tag("dance", 5),
        Tag("pop", 1),  # below min_weight → dropped
    ]

    result = classify_genres(
        artist_tags, album_tags, vocab, _settings(min_weight=2), lookup_artist="Daft Punk"
    )

    # Merge by max weight: electronic 100, house 63, disco 43, dance 36, funk 32,
    # techno 26, electronica 11, electro 2. disco/funk only exist via the album.
    assert result == [
        "electronic",
        "house",
        "disco",
        "dance",
        "funk",
        "techno",
        "electronica",
        "electro",
    ]


def test_classify_genres_max_count_caps_top_n() -> None:
    vocab = load_vocabulary()
    artist_tags = [
        Tag("electronic", 100),
        Tag("house", 63),
        Tag("dance", 36),
        Tag("techno", 26),
    ]

    result = classify_genres(
        artist_tags,
        None,
        vocab,
        _settings(min_weight=2, max_count=2),
        lookup_artist="Daft Punk",
    )

    assert result == ["electronic", "house"]


def test_classify_genres_min_weight_drops_weak_tail() -> None:
    vocab = load_vocabulary()
    artist_tags = [
        Tag("electronic", 100),
        Tag("house", 5),
        Tag("techno", 1),  # below a higher threshold
    ]

    result = classify_genres(
        artist_tags, None, vocab, _settings(min_weight=5), lookup_artist="Daft Punk"
    )

    assert result == ["electronic", "house"]  # techno(1) dropped


def test_classify_genres_empty_and_no_match_inputs() -> None:
    vocab = load_vocabulary()
    settings = _settings(min_weight=2)

    assert classify_genres([], None, vocab, settings, lookup_artist="Artist") == []
    # All non-vocab tags → empty.
    no_match_tags = [Tag("2013", 100), Tag("swedish", 80)]
    assert classify_genres(no_match_tags, None, vocab, settings, lookup_artist="Artist") == []
    # album_tags=None vs an empty album list both behave (empty album contributes nothing).
    assert classify_genres([], [], vocab, settings, lookup_artist="Artist") == []


# --- overlay collision handling ------------------------------------------------------


def test_overlay_alias_collision_is_skipped_with_warning(
    tmp_path: Path,
    captured_warnings: pytest.LogCaptureFixture,
) -> None:
    # A tiny standalone base vocab so the collision target is deterministic.
    vocab_yml = tmp_path / "vocab.yml"
    vocab_yml.write_text(
        "version: 1\ngenres:\n- name: rock\n  aliases: []\n- name: techno\n  aliases: []\n",
        encoding="utf-8",
    )
    # Overlay tries to alias "rock" onto a NEW genre — alias fold-key "rock" is owned by
    # the base "rock" genre → collision → skip the alias, keep the new genre + base map.
    overlay_yml = tmp_path / "overlay.yml"
    overlay_yml.write_text(
        "version: 1\n"
        "genres:\n"
        "- name: indie\n"
        "  aliases: [rock]\n",  # "rock" already owned by the base "rock" genre
        encoding="utf-8",
    )

    vocab = load_vocabulary(vocabulary_path=vocab_yml, overlay_path=overlay_yml)

    # The base mapping is preserved (alias NOT stolen by "indie").
    assert vocab.match("rock") == "rock"
    # The new overlay genre still exists.
    assert vocab.match("indie") == "indie"
    # The collision was reported.
    assert any("collides" in record.message for record in captured_warnings.records)


def test_overlay_name_collision_skips_whole_entry_with_warning(
    tmp_path: Path,
    captured_warnings: pytest.LogCaptureFixture,
) -> None:
    vocab_yml = tmp_path / "vocab.yml"
    vocab_yml.write_text(
        "version: 1\ngenres:\n- name: r&b\n  aliases: []\n",
        encoding="utf-8",
    )
    # Overlay entry whose NAME folds to "rb" (already owned by "r&b") but with a different
    # canonical spelling → whole entry skipped, its aliases never added.
    overlay_yml = tmp_path / "overlay.yml"
    overlay_yml.write_text(
        "version: 1\ngenres:\n- name: RB\n  aliases: [soul]\n",
        encoding="utf-8",
    )

    vocab = load_vocabulary(vocabulary_path=vocab_yml, overlay_path=overlay_yml)

    assert vocab.match("r&b") == "r&b"
    assert vocab.match("RB") == "r&b"  # base mapping intact
    assert vocab.match("soul") is None  # the skipped entry's alias was not added
    assert any("collides" in record.message for record in captured_warnings.records)


def test_overlay_extends_existing_genre_with_new_aliases(tmp_path: Path) -> None:
    vocab_yml = tmp_path / "vocab.yml"
    vocab_yml.write_text(
        "version: 1\ngenres:\n- name: chiptune\n  aliases: []\n",
        encoding="utf-8",
    )
    overlay_yml = tmp_path / "overlay.yml"
    overlay_yml.write_text(
        "version: 1\ngenres:\n- name: chiptune\n  aliases: [8-bit, 8bit]\n",
        encoding="utf-8",
    )

    vocab = load_vocabulary(vocabulary_path=vocab_yml, overlay_path=overlay_yml)

    assert vocab.match("8-bit") == "chiptune"
    assert vocab.match("8bit") == "chiptune"


# --- overlay deny rules --------------------------------------------------------------

_DENY_BASE_VOCAB = (
    "version: 1\ngenres:\n- name: indie pop\n  aliases: []\n- name: oi\n  aliases: []\n"
)
_CANNONS_TAGS = [Tag("indie pop", 100), Tag("oi", 60)]


def _deny_vocabulary(tmp_path: Path, deny_yaml: str) -> Vocabulary:
    """Load a two-genre vocabulary whose overlay holds only *deny_yaml*."""
    vocab_yml = tmp_path / "vocab.yml"
    vocab_yml.write_text(_DENY_BASE_VOCAB, encoding="utf-8")
    overlay_yml = tmp_path / "overlay.yml"
    overlay_yml.write_text(f"version: 1\ngenres: []\n{deny_yaml}", encoding="utf-8")
    return load_vocabulary(vocabulary_path=vocab_yml, overlay_path=overlay_yml)


def test_overlay_deny_for_artist_drops_genre_only_for_that_artist(tmp_path: Path) -> None:
    vocab = _deny_vocabulary(tmp_path, "deny:\n- genre: Oi!\n  artists: [Cannons]\n")
    settings = _settings()

    assert classify_genres(_CANNONS_TAGS, None, vocab, settings, lookup_artist="Cannons") == [
        "indie pop",
    ]
    # The artist name compares by fold-key, like the genre name.
    assert classify_genres(_CANNONS_TAGS, None, vocab, settings, lookup_artist="CANNONS") == [
        "indie pop",
    ]
    # A denied genre arriving through the album tags is dropped too.
    album_only = [Tag("oi", 90)]
    assert classify_genres(
        [Tag("indie pop", 100)], album_only, vocab, settings, lookup_artist="Cannons"
    ) == ["indie pop"]
    assert classify_genres(_CANNONS_TAGS, None, vocab, settings, lookup_artist="Cock Sparrer") == [
        "indie pop",
        "oi",
    ]


def test_overlay_global_deny_drops_genre_for_every_artist(tmp_path: Path) -> None:
    # A global entry outranks a per-artist entry for the same genre.
    deny_yaml = "deny:\n- genre: oi\n  artists: [Cannons]\n- genre: oi\n"
    vocab = _deny_vocabulary(tmp_path, deny_yaml)
    settings = _settings()

    for artist in ("Cannons", "Cock Sparrer"):
        result = classify_genres(_CANNONS_TAGS, None, vocab, settings, lookup_artist=artist)
        assert result == ["indie pop"]


def test_overlay_denied_genre_does_not_consume_a_capped_slot(tmp_path: Path) -> None:
    vocab = _deny_vocabulary(tmp_path, "deny:\n- genre: oi\n  artists: [Cannons]\n")
    tags = [Tag("oi", 100), Tag("indie pop", 50)]

    result = classify_genres(tags, None, vocab, _settings(max_count=1), lookup_artist="Cannons")

    assert result == ["indie pop"]


def test_overlay_deny_of_unknown_genre_warns_and_is_ignored(
    tmp_path: Path,
    captured_warnings: pytest.LogCaptureFixture,
) -> None:
    vocab = _deny_vocabulary(tmp_path, "deny:\n- genre: zeuhl\n  artists: [Cannons]\n")

    result = classify_genres(_CANNONS_TAGS, None, vocab, _settings(), lookup_artist="Cannons")

    assert result == ["indie pop", "oi"]
    assert vocab.denied_everywhere == frozenset()
    assert vocab.denied_for_artists == {}
    assert any("not in the vocabulary" in record.message for record in captured_warnings.records)


@pytest.mark.parametrize(
    "deny_yaml",
    [
        "deny:\n- genre: oi\n  artists: Cannons\n",
        "deny:\n- genre: oi\n  artists:\n",
        "deny:\n- genre: oi\n  artists: []\n",
        "deny:\n- artists: [Cannons]\n",
    ],
    ids=["artists-not-a-list", "artists-null", "artists-empty", "genre-missing"],
)
def test_overlay_deny_entry_with_unusable_fields_warns_and_denies_nothing(
    tmp_path: Path,
    captured_warnings: pytest.LogCaptureFixture,
    deny_yaml: str,
) -> None:
    # A present but unusable artists value never widens into a deny for every artist.
    vocab = _deny_vocabulary(tmp_path, deny_yaml)
    settings = _settings()

    for artist in ("Cannons", "Cock Sparrer"):
        result = classify_genres(_CANNONS_TAGS, None, vocab, settings, lookup_artist=artist)
        assert result == ["indie pop", "oi"]
    assert any("skipping entry" in record.message for record in captured_warnings.records)


def test_overlay_deny_artist_folding_to_empty_key_is_skipped(
    tmp_path: Path,
    captured_warnings: pytest.LogCaptureFixture,
) -> None:
    vocab = _deny_vocabulary(tmp_path, "deny:\n- genre: oi\n  artists: [椎名林檎, Cannons]\n")
    settings = _settings()

    # Every name without an ASCII letter or digit shares the empty fold-key.
    unrelated = classify_genres(_CANNONS_TAGS, None, vocab, settings, lookup_artist="東京事変")
    assert unrelated == ["indie pop", "oi"]
    named = classify_genres(_CANNONS_TAGS, None, vocab, settings, lookup_artist="Cannons")
    assert named == ["indie pop"]
    assert any("empty key" in record.message for record in captured_warnings.records)


def test_overlay_deny_artist_yaml_reads_as_non_string_warns_and_is_skipped(
    tmp_path: Path,
    captured_warnings: pytest.LogCaptureFixture,
) -> None:
    # Unquoted, YAML reads Yes as a boolean and 311 as an integer, never as band names.
    vocab = _deny_vocabulary(tmp_path, "deny:\n- genre: oi\n  artists: [Yes, 311, Genesis]\n")

    assert vocab.denied_for_artists == {"oi": frozenset({"genesis"})}
    assert vocab.denies("oi", "Genesis")
    assert not vocab.denies("oi", "Yes")
    assert not vocab.denies("oi", "311")
    non_string = [
        record for record in captured_warnings.records if "not a string" in record.message
    ]
    assert len(non_string) == 2


@pytest.mark.parametrize(
    "deny_yaml",
    ["deny:\n- oi\n", "deny: oi\n", "deny:\n  genre: oi\n"],
    ids=["entry-not-a-mapping", "deny-a-string", "deny-a-mapping"],
)
def test_overlay_deny_value_with_unusable_shape_warns_and_denies_nothing(
    tmp_path: Path,
    captured_warnings: pytest.LogCaptureFixture,
    deny_yaml: str,
) -> None:
    vocab = _deny_vocabulary(tmp_path, deny_yaml)

    assert vocab.denied_everywhere == frozenset()
    assert vocab.denied_for_artists == {}
    assert any("skipping" in record.message for record in captured_warnings.records)
