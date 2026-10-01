"""Album original-year fill: group → look up MusicBrainz → blank-fill ``originaldate``.

The year-axis orchestrator, a near-clone of :mod:`tagmend.engine.genres`. It selects the
in-scope files that derive ``pending`` on :data:`tagmend.engine.axis.YEAR_AXIS`, groups the
blank ones by ``(albumartist-else-artist, album)`` (the SAME identity genre uses), looks up each
group's original first-release year via MusicBrainz, and **blank-fills** ``originaldate``.

Design notes (the spec):

* **Additive blank-fill only:** ``date`` (the reissue year) is never written, and a present
  ``originaldate`` is never overwritten. A selected file that already carries one records
  ``done`` with no lookup, so it never blocks the blank files behind it in file-id order.
* **No accidental deletion (P0):** the resolver stages only ``originaldate``, the one field
  it decides, and :func:`tagmend.engine.staging._stage_one` merges it onto the tags read from
  disk, so the commit's delete-on-absent write can never drop ``artist``/``genre``/etc.
* **Outcome rows:** a filled file records ``done`` snapshotting the staged target, a miss
  records ``no_match``, and a transient MusicBrainz error writes nothing. A later change to the
  resolved artist, the album or ``originaldate`` makes the row stale and the file re-opens.

Like the rest of the conn-owning layer, the public functions here own their connection and
commit. The building blocks in :mod:`tagmend.engine.store` never commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tagmend.engine import axis, axis_status, clock, db, lookup_clients, schema, staging, store
from tagmend.engine.musicbrainz import MusicBrainzClient, MusicBrainzError
from tagmend.engine.validation import check_limit
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.musicbrainz import MBReleaseGroupSource

logger = get_logger(__name__)

# The single managed field the year axis fills (shared with the status derivation).
_YEAR_FIELD = axis.YEAR_AXIS.fields[0]


# --- result types --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolveYearsResult:
    """Immutable summary of one :func:`resolve_years` call, JSON-ready for the MCP tool."""

    settled: int
    staged_files: int
    no_match: int
    pending_remaining: int
    more: bool
    mappings: list[dict[str, str | None]]
    summary: str
    errors: int = 0
    error_items: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "settled": self.settled,
            "staged_files": self.staged_files,
            "no_match": self.no_match,
            "pending_remaining": self.pending_remaining,
            "more": self.more,
            "mappings": [dict(m) for m in self.mappings],
            "errors": self.errors,
            "error_items": [dict(e) for e in self.error_items],
            "summary": self.summary,
        }


@dataclass(slots=True)
class _Tally:
    """Mutable accumulator for one ``resolve_years`` run, frozen into the result at end."""

    settled: int = 0
    staged_files: int = 0
    no_match: int = 0
    # identity -> original_date (one mapping per resolved album group).
    mappings: dict[tuple[str | None, str | None], str] = field(default_factory=dict)
    # One item per album group whose MusicBrainz lookup failed and per file staging refused, so
    # an outage or an unstageable file is visible.
    error_items: list[dict[str, str]] = field(default_factory=list)


# --- public entry --------------------------------------------------------------------


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
    whole library. The selection is the first *limit* (default ``year_stage_limit``) present
    files in scope that derive ``pending``. A selected file that already carries
    ``originaldate`` records ``done`` with no lookup. The blank ones are grouped by
    ``(albumartist-else-artist, album)``, and per group MusicBrainz is asked for the original
    first-release year. A hit stages ``originaldate`` (``origin='auto'``, only that field) and
    records ``done``. A miss records ``no_match`` against the resolved identity. ``date`` is
    never written. A transient MusicBrainz error leaves the group ``pending`` without aborting
    the call.

    *dry_run* returns the proposed mappings and the would-settle and would-stage counts and
    writes nothing. Lookups still run. A cached answer costs nothing and a cache miss makes a
    live request. A dry run skips the empty-staging precondition. A non-dry-run raises
    :class:`ValueError` if anything is already staged, and any run raises it for a negative
    *limit* or an unknown file id. *client* lets callers inject an
    :class:`tagmend.engine.musicbrainz.MBReleaseGroupSource`, such as a fake in tests. When
    *client* is ``None`` a real :class:`MusicBrainzClient` is built. This function owns its
    connection, and ``stage_tags`` opens its own.
    """
    check_limit(limit)
    effective_limit = limit if limit is not None else settings.year_stage_limit
    tally = _Tally()

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)

        if not dry_run and store.any_staged(connection):
            message = "commit or unstage pending changes first"
            raise ValueError(message)

        scoped_ids = store.files_in_scope(
            connection,
            value_fields=axis.YEAR_AXIS.scope_fields,
            value=value,
            file_ids=file_ids,
        )
        pending = store.pending_file_ids(connection, axis.YEAR_AXIS, scoped_ids)
        blanks = _settle_present(connection, pending[:effective_limit], tally, dry_run=dry_run)
        groups = _group_by_identity(connection, blanks)
        if groups:
            _process_groups(settings, connection, groups, client, tally, dry_run=dry_run)
        pending_remaining = len(store.pending_file_ids(connection, axis.YEAR_AXIS, scoped_ids))
    finally:
        connection.close()

    return _build_result(tally, pending_remaining=pending_remaining, dry_run=dry_run)


# --- selection -----------------------------------------------------------------------


def _settle_present(
    conn: sqlite3.Connection,
    selected: list[int],
    tally: _Tally,
    *,
    dry_run: bool,
) -> list[int]:
    """Record ``done`` for each selected file already carrying a year. Return the blank ones.

    A present value is never overwritten, so its file needs no lookup, and without a row it
    would stay ``pending`` at the front of the file-id order forever.
    """
    blanks: list[int] = []
    present: list[int] = []
    for fid in selected:
        values = store.get_tags(conn, fid).get(_YEAR_FIELD, [])
        if any(value.strip() for value in values):
            present.append(fid)
        else:
            blanks.append(fid)

    tally.settled += len(present)
    if dry_run or not present:
        return blanks
    now = clock.utc_now()
    for fid in present:
        store.record_outcome(conn, axis.YEAR_AXIS, file_id=fid, status="done", now=now)
    conn.commit()
    return blanks


def _group_by_identity(
    conn: sqlite3.Connection,
    file_ids: list[int],
) -> dict[axis.LookupIdentity, list[int]]:
    """Group file ids by their album identity, preserving first-seen group order."""
    groups: dict[axis.LookupIdentity, list[int]] = {}
    for fid in file_ids:
        identity = axis.lookup_identity(store.get_tags(conn, fid))
        groups.setdefault(identity, []).append(fid)
    return groups


# --- processing ----------------------------------------------------------------------


def _process_groups(  # noqa: PLR0913 - cohesive orchestration inputs
    settings: Settings,
    conn: sqlite3.Connection,
    groups: dict[axis.LookupIdentity, list[int]],
    client: MBReleaseGroupSource | None,
    tally: _Tally,
    *,
    dry_run: bool,
) -> None:
    """Resolve each group via *client* (built if None) and settle its files."""
    with lookup_clients.injected_or_owned(
        client,
        lambda: MusicBrainzClient.from_settings(settings, conn),
    ) as source:
        for identity, fids in groups.items():
            _process_one_group(settings, conn, identity, fids, source, tally, dry_run=dry_run)


def _process_one_group(  # noqa: PLR0913 - cohesive per-group inputs
    settings: Settings,
    conn: sqlite3.Connection,
    identity: axis.LookupIdentity,
    file_ids: list[int],
    client: MBReleaseGroupSource,
    tally: _Tally,
    *,
    dry_run: bool,
) -> None:
    """Resolve one ``(artist, album)`` group and blank-fill / mark its files accordingly.

    A transient :class:`MusicBrainzError` writes nothing, records one error item, and returns
    without aborting the wider call, so the group's files stay ``pending``. A file staging
    refuses records its own error item and stays ``pending`` while its siblings settle.
    """
    failed: set[int] = set()

    # A pending file has a year identity, which needs an artist and an album.
    lookup_artist = identity.artist
    lookup_album = identity.album
    assert lookup_artist is not None  # noqa: S101 - selection invariant
    assert lookup_album is not None  # noqa: S101 - selection invariant

    try:
        resolved = client.album_first_release(lookup_artist, lookup_album)
    except MusicBrainzError as exc:
        logger.warning(
            "musicbrainz error for artist=%r album=%r: %s",
            lookup_artist,
            lookup_album,
            exc,
        )
        tally.error_items.append(
            {"key": f"{lookup_artist} - {lookup_album}", "message": str(exc)},
        )
        return

    tally.settled += len(file_ids)
    status = "no_match"
    if resolved is None:
        tally.no_match += len(file_ids)
    else:
        status = "done"
        tally.mappings[(identity.artist, identity.album)] = resolved.original_date

    # The lookup already happened, so a preview can and must report the outcome. Only the
    # stage and the status rows are withheld until the real run.
    if dry_run:
        if resolved is not None:
            tally.staged_files += len(file_ids)
        return
    # Every stage runs before any row write, because stage_tags needs the write lock this
    # connection would otherwise hold.
    if resolved is not None:
        failed = _stage_group(settings, file_ids, resolved.original_date, tally)
    now = clock.utc_now()
    for fid in file_ids:
        if fid not in failed:
            store.record_outcome(conn, axis.YEAR_AXIS, file_id=fid, status=status, now=now)
    conn.commit()


def _stage_group(
    settings: Settings,
    file_ids: list[int],
    original_date: str,
    tally: _Tally,
) -> set[int]:
    """Stage *original_date* on each of *file_ids* and return the ids staging refused.

    A refused file (vanished, unreadable or unwritable since the scan) is itemized and taken
    back out of ``settled``, so it stays ``pending`` without aborting its siblings or the call.
    """
    failed: set[int] = set()
    for fid in file_ids:
        try:
            staged = _stage_resolved(settings, fid, original_date)
        except ValueError as exc:
            logger.warning("resolve_years: file_id=%d not staged: %s", fid, exc)
            tally.error_items.append({"key": f"file_id={fid}", "message": str(exc)})
            tally.settled -= 1
            failed.add(fid)
            continue
        if staged:
            tally.staged_files += 1
    return failed


def _stage_resolved(settings: Settings, file_id: int, original_date: str) -> bool:
    """Stage *original_date* for *file_id*, passing ONLY ``originaldate`` (P0, no deletion).

    :func:`tagmend.engine.staging._stage_one` merges it onto the tags read from disk, so
    every other managed tag keeps its on-disk value through the commit's delete-on-absent
    write. ``originaldate`` is fill-only: selection read the snapshot mirror, which can lag the
    file, so a value already on disk wins and ``False`` is returned. ``stage_tags`` owns its
    conn.
    """
    return staging.stage_tags(
        settings,
        file_id=file_id,
        tags={_YEAR_FIELD: [original_date]},
        origin="auto",
        note=f"musicbrainz: {original_date}",
        fill_only=frozenset({_YEAR_FIELD}),
    )


# --- result --------------------------------------------------------------------------


def _build_result(
    tally: _Tally,
    *,
    pending_remaining: int,
    dry_run: bool,
) -> ResolveYearsResult:
    """Freeze the run's tally + counts into the public :class:`ResolveYearsResult`."""
    mappings = [
        {"artist": artist, "album": album, "original_date": date}
        for (artist, album), date in tally.mappings.items()
    ]
    return ResolveYearsResult(
        settled=tally.settled,
        staged_files=tally.staged_files,
        no_match=tally.no_match,
        pending_remaining=pending_remaining,
        more=not dry_run and tally.settled > 0 and pending_remaining > 0,
        mappings=mappings,
        summary=_summarize(tally, pending_remaining=pending_remaining, dry_run=dry_run),
        errors=len(tally.error_items),
        error_items=list(tally.error_items),
    )


def _summarize(tally: _Tally, *, pending_remaining: int, dry_run: bool) -> str:
    """Build a short, plain human summary of what settled and what is left.

    A dry run records nothing, so its remainder is not resumable and is worded accordingly.
    """
    parts = [
        f"Settled {tally.settled} file(s): staged {tally.staged_files}, no_match {tally.no_match}.",
    ]
    if dry_run:
        parts.append(
            f"A dry run records nothing, so {pending_remaining} file(s) in scope stay pending "
            f"and an identical call previews the same files.",
        )
    elif pending_remaining > 0:
        parts.append(f"{pending_remaining} file(s) still pending. Call again to continue.")
    errors = len(tally.error_items)
    if errors > 0:
        parts.append(f"{errors} item(s) errored and stay pending. Re-run to retry.")
    return " ".join(parts)


# --- status tools --------------------------------------------------------------------


def set_year_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
    status: str,
) -> int:
    """Record ``manual`` on the year axis for every file in scope.

    Scope is *file_ids* when given, else every file carrying *value* as its ``album`` tag.
    With neither the call changes nothing and returns 0. A ``manual`` row is sticky until
    :func:`reset_year_status`. Returns the number of files affected. Raises
    :class:`ValueError` for any *status* other than ``manual`` or an unknown file id.
    """
    return axis_status.set_manual_status(
        settings,
        axis.YEAR_AXIS,
        file_ids=file_ids,
        value=value,
        status=status,
    )


def reset_year_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> int:
    """Delete the year status row of every file in scope, the one hand-back of ``manual``.

    Same scoping as :func:`set_year_status`. Returns the number of files affected.
    """
    return axis_status.reset_status(
        settings,
        axis.YEAR_AXIS,
        file_ids=file_ids,
        value=value,
    )
