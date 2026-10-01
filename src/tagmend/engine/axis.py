"""The metadata-axis abstraction: one outcome-row status model for genre, artist and year.

Each tag axis (genre, artist, year) keeps at most one ``file_<axis>_status`` row per file. The row
holds one outcome (``done``, ``no_match`` or ``manual``) and two snapshots: the identity the outcome
was decided against (the two :attr:`Axis.source_columns`) and the values of :attr:`Axis.fields` it
describes (``source_value``, JSON, NULL on a row written before the snapshot existed).
:func:`tagmend.engine.store.derived_status` derives every user-facing status from that row, the
staging area and the current tags. A ``done`` or ``no_match`` row counts only while both snapshots
still match the file, so a revert, a rescan after an external edit or an identity fix re-opens the
file with no writer. A ``manual`` row is sticky until ``reset_<axis>_status`` deletes it.

The mismatch axis keeps its disposition model. A ``legit_ignore`` or ``misfiled_deferred`` row
blocks while its snapshotted tag value is unchanged (:func:`mismatch_decision_blocks`), and the
file is ``pending`` otherwise (:func:`tagmend.engine.store.derived_mismatch_status`).

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
class StatusRow:
    """One row from a ``file_<axis>_status`` table, with its two source-identity values.

    ``source_primary`` / ``source_secondary`` are positional: each axis maps them to its own
    column names via :attr:`Axis.source_columns`.
    """

    status: str
    source_primary: str | None
    source_secondary: str | None


@dataclass(frozen=True, slots=True)
class Identity:
    """The two identity values an outcome is decided against, in :class:`StatusRow` order."""

    primary: str | None
    secondary: str | None


@dataclass(frozen=True, slots=True)
class Axis:
    """Everything that differs between the metadata axes.

    A new axis slots in as one of these values plus a resolver, never a copied module.
    """

    name: str
    """The key used in tool params, ``list_files`` filters and ``get_library_stats`` blocks."""

    fields: tuple[str, ...]
    """The managed tags this axis decides: the staged test, the value snapshot and the commit
    writer's ``manual`` trigger."""

    status_table: str
    """The ``file_<name>_status`` table name."""

    source_columns: tuple[str, str]
    """The table's two identity column names, in :class:`StatusRow` order."""

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


def _album_identity(tags: Mapping[str, list[str]]) -> Identity | None:
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


# --- the four axes -------------------------------------------------------------------

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
    identity=_album_identity,
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

MISMATCH_AXIS: Final = Axis(
    name="mismatch",
    # The two identity fields the detector reads. This axis has no staged/done derivation,
    # and these fields collide with the artist axis's, so they never feed derived_status.
    fields=("albumartist", "artist"),
    status_table="file_mismatch_status",
    # Positional: source_primary = the disagreeing FIELD NAME, source_secondary = its VALUE.
    source_columns=("source_field", "source_value"),
    workflow_statuses=frozenset({"pending", "legit_ignore", "misfiled_deferred"}),
    scope_fields=("artist", "albumartist"),
    identity=None,
)

# The axes that keep outcome rows, in the order every report lists them.
TAG_AXES: Final = (GENRE_AXIS, ARTIST_AXIS, YEAR_AXIS)

# The outcomes only a resolver writes. Their rows count only while both snapshots match.
RESOLVER_OUTCOMES: Final = frozenset({"done", "no_match"})


def identity_of(axis: Axis, tags: Mapping[str, list[str]]) -> Identity | None:
    """Return *axis*'s identity for *tags*. Raises :class:`ValueError` for the mismatch axis."""
    if axis.identity is None:
        message = f"the {axis.name} axis keeps no outcome rows"
        raise ValueError(message)
    return axis.identity(tags)


def field_values(axis: Axis, tags: Mapping[str, list[str]]) -> dict[str, list[str]]:
    """Return the values of every :attr:`Axis.fields` tag, an absent tag as ``[]``."""
    return {name: list(tags.get(name, [])) for name in axis.fields}


def mismatch_decision_blocks(decision: StatusRow, identity: Identity) -> bool:
    """Whether a mismatch disposition still silences its file.

    ``source_primary`` is the FIELD NAME the disagreement was recorded on
    (``'albumartist'``/``'artist'``) and ``source_secondary`` is that field's VALUE at decision
    time. *identity* carries the file's CURRENT first ``albumartist`` (``primary``) and
    ``artist`` (``secondary``). A disposition blocks only while the snapshotted value equals
    the current value of its own source field, so any change to that field, its removal
    included, makes the disposition stale and the file re-surfaces. A ``None`` source field
    compares its snapshot against ``None``.
    """
    if decision.source_primary == "albumartist":
        current = identity.primary
    elif decision.source_primary == "artist":
        current = identity.secondary
    else:
        current = None
    return decision.source_secondary == current


# --- outcome rows (the three tag axes) -----------------------------------------------


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
    primary_col, secondary_col = axis.source_columns
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
    primary_col, secondary_col = axis.source_columns
    conn.execute(
        f"INSERT OR REPLACE INTO {axis.status_table} ("  # noqa: S608 - identifiers from trusted Axis
        f"  file_id, status, {primary_col}, {secondary_col}, source_value, updated_at"
        f") VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, status, identity.primary, identity.secondary, source_value, now),
    )


# --- generic status row access (parameterized by Axis) -------------------------------


def get_status(conn: sqlite3.Connection, axis: Axis, file_id: int) -> StatusRow | None:
    """Return *file_id*'s stored decision on *axis*, or ``None`` if it has none."""
    primary_col, secondary_col = axis.source_columns
    cursor = conn.execute(
        f"SELECT status, {primary_col}, {secondary_col} "  # noqa: S608 - column names from trusted Axis
        f"FROM {axis.status_table} WHERE file_id = ?",
        (file_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return StatusRow(
        status=str(row[0]),
        source_primary=None if row[1] is None else str(row[1]),
        source_secondary=None if row[2] is None else str(row[2]),
    )


def set_status(  # noqa: PLR0913 - cohesive keyword-only status payload
    conn: sqlite3.Connection,
    axis: Axis,
    *,
    file_id: int,
    status: str,
    source_primary: str | None,
    source_secondary: str | None,
    now: str,
) -> None:
    """Insert or replace *file_id*'s stored decision on *axis* with its two source values."""
    primary_col, secondary_col = axis.source_columns
    conn.execute(
        f"INSERT OR REPLACE INTO {axis.status_table} ("  # noqa: S608 - identifiers from trusted Axis
        f"  file_id, status, {primary_col}, {secondary_col}, updated_at"
        f") VALUES (?, ?, ?, ?, ?)",
        (file_id, status, source_primary, source_secondary, now),
    )


def delete_status(conn: sqlite3.Connection, axis: Axis, file_id: int) -> None:
    """Remove *file_id*'s stored decision on *axis* (no-op if none)."""
    conn.execute(
        f"DELETE FROM {axis.status_table} WHERE file_id = ?",  # noqa: S608 - table from trusted Axis
        (file_id,),
    )
