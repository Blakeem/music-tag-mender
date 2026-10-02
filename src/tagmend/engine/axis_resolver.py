"""The group-lookup runner behind ``resolve_genres`` and ``resolve_years``.

Both resolvers select an axis's ``pending`` files, group them by lookup identity, look each group
up once and settle every file of the group on that one answer. Only the lookup, the stage and an
optional pre-pass differ between the axes, so each resolver supplies an :class:`AxisResolver`
and :func:`run` owns the rest: the connection, the empty-staging precondition, the selection,
the outcome rows and the counts.

Every count is files. A transient lookup error writes nothing and leaves the group ``pending``.
A file staging refuses is itemized under ``file_id=<id>`` and stays ``pending`` while its
siblings settle. A dry run asks the stage callable whether each file would stage, so its
``staged_files`` counts the files the real run would stage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tagmend.engine import axis, clock, db, lookup_clients, schema, store
from tagmend.engine.validation import check_limit
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping
    from contextlib import AbstractContextManager

    from tagmend.config import Settings

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AxisResolver[C, R]:
    """The parts of a group-lookup resolver that differ by axis.

    *C* is the lookup client and *R* the answer one group lookup resolves to.
    """

    axis_: axis.Axis
    """The axis whose ``pending`` files are selected and whose outcome rows are written."""

    build_client: Callable[[sqlite3.Connection], AbstractContextManager[C]]
    """Builds the real client on the runner's connection when no client is injected."""

    lookup: Callable[[C, axis.LookupIdentity], R | None]
    """Looks one group up. ``None`` means nothing usable, which settles the group ``no_match``."""

    transient_error: type[Exception]
    """The client's retryable failure, which leaves the group ``pending`` instead of aborting."""

    group_key: Callable[[axis.LookupIdentity], str]
    """The ``error_items`` key of a group whose lookup failed."""

    stage: Callable[[sqlite3.Connection, int, R, bool], bool]
    """Stages the answer on one file and returns whether a row was staged. Given ``True`` as its
    last argument (a dry run) it stages nothing and returns whether the real run would stage."""

    settles_without_lookup: Callable[[Mapping[str, list[str]]], bool] | None = None
    """Picks the selected files that settle ``done`` from their own tags with no lookup."""


@dataclass(frozen=True, slots=True)
class ResolverRun[R]:
    """One :func:`run`'s counts, plus each looked-up group's answer for the axis's extra field."""

    settled: int
    staged_files: int
    no_match: int
    pending_remaining: int
    more: bool
    errors: int
    error_items: list[dict[str, str]]
    summary: str
    answers: dict[axis.LookupIdentity, R | None]


@dataclass(slots=True)
class _Tally[R]:
    """Mutable accumulator for one :func:`run`, frozen into a :class:`ResolverRun` at the end."""

    settled: int = 0
    staged_files: int = 0
    no_match: int = 0
    error_items: list[dict[str, str]] = field(default_factory=list)
    answers: dict[axis.LookupIdentity, R | None] = field(default_factory=dict)


def run[C, R](  # noqa: PLR0913 - cohesive keyword-only scope + injection params
    settings: Settings,
    resolver: AxisResolver[C, R],
    *,
    value: str | None = None,
    album: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None,
    default_limit: int,
    dry_run: bool,
    client: C | None,
) -> ResolverRun[R]:
    """Settle the first *limit* (else *default_limit*) ``pending`` files in scope on *resolver*.

    Scope is *file_ids* when given, else every file carrying *value* in one of the axis's
    ``scope_fields`` (narrowed to *album* when given), else the whole library. *client* is used
    when given, else the resolver builds and owns one. A non-dry run raises :class:`ValueError`
    while anything is staged. Any run raises it for a negative *limit*, an unknown file id or
    *album* without *value*. Owns its connection, and each stage opens its own.
    """
    check_limit(limit)
    effective_limit = default_limit if limit is None else limit
    tally: _Tally[R] = _Tally()
    pending_remaining = 0

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)

        # Staging replaces a file's pending row, so a resolver run would silently discard a
        # manual fix to another field that is still waiting for its commit.
        if not dry_run and store.any_staged(connection):
            message = "commit or unstage pending changes first"
            raise ValueError(message)

        scoped_ids = store.files_in_scope(
            connection,
            value_fields=resolver.axis_.scope_fields,
            value=value,
            album=album,
            file_ids=file_ids,
        )
        selected = store.pending_file_ids(connection, resolver.axis_, scoped_ids)[:effective_limit]
        to_look_up = _settle_without_lookup(connection, resolver, selected, tally, dry_run=dry_run)
        groups = _group_by_identity(connection, to_look_up)
        if groups:
            _process_groups(connection, resolver, groups, client, tally, dry_run=dry_run)
        pending_remaining = len(store.pending_file_ids(connection, resolver.axis_, scoped_ids))
    finally:
        connection.close()

    return _build_run(tally, pending_remaining=pending_remaining, dry_run=dry_run)


def _settle_without_lookup[C, R](
    conn: sqlite3.Connection,
    resolver: AxisResolver[C, R],
    selected: list[int],
    tally: _Tally[R],
    *,
    dry_run: bool,
) -> list[int]:
    """Record ``done`` for each selected file the pre-pass settles. Return the files to look up.

    Without a row such a file would stay ``pending`` at the front of the file-id order forever.
    """
    predicate = resolver.settles_without_lookup
    settled_now: list[int] = []
    to_look_up: list[int] = []
    now = clock.utc_now()

    if predicate is None:
        return selected
    for fid in selected:
        if predicate(store.get_tags(conn, fid)):
            settled_now.append(fid)
        else:
            to_look_up.append(fid)

    tally.settled += len(settled_now)
    if dry_run or not settled_now:
        return to_look_up
    for fid in settled_now:
        store.record_outcome(conn, resolver.axis_, file_id=fid, status="done", now=now)
    conn.commit()
    return to_look_up


def _group_by_identity(
    conn: sqlite3.Connection,
    file_ids: list[int],
) -> dict[axis.LookupIdentity, list[int]]:
    """Group file ids by their lookup identity, preserving first-seen group order."""
    groups: dict[axis.LookupIdentity, list[int]] = {}
    for fid in file_ids:
        identity = axis.lookup_identity(store.get_tags(conn, fid))
        groups.setdefault(identity, []).append(fid)
    return groups


def _process_groups[C, R](  # noqa: PLR0913 - cohesive orchestration inputs
    conn: sqlite3.Connection,
    resolver: AxisResolver[C, R],
    groups: dict[axis.LookupIdentity, list[int]],
    client: C | None,
    tally: _Tally[R],
    *,
    dry_run: bool,
) -> None:
    """Resolve each group via *client*, built by the resolver when ``None``."""
    with lookup_clients.injected_or_owned(client, lambda: resolver.build_client(conn)) as source:
        for identity, fids in groups.items():
            _process_one_group(conn, resolver, source, identity, fids, tally, dry_run=dry_run)


def _process_one_group[C, R](  # noqa: PLR0913 - cohesive per-group inputs
    conn: sqlite3.Connection,
    resolver: AxisResolver[C, R],
    source: C,
    identity: axis.LookupIdentity,
    file_ids: list[int],
    tally: _Tally[R],
    *,
    dry_run: bool,
) -> None:
    """Look one group up and settle its files on the answer.

    The group's status rows are committed together at the end. A dry run counts the outcome and
    writes nothing.
    """
    key = resolver.group_key(identity)
    failed: set[int] = set()
    answer: R | None = None
    status = "no_match"

    try:
        answer = resolver.lookup(source, identity)
    except resolver.transient_error as exc:
        logger.warning("%s lookup failed for %r: %s", resolver.axis_.name, key, exc)
        tally.error_items.append({"key": key, "message": str(exc)})
        return

    tally.answers[identity] = answer
    if answer is None:
        tally.no_match += len(file_ids)
    else:
        status = "done"
        # Every stage runs before any row write, because stage_tags needs the write lock this
        # connection would otherwise hold.
        failed = _stage_group(conn, resolver, file_ids, answer, tally, dry_run=dry_run)
    # A file that could not be staged writes no row, so it stays pending for the next call.
    tally.settled += len(file_ids) - len(failed)

    # The lookup already happened, so a preview reports the outcome. Only the stage and the
    # status rows are withheld until the real run.
    if dry_run:
        return
    now = clock.utc_now()
    for fid in file_ids:
        if fid not in failed:
            store.record_outcome(conn, resolver.axis_, file_id=fid, status=status, now=now)
    conn.commit()


def _stage_group[C, R](  # noqa: PLR0913 - cohesive per-group inputs
    conn: sqlite3.Connection,
    resolver: AxisResolver[C, R],
    file_ids: list[int],
    answer: R,
    tally: _Tally[R],
    *,
    dry_run: bool,
) -> set[int]:
    """Stage *answer* on each of *file_ids* and return the ids staging refused.

    A refused file (vanished, unreadable or unwritable since the scan) is itemized, so it stays
    ``pending`` without aborting its siblings or the call.
    """
    failed: set[int] = set()
    for fid in file_ids:
        try:
            staged = resolver.stage(conn, fid, answer, dry_run)
        except ValueError as exc:
            logger.warning("%s: file_id=%d not staged: %s", resolver.axis_.name, fid, exc)
            tally.error_items.append({"key": f"file_id={fid}", "message": str(exc)})
            failed.add(fid)
            continue
        if staged:
            tally.staged_files += 1
    return failed


def _build_run[R](tally: _Tally[R], *, pending_remaining: int, dry_run: bool) -> ResolverRun[R]:
    """Freeze the run's tally and counts into a :class:`ResolverRun`."""
    return ResolverRun(
        settled=tally.settled,
        staged_files=tally.staged_files,
        no_match=tally.no_match,
        pending_remaining=pending_remaining,
        more=not dry_run and tally.settled > 0 and pending_remaining > 0,
        errors=len(tally.error_items),
        error_items=list(tally.error_items),
        summary=_summarize(tally, pending_remaining=pending_remaining, dry_run=dry_run),
        answers=dict(tally.answers),
    )


def _summarize[R](tally: _Tally[R], *, pending_remaining: int, dry_run: bool) -> str:
    """Build a short, plain human summary of what settled and what is left.

    A dry run records nothing, so its remainder is not resumable and is worded accordingly.
    """
    errors = len(tally.error_items)
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
    if errors > 0:
        parts.append(f"{errors} item(s) errored and their files stay pending. Re-run to retry.")
    return " ".join(parts)
