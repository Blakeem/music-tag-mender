"""Album original-year fill: group → look up MusicBrainz → blank-fill ``originaldate``.

The year-axis orchestrator (a near-clone of :mod:`tagmend.engine.genres`'s identity/status
model, borrowing :mod:`tagmend.engine.artists`'s dry-run / value-scoped-exclusion / result
ergonomics). It ties the read path, the cached MusicBrainz client, and the revertible
staging engine together: it groups in-scope files by ``(albumartist-else-artist, album)``
— the SAME identity genre uses — looks up each group's original first-release year via
MusicBrainz, and **blank-fills** ``originaldate`` only on files whose ``originaldate`` is
currently empty (never overwriting an existing value, and **never** touching ``date``).

Design notes (the spec):

* **Additive blank-fill only:** ``date`` (the reissue year) is never written; a file that
  already carries ``originaldate`` is skipped (``skipped_present``). On a correctly-tagged
  library this is a no-op.
* **No accidental deletion (P0):** the staged target is built from the file's own managed
  subset with only ``originaldate`` set, so the commit's delete-on-absent write can never
  drop ``artist``/``genre``/etc.
* **``no_match`` stores the RESOLVED identity** (``albumartist``-else-``artist`` + ``album``)
  — exactly as :func:`tagmend.engine.genres._process_one_group` — so a later change to that
  resolved artist OR the album makes the decision stale and re-processable.
* **"Done" is derived**, never stored — re-running after commit is a no-op (the files now
  have ``originaldate`` → ``skipped_present``).

Like the rest of the conn-owning layer, the public functions here own their connection and
commit; the building blocks in :mod:`tagmend.engine.store` never commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tagmend.engine import axis, db, genres, schema, staging, store, versioning
from tagmend.engine.musicbrainz import MusicBrainzClient, MusicBrainzError
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.genres import _Identity
    from tagmend.engine.musicbrainz import MBAlbumSource

logger = get_logger(__name__)

# The single managed field the year axis fills (shared with the status derivation).
_YEAR_FIELD = "originaldate"

# The two states ``set_year_status`` is allowed to drive: ``manual`` writes a sticky
# exclusion row; ``pending`` deletes it (re-queue). ``no_match`` is engine-owned.
_USER_STATUSES = frozenset({"manual", "pending"})


def _utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string (the engine's timestamp form)."""
    return datetime.now(UTC).isoformat()


# --- result types --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolveYearsResult:
    """Immutable summary of one :func:`resolve_years` call, JSON-ready for the MCP tool."""

    processed: int
    staged_files: int
    no_match: int
    skipped_present: int
    skipped_no_album: int
    skipped_no_artist: int
    skipped_manual: int
    pending_remaining: int
    more: bool
    mappings: list[dict[str, str | None]]
    summary: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "processed": self.processed,
            "staged_files": self.staged_files,
            "no_match": self.no_match,
            "skipped_present": self.skipped_present,
            "skipped_no_album": self.skipped_no_album,
            "skipped_no_artist": self.skipped_no_artist,
            "skipped_manual": self.skipped_manual,
            "pending_remaining": self.pending_remaining,
            "more": self.more,
            "mappings": [dict(m) for m in self.mappings],
            "summary": self.summary,
        }


@dataclass(slots=True)
class _Tally:
    """Mutable accumulator for one ``resolve_years`` run, frozen into the result at end."""

    staged_files: int = 0
    no_match: int = 0
    skipped_present: int = 0
    skipped_no_album: int = 0
    skipped_no_artist: int = 0
    skipped_manual: int = 0
    # identity -> original_date (one mapping per resolved album group).
    mappings: dict[tuple[str | None, str | None], str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A processable file plus the album identity it will be looked up against."""

    file_id: int
    identity: _Identity


# --- public entry --------------------------------------------------------------------


def resolve_years(  # noqa: PLR0913 - cohesive keyword-only scope + injection params
    settings: Settings,
    *,
    album: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    client: MBAlbumSource | None = None,
) -> ResolveYearsResult:
    """Blank-fill ``originaldate`` from MusicBrainz for in-scope files (writes no disk).

    Selects the in-scope files that need filling (a blank ``originaldate``, an ``album`` and
    artist, not sticky ``manual``, no non-stale ``no_match``), groups them by
    ``(albumartist-else-artist, album)``, and per group looks up the original first-release
    year on MusicBrainz: on a hit it stages ``originaldate`` (``origin='auto'``, only that
    field) on the group's blank files; on a miss it records a ``no_match`` against the
    resolved identity. ``date`` is never written. A transient MusicBrainz error leaves the
    group pending (no status row) without aborting the call.

    *limit* caps the number of groups processed per call; the remainder is reported via
    ``pending_remaining`` / ``more``. *dry_run* returns the proposed mappings + would-stage
    count, stages nothing, and works from cache (no precondition). A non-dry-run raises
    :class:`ValueError` if anything is already staged. *client* lets callers inject an
    :class:`tagmend.engine.musicbrainz.MBAlbumSource` (a fake in tests); when ``None`` a real
    :class:`MusicBrainzClient` is built. Owns its connection; ``stage_tags`` opens its own.
    """
    effective_limit = limit if limit is not None else settings.album_stage_limit

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)

        if not dry_run and store.any_staged(connection):
            message = "commit or unstage pending changes first"
            raise ValueError(message)

        candidate_ids = _candidate_scope(connection, album=album, file_ids=file_ids)

        tally = _Tally()
        processable = _select(connection, candidate_ids, tally)
        groups = _group_by_identity(processable)

        group_items = list(groups.items())
        to_process = group_items[:effective_limit]
        pending_remaining = len(group_items) - len(to_process)

        if to_process:
            _process_groups(settings, connection, to_process, client, tally, dry_run=dry_run)
    finally:
        connection.close()

    return _build_result(
        tally,
        processed=len(to_process),
        pending_remaining=pending_remaining,
        dry_run=dry_run,
    )


# --- selection -----------------------------------------------------------------------


def _candidate_scope(
    conn: sqlite3.Connection,
    *,
    album: str | None,
    file_ids: list[int] | None,
) -> list[int]:
    """Resolve the candidate file ids for a ``resolve_years`` run, in ascending id order.

    Precedence: *file_ids* (when given) win; else *album* narrows to every file carrying it
    as its ``album`` tag (``store.files_in_scope`` only honors ``album`` alongside an
    ``artist``, so the album-only scope is resolved explicitly here); else every tracked
    file. This is what makes ``resolve_years(album=…)`` actually scope to that album rather
    than fan MusicBrainz lookups across the whole library.
    """
    if file_ids is None and album is not None:
        return store.files_by_tag_value(conn, "album", album)
    return store.files_in_scope(conn, file_ids=file_ids)


def _select(
    conn: sqlite3.Connection,
    candidate_ids: list[int],
    tally: _Tally,
) -> list[_Candidate]:
    """Classify each candidate into a bucket; return only the processable ones.

    Mutates *tally* with the skip counts. A candidate is processable when it has an artist
    and an album, its ``originaldate`` is currently blank, it is not already done
    (staged / committed ``originaldate`` revision), and it has no blocking year decision.
    """
    year_fields = axis.YEAR_AXIS.fields
    processable: list[_Candidate] = []
    for fid in candidate_ids:
        tags = store.get_tags(conn, fid)
        identity = genres._identity(tags)  # noqa: SLF001 - shared identity shape

        if identity.artist is None:
            tally.skipped_no_artist += 1
            continue
        if identity.album is None:
            tally.skipped_no_album += 1
            continue
        if tags.get(_YEAR_FIELD):
            tally.skipped_present += 1
            continue

        if store.has_staged_change_for(conn, fid, year_fields) or store.has_auto_change_for(
            conn,
            fid,
            year_fields,
        ):
            # Already filled by us (staged or committed) — treat as present/done.
            tally.skipped_present += 1
            continue

        decision = store.get_year_status(conn, fid)
        if decision is not None and _decision_blocks(decision, identity):
            # A sticky 'manual' is reported; a non-stale 'no_match' is silently held back
            # (a stale no_match does not block and falls through to be reprocessed).
            if decision.status == "manual":
                tally.skipped_manual += 1
            continue

        processable.append(_Candidate(file_id=fid, identity=identity))
    return processable


def _decision_blocks(decision: store.YearStatusRow, identity: _Identity) -> bool:
    """Whether a stored year decision still blocks processing for the current identity.

    The shared year staleness/sticky rule (``manual`` always blocks; ``no_match`` blocks
    only while NOT stale) lives on the axis, so this skip path and the user-facing
    :func:`tagmend.engine.store.derived_year_status` can never drift.
    """
    return axis.YEAR_AXIS.decision_blocks(
        axis.StatusRow(
            status=decision.status,
            source_primary=decision.source_artist,
            source_secondary=decision.source_album,
        ),
        axis.Identity(primary=identity.artist, secondary=identity.album),
    )


def _group_by_identity(candidates: list[_Candidate]) -> dict[_Identity, list[int]]:
    """Group file ids by their album identity, preserving first-seen group order."""
    groups: dict[_Identity, list[int]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.identity, []).append(candidate.file_id)
    return groups


# --- processing ----------------------------------------------------------------------


def _process_groups(  # noqa: PLR0913 - cohesive orchestration inputs
    settings: Settings,
    conn: sqlite3.Connection,
    groups: list[tuple[_Identity, list[int]]],
    client: MBAlbumSource | None,
    tally: _Tally,
    *,
    dry_run: bool,
) -> None:
    """Resolve each group via *client* (built if None) and stage / mark its files."""
    if client is not None:
        for identity, fids in groups:
            _process_one_group(settings, conn, identity, fids, client, tally, dry_run=dry_run)
        return

    with MusicBrainzClient(
        settings.musicbrainz_user_agent,
        conn,
        rate_per_sec=settings.musicbrainz_rate_per_sec,
    ) as owned_client:
        for identity, fids in groups:
            _process_one_group(
                settings,
                conn,
                identity,
                fids,
                owned_client,
                tally,
                dry_run=dry_run,
            )


def _process_one_group(  # noqa: PLR0913 - cohesive per-group inputs
    settings: Settings,
    conn: sqlite3.Connection,
    identity: _Identity,
    file_ids: list[int],
    client: MBAlbumSource,
    tally: _Tally,
    *,
    dry_run: bool,
) -> None:
    """Resolve one ``(artist, album)`` group and blank-fill / mark its files accordingly.

    A transient :class:`MusicBrainzError` leaves the group's files pending (no status row)
    and returns without aborting the wider call.
    """
    # ``_select`` guarantees non-None artist and album for every processable candidate.
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
        return

    if resolved is not None:
        tally.mappings[(identity.artist, identity.album)] = resolved.original_date
        for fid in file_ids:
            if not dry_run:
                _stage_resolved(settings, conn, fid, resolved.original_date)
            tally.staged_files += 1
        return

    # The lookup already happened, so a preview can and must report the miss; only the
    # sticky status row is withheld until the real run.
    tally.no_match += len(file_ids)
    if dry_run:
        return
    now = _utc_now()
    for fid in file_ids:
        store.set_year_status(
            conn,
            file_id=fid,
            status="no_match",
            source_artist=identity.artist,
            source_album=identity.album,
            now=now,
        )
    conn.commit()


def _stage_resolved(
    settings: Settings,
    conn: sqlite3.Connection,
    file_id: int,
    original_date: str,
) -> None:
    """Stage *original_date* for *file_id*, setting ONLY ``originaldate`` (P0 — no deletion).

    The target starts from the file's own managed subset so every other managed tag is
    preserved through the commit's delete-on-absent write. ``stage_tags`` owns its conn.
    """
    target = dict(versioning.managed_subset(store.get_tags(conn, file_id)))
    target[_YEAR_FIELD] = [original_date]
    staging.stage_tags(
        settings,
        file_id=file_id,
        managed_tags=target,
        origin="auto",
        note=f"musicbrainz: {original_date}",
    )


# --- result --------------------------------------------------------------------------


def _build_result(
    tally: _Tally,
    *,
    processed: int,
    pending_remaining: int,
    dry_run: bool,
) -> ResolveYearsResult:
    """Freeze the run's tally + counts into the public :class:`ResolveYearsResult`."""
    mappings = [
        {"artist": artist, "album": album, "original_date": date}
        for (artist, album), date in tally.mappings.items()
    ]
    more = pending_remaining > 0
    summary = _summarize(
        tally,
        processed=processed,
        pending_remaining=pending_remaining,
        dry_run=dry_run,
    )
    return ResolveYearsResult(
        processed=processed,
        staged_files=tally.staged_files,
        no_match=tally.no_match,
        skipped_present=tally.skipped_present,
        skipped_no_album=tally.skipped_no_album,
        skipped_no_artist=tally.skipped_no_artist,
        skipped_manual=tally.skipped_manual,
        pending_remaining=pending_remaining,
        more=more,
        mappings=mappings,
        summary=summary,
    )


def _summarize(
    tally: _Tally,
    *,
    processed: int,
    pending_remaining: int,
    dry_run: bool,
) -> str:
    """Build a short, plain human summary of what was and was not processed.

    Every count carries its unit because the run mixes two: ``processed`` counts album
    groups, every other count counts files. A dry run records neither a stage nor a
    ``no_match``, so its remainder is not resumable and is worded accordingly.
    """
    parts = [
        f"Processed {processed} album group(s): staged {tally.staged_files} file(s), "
        f"no_match {tally.no_match} file(s).",
        f"Skipped {tally.skipped_present} file(s) present, "
        f"{tally.skipped_no_album} file(s) no_album, "
        f"{tally.skipped_no_artist} file(s) no_artist, "
        f"{tally.skipped_manual} file(s) manual.",
    ]
    if pending_remaining > 0 and dry_run:
        parts.append(
            f"Processed the first {processed} of {processed + pending_remaining} album "
            f"group(s) in scope. A dry run records nothing, so an identical call "
            f"re-processes the same groups. Raise limit above {processed}, or scope with "
            f"album= / file_ids=, to reach the remaining {pending_remaining} group(s).",
        )
    elif pending_remaining > 0:
        parts.append(f"{pending_remaining} group(s) still pending — call again to continue.")
    return " ".join(parts)


# --- status tools --------------------------------------------------------------------


def _year_scope(
    conn: sqlite3.Connection,
    *,
    file_ids: list[int] | None,
    value: str | None,
) -> list[int]:
    """Resolve the in-scope file ids for the year status tools, in ascending id order.

    *file_ids* (when given) win; otherwise *value* matches every file carrying it as its
    ``album`` tag. With neither given the scope is empty (the tools require a target).
    """
    if file_ids is not None:
        return store.files_in_scope(conn, file_ids=file_ids)
    if value is None:
        return []
    return store.files_by_tag_value(conn, "album", value)


def set_year_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
    status: str,
) -> int:
    """Set ``manual`` (exclude) or ``pending`` (re-queue) for every file in scope.

    Scope is *file_ids* when given, else every file carrying *value* as its ``album`` tag.
    ``manual`` writes a sticky row (recording the file's resolved identity for audit) so
    :func:`resolve_years` always skips it; ``pending`` deletes any row, re-queuing the
    file. Returns the number of files affected. Raises :class:`ValueError` for an unknown
    *status*. Owns its transaction.
    """
    if status not in _USER_STATUSES:
        message = f"invalid status: {status!r} (expected manual|pending)"
        raise ValueError(message)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        scoped = _year_scope(connection, file_ids=file_ids, value=value)
        now = _utc_now()
        for fid in scoped:
            if status == "manual":
                identity = genres._identity(store.get_tags(connection, fid))  # noqa: SLF001
                store.set_year_status(
                    connection,
                    file_id=fid,
                    status="manual",
                    source_artist=identity.artist,
                    source_album=identity.album,
                    now=now,
                )
            else:
                store.delete_year_status(connection, fid)
        connection.commit()
    finally:
        connection.close()

    logger.info("set year status=%s for %d file(s)", status, len(scoped))
    return len(scoped)


def reset_year_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> int:
    """Delete the year status row for every file in scope (back to ``pending``).

    Same ``album``-value scoping as :func:`set_year_status`. Returns the number of files
    affected. Owns its transaction.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        scoped = _year_scope(connection, file_ids=file_ids, value=value)
        for fid in scoped:
            store.delete_year_status(connection, fid)
        connection.commit()
    finally:
        connection.close()

    logger.info("reset year status for %d file(s)", len(scoped))
    return len(scoped)


# --- discovery -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlbumRow:
    """One distinct ``(albumartist-else-artist, album)`` group with its status, for listing."""

    artist: str | None
    album: str
    file_count: int
    year_status: str
    blank_originaldate: int

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "artist": self.artist,
            "album": self.album,
            "file_count": self.file_count,
            "year_status": self.year_status,
            "blank_originaldate": self.blank_originaldate,
        }


def list_albums(
    settings: Settings,
    *,
    year_status: str | None = None,
    actionable: bool = False,
    limit: int | None = None,
) -> list[AlbumRow]:
    """Return each distinct album group with its file count + a representative status.

    Groups in-scope files by ``(albumartist-else-artist, album)`` (the album identity) and
    reports the derived year status of the group's first file plus ``blank_originaldate``
    — the count of the group's files whose ``originaldate`` tag is empty (the files
    :func:`resolve_years` can actually fill; ``> 0`` marks an actionable group). A
    discovery aid for scoping ``resolve_years``. Read-only.

    *year_status* (when given) keeps only groups whose derived status matches. *actionable*
    keeps only the actionable groups, those with ``blank_originaldate > 0``. The two
    compose, and both are applied AFTER ordering and BEFORE *limit*. *limit* (when given)
    caps the number of rows returned so a large library stays context-cheap.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        groups: dict[tuple[str | None, str], list[int]] = {}
        blanks: dict[tuple[str | None, str], int] = {}
        for fid in store.files_in_scope(connection):
            tags = store.get_tags(connection, fid)
            identity = genres._identity(tags)  # noqa: SLF001
            if identity.album is None:
                continue
            key = (identity.artist, identity.album)
            groups.setdefault(key, []).append(fid)
            if not tags.get(_YEAR_FIELD):
                blanks[key] = blanks.get(key, 0) + 1

        rows = [
            AlbumRow(
                artist=artist,
                album=album,
                file_count=len(fids),
                year_status=store.derived_year_status(connection, fids[0]),
                blank_originaldate=blanks.get((artist, album), 0),
            )
            for (artist, album), fids in groups.items()
        ]
    finally:
        connection.close()

    ordered = sorted(rows, key=lambda r: (r.artist or "", r.album))
    if year_status is not None:
        ordered = [row for row in ordered if row.year_status == year_status]
    if actionable:
        ordered = [row for row in ordered if row.blank_originaldate > 0]
    if limit is not None:
        ordered = ordered[:limit]
    return ordered
