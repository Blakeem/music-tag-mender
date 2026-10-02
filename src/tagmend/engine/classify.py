"""Genre resolution: controlled vocabulary + the Last.fm tag → canonical-genre pipeline.

This module turns raw Last.fm community tags into a clean, ordered list of canonical
genre names spelled against the bundled **MusicBrainz** vocabulary. It owns two pieces:

* :func:`load_vocabulary`: load ``data/genre_vocabulary.yml`` (generated, collision-free
  by construction) and merge the user-editable ``data/genre_overlay.yml`` over it per
  ``docs/genre-tagging-spec.md`` §4.5, producing a frozen :class:`Vocabulary` whose
  ``fold-key → canonical name`` index is single-valued and which carries the overlay's
  ``deny:`` rules.
* :func:`classify_genres`: the pure pipeline of spec §6. It fold-matches each tag to a
  canonical name, drops sub-threshold weights, merges artist + album by *max* weight, drops
  the genres the overlay denies for the lookup artist, orders by weight desc then name asc,
  and optionally caps at ``genre_max_count``.

The **fold-key** (:func:`tagmend.engine.text_keys.alnum_key`) is a match/dedup key only. The
**canonical spelling** (the vocabulary ``name``) is what gets written to files. Conflating
the two is the main source of bugs (see spec §3).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Final

import yaml

from tagmend.engine.text_keys import alnum_key
from tagmend.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from tagmend.config import Settings
    from tagmend.engine.lastfm import Tag

logger = get_logger(__name__)

_DATA_PACKAGE: Final = "tagmend"
_VOCABULARY_RESOURCE: Final = "genre_vocabulary.yml"
_OVERLAY_RESOURCE: Final = "genre_overlay.yml"


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """An immutable ``fold-key → canonical name`` genre index plus the overlay's deny rules.

    Built by :func:`load_vocabulary`. ``match`` folds an incoming Last.fm tag name and
    returns the canonical spelling to write, or ``None`` when the tag is not a known genre.
    ``denied_everywhere`` holds the canonical genres denied for every artist.
    ``denied_for_artists`` maps a canonical genre to the artist fold-keys it is denied for.
    """

    index: Mapping[str, str]
    denied_everywhere: frozenset[str]
    denied_for_artists: Mapping[str, frozenset[str]]

    def match(self, tag_name: str) -> str | None:
        """Return the canonical genre name for *tag_name*, or ``None`` if not in vocab."""
        return self.index.get(alnum_key(tag_name))

    def denies(self, genre: str, artist: str) -> bool:
        """Return whether the overlay denies canonical *genre* for lookup *artist*."""
        if genre in self.denied_everywhere:
            return True
        return alnum_key(artist) in self.denied_for_artists.get(genre, frozenset())

    def __len__(self) -> int:
        """Return the number of distinct fold-keys (matchable spellings) in the index."""
        return len(self.index)


# --- vocabulary loading --------------------------------------------------------------


def load_vocabulary(
    *,
    vocabulary_path: Path | None = None,
    overlay_path: Path | None = None,
) -> Vocabulary:
    """Load the genre vocabulary and merge the overlay over it into a :class:`Vocabulary`.

    Defaults load the two bundled files via :mod:`importlib.resources`. The optional path
    params let tests substitute temp files (e.g. to exercise overlay collisions).

    The generated vocabulary is collision-free by construction, so a duplicate fold-key
    there is a real build bug and raises :class:`ValueError`. The overlay is user-editable,
    so its collisions are tolerated: an alias/name folding to a *different* existing genre
    is logged and skipped rather than crashing (spec §4.5). The overlay's ``deny:`` entries
    resolve against the merged index, so a deny can name an overlay genre.
    """
    # Input: parse both YAML layers into {name, aliases} and {genre, artists} entry lists.
    vocab_document = _load_document(vocabulary_path, _VOCABULARY_RESOURCE)
    overlay_document = _load_document(overlay_path, _OVERLAY_RESOURCE)
    vocab_entries = _document_entries(vocab_document, "genres")
    overlay_entries = _document_entries(overlay_document, "genres")
    deny_entries = _deny_entries(overlay_document)

    # Process: build the base index (single-valued by construction), merge the overlay, then
    # resolve the deny rules against the merged index.
    index = _build_base_index(vocab_entries)
    _merge_overlay(index, overlay_entries)
    denied_everywhere, denied_for_artists = _build_deny_rules(index, deny_entries)

    # Output: an immutable view over the assembled index and deny rules.
    return Vocabulary(
        index=dict(index),
        denied_everywhere=denied_everywhere,
        denied_for_artists=denied_for_artists,
    )


def _build_base_index(entries: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """Index the generated vocabulary, asserting it stays single-valued (it must be).

    A name or alias whose fold-key is already owned signals a corrupt/buggy generated
    file (it is collision-free by construction), so this raises rather than papering over
    silent mis-mapping.
    """
    index: dict[str, str] = {}
    for entry in entries:
        name = _entry_name(entry)
        if name is None:
            continue
        _claim_or_raise(index, alnum_key(name), name, label="name")
        for alias in _entry_aliases(entry):
            key = alnum_key(alias)
            if key in index and index[key] != name:
                _claim_or_raise(index, key, name, label="alias", source=alias)
            index.setdefault(key, name)
    return index


def _claim_or_raise(
    index: dict[str, str],
    key: str,
    name: str,
    *,
    label: str,
    source: str | None = None,
) -> None:
    """Map *key* → *name* in the base index, raising on a real (different-owner) collision."""
    existing = index.get(key)
    if existing is not None and existing != name:
        spelling = source if source is not None else name
        message = (
            f"vocabulary {label} {spelling!r} (fold {key!r}) collides: "
            f"already mapped to {existing!r}, cannot remap to {name!r}"
        )
        raise ValueError(message)
    index[key] = name


def _merge_overlay(
    index: dict[str, str],
    entries: Iterable[Mapping[str, object]],
) -> None:
    """Merge the user/AI overlay over the base index in place (spec §4.5).

    An overlay entry whose ``name`` folds to an existing genre adds its aliases to that
    genre; otherwise it becomes a new genre. For every name/alias, a fold-key already
    owned by a *different* genre is a collision: logged and skipped (the overlay is
    user-editable, never crash on it). A fold-key redundant with its own genre is a no-op.
    """
    for entry in entries:
        name = _entry_name(entry)
        if name is None:
            continue

        name_key = alnum_key(name)
        owner = index.get(name_key)
        if owner is not None and owner != name:
            logger.warning(
                "overlay genre %r (fold %r) collides with existing %r; skipping entry",
                name,
                name_key,
                owner,
            )
            continue

        # New genre, or an existing one we are extending with extra aliases.
        canonical = owner if owner is not None else name
        index.setdefault(name_key, canonical)
        for alias in _entry_aliases(entry):
            _merge_overlay_alias(index, alnum_key(alias), alias, canonical)


def _merge_overlay_alias(
    index: dict[str, str],
    key: str,
    spelling: str,
    canonical: str,
) -> None:
    """Add one overlay alias to *canonical*, or warn-and-skip on a cross-genre collision."""
    existing = index.get(key)
    if existing is None:
        index[key] = canonical
        return
    if existing != canonical:
        logger.warning(
            "overlay alias %r (fold %r) collides with existing %r; skipping alias",
            spelling,
            key,
            existing,
        )


def _build_deny_rules(
    index: Mapping[str, str],
    entries: Iterable[Mapping[str, object]],
) -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    """Resolve the overlay's ``deny:`` entries against the merged index (spec §4.5).

    Returns the genres denied everywhere and, per genre, the artist fold-keys it is denied
    for. An entry naming no vocabulary genre, or with an unusable ``artists`` value, is logged
    and skipped like an overlay collision. An absent ``artists`` key denies the genre everywhere.
    """
    everywhere: set[str] = set()
    for_artists: dict[str, set[str]] = {}
    for entry in entries:
        genre = _deny_genre(index, entry)
        if genre is None:
            continue
        if "artists" not in entry:
            everywhere.add(genre)
            continue
        artist_keys = _deny_artist_keys(entry.get("artists"), genre)
        for_artists.setdefault(genre, set()).update(artist_keys)
    denied_for_artists = {genre: frozenset(keys) for genre, keys in for_artists.items() if keys}
    return frozenset(everywhere), denied_for_artists


def _deny_genre(index: Mapping[str, str], entry: Mapping[str, object]) -> str | None:
    """Return the canonical genre a deny entry names, or ``None`` after logging why not."""
    raw = entry.get("genre")
    if not isinstance(raw, str) or not raw.strip():
        logger.warning("overlay deny entry %r names no genre, skipping entry", entry)
        return None
    key = alnum_key(raw)
    canonical = index.get(key)
    if canonical is None:
        logger.warning(
            "overlay deny genre %r (fold %r) is not in the vocabulary, skipping entry",
            raw,
            key,
        )
    return canonical


def _deny_artist_keys(raw: object, genre: str) -> set[str]:
    """Return the artist fold-keys of one deny entry, logging every name it cannot use.

    YAML reads an unquoted name such as ``Yes`` or ``311`` as a boolean or a number, so a
    non-string name is skipped rather than guessed at. A name folding to an empty key is
    skipped, since every name without an ASCII letter or digit folds to that same key and the
    deny would reach unrelated artists.
    """
    keys: set[str] = set()
    if not isinstance(raw, list):
        logger.warning("overlay deny for %r has a non-list artists value, skipping entry", genre)
        return keys
    for name in raw:
        if not isinstance(name, str):
            logger.warning(
                "overlay deny artist %r for %r is not a string (quote it in YAML), skipping artist",
                name,
                genre,
            )
            continue
        key = alnum_key(name)
        if not key:
            logger.warning(
                "overlay deny artist %r for %r folds to an empty key, skipping artist",
                name,
                genre,
            )
            continue
        keys.add(key)
    if not keys:
        logger.warning("overlay deny for %r names no usable artist, skipping entry", genre)
    return keys


def _load_document(path: Path | None, resource: str) -> Mapping[str, object]:
    """Parse a genre YAML file from *path* or the bundled *resource* into its top mapping."""
    document = yaml.safe_load(_read_text(path, resource))
    if not isinstance(document, dict):
        return {}
    return document


def _document_entries(document: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    """Return the mapping entries of the *key* list in a parsed genre YAML document."""
    entries = document.get(key)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _deny_entries(document: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Return the mapping entries of the overlay's ``deny:`` list, logging every other item."""
    raw = document.get("deny")
    entries: list[Mapping[str, object]] = []
    if raw is None:
        return entries
    if not isinstance(raw, list):
        logger.warning("overlay deny value %r is not a list, skipping every deny", raw)
        return entries
    for item in raw:
        if not isinstance(item, dict):
            logger.warning(
                "overlay deny entry %r is not a {genre, artists} mapping, skipping entry",
                item,
            )
            continue
        entries.append(item)
    return entries


def _read_text(path: Path | None, resource: str) -> str:
    """Return the YAML text from an explicit *path*, else the bundled *resource*."""
    if path is not None:
        return path.read_text(encoding="utf-8")
    data_resource = resources.files(_DATA_PACKAGE) / "data" / resource
    return data_resource.read_text(encoding="utf-8")


def _entry_name(entry: Mapping[str, object]) -> str | None:
    """Return a non-empty ``name`` string from a genre entry, or ``None`` if unusable."""
    raw = entry.get("name")
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    return name or None


def _entry_aliases(entry: Mapping[str, object]) -> list[str]:
    """Return the non-empty string aliases of a genre entry (tolerating a missing list)."""
    raw = entry.get("aliases")
    if not isinstance(raw, list):
        return []
    return [alias.strip() for alias in raw if isinstance(alias, str) and alias.strip()]


# --- resolution pipeline -------------------------------------------------------------


def classify_genres(
    artist_tags: list[Tag],
    album_tags: list[Tag] | None,
    vocab: Vocabulary,
    settings: Settings,
    *,
    lookup_artist: str,
) -> list[str]:
    """Resolve Last.fm tags to an ordered list of canonical genre names (spec §6).

    Pure. For each present source (artist, and album when not ``None``): fold-match each
    tag to a canonical name via *vocab* (dropping non-matches), and drop tags weighing
    less than ``settings.genre_min_weight``. The sources are unioned with the merged weight
    per genre taken as the **max** across the sources it appeared in. A genre the overlay
    denies for *lookup_artist* is dropped before ordering, so it never takes a capped slot.
    The result is ordered by merged weight descending then canonical name ascending, and
    capped at ``settings.genre_max_count`` when that is set.
    """
    # Input: per-source survivors → canonical name with its (filtered) source weight.
    artist_survivors = _survivors(artist_tags, vocab, settings.genre_min_weight)
    album_survivors = (
        _survivors(album_tags, vocab, settings.genre_min_weight) if album_tags is not None else {}
    )

    # Process: merge by max weight, drop denied genres, then order by weight desc, name asc.
    merged = _merge_max(artist_survivors, album_survivors)
    allowed = {
        name: weight for name, weight in merged.items() if not vocab.denies(name, lookup_artist)
    }
    ordered = sorted(allowed.items(), key=lambda item: (-item[1], item[0]))
    names = [name for name, _weight in ordered]

    # Output: optionally cap to the top N.
    if settings.genre_max_count is not None:
        return names[: settings.genre_max_count]
    return names


def _survivors(
    tags: list[Tag],
    vocab: Vocabulary,
    min_weight: int,
) -> dict[str, int]:
    """Map a source's tags to ``canonical name → weight``, dropping non-vocab and weak tags.

    A canonical name matched by several raw spellings in one source keeps its max weight.
    """
    survivors: dict[str, int] = {}
    for tag in tags:
        if tag.weight < min_weight:
            continue
        name = vocab.match(tag.name)
        if name is None:
            continue
        survivors[name] = max(survivors.get(name, tag.weight), tag.weight)
    return survivors


def _merge_max(
    artist: Mapping[str, int],
    album: Mapping[str, int],
) -> dict[str, int]:
    """Union two ``name → weight`` maps, keeping the max weight per name (spec §6 step 4)."""
    merged: dict[str, int] = dict(artist)
    for name, weight in album.items():
        merged[name] = max(merged.get(name, weight), weight)
    return merged
