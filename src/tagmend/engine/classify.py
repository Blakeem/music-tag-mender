"""Genre resolution: controlled vocabulary + the Last.fm tag → canonical-genre pipeline.

This module turns raw Last.fm community tags into a clean, ordered list of canonical
genre names spelled against the bundled **MusicBrainz** vocabulary. It owns two pieces:

* :func:`load_vocabulary` — load ``data/genre_vocabulary.yml`` (generated, collision-free
  by construction) and merge the user-editable ``data/genre_overlay.yml`` over it per
  ``docs/genre-tagging-spec.md`` §4.5, producing a frozen :class:`Vocabulary` whose
  ``fold-key → canonical name`` index is single-valued.
* :func:`resolve_genres` — the pure pipeline of spec §6: fold-match each tag to a
  canonical name, drop sub-threshold weights, merge artist + album by *max* weight, order
  by weight desc then name asc, and optionally cap at ``genre_max_count``.

The **fold-key** (``fold``) is a match/dedup key only; the **canonical spelling** (the
vocabulary ``name``) is what gets written to files. Conflating the two is the main source
of bugs — see spec §3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Final

import yaml

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

_FOLD_STRIP: Final = re.compile(r"[^a-z0-9]+")


def fold(s: str) -> str:
    """Return the fold-key for *s*: lowercase, then strip everything non-``[a-z0-9]``.

    The canonical definition from ``docs/genre-tagging-spec.md`` §3. Used only to compare
    spelling/spacing/punctuation variants as equal — never written to disk.
    """
    return _FOLD_STRIP.sub("", s.lower())


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """An immutable ``fold-key → canonical name`` genre index.

    Built by :func:`load_vocabulary`. ``match`` folds an incoming Last.fm tag name and
    returns the canonical spelling to write, or ``None`` when the tag is not a known genre.
    """

    index: Mapping[str, str]

    def match(self, tag_name: str) -> str | None:
        """Return the canonical genre name for *tag_name*, or ``None`` if not in vocab."""
        return self.index.get(fold(tag_name))

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
    is logged and skipped rather than crashing (spec §4.5).
    """
    # Input: parse both YAML layers into {name, aliases} entry lists.
    vocab_entries = _load_entries(vocabulary_path, _VOCABULARY_RESOURCE)
    overlay_entries = _load_entries(overlay_path, _OVERLAY_RESOURCE)

    # Process: build the base index (single-valued by construction), then merge overlay.
    index = _build_base_index(vocab_entries)
    _merge_overlay(index, overlay_entries)

    # Output: an immutable view over the assembled index.
    return Vocabulary(index=dict(index))


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
        _claim_or_raise(index, fold(name), name, label="name")
        for alias in _entry_aliases(entry):
            key = fold(alias)
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

        name_key = fold(name)
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
            _merge_overlay_alias(index, fold(alias), alias, canonical)


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


def _load_entries(
    path: Path | None,
    resource: str,
) -> list[Mapping[str, object]]:
    """Read a genre YAML file's ``genres:`` list from *path* or the bundled *resource*."""
    text = _read_text(path, resource)
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        return []
    genres = document.get("genres")
    if not isinstance(genres, list):
        return []
    return [entry for entry in genres if isinstance(entry, dict)]


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


def resolve_genres(
    artist_tags: list[Tag],
    album_tags: list[Tag] | None,
    vocab: Vocabulary,
    settings: Settings,
) -> list[str]:
    """Resolve Last.fm tags to an ordered list of canonical genre names (spec §6).

    Pure. For each present source (artist, and album when not ``None``): fold-match each
    tag to a canonical name via *vocab* (dropping non-matches), and drop tags weighing
    less than ``settings.genre_min_weight``. The sources are unioned with the merged weight
    per genre taken as the **max** across the sources it appeared in. The result is ordered
    by merged weight descending then canonical name ascending, and capped at
    ``settings.genre_max_count`` when that is set.
    """
    # Input: per-source survivors → canonical name with its (filtered) source weight.
    artist_survivors = _survivors(artist_tags, vocab, settings.genre_min_weight)
    album_survivors = (
        _survivors(album_tags, vocab, settings.genre_min_weight) if album_tags is not None else {}
    )

    # Process: merge by max weight, then order by weight desc, name asc.
    merged = _merge_max(artist_survivors, album_survivors)
    ordered = sorted(merged.items(), key=lambda item: (-item[1], item[0]))
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
