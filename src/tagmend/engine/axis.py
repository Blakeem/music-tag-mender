"""The metadata-axis abstraction: one outcome-row status model for genre, artist, year and song.

Each tag axis keeps at most one ``file_<axis>_status`` row per file. The row holds one outcome
(``done``, ``no_match`` or ``manual``) and two snapshots: the identity the outcome was decided
against (the two :attr:`Axis.source_columns`) and the values of :attr:`Axis.fields` it describes
(``source_value``, JSON, NULL on a row written before the snapshot existed).
:func:`tagmend.engine.store.derived_status` derives every user-facing status from that row, the
staging area and the current tags. A ``done`` or ``no_match`` row counts only while both snapshots
still match the file, so a revert, a rescan after an external edit or an identity fix re-opens the
file with no writer. A ``manual`` row is sticky until ``reset_<axis>_status`` deletes it.

The mismatch axis keeps path decisions instead of outcomes. Its one ``source_value`` JSON holds
the snapshot, and :mod:`tagmend.engine.mismatch` classifies it against the comparator.

Like the rest of :mod:`tagmend.engine.store`, the helpers here never commit. The conn-owning
layer owns the transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Identity:
    """The two identity values an outcome is decided against, in source-column order."""

    primary: str | None
    secondary: str | None


@dataclass(frozen=True, slots=True)
class Axis:
    """Everything that differs between the metadata axes.

    A new axis slots in as one of these values plus a resolver, never a copied module. A
    resolver's outcome reads only the axis's identity and fields, which is what lets a snapshot
    mismatch alone re-open a file.
    """

    name: str
    """The key used in tool params, ``list_files`` filters and ``get_library_stats`` blocks."""

    fields: tuple[str, ...]
    """The managed tags this axis decides: the staged test, the value snapshot and the commit
    writer's ``manual`` trigger."""

    status_table: str
    """The ``file_<name>_status`` table name."""

    source_columns: tuple[str, str] | None
    """The table's two identity column names, in :class:`Identity` order. ``None`` on the
    mismatch axis, which keeps no outcome rows."""

    workflow_statuses: frozenset[str]
    """The valid derived-status set."""

    scope_fields: tuple[str, ...]
    """The tags a resolver's or status tool's ``value`` matches exactly."""

    identity: Callable[[Mapping[str, list[str]]], Identity | None] | None
    """Current tags to the identity an outcome is decided against, or ``None`` when the file
    has none. ``None`` on the mismatch axis, which keeps dispositions instead of outcomes."""


# --- lookup identity -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LookupIdentity:
    """The ``(artist, album)`` a file is looked up / classified against."""

    artist: str | None
    album: str | None


def first_nonblank(values: list[str] | None) -> str | None:
    """Return the first value that is non-blank after ``str.strip()``, else ``None``.

    Whitespace-only tag values (spaces, tabs, newlines) count as absent, so a file holding only
    such values has no identity rather than looking up ``" "`` junk. The value is returned
    verbatim (unstripped), preserving the exact lookup string for non-blank tags.
    """
    if not values:
        return None
    for value in values:
        if value.strip():
            return value
    return None


def lookup_identity(tags: Mapping[str, list[str]]) -> LookupIdentity:
    """Derive the lookup identity for a file's tags.

    Lookup artist is the first non-blank ``albumartist`` value when one exists (better
    identity for compilations), else the first non-blank ``artist`` value, or ``None`` when
    neither has a non-blank value. Album is the first non-blank ``album`` value, or ``None``.
    """
    lookup_artist = first_nonblank(tags.get("albumartist")) or first_nonblank(
        tags.get("artist"),
    )
    lookup_album = first_nonblank(tags.get("album"))
    return LookupIdentity(artist=lookup_artist, album=lookup_album)


def _genre_identity(tags: Mapping[str, list[str]]) -> Identity | None:
    """Genre identity: (albumartist-else-artist, album), ``None`` without either artist field."""
    lookup = lookup_identity(tags)
    if lookup.artist is None:
        return None
    return Identity(primary=lookup.artist, secondary=lookup.album)


def _year_identity(tags: Mapping[str, list[str]]) -> Identity | None:
    """Year identity: the genre identity, ``None`` also without an album to look up."""
    lookup = lookup_identity(tags)
    if lookup.artist is None or lookup.album is None:
        return None
    return Identity(primary=lookup.artist, secondary=lookup.album)


def _artist_identity(tags: Mapping[str, list[str]]) -> Identity | None:
    """Artist identity: (first artist, first albumartist), ``None`` when both are blank."""
    artist = first_nonblank(tags.get("artist"))
    albumartist = first_nonblank(tags.get("albumartist"))
    if artist is None and albumartist is None:
        return None
    return Identity(primary=artist, secondary=albumartist)


def _song_identity(tags: Mapping[str, list[str]]) -> Identity:
    """Song identity: (release id, release-track id), each blank as ``""`` and never ``None``.

    The song axis identifies a file by its audio, so a file with no artist or album still has a
    song status. Carrying the release ids makes a rebind re-open a ``done`` row.
    """
    return Identity(
        primary=first_nonblank(tags.get("musicbrainz_albumid")) or "",
        secondary=first_nonblank(tags.get("musicbrainz_releasetrackid")) or "",
    )


# --- the five axes -------------------------------------------------------------------

_TAG_AXIS_STATUSES: Final = frozenset(
    {"pending", "no_identity", "no_match", "manual", "staged", "done"},
)

GENRE_AXIS: Final = Axis(
    name="genre",
    fields=("genre",),
    status_table="file_genre_status",
    source_columns=("source_artist", "source_album"),
    workflow_statuses=_TAG_AXIS_STATUSES,
    scope_fields=("artist", "albumartist"),
    identity=_genre_identity,
)

ARTIST_AXIS: Final = Axis(
    name="artist",
    # Every field resolve_artists writes, so a sort-name or id normalisation is part of the
    # decided value and a human edit of any of them is a human decision on this axis.
    fields=(
        "artist",
        "albumartist",
        "artists",
        "musicbrainz_artistid",
        "musicbrainz_albumartistid",
        "artistsort",
        "albumartistsort",
    ),
    status_table="file_artist_status",
    source_columns=("source_artist", "source_albumartist"),
    workflow_statuses=_TAG_AXIS_STATUSES,
    scope_fields=("artist", "albumartist"),
    identity=_artist_identity,
)

YEAR_AXIS: Final = Axis(
    name="year",
    fields=("originaldate",),
    status_table="file_year_status",
    source_columns=("source_artist", "source_album"),
    workflow_statuses=_TAG_AXIS_STATUSES,
    scope_fields=("album",),
    identity=_year_identity,
)

# The song resolver never writes no_match, since an empty AcoustID answer can be a timeout, and
# its identity is never None.
SONG_AXIS: Final = Axis(
    name="song",
    fields=("title", "tracknumber", "discnumber"),
    status_table="file_song_status",
    source_columns=("source_release_mbid", "source_release_track_mbid"),
    workflow_statuses=frozenset({"pending", "manual", "staged", "done"}),
    # No lookup name field exists on this axis, so a value scopes a whole album.
    scope_fields=("album",),
    identity=_song_identity,
)

# A path decision settles no tag, so the axis decides no field and has no staged state.
MISMATCH_AXIS: Final = Axis(
    name="mismatch",
    fields=(),
    status_table="file_mismatch_status",
    source_columns=None,
    workflow_statuses=frozenset({"pending", "legit_ignore", "misfiled_deferred"}),
    scope_fields=("artist", "albumartist"),
    identity=None,
)

# The axes that keep outcome rows, in the order every report lists them.
TAG_AXES: Final = (GENRE_AXIS, ARTIST_AXIS, YEAR_AXIS, SONG_AXIS)

# The outcomes only a resolver writes. Their rows count only while both snapshots match.
RESOLVER_OUTCOMES: Final = frozenset({"done", "no_match"})


def identity_of(axis: Axis, tags: Mapping[str, list[str]]) -> Identity | None:
    """Return *axis*'s identity for *tags*. Raises :class:`ValueError` for the mismatch axis."""
    if axis.identity is None:
        message = f"the {axis.name} axis keeps no outcome rows"
        raise ValueError(message)
    return axis.identity(tags)


def _outcome_columns(axis: Axis) -> tuple[str, str]:
    """Return *axis*'s two identity columns. Raises :class:`ValueError` for the mismatch axis."""
    if axis.source_columns is None:
        message = f"the {axis.name} axis keeps no outcome rows"
        raise ValueError(message)
    return axis.source_columns


def field_values(axis: Axis, tags: Mapping[str, list[str]]) -> dict[str, list[str]]:
    """Return the values of every :attr:`Axis.fields` tag, an absent tag as ``[]``."""
    return {name: list(tags.get(name, [])) for name in axis.fields}


# --- outcome rows (the tag axes) -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutcomeRow:
    """One tag-axis status row: the outcome plus its identity and value snapshots.

    ``values`` is ``None`` on a row written before the value snapshot existed, which the
    classifier treats as matching.
    """

    status: str
    identity: Identity
    values: dict[str, list[str]] | None


def get_outcome(conn: sqlite3.Connection, axis: Axis, file_id: int) -> OutcomeRow | None:
    """Return *file_id*'s outcome row on the tag *axis*, or ``None`` if it has none."""
    primary_col, secondary_col = _outcome_columns(axis)
    row = conn.execute(
        f"SELECT status, {primary_col}, {secondary_col}, source_value "  # noqa: S608 - trusted Axis
        f"FROM {axis.status_table} WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    if row is None:
        return None
    values = None if row[3] is None else cast("dict[str, list[str]]", json.loads(str(row[3])))
    return OutcomeRow(
        status=str(row[0]),
        identity=Identity(
            primary=None if row[1] is None else str(row[1]),
            secondary=None if row[2] is None else str(row[2]),
        ),
        values=values,
    )


def put_outcome(  # noqa: PLR0913 - cohesive keyword-only outcome payload
    conn: sqlite3.Connection,
    axis: Axis,
    *,
    file_id: int,
    status: str,
    tags: Mapping[str, list[str]],
    now: str,
) -> None:
    """Insert or replace *file_id*'s outcome on the tag *axis*, snapshotting *tags*.

    *tags* are the values the outcome describes: the current tags, the tags a staged change
    commits, or the tags read back after a write. A file with no identity stores two NULLs.
    """
    identity = identity_of(axis, tags) or Identity(primary=None, secondary=None)
    source_value = json.dumps(field_values(axis, tags), sort_keys=True, separators=(",", ":"))
    primary_col, secondary_col = _outcome_columns(axis)
    conn.execute(
        f"INSERT OR REPLACE INTO {axis.status_table} ("  # noqa: S608 - identifiers from trusted Axis
        f"  file_id, status, {primary_col}, {secondary_col}, source_value, updated_at"
        f") VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, status, identity.primary, identity.secondary, source_value, now),
    )


# --- status row removal (parameterized by Axis) --------------------------------------


def delete_status(conn: sqlite3.Connection, axis: Axis, file_id: int) -> None:
    """Remove *file_id*'s stored decision on *axis* (no-op if none)."""
    conn.execute(
        f"DELETE FROM {axis.status_table} WHERE file_id = ?",  # noqa: S608 - table from trusted Axis
        (file_id,),
    )
