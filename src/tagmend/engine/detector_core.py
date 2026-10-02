"""The shared core of the ``detect_*`` family: tiers, folder buckets, views and positions."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from tagmend.engine import path_keys
from tagmend.engine.text_keys import alnum_ascii_key, display_key
from tagmend.engine.validation import require_choice

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, Iterable, Mapping

    from _typeshed import DataclassInstance


class Tier(StrEnum):
    """Confidence tier for a flagged row (most to least severe)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


TIER_RANK: Final = {Tier.HIGH: 0, Tier.MEDIUM: 1, Tier.LOW: 2}
TIERS: Final = frozenset(t.value for t in Tier)

# Leaf folder names that are not normal albums: a guest or other artist here is legitimate,
# so such a folder holds several releases by design.
NON_ALBUM_FOLDERS: Final = frozenset(
    {"singles", "featured", "remixes", "bonus", "live", "ep"},
)

# A tracknumber/discnumber may be stored as "7" or as the "7/12" slash form. Only the part
# before the slash is the position.
_SLASH: Final = "/"

# The values Go's ``strconv.ParseBool`` accepts, which is what a server actually tests the
# compilation tag with. ``yes`` is a real tag value in the wild and reads as false.
COMPILATION_TRUE: Final = frozenset({"1", "t", "T", "true", "TRUE", "True"})
VARIOUS_ARTISTS: Final = "Various Artists"
_UNKNOWN_ARTIST: Final = "[Unknown Artist]"


class _HasFolder(Protocol):
    """Anything that sits in one library folder."""

    @property
    def folder(self) -> str: ...


class _HasTier(Protocol):
    """A detector row that carries a tier."""

    @property
    def tier(self) -> str: ...


class _TieredRow(_HasFolder, _HasTier, Protocol):
    """A detector row that sits in one folder and carries a tier."""


class _FieldRow(_TieredRow, Protocol):
    """A detector row that reports one field of one file."""

    @property
    def file_id(self) -> int: ...

    @property
    def filename(self) -> str: ...

    @property
    def field(self) -> str: ...


def validate_tier(tier: str | None) -> None:
    """Raise :class:`ValueError` for a *tier* outside :data:`TIERS`. ``None`` passes."""
    require_choice("tier", tier, TIERS)


# Punctuation-insensitive, because a folder named ``E.P.`` or ``Live!`` holds an EP or a live set.
_NON_ALBUM_KEYS: Final = frozenset(alnum_ascii_key(name) for name in NON_ALBUM_FOLDERS)


def is_non_album_folder(folder: str) -> bool:
    """Return whether *folder*'s leaf name marks a collection rather than one album."""
    return alnum_ascii_key(Path(folder).name) in _NON_ALBUM_KEYS


def display_album_artist(
    albumartist: str | None,
    compilation: str | None,
    artist: str | None,
) -> str:
    """Return the album artist a server would group a file under.

    The compilation marker outranks the track artist, so a various-artists release with no
    album artist stays one album instead of scattering across every track's artist.
    """
    if albumartist and albumartist.strip():
        return albumartist.strip()
    if (compilation or "").strip() in COMPILATION_TRUE:
        return VARIOUS_ARTISTS
    if artist and artist.strip():
        return artist.strip()
    return _UNKNOWN_ARTIST


def album_identity(
    release_mbid: str | None,
    album_artist: str,
    album: str | None,
    date: str | None,
) -> tuple[str, ...]:
    """Return the tuple that decides which album a file belongs to.

    *album_artist* is the :func:`display_album_artist` of the file. A release id settles the
    file on its own. A date is compared verbatim, since ``2005`` and ``2005-06-01`` group apart.
    """
    release = (release_mbid or "").strip()
    if release:
        return ("release", release)
    return ("name", display_key(album_artist), display_key(album or ""), (date or "").strip())


def release_date(values: Mapping[str, str]) -> str | None:
    """Return the release date :func:`album_identity` compares, from one file's tag values."""
    # A raw Vorbis YEAR is the only release-date spelling no alias maps to ``date``.
    return (values.get("date") or "").strip() or values.get("year")


def group_by_key[T, K](items: Iterable[T], key: Callable[[T], K]) -> dict[K, list[T]]:
    """Bucket *items* by *key*, preserving first-seen order within each bucket."""
    grouped: dict[K, list[T]] = {}
    for item in items:
        grouped.setdefault(key(item), []).append(item)
    return grouped


def _folder_of(item: _HasFolder) -> str:
    """Return the folder *item* sits in."""
    return item.folder


def group_by_folder[T: _HasFolder](items: Iterable[T]) -> dict[str, list[T]]:
    """Bucket *items* by their folder, preserving first-seen order within each folder."""
    return group_by_key(items, _folder_of)


def ordered[R: _FieldRow](rows: list[R]) -> list[R]:
    """Return *rows* most-severe first, then stably by location and field."""
    return sorted(rows, key=lambda r: (TIER_RANK[Tier(r.tier)], r.folder, r.filename, r.field))


def tiers_by_file(rows: Iterable[_FieldRow]) -> Counter[str]:
    """Count files by their most severe row, so the counts sum to the file count."""
    worst: dict[int, Tier] = {}
    for row in rows:
        tier = Tier(row.tier)
        current = worst.get(row.file_id)
        if current is None or TIER_RANK[tier] < TIER_RANK[current]:
            worst[row.file_id] = tier
    return Counter(tier.value for tier in worst.values())


def rows_in_tier[T: _HasTier](rows: list[T], tier: str | None) -> list[T]:
    """Return the *rows* in *tier*, or every row when *tier* is ``None``.

    Every tiered detector filters by tier first and builds its grouped view from the result, so
    a group appears only when it holds a file of that tier.
    """
    if tier is None:
        return rows
    return [row for row in rows if row.tier == tier]


def regroup[G: _HasFolder, R: _HasFolder](
    groups: list[G],
    rows: list[R],
    refold: Callable[[G, list[R]], G],
    *,
    key: Callable[[G | R], Hashable] = _folder_of,
) -> list[G]:
    """Refold each of *groups* over the *rows* sharing its *key*, dropping a group with none."""
    rows_by_key = group_by_key(rows, key)
    return [refold(group, rows_by_key[key(group)]) for group in groups if key(group) in rows_by_key]


def narrow[D: DataclassInstance, R: _TieredRow, G: _HasFolder](  # noqa: PLR0913 - one keyword per view knob and per detector difference
    report: D,
    *,
    rows: list[R],
    groups: list[G],
    secondary_field: str,
    secondary_rows: list[R],
    secondary_in_tier: bool,
    refold: Callable[[G, list[R]], G],
    key: Callable[[G | R], Hashable] = _folder_of,
    tier: str | None,
    folder_key: str | None,
    limit: int | None,
    group: bool,
) -> D:
    """Return *report* with its rows and groups narrowed for display. The run counts never change.

    *secondary_field* names the report's second row list, which every filter narrows alongside
    *rows*, so a caller expanding one folder does not also receive every other folder's rows. A
    *tier* keeps the secondary rows of that tier when *secondary_in_tier* is set, and otherwise
    drops them, since context rows sit outside every tier count. Groups ride only on the grouped
    view. A *folder_key* wins over *group*: that call returns the folder's flat rows and no
    groups. A *tier* filters the rows first, and *refold* rebuilds each group, matched by *key*,
    over the rows the filter kept.
    """
    # Input: the tier filter, which also decides which groups survive.
    view_rows = rows_in_tier(rows, tier)
    if secondary_in_tier:
        view_secondary = rows_in_tier(secondary_rows, tier)
    else:
        view_secondary = secondary_rows if tier is None else []
    tier_groups = (
        groups if tier is None else regroup(groups, view_rows + view_secondary, refold, key=key)
    )

    # Process: the folder and the limit, then the flat or the grouped shape.
    if folder_key is not None:
        view_rows = [r for r in view_rows if path_keys.path_key(r.folder) == folder_key]
        view_secondary = [r for r in view_secondary if path_keys.path_key(r.folder) == folder_key]
    if limit is not None:
        view_rows = view_rows[:limit]
        view_secondary = view_secondary[:limit]
    flat = not group or folder_key is not None
    view_groups = [] if flat else tier_groups
    if limit is not None:
        view_groups = view_groups[:limit]

    # Output: every other report field describes the whole run, which no view changes.
    return replace(
        report,
        rows=view_rows if flat else [],
        groups=view_groups,
        **{secondary_field: view_secondary if flat else []},
    )


def parse_position(value: str | None) -> int | None:
    """Return the position of an ``n`` / ``n/total`` tag value, or ``None`` if unusable."""
    if not value:
        return None
    head = value.split(_SLASH, 1)[0].strip()
    # isdecimal, not isdigit: isdigit accepts superscripts and enclosed digits that int()
    # rejects, and one such tag would abort the whole run.
    return int(head) if head.isdecimal() else None
