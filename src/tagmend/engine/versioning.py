"""Append-only tag-revision history + revert.

The heart of TagMend's safety story. Every managed-tag change to a file appends a new
row to ``tag_revisions`` keyed ``(file_id, version)``. Version 0 is the original as-found
baseline and each later row is +1. A row stores a FULL snapshot of the managed tags plus a
human-readable diff. History is never mutated or deleted.

**Revert is itself an append, not a pointer move.** ``revert_tags(file, target)`` reads the
target revision's snapshot, writes it back to the file, refreshes the live ``file_tags``
snapshot, and appends a *new* revision (``origin='revert'``, ``reverted_to_version=target``)
copying that state forward. So there is no mutable "current version" pointer: the
current state is always ``MAX(version)`` and is already materialized in the live
``files``/``file_tags`` tables. Nothing is ever destroyed, you can revert a revert, and
"rolling forward" is just reverting to the version that holds the state you want.

Why version 0 is captured lazily (:func:`ensure_baseline` on first write, not at scan):
files that are never edited get no revision rows, so the log stays proportional to
*changes*, not to library size. A snapshot covers only the fields its own managed set
governed, so before a write to a file whose latest revision predates the current set,
:func:`observe_widened_fields` appends a ``scan`` re-baseline that records the newer fields.
An edit made outside TagMend reaches no revision on its own, so staging and revert first record
it as a ``scan`` revision (:func:`observe_drift`) before writing over it.

Transaction ownership mirrors the rest of the engine: :func:`ensure_baseline`,
:func:`observe_widened_fields`, :func:`observe_drift`, :func:`append_revision` and
:func:`write_and_resync` take an open connection and never commit (building blocks a future
cascade can batch inside one transaction). :func:`revert_tags` owns its own connection and
commit, like :func:`tagmend.engine.library.scan_library`, because it pairs a disk write with
DB writes as one user-facing action.

See PLAN.md §7 (versioning/undo semantics) and §11 (safety model).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import mutagen

from tagmend.engine import (
    clock,
    commits,
    covers,
    db,
    ledger_lock,
    paths,
    pictures,
    resync,
    schema,
    store,
    trash,
)
from tagmend.engine.resync import TAG_FILE_ERRORS
from tagmend.engine.serialize import FieldDict
from tagmend.engine.tags import (
    MANAGED_SET_VERSION,
    MANAGED_TAGS,
    governed_tags,
    read_tags,
    write_managed_tags,
)
from tagmend.log import get_logger

if TYPE_CHECKING:
    import os
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.store import Revision

logger = get_logger(__name__)


def managed_subset(tags: dict[str, list[str]]) -> dict[str, list[str]]:
    """Narrow a full tag map to just the managed keys actually present."""
    return {key: tags[key] for key in MANAGED_TAGS if key in tags}


def compute_diff(
    before: dict[str, list[str]],
    after: dict[str, list[str]],
) -> dict[str, dict[str, list[str]]]:
    """Return ``{tag: {"from": [...], "to": [...]}}`` for changed managed tags only.

    Both inputs are already managed-only. List equality is order-sensitive, since a
    tag's multi-value order is meaningful (e.g. ``genre = [primary, secondary]``).
    An added tag has ``from=[]`` and a removed tag has ``to=[]``. Unchanged tags are
    omitted, so an empty result means "nothing changed".
    """
    diff: dict[str, dict[str, list[str]]] = {}
    for key in set(before) | set(after):
        old = before.get(key, [])
        new = after.get(key, [])
        if old != new:
            diff[key] = {"from": old, "to": new}
    return diff


def ensure_baseline(
    conn: sqlite3.Connection,
    file_id: int,
    *,
    managed_tags: dict[str, list[str]],
    now: str,
) -> bool:
    """Capture version 0 for *file_id* if it has none yet (idempotent).

    *managed_tags* may be a full tag map, and only the managed subset is snapshotted. The
    baseline is a ``scan`` revision with an empty diff. Returns ``True`` if a baseline was
    written, ``False`` if one already existed. Does not commit.
    """
    if store.max_version(conn, file_id) is not None:
        return False
    store.insert_revision(
        conn,
        file_id=file_id,
        version=0,
        origin="scan",
        managed_tags=managed_subset(managed_tags),
        diff={},
        now=now,
    )
    return True


def predates_managed_set(revision: Revision) -> bool:
    """Whether *revision* was stamped under an older managed set than the current one."""
    return revision.managed_set < MANAGED_SET_VERSION


def observe_widened_fields(
    conn: sqlite3.Connection,
    file_id: int,
    *,
    managed_tags: dict[str, list[str]],
    now: str,
) -> bool:
    """Re-baseline *file_id* under the current managed set when its latest revision predates it.

    Without this, a change to a newer field has no record of the field's previous value, so its
    commit diff reads ``from: []`` and a revert cannot restore it. The appended ``scan`` revision
    snapshots *managed_tags* (read from disk) under the current set. Its diff covers only the
    fields the older set governed, so it holds external drift and is ``{}`` when there is none.
    Returns whether a row was written. Does not commit.
    """
    latest = store.latest_revision(conn, file_id)
    if latest is None or not predates_managed_set(latest):
        return False

    governed = governed_tags(latest.managed_set)
    snapshot = managed_subset(managed_tags)
    drift = compute_diff(_restrict(latest.managed_tags, governed), _restrict(snapshot, governed))
    store.insert_revision(
        conn,
        file_id=file_id,
        version=latest.version + 1,
        origin="scan",
        managed_tags=snapshot,
        diff=drift,
        now=now,
        note=f"re-baseline: managed set {latest.managed_set} -> {MANAGED_SET_VERSION}",
    )
    return True


def _restrict(tags: dict[str, list[str]], fields: frozenset[str]) -> dict[str, list[str]]:
    """Return the entries of *tags* whose key is in *fields*."""
    return {key: values for key, values in tags.items() if key in fields}


def observe_drift(
    conn: sqlite3.Connection,
    file_id: int,
    *,
    managed_tags: dict[str, list[str]],
    now: str,
) -> bool:
    """Append a ``scan`` revision when *managed_tags* (read from disk) differ from the latest one.

    A commit or revert revision diffs against the latest revision, so an unobserved external value
    would otherwise sit in no revision. A latest revision under an older managed set is
    :func:`observe_widened_fields`'s case and is left alone. Returns whether a row was written.
    Does not commit.
    """
    latest = store.latest_revision(conn, file_id)
    if latest is None or predates_managed_set(latest):
        return False

    snapshot = managed_subset(managed_tags)
    drift = compute_diff(latest.managed_tags, snapshot)
    if not drift:
        return False
    store.insert_revision(
        conn,
        file_id=file_id,
        version=latest.version + 1,
        origin="scan",
        managed_tags=snapshot,
        diff=drift,
        now=now,
        note="observed external edit",
    )
    return True


def append_revision(  # noqa: PLR0913 - cohesive revision-append inputs
    conn: sqlite3.Connection,
    file_id: int,
    *,
    managed_tags: dict[str, list[str]],
    origin: str,
    now: str,
    note: str | None = None,
    commit_id: int | None = None,
) -> int | None:
    """Append a new revision recording the change from the latest snapshot to *managed_tags*.

    *managed_tags* may be a full tag map, and only the managed subset is compared/stored.
    *commit_id* groups this change with the other files in the same commit (``None`` for
    an ungrouped edit). Returns the new version number, or ``None`` when nothing managed
    actually changed (no row is written, so the log stays meaningful). Raises
    :class:`ValueError` if no baseline exists yet (call :func:`ensure_baseline` first).
    Does not commit.
    """
    previous_revision = store.latest_revision(conn, file_id)
    if previous_revision is None:
        message = f"no baseline revision for file_id={file_id}, call ensure_baseline first"
        raise ValueError(message)

    new_snapshot = managed_subset(managed_tags)
    diff = compute_diff(previous_revision.managed_tags, new_snapshot)
    if not diff:
        return None

    version = previous_revision.version + 1
    store.insert_revision(
        conn,
        file_id=file_id,
        version=version,
        origin=origin,
        managed_tags=new_snapshot,
        diff=diff,
        now=now,
        commit_id=commit_id,
        note=note,
    )
    return version


def write_and_resync(  # noqa: PLR0913 - cohesive keyword-only write inputs
    conn: sqlite3.Connection,
    file_id: int,
    path: Path,
    target: dict[str, list[str]],
    *,
    write: bool,
    droppable_frames: frozenset[str],
    now: str,
) -> dict[str, list[str]]:
    """Write *target* to *path* when *write* holds, then re-sync the ledger to the file's bytes.

    :func:`tagmend.engine.resync.resync_snapshot` does the re-sync. Returns the re-read tags.
    Does not commit.
    """
    before_write = path.stat()
    audio_proven = False
    if write:
        audio_proven = write_managed_tags(
            path, target, droppable_frames=droppable_frames
        ).audio_proven
    return resync.resync_snapshot(
        conn,
        file_id,
        path,
        before=(before_write.st_size, before_write.st_mtime_ns),
        audio_proven=audio_proven,
        now=now,
    )


def _revert_target_tags(
    target: Revision,
    later: list[Revision],
    current: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Return the managed-tag dict to write when reverting to *target*.

    :func:`~tagmend.engine.tags.write_managed_tags` deletes every managed key ABSENT from the
    dict it is given, so a snapshot is exact only for the fields its own managed set governed.
    A governed field absent from it was empty at capture and is deleted. A field outside that
    set is read from the first revision in *later* (the revisions after *target*) whose set
    governs it, absence included, when that revision is a ``scan`` observation. Otherwise the
    file's *current* value is kept: a commit or revert snapshot holds that change's own output,
    and an observation after it would only see that output.
    """
    governed = governed_tags(target.managed_set)

    planned: dict[str, list[str]] = {}
    for field in MANAGED_TAGS - governed:
        first = next(
            (revision for revision in later if field in governed_tags(revision.managed_set)),
            None,
        )
        source = first.managed_tags if first is not None and first.origin == "scan" else current
        if field in source:
            planned[field] = source[field]
    planned.update(target.managed_tags)
    return planned


def _revert_plan(
    conn: sqlite3.Connection,
    file_id: int,
    target_version: int,
) -> tuple[Revision, Path]:
    """Return the revision to restore and the file's path, or raise :class:`ValueError`.

    The file is checked before the revision, so a mistyped file id reports "unknown file_id"
    rather than a missing revision.
    """
    file_row = store.get_file_by_id(conn, file_id)
    if file_row is None:
        message = f"unknown file_id={file_id}"
        raise ValueError(message)
    if file_row.is_missing:
        message = f"cannot revert a missing file (file_id={file_id})"
        raise ValueError(message)
    target = store.get_revision(conn, file_id, target_version)
    if target is None:
        message = f"no revision {target_version} for file_id={file_id}"
        raise ValueError(message)
    return target, Path(file_row.folder) / file_row.filename


def _planned_revert(
    conn: sqlite3.Connection,
    target: Revision,
    current: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Return the managed tags a revert to *target* writes over *current* (the disk's)."""
    return _revert_target_tags(
        target,
        store.revisions_after(conn, target.file_id, target.version),
        current,
    )


def _overwrites_external_edit(
    latest: Revision,
    current: dict[str, list[str]],
    planned: dict[str, list[str]],
) -> bool:
    """Whether writing *planned* over the disk's *current* tags destroys a value no revision holds.

    Only the fields *latest*'s managed set governed are compared. A disk already holding
    *planned* loses nothing, and is how a crash after a revert's own write looks on its rerun.
    """
    if not compute_diff(current, planned):
        return False
    governed = governed_tags(latest.managed_set)
    return bool(
        compute_diff(_restrict(latest.managed_tags, governed), _restrict(current, governed))
    )


def _observe_durably(conn: sqlite3.Connection, target: Revision, path: Path) -> bool:
    """Record the disk as a ``scan`` revision when no revision holds it, and commit that first.

    A stale-set file is re-baselined and an external edit is observed (:func:`observe_drift`)
    before any revert write. A crash after that write then rolls back only the revert row, and
    its rerun finds the disk holding the target, which is never recorded as drift. Returns
    whether the revert would overwrite an external edit (:func:`_overwrites_external_edit`).
    """
    file_id = target.file_id
    now = clock.utc_now()
    current = managed_subset(read_tags(path).tags)
    latest = store.latest_revision(conn, file_id)
    if latest is None:  # pragma: no cover - defensive, the target exists
        message = f"no baseline for file_id={file_id}"
        raise RuntimeError(message)

    drifted = _overwrites_external_edit(latest, current, _planned_revert(conn, target, current))
    if predates_managed_set(latest):
        observe_widened_fields(conn, file_id, managed_tags=current, now=now)
    elif drifted:
        observe_drift(conn, file_id, managed_tags=current, now=now)
    conn.commit()
    return drifted


def _signature(path: Path) -> tuple[int, int] | None:
    """Return *path*'s ``(size, mtime_ns)``, or ``None`` when it cannot be read.

    An atomic tag write replaces the file, so an unchanged signature shows no write landed.
    """
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return stat_result.st_size, stat_result.st_mtime_ns


def _revert_file(  # noqa: PLR0913 - cohesive keyword-only per-file revert inputs
    conn: sqlite3.Connection,
    file_id: int,
    target_version: int,
    *,
    note: str | None,
    commit_id: int,
    droppable_frames: frozenset[str],
) -> tuple[int, bool]:
    """Restore one file to *target_version* and append the revert revision. No commit.

    The shared per-file revert core (single-file revert = a group revert of one). The caller
    has already re-baselined a stale-set file and observed an external edit
    (:func:`_observe_durably`). It validates, writes the target snapshot to disk FIRST,
    refreshes the live ``file_tags`` snapshot from the re-read file, then appends a new
    ``origin='revert'`` revision (``reverted_to_version=target_version``) under *commit_id*.
    Unlike :func:`append_revision`, a revert is **always** recorded even when the managed tags
    did not change. It is an explicit, audited action and is what makes "revert a revert" work.
    Returns ``(new version, changed)``, where *changed* is ``False`` for a revert that moved
    nothing on disk (an empty diff). Callers report that as ``noop`` rather than as a
    successful revert. The write is given *droppable_frames*.

    Raises :class:`ValueError` if the file is unknown, the file is flagged missing, or the
    target revision is unknown (:func:`_revert_plan`). Leaves all DB writes in the open
    transaction. The caller owns the ``conn.commit()``.
    """
    target, path = _revert_plan(conn, file_id, target_version)
    now = clock.utc_now()
    current = managed_subset(read_tags(path).tags)

    # Disk write first, before the revert row: a write failure aborts with no row. A revert
    # that moves nothing skips the write, so it never rewrites the file.
    planned = _planned_revert(conn, target, current)
    reverted_tags = write_and_resync(
        conn,
        file_id,
        path,
        planned,
        write=bool(compute_diff(current, planned)),
        droppable_frames=droppable_frames,
        now=now,
    )

    # Append the revert (always, even on an empty diff).
    previous_revision = store.latest_revision(conn, file_id)
    if previous_revision is None:  # pragma: no cover - defensive, the target existed
        message = f"no baseline for file_id={file_id}"
        raise RuntimeError(message)

    reverted_snapshot = managed_subset(reverted_tags)
    diff = compute_diff(previous_revision.managed_tags, reverted_snapshot)
    version = previous_revision.version + 1
    store.insert_revision(
        conn,
        file_id=file_id,
        version=version,
        origin="revert",
        managed_tags=reverted_snapshot,
        diff=diff,
        now=now,
        reverted_to_version=target_version,
        commit_id=commit_id,
        note=note,
    )
    return version, bool(diff)


@dataclass(frozen=True, slots=True)
class RevertResult(FieldDict):
    """Summary of a single-file revert: the new revision and the commit recording it.

    A dry run records nothing, so its ``new_version`` and ``commit_id`` are ``None``.
    """

    file_id: int
    target_version: int
    new_version: int | None
    commit_id: int | None
    status: str  # 'reverted' (tags moved) | 'noop' (already at the target state)
    dry_run: bool


@ledger_lock.mutating
def revert_tags(
    settings: Settings,
    file_id: int,
    version: int,
    *,
    note: str | None = None,
    dry_run: bool = False,
) -> RevertResult:
    """Restore *file_id* to *version*, recorded as its own ``origin='revert'`` commit.

    Every disk mutation is a commit (PLAN.md §7): the revert revision lands under a
    fresh single-file commit row, so it shows in ``list_commits`` and can itself be
    undone with :func:`revert_commit`. Refuses if the file has a pending staged change
    (commit or unstage it first, since a staged target computed against the pre-revert
    state would silently override the revert at the next commit), or a staged path change,
    since a tag write replaces the file the move is about to carry.

    The revision is appended either way, but ``status`` reports what actually happened:
    ``'reverted'`` when the managed tags moved, ``'noop'`` when the file already held the
    target state and nothing on disk changed.

    *dry_run* reports that same status without writing the file or the ledger, and keeps
    every refusal of the real call, so the preview predicts it.

    Raises :class:`ValueError` if the file is unknown, flagged missing, or staged, or the
    target revision is unknown. Owns its transaction. The ``applying`` commit row is durable
    before the disk write, as in ``commit_tags`` and :func:`revert_commit`. A failure that left
    the file untouched marks the row ``applied`` with no revision and re-raises. A failure or a
    crash after the write leaves it ``applying``, so ``check_health`` reports it, and a rerun
    records the revert.
    """
    new_version: int | None = None
    commit_id: int | None = None
    changed = False
    before_write: tuple[int, int] | None = None

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)

        if store.is_staged(connection, file_id):
            message = (
                f"file_id={file_id} has a staged change - commit or unstage it before reverting"
            )
            raise ValueError(message)
        if store.get_staged_path(connection, file_id) is not None:
            message = (
                f"file_id={file_id} has a staged path change - run commit_paths or unstage_paths "
                "before reverting"
            )
            raise ValueError(message)

        target, path = _revert_plan(connection, file_id, version)
        if dry_run:
            changed = _preview_kind(connection, target, path) != "noop"
        else:
            # An observed external edit is still overwritten, because its scan revision keeps it.
            _observe_durably(connection, target, path)
            before_write = _signature(path)
            commit_id = commits.create_commit(
                connection,
                origin="revert",
                message=note,
                now=clock.utc_now(),
            )
            connection.commit()  # commit row durable before the disk write
            try:
                new_version, changed = _revert_file(
                    connection,
                    file_id,
                    version,
                    note=note,
                    commit_id=commit_id,
                    droppable_frames=frozenset(settings.id3_droppable_frames),
                )
            except TAG_FILE_ERRORS:
                connection.rollback()
                if _signature(path) == before_write:
                    commits.set_commit_status(connection, commit_id, "applied")
                    connection.commit()
                raise
            commits.set_commit_status(connection, commit_id, "applied")
            connection.commit()
    finally:
        connection.close()

    status = "reverted" if changed else "noop"
    logger.info(
        "%s file_id=%d to version %d (new version %s, commit %s, dry_run=%s)",
        status,
        file_id,
        version,
        new_version,
        commit_id,
        dry_run,
    )
    return RevertResult(
        file_id=file_id,
        target_version=version,
        new_version=new_version,
        commit_id=commit_id,
        status=status,
        dry_run=dry_run,
    )


# --- commit-level revert (group undo, PLAN.md §7 "reverting a whole commit_id") ------


# The two plan-pass kinds that go on to :func:`_revert_file`. A 'noop' file is still
# processed (the revert revision is always appended). Only its reported status differs.
_PROCESSABLE_KINDS: Final = frozenset({"revertable", "noop"})


def _would_change(
    latest: Revision,
    current: dict[str, list[str]],
    planned: dict[str, list[str]],
) -> bool:
    """Whether a revert writing *planned* over the disk's *current* tags records any change.

    Mirrors the revision :func:`_revert_file` appends, so a dry run cannot promise a revert that
    delivers nothing. A revert that writes diffs from the observed disk. One that writes nothing
    diffs from *latest*, unless *latest* predates the current set, whose re-baseline is the disk.
    """
    if compute_diff(current, planned):
        return True
    return not predates_managed_set(latest) and bool(compute_diff(latest.managed_tags, planned))


def _preview_kind(conn: sqlite3.Connection, target: Revision, path: Path) -> str:
    """Classify a revert to *target* from one disk read: 'revertable', 'noop' or a skip.

    'skipped_later_changes' means the revert would overwrite an external edit no revision holds
    (:func:`_overwrites_external_edit`). The read is accepted deliberately: an honest preview is
    the point. An unreadable file is reported as revertable, leaving the real run's error
    handling to deal with it.
    """
    latest = store.latest_revision(conn, target.file_id)
    if latest is None:  # pragma: no cover - defensive, the target exists
        return "revertable"
    try:
        current = managed_subset(read_tags(path).tags)
    except (OSError, mutagen.MutagenError) as exc:  # type: ignore[attr-defined]
        logger.warning("revert preview: file_id=%d unreadable: %s", target.file_id, exc)
        return "revertable"

    planned = _planned_revert(conn, target, current)
    if _overwrites_external_edit(latest, current, planned):
        return "skipped_later_changes"
    if not _would_change(latest, current, planned):
        return "noop"
    return "revertable"


def _no_later_changes(conn: sqlite3.Connection, file_id: int, version: int) -> bool:
    """Whether every revision of *file_id* after *version* is a drift-free ``scan`` observation.

    A re-baseline with an empty diff records no change, so it never blocks undoing the commit
    before it. A noop revert is an audited action, so it still counts as a later change.
    """
    return all(
        revision.origin == "scan" and not revision.diff
        for revision in store.revisions_after(conn, file_id, version)
    )


def _classify_for_revert(conn: sqlite3.Connection, revision: Revision) -> str:
    """Classify one commit revision: 'revertable' | 'noop' | 'skipped_later_changes' | 'missing'.

    Read-only (shared by the dry run and the plan pass of a real run). A file is revertable
    iff it still exists on disk and nothing changed it after the commit's revision: neither a
    later revision (:func:`_no_later_changes`) nor an edit made outside TagMend
    (:func:`_preview_kind`). Skip+report semantics: later changes are never silently
    destroyed. Revert those files per-file, deliberately, if that is really wanted. 'noop' is
    a revertable file whose revert would move nothing (see :func:`_would_change`). It is still
    processed, just reported honestly.
    """
    file_row = store.get_file_by_id(conn, revision.file_id)
    if file_row is None or file_row.is_missing:
        return "missing"
    path = Path(file_row.folder) / file_row.filename
    if not path.exists():
        return "missing"
    if not _no_later_changes(conn, revision.file_id, revision.version):
        return "skipped_later_changes"
    target = store.get_revision(conn, revision.file_id, revision.version - 1)
    if target is None:  # pragma: no cover - defensive, a commit revision has a predecessor
        return "revertable"
    return _preview_kind(conn, target, path)


def _require_tag_commit(
    commit_id: int,
    logs: dict[str, int],
    path: str | os.PathLike[str] | None,
) -> None:
    """Refuse a commit holding no tag log row, and a *path* scope, which only a path commit has."""
    if "tag_revisions" not in logs:
        message = (
            f"commit {commit_id} holds no change in the tag, path, cover or picture log to revert"
        )
        raise ValueError(message)
    if path is not None:
        message = (
            f"commit {commit_id} changed tags, and path= selects the files of a path commit "
            "only. Revert it whole, or one file with revert_tags"
        )
        raise ValueError(message)


def _require_revertable(conn: sqlite3.Connection, commit_id: int) -> None:
    """Refuse an unknown commit, one still ``applying``, and any staged row of any domain."""
    target = commits.get_commit_in(conn, commit_id)
    if target is None:
        message = f"unknown commit_id={commit_id}"
        raise ValueError(message)
    if target.status == "applying":
        message = (
            f"commit {commit_id} is still applying (interrupted run?) - "
            "run commit_tags, commit_paths, commit_covers or commit_pictures to recover, "
            "then retry"
        )
        raise ValueError(message)
    if store.any_staged(conn):
        raise ValueError(paths.STAGING_NOT_EMPTY)


def _require_whole_commit(
    commit_id: int,
    path: str | os.PathLike[str] | None,
    *,
    changed: str,
) -> None:
    """Refuse a *path* scope on a cover or picture commit, since only a path commit has one."""
    if path is not None:
        message = (
            f"commit {commit_id} {changed}, and path= selects the files of a path commit "
            "only. Revert it whole"
        )
        raise ValueError(message)


@ledger_lock.mutating
def revert_commit(
    settings: Settings,
    commit_id: int,
    *,
    note: str | None = None,
    dry_run: bool = False,
    path: str | os.PathLike[str] | None = None,
) -> commits.RevertCommitResult:
    """Undo an entire commit as a unit: revert every file it changed to its pre-commit state.

    Domain-neutral: a commit whose rows sit in ``path_revisions`` or ``sidecar_moves`` is undone
    by :func:`tagmend.engine.paths.revert_commit_moves`, which moves each file and sidecar back
    to its source. *path* applies to such a commit only and keeps the files and sidecars sitting
    at or under it now. A commit whose rows sit in ``cover_writes`` is undone by
    :func:`tagmend.engine.covers.revert_cover_commit`, which sends each cover it created to the
    OS trash (:func:`tagmend.engine.trash.send_to_trash`) and writes each cover it removed again.
    A commit whose rows sit in ``picture_writes`` is undone by
    :func:`tagmend.engine.pictures.revert_picture_commit`, which writes each embedded picture it
    removed back into its file and removes each one it restored. A commit with no tag, path,
    cover or picture row raises :class:`ValueError`, and so does *path* on a tag, cover or
    picture commit. A tag commit is undone by :func:`_revert_tag_commit`.

    Guards: the target must exist and not be ``status='applying'`` (run ``commit_tags``,
    ``commit_paths``, ``commit_covers`` or ``commit_pictures`` to recover an interrupted run
    first). ``interrupted`` targets are allowed (reverts whatever they durably committed). The
    staging area, tag, path, cover and picture rows alike, must be EMPTY: commit or unstage
    pending work before rolling back (git's "commit or stash first"). Owns its connection.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        _require_revertable(connection, commit_id)
        logs = store.commit_log_counts(connection, commit_id)
        if "path_revisions" in logs or "sidecar_moves" in logs:
            return paths.revert_commit_moves(
                connection, settings, commit_id, note=note, dry_run=dry_run, path=path
            )
        if "cover_writes" in logs:
            _require_whole_commit(commit_id, path, changed="wrote covers")
            return covers.revert_cover_commit(
                connection,
                settings,
                commit_id,
                note=note,
                dry_run=dry_run,
                trash=trash.send_to_trash,
            )
        if "picture_writes" in logs:
            _require_whole_commit(commit_id, path, changed="changed embedded pictures")
            return pictures.revert_picture_commit(
                connection, settings, commit_id, note=note, dry_run=dry_run
            )
        _require_tag_commit(commit_id, logs, path)
        return _revert_tag_commit(connection, settings, commit_id, note=note, dry_run=dry_run)
    finally:
        connection.close()


def _revert_tag_commit(
    connection: sqlite3.Connection,
    settings: Settings,
    commit_id: int,
    *,
    note: str | None,
    dry_run: bool,
) -> commits.RevertCommitResult:
    """Undo the tag commit *commit_id* on the caller's open *connection*, which it never closes.

    The group counterpart of :func:`revert_tags` (PLAN.md §7: "reverting a whole
    ``commit_id`` undoes an entire run"). For each revision the target commit created,
    the file is restored to the snapshot just before it (``version - 1``, the
    baseline when the commit created version 1), appended as a new revision under ONE
    fresh ``origin='revert'`` commit whose ``reverted_from`` records the undone
    commit. History stays append-only: nothing is destroyed, and the revert commit can
    itself be reverted.

    Skip + report: a file changed again by a LATER commit or revert, or edited outside TagMend,
    is skipped (status ``skipped_later_changes``), never silently rolled past. Both are checked
    in the plan pass and again just before the file is written. An outside edit is recorded as a
    ``scan`` revision first. Revert it per-file if that is really wanted. A drift-free ``scan``
    re-baseline is not a change. Missing files are reported, a per-file disk failure is
    recorded as ``error`` and the rest of the group still completes.

    A file already holding its pre-commit state is reported ``noop``: the revert revision
    is still appended (revert is always audited), but it is not counted as ``reverted``,
    so the summary never claims a change that did not happen.

    *dry_run* returns the full per-file classification without touching anything
    (``commit_id`` is ``None``, ``status='reverted'`` means "would be reverted",
    ``noop`` means "would change nothing"). The preview reads each planned file from disk
    to tell those two apart.

    Crash recovery is resume-free, like ``commit_tags``: just run it again. Files
    already reverted by the crashed run now have a later revision and report as
    ``skipped_later_changes``, so nothing is double-reverted. Commits per file, mirroring the
    commit loop.
    """
    # Plan pass (read-only): classify every file the target commit changed.
    planned: list[tuple[Revision, str]] = [
        (revision, _classify_for_revert(connection, revision))
        for revision in store.revisions_for_commit(connection, commit_id)
    ]
    processable = [revision for revision, kind in planned if kind in _PROCESSABLE_KINDS]

    if dry_run or not processable:
        outcomes = [
            commits.FileRevertOutcome(
                file_id=revision.file_id,
                target_version=revision.version - 1 if kind in _PROCESSABLE_KINDS else None,
                new_version=None,
                status="reverted" if kind == "revertable" else kind,
            )
            for revision, kind in planned
        ]
        return commits.summarize_revert(
            commit_id=None,
            reverted_from=commit_id,
            dry_run=dry_run,
            outcomes=outcomes,
        )

    new_commit = commits.create_commit(
        connection,
        origin="revert",
        message=note,
        now=clock.utc_now(),
        reverted_from=commit_id,
    )
    connection.commit()  # commit row durable before any per-file work

    outcomes = []
    for revision, kind in planned:
        if kind not in _PROCESSABLE_KINDS:
            outcomes.append(
                commits.FileRevertOutcome(
                    file_id=revision.file_id,
                    target_version=None,
                    new_version=None,
                    status=kind,
                ),
            )
            continue
        # A later commit landing after the plan pass is skipped too. This runs before
        # _observe_durably, whose re-baseline would count as a later revision.
        if not _no_later_changes(connection, revision.file_id, revision.version):
            outcomes.append(
                commits.FileRevertOutcome(
                    file_id=revision.file_id,
                    target_version=None,
                    new_version=None,
                    status="skipped_later_changes",
                ),
            )
            continue
        try:
            restored, file_path = _revert_plan(connection, revision.file_id, revision.version - 1)
            # An external edit landing after the plan pass is kept and skipped the same way.
            if _observe_durably(connection, restored, file_path):
                outcomes.append(
                    commits.FileRevertOutcome(
                        file_id=revision.file_id,
                        target_version=None,
                        new_version=None,
                        status="skipped_later_changes",
                    ),
                )
                continue
            new_version, changed = _revert_file(
                connection,
                revision.file_id,
                revision.version - 1,
                note=note,
                commit_id=new_commit,
                droppable_frames=frozenset(settings.id3_droppable_frames),
            )
            connection.commit()  # the disk write is done, so the revision is now durable
        except TAG_FILE_ERRORS as exc:
            connection.rollback()
            logger.warning(
                "revert_commit %d: file_id=%d failed: %s",
                commit_id,
                revision.file_id,
                exc,
            )
            outcomes.append(
                commits.FileRevertOutcome(
                    file_id=revision.file_id,
                    target_version=revision.version - 1,
                    new_version=None,
                    status="error",
                    detail=str(exc),
                ),
            )
            continue
        outcomes.append(
            commits.FileRevertOutcome(
                file_id=revision.file_id,
                target_version=revision.version - 1,
                new_version=new_version,
                status="reverted" if changed else "noop",
            ),
        )

    commits.set_commit_status(connection, new_commit, "applied")
    connection.commit()

    result = commits.summarize_revert(
        commit_id=new_commit,
        reverted_from=commit_id,
        dry_run=False,
        outcomes=outcomes,
    )
    logger.info(
        "revert of commit %d as commit %d: reverted=%d noop=%d skipped=%d missing=%d errors=%d",
        commit_id,
        new_commit,
        result.reverted,
        result.noop,
        result.skipped,
        result.missing,
        result.errors,
    )
    return result


def history_tags(settings: Settings, file_id: int) -> list[Revision]:
    """Return *file_id*'s full revision log, oldest (version 0) first. Read-only.

    Raises :class:`ValueError` for an unknown *file_id*, so a typo does not read as a file
    with no history.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        if store.get_file_by_id(connection, file_id) is None:
            message = f"unknown file_id={file_id}"
            raise ValueError(message)
        return store.get_revisions(connection, file_id)
    finally:
        connection.close()


def commit_logs(settings: Settings, commit_id: int) -> dict[str, int]:
    """Return how many rows each revision log holds for *commit_id*, naming non-empty logs only.

    Read-only. This is how ``get_commit`` says which domain a commit changed.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        return store.commit_log_counts(connection, commit_id)
    finally:
        connection.close()
