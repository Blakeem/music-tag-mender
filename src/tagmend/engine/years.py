"""Album original-year fill: group → look up MusicBrainz → blank-fill ``originaldate``.

The year-axis resolver, run by :func:`tagmend.engine.axis_resolver.run` as genre is. It
selects the in-scope files that derive ``pending`` on :data:`tagmend.engine.axis.YEAR_AXIS`,
groups the blank ones by ``(albumartist-else-artist, album)`` (the SAME identity genre uses),
looks up each group's first-release date via MusicBrainz, and **blank-fills** ``originaldate``.

Design notes (the spec):

* **Additive blank-fill only:** ``date`` (the reissue year) is never written, and a present
  ``originaldate`` is never overwritten. A selected file that already carries one records
  ``done`` with no lookup, so it never blocks the blank files behind it in file-id order.
* **No accidental deletion (P0):** the resolver stages only ``originaldate``, the one field
  it decides, and :func:`tagmend.engine.staging._stage_one` merges it onto the tags read from
  disk, so the commit's delete-on-absent write can never drop ``artist``/``genre``/etc.
* **Outcome rows:** a filled file records ``done`` snapshotting the staged target, a miss
  records ``no_match``, and a MusicBrainz lookup error writes nothing. A later change to the
  resolved artist, the album or ``originaldate`` makes the row stale and the file re-opens.

Like the rest of the conn-owning layer, the public functions here own their connection and
commit. The building blocks in :mod:`tagmend.engine.store` never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tagmend.engine import axis, axis_resolver, ledger_lock, staging
from tagmend.engine.musicbrainz import (
    MusicBrainzClient,
    MusicBrainzError,
    MusicBrainzUnavailableError,
)
from tagmend.engine.serialize import FieldDict

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from tagmend.config import Settings
    from tagmend.engine.musicbrainz import MBReleaseGroup, MBReleaseGroupSource

# The single managed field the year axis fills (shared with the status derivation).
_YEAR_FIELD = axis.YEAR_AXIS.fields[0]


# --- result types --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolveYearsResult(FieldDict):
    """Immutable summary of one :func:`resolve_years` call, JSON-ready for the MCP tool."""

    settled: int
    staged_files: int
    no_match: int
    pending_remaining: int
    more: bool
    mappings: list[dict[str, str | None]]
    errors: int
    error_items: list[dict[str, str]]
    summary: str


# --- public entry --------------------------------------------------------------------


@ledger_lock.mutating
def resolve_years(  # noqa: PLR0913 - cohesive keyword-only scope + injection params
    settings: Settings,
    *,
    value: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    client: MBReleaseGroupSource | None = None,
) -> ResolveYearsResult:
    """Blank-fill ``originaldate`` from MusicBrainz for in-scope ``pending`` files (writes no disk).

    Scope is *file_ids* when given, else every file whose ``album`` equals *value*, else the
    whole library. The call settles up to *limit* (default ``year_stage_limit``) present files
    in scope that derive ``pending``, in file-id order, and reads past a file staging refuses.
    A selected file that already carries ``originaldate`` records ``done`` with no lookup. The
    blank ones are grouped by ``(albumartist-else-artist, album)``, and per group MusicBrainz
    is asked for the original first-release date. A hit stages ``originaldate``
    (``origin='auto'``, only that field) and records ``done``. A miss records ``no_match``
    against the resolved identity. ``date`` is never written. A MusicBrainz lookup error leaves
    the group ``pending`` without aborting the call, and the call reads past it. A
    :class:`MusicBrainzUnavailableError` also stops the call reading past refused files and
    failed groups.

    *dry_run* returns the proposed mappings and the would-settle and would-stage counts and
    writes nothing. A file counts as would-stage only when its ``originaldate`` is blank on
    disk, as the real run's fill-only stage requires. Lookups still run. A cached answer costs
    nothing and a cache miss makes a live request. A dry run skips the empty-staging
    precondition. A non-dry-run raises :class:`ValueError` if anything is already staged, and
    any run raises it for a negative *limit* or an unknown file id. *client* lets callers
    inject an :class:`tagmend.engine.musicbrainz.MBReleaseGroupSource`, such as a fake in
    tests. When *client* is ``None`` a real :class:`MusicBrainzClient` is built. This function
    owns its connection, and ``stage_tags`` opens its own.
    """
    resolver: axis_resolver.AxisResolver[MBReleaseGroupSource, MBReleaseGroup] = (
        axis_resolver.AxisResolver(
            axis_=axis.YEAR_AXIS,
            build_client=lambda conn: MusicBrainzClient.from_settings(settings, conn),
            lookup=_first_release,
            lookup_error=MusicBrainzError,
            unavailable_error=MusicBrainzUnavailableError,
            group_key=_album_key,
            stage=lambda conn, fid, release_group, dry_run: _stage_resolved(
                settings, conn, fid, release_group.original_date, dry_run=dry_run
            ),
            settles_without_lookup=holds_year,
        )
    )

    outcome = axis_resolver.run(
        settings,
        resolver,
        value=value,
        file_ids=file_ids,
        limit=limit,
        default_limit=settings.year_stage_limit,
        dry_run=dry_run,
        client=client,
    )

    mappings: list[dict[str, str | None]] = [
        {
            "artist": identity.artist,
            "album": identity.album,
            "original_date": release_group.original_date,
        }
        for identity, release_group in outcome.answers.items()
        if release_group is not None
    ]
    return ResolveYearsResult(
        settled=outcome.settled,
        staged_files=outcome.staged_files,
        no_match=outcome.no_match,
        pending_remaining=outcome.pending_remaining,
        more=outcome.more,
        mappings=mappings,
        summary=outcome.summary,
        errors=outcome.errors,
        error_items=outcome.error_items,
    )


# --- per-axis parts ------------------------------------------------------------------


def holds_year(tags: Mapping[str, list[str]]) -> bool:
    """Whether *tags* hold a non-blank ``originaldate``, which the fill never overwrites."""
    return any(value.strip() for value in tags.get(_YEAR_FIELD, []))


def _album_key(identity: axis.LookupIdentity) -> str:
    """Key a failed group's error item by its looked-up artist and album."""
    return f"{identity.artist} - {identity.album}"


def _first_release(
    client: MBReleaseGroupSource,
    identity: axis.LookupIdentity,
) -> MBReleaseGroup | None:
    """Ask MusicBrainz for the first release of one ``(artist, album)`` group."""
    # A pending file has a year identity, which needs an artist and an album.
    assert identity.artist is not None  # noqa: S101 - selection invariant
    assert identity.album is not None  # noqa: S101 - selection invariant
    return client.album_first_release(identity.artist, identity.album)


def _stage_resolved(
    settings: Settings,
    conn: sqlite3.Connection,
    file_id: int,
    original_date: str,
    *,
    dry_run: bool,
) -> bool:
    """Stage *original_date* for *file_id*, passing ONLY ``originaldate`` (P0, no deletion).

    :func:`tagmend.engine.staging._stage_one` merges it onto the tags read from disk, so
    every other managed tag keeps its on-disk value through the commit's delete-on-absent
    write. ``originaldate`` is fill-only: selection read the snapshot mirror, which can lag the
    file, so a value already on disk wins and ``False`` is returned. A dry run runs the same
    disk read and refusals and stages nothing. ``stage_tags`` owns its conn.
    """
    tags = {_YEAR_FIELD: [original_date]}
    fill_only = frozenset({_YEAR_FIELD})
    if dry_run:
        return staging.would_stage(settings, conn, file_id=file_id, tags=tags, fill_only=fill_only)
    return staging.stage_tags(
        settings,
        file_id=file_id,
        tags=tags,
        origin="auto",
        note=f"musicbrainz: {original_date}",
        fill_only=fill_only,
    )
