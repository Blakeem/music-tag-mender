"""The metadata-axis abstraction: ONE parameterized status machinery for genre/artist.

Genre and artist (and the album/song axes to come) each carry a per-file *workflow
status* — ``pending`` / ``staged`` / ``done`` / ``manual`` (and ``no_match`` on the genre
axis). Their machinery is identical bar a handful of values, so it lives here ONCE as a
parameterized :class:`Axis`: the name, the managed-tag *fields* its field-aware
``staged``/``done`` derivation keys on, its ``file_<name>_status`` side table and that
table's source-identity columns, its valid workflow-status set, and the staleness rule
that decides whether a *stored* decision still blocks reprocessing.

The two asymmetries the abstraction must preserve (NOT flatten):

* **genre has a ``no_match`` state; artist does not.** A stored genre ``no_match`` goes
  *stale* when the file's lookup identity changes, which re-opens it for processing; a
  stored artist ``manual`` is *sticky* (never goes stale). Both are encoded in each axis's
  :attr:`Axis.decision_blocks` predicate, so the skip path
  (:mod:`tagmend.engine.genres` / :mod:`tagmend.engine.artists`) and the user-facing
  derivation (:func:`derived_status`) share one source of truth and can never drift.
* **the source-identity columns differ:** ``file_genre_status`` records
  ``(source_artist, source_album)``; ``file_artist_status`` records
  ``(source_artist, source_albumartist)``. Each axis names its two columns in
  :attr:`Axis.source_columns`; the generic SQL is built from those names.

Like the rest of :mod:`tagmend.engine.store`, the helpers here never commit — the
conn-owning layer (genres/artists/library) owns the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StatusRow:
    """One row from a ``file_<axis>_status`` table, with its two source-identity values.

    ``source_primary`` / ``source_secondary`` are positional: each axis maps them to its
    own column names via :attr:`Axis.source_columns` (genre →
    ``source_artist``/``source_album``; artist → ``source_artist``/``source_albumartist``).
    """

    status: str
    source_primary: str | None
    source_secondary: str | None


@dataclass(frozen=True, slots=True)
class Identity:
    """The two source-identity values a status decision is taken / re-checked against.

    Positional, matching :class:`StatusRow`: ``primary`` is the lookup artist on both axes;
    ``secondary`` is the album (genre) or the album-artist (artist).
    """

    primary: str | None
    secondary: str | None


@dataclass(frozen=True, slots=True)
class Axis:
    """Everything that differs between the genre, artist (and future) metadata axes.

    A new axis slots in as one of these values plus a resolver — never a copied module.
    """

    name: str
    """The key used in tool params, ``list_files`` filters, ``library_stats`` blocks, and
    the ``diff`` JSON paths (e.g. ``"genre"`` / ``"artist"``)."""

    fields: tuple[str, ...]
    """The managed-tag names this axis keys on; drives the field-aware
    ``has_staged_change_for`` / ``has_auto_change_for`` (``json_extract(diff, '$.<field>')``).
    genre → ``("genre",)``; artist → ``("artist", "albumartist")``."""

    status_table: str
    """The ``file_<name>_status`` table name."""

    source_columns: tuple[str, str]
    """The table's two source-identity column names, in :class:`StatusRow` order
    (``source_primary`` first). genre → ``("source_artist", "source_album")``; artist →
    ``("source_artist", "source_albumartist")``."""

    workflow_statuses: frozenset[str]
    """The valid derived-status set (5 for genre incl. ``no_match``; 4 for artist)."""

    decision_blocks: Callable[[StatusRow, Identity], bool]
    """Whether a STORED decision still blocks reprocessing for the given current identity.

    The single source of truth shared by the skip path and the derived-status report:
    genre's ``no_match`` blocks only while NOT stale (identity unchanged), ``manual`` always
    blocks; artist's ``manual`` always blocks (sticky, no staleness re-check)."""


# --- genre / artist staleness rules --------------------------------------------------


def _genre_decision_blocks(decision: StatusRow, identity: Identity) -> bool:
    """Genre rule: ``manual`` always blocks; ``no_match`` blocks unless its identity changed.

    A ``no_match`` whose ``source`` identity no longer matches the file's current identity is
    *stale* and falls through to be reprocessed (``stage_genres`` retries it).
    """
    if decision.status == "manual":
        return True
    if decision.status == "no_match":
        return (
            decision.source_primary == identity.primary
            and decision.source_secondary == identity.secondary
        )
    return False


def _artist_decision_blocks(decision: StatusRow, _identity: Identity) -> bool:
    """Artist rule: a stored ``manual`` is sticky — it always blocks (no staleness re-check)."""
    return decision.status == "manual"


# --- the two axes --------------------------------------------------------------------

GENRE_AXIS: Final = Axis(
    name="genre",
    fields=("genre",),
    status_table="file_genre_status",
    source_columns=("source_artist", "source_album"),
    # Five user-facing genre states: two stored (`no_match`/`manual`), two derived
    # (`staged`/`done`), `pending` = none of the above.
    workflow_statuses=frozenset({"pending", "no_match", "manual", "staged", "done"}),
    decision_blocks=_genre_decision_blocks,
)

ARTIST_AXIS: Final = Axis(
    name="artist",
    fields=("artist", "albumartist"),
    status_table="file_artist_status",
    source_columns=("source_artist", "source_albumartist"),
    # Four artist states: one stored (sticky `manual`), two derived (`staged`/`done`),
    # `pending` = none of the above. There is NO `no_match` state on this axis.
    workflow_statuses=frozenset({"pending", "manual", "staged", "done"}),
    decision_blocks=_artist_decision_blocks,
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
    """Insert or replace *file_id*'s stored decision on *axis*.

    The two source values record what the decision was taken against (audit + the genre
    staleness re-check); each is stored in the axis's own source column.
    """
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
