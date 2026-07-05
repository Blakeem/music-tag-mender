"""Append-only tag-revision history + revert (M3).

The heart of TagMend's safety story. Every managed-tag change to a file appends a new
row to ``tag_revisions`` keyed ``(file_id, version)`` — version 0 is the original
as-found baseline, +1 per change — storing a FULL snapshot of the managed tags plus a
human-readable diff. History is never mutated or deleted.

**Revert is itself an append, not a pointer move.** ``revert(file, target)`` reads the
target revision's snapshot, writes it back to the file, refreshes the live ``file_tags``
snapshot, and appends a *new* revision (``origin='revert'``, ``reverted_from=target``)
copying that state forward. So there is no mutable "current version" pointer: the
current state is always ``MAX(version)`` and is already materialized in the live
``files``/``file_tags`` tables. Nothing is ever destroyed, you can revert a revert, and
"rolling forward" is just reverting to the version that holds the state you want.

Why version 0 is captured lazily (:func:`ensure_baseline` on first write, not at scan):
files that are never edited get no revision rows, so the log stays proportional to
*changes*, not to library size.

Transaction ownership mirrors the rest of the engine: :func:`ensure_baseline`,
:func:`append_revision`, and :func:`history` take an open connection and never commit
(building blocks a future cascade can batch inside one transaction). :func:`revert`
owns its own connection/commit — like :func:`tagmend.engine.library.scan_library` —
because it pairs a disk write with DB writes as one atomic user-facing action.

See PLAN.md §7 (versioning/undo semantics) and §11 (safety model).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import mutagen

from tagmend.engine import commits, db, schema, store
from tagmend.engine.tags import (
    MANAGED_TAGS,
    ORIGINAL_MANAGED_TAGS,
    read_tags,
    write_managed_tags,
)
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.store import Revision

logger = get_logger(__name__)


def _utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    return datetime.now(UTC).isoformat()


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
    An added tag has ``from=[]``; a removed tag has ``to=[]``; unchanged tags are
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
    origin: str = "scan",
) -> bool:
    """Capture version 0 for *file_id* if it has none yet (idempotent).

    *managed_tags* may be a full tag map; only the managed subset is snapshotted. The
    baseline carries an empty diff. Returns ``True`` if a baseline was written, ``False``
    if one already existed. Does not commit.
    """
    if store.max_version(conn, file_id) is not None:
        return False
    store.insert_revision(
        conn,
        file_id=file_id,
        version=0,
        origin=origin,
        managed_tags=managed_subset(managed_tags),
        diff={},
        now=now,
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

    *managed_tags* may be a full tag map; only the managed subset is compared/stored.
    *commit_id* groups this change with the other files in the same commit (``None`` for
    an ungrouped edit). Returns the new version number, or ``None`` when nothing managed
    actually changed (no row is written — the log stays meaningful). Raises
    :class:`ValueError` if no baseline exists yet (call :func:`ensure_baseline` first).
    Does not commit.
    """
    previous = store.max_version(conn, file_id)
    if previous is None:
        message = f"no baseline revision for file_id={file_id}; call ensure_baseline first"
        raise ValueError(message)
    previous_revision = store.get_revision(conn, file_id, previous)
    if previous_revision is None:  # pragma: no cover - defensive; previous came from MAX()
        message = f"revision {previous} vanished for file_id={file_id}"
        raise RuntimeError(message)

    new_snapshot = managed_subset(managed_tags)
    diff = compute_diff(previous_revision.managed_tags, new_snapshot)
    if not diff:
        return None

    version = previous + 1
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


def _revert_target_tags(
    path: Path,
    snapshot: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Return the managed-tag dict to write when reverting *path* to *snapshot*.

    Guards the widened-:data:`~tagmend.engine.tags.MANAGED_TAGS` migration hazard.
    :func:`~tagmend.engine.tags.write_managed_tags` deletes every managed key ABSENT from
    the dict it is given, so writing a historical snapshot verbatim is only safe for the
    keys that snapshot actually governed. Snapshots captured before the managed set grew
    (every pre-widening baseline/commit) structurally lack the newer identity/MusicBrainz
    fields, and their absence there means "not tracked when captured", not "delete" —
    writing verbatim would silently strip title/album/track/disc/sort/MB-id tags the revert
    was never asked to touch.

    So the widened fields are taken from the file's CURRENT on-disk values wherever
    *snapshot* omits them (preserve, never delete). The
    :data:`~tagmend.engine.tags.ORIGINAL_MANAGED_TAGS` keep strict delete-on-revert: they
    have been managed since version 0 of every file's history, so their absence is a genuine
    "was empty then" and reverting must restore that emptiness (undo a later-added
    genre/artist/…). This mirrors the merge-onto-current guard the staging path already
    applies in :func:`tagmend.engine.staging.stage_tags`.
    """
    target = managed_subset(read_tags(path).tags)
    target.update(snapshot)
    for field in ORIGINAL_MANAGED_TAGS:
        if field not in snapshot:
            target.pop(field, None)
    return target


def _revert_file(
    conn: sqlite3.Connection,
    file_id: int,
    target_version: int,
    *,
    note: str | None,
    commit_id: int,
) -> int:
    """Restore one file to *target_version* and append the revert revision. No commit.

    The shared per-file revert core (single-file revert = a group revert of one).
    Validates, writes the target snapshot to disk FIRST, refreshes the live
    ``file_tags`` snapshot from the re-read file, then appends a new
    ``origin='revert'`` revision (``reverted_from=target_version``) under *commit_id*.
    Unlike :func:`append_revision`, a revert is **always** recorded even when the
    managed tags did not change — it is an explicit, audited action and is what makes
    "revert a revert" work. Returns the new version.

    Raises :class:`ValueError` if the target revision is unknown, the file is unknown,
    or the file is flagged missing. Leaves all DB writes in the open transaction —
    the caller owns the ``conn.commit()``.
    """
    target = store.get_revision(conn, file_id, target_version)
    if target is None:
        message = f"no revision {target_version} for file_id={file_id}"
        raise ValueError(message)
    file_row = store.get_file_by_id(conn, file_id)
    if file_row is None:
        message = f"unknown file_id={file_id}"
        raise ValueError(message)
    if file_row.is_missing:
        message = f"cannot revert a missing file (file_id={file_id})"
        raise ValueError(message)

    # Disk write first, before any DB append: a write failure aborts with no row. The
    # snapshot is merged onto the file's current widened fields so reverting to a
    # pre-widening revision cannot delete tags it never governed (see _revert_target_tags).
    path = Path(file_row.folder) / file_row.filename
    write_managed_tags(path, _revert_target_tags(path, target.managed_tags))

    # Refresh the live snapshot so file_tags reflects the actual on-disk state.
    now = _utc_now()
    reverted_tags = read_tags(path).tags
    store.replace_tags(conn, file_id, reverted_tags, now)
    # Re-sync the files-row signature to the just-written bytes (same fields the scanner
    # stats), so the next incremental scan sees the reverted file as unchanged.
    stat_result = path.stat()
    store.update_signature(
        conn,
        file_id,
        size_bytes=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        now=now,
    )

    # Append the revert (always, even on an empty diff).
    previous = store.max_version(conn, file_id)
    if previous is None:  # pragma: no cover - defensive; target existed
        message = f"no baseline for file_id={file_id}"
        raise RuntimeError(message)
    previous_revision = store.get_revision(conn, file_id, previous)
    if previous_revision is None:  # pragma: no cover - defensive
        message = f"revision {previous} vanished for file_id={file_id}"
        raise RuntimeError(message)

    reverted_snapshot = managed_subset(reverted_tags)
    version = previous + 1
    store.insert_revision(
        conn,
        file_id=file_id,
        version=version,
        origin="revert",
        managed_tags=reverted_snapshot,
        diff=compute_diff(previous_revision.managed_tags, reverted_snapshot),
        now=now,
        reverted_from=target_version,
        commit_id=commit_id,
        note=note,
    )
    return version


@dataclass(frozen=True, slots=True)
class RevertResult:
    """Summary of a single-file revert: the new revision and the commit recording it."""

    file_id: int
    target_version: int
    new_version: int
    commit_id: int

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "file_id": self.file_id,
            "target_version": self.target_version,
            "new_version": self.new_version,
            "commit_id": self.commit_id,
        }


def revert(
    settings: Settings,
    file_id: int,
    target_version: int,
    *,
    note: str | None = None,
) -> RevertResult:
    """Restore *file_id* to *target_version*, recorded as its own ``origin='revert'`` commit.

    Every disk mutation is a commit (PLAN.md §7): the revert revision lands under a
    fresh single-file commit row, so it shows in ``list_commits`` and can itself be
    undone with :func:`revert_commit`. Refuses if the file has a pending staged change
    (commit or unstage it first — a staged target computed against the pre-revert
    state would silently override the revert at the next commit).

    Raises :class:`ValueError` if the target revision is unknown, the file is unknown,
    flagged missing, or staged. Owns its transaction: the commit row, the disk write,
    and the revision land in ONE transaction, so a crash leaves no partial DB state
    (re-running the revert is idempotent).
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)

        if store.is_staged(connection, file_id):
            message = (
                f"file_id={file_id} has a staged change - commit or unstage it before reverting"
            )
            raise ValueError(message)

        commit_id = commits.create_commit(
            connection,
            origin="revert",
            message=note,
            now=_utc_now(),
        )
        version = _revert_file(
            connection,
            file_id,
            target_version,
            note=note,
            commit_id=commit_id,
        )
        commits.set_commit_status(connection, commit_id, "applied")
        connection.commit()
    finally:
        connection.close()

    logger.info(
        "reverted file_id=%d to version %d (new version %d, commit %d)",
        file_id,
        target_version,
        version,
        commit_id,
    )
    return RevertResult(
        file_id=file_id,
        target_version=target_version,
        new_version=version,
        commit_id=commit_id,
    )


# --- commit-level revert (group undo; PLAN.md §7 "reverting a whole commit_id") ------


@dataclass(frozen=True, slots=True)
class FileRevertOutcome:
    """What happened (or would happen, on a dry run) to one file of the target commit."""

    file_id: int
    target_version: int | None  # version restored (the commit's version - 1); None if not
    new_version: int | None  # the appended revert revision; None if not reverted
    status: str  # 'reverted' | 'skipped_later_changes' | 'missing' | 'error'
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RevertCommitResult:
    """Immutable summary of a commit-level revert run."""

    commit_id: int | None  # the NEW revert commit; None on dry run / nothing revertable
    reverted_from: int  # the target commit id
    dry_run: bool
    reverted: int
    skipped: int
    missing: int
    errors: int
    outcomes: tuple[FileRevertOutcome, ...]

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "commit_id": self.commit_id,
            "reverted_from": self.reverted_from,
            "dry_run": self.dry_run,
            "reverted": self.reverted,
            "skipped": self.skipped,
            "missing": self.missing,
            "errors": self.errors,
            "outcomes": [
                {
                    "file_id": o.file_id,
                    "target_version": o.target_version,
                    "new_version": o.new_version,
                    "status": o.status,
                    "detail": o.detail,
                }
                for o in self.outcomes
            ],
        }


def _classify_for_revert(conn: sqlite3.Connection, revision: Revision) -> str:
    """Classify one commit revision for revert: 'revertable' | 'skipped_later_changes' | 'missing'.

    Read-only (shared by the dry run and the plan pass of a real run). A file is
    revertable iff it still exists on disk and the commit's revision is its LATEST —
    skip+report semantics: later changes are never silently destroyed; revert those
    files per-file, deliberately, if that is really wanted.
    """
    file_row = store.get_file_by_id(conn, revision.file_id)
    if file_row is None or file_row.is_missing:
        return "missing"
    path = Path(file_row.folder) / file_row.filename
    if not path.exists():
        return "missing"
    latest = store.max_version(conn, revision.file_id)
    if latest != revision.version:
        return "skipped_later_changes"
    return "revertable"


def _summarize_revert(
    *,
    commit_id: int | None,
    reverted_from: int,
    dry_run: bool,
    outcomes: list[FileRevertOutcome],
) -> RevertCommitResult:
    """Fold per-file outcomes into the public :class:`RevertCommitResult`."""
    return RevertCommitResult(
        commit_id=commit_id,
        reverted_from=reverted_from,
        dry_run=dry_run,
        reverted=sum(1 for o in outcomes if o.status == "reverted"),
        skipped=sum(1 for o in outcomes if o.status == "skipped_later_changes"),
        missing=sum(1 for o in outcomes if o.status == "missing"),
        errors=sum(1 for o in outcomes if o.status == "error"),
        outcomes=tuple(outcomes),
    )


def revert_commit(
    settings: Settings,
    commit_id: int,
    *,
    note: str | None = None,
    dry_run: bool = False,
) -> RevertCommitResult:
    """Undo an entire commit as a unit: revert every file it changed to its pre-commit state.

    The group counterpart of :func:`revert` (PLAN.md §7: "reverting a whole
    ``commit_id`` undoes an entire run"). For each revision the target commit created,
    the file is restored to the snapshot just before it (``version - 1`` — the
    baseline when the commit created version 1), appended as a new revision under ONE
    fresh ``origin='revert'`` commit whose ``reverted_from`` records the undone
    commit. History stays append-only: nothing is destroyed, and the revert commit can
    itself be reverted.

    Skip + report: a file changed again by a LATER commit is skipped (status
    ``skipped_later_changes``), never silently rolled past — revert it per-file if
    that is really wanted. Missing files are reported, a per-file disk failure is
    recorded as ``error`` and the rest of the group still completes.

    Guards: the target must exist and not be ``status='applying'`` (run ``commit_tags``
    to recover an interrupted run first); ``interrupted`` targets are allowed (reverts
    whatever they durably committed). The staging area must be EMPTY — commit or
    unstage pending work before rolling back (git's "commit or stash first").

    *dry_run* returns the full per-file classification without touching anything
    (``commit_id`` is ``None``; ``status='reverted'`` means "would be reverted").

    Crash recovery is resume-free, like ``commit_tags``: just run it again — files
    already reverted by the crashed run now have a later revision and report as
    ``skipped_later_changes``, so nothing is double-reverted. Owns its transactions
    (per-file durability, mirroring the commit loop).
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)

        target = commits.get_commit(connection, commit_id)
        if target is None:
            message = f"unknown commit_id={commit_id}"
            raise ValueError(message)
        if target.status == "applying":
            message = (
                f"commit {commit_id} is still applying (interrupted run?) - "
                "run commit_tags to recover, then retry"
            )
            raise ValueError(message)
        if store.any_staged(connection):
            message = (
                "staging area is not empty - "
                "commit or unstage pending changes before reverting a commit"
            )
            raise ValueError(message)

        # Plan pass (read-only): classify every file the target commit changed.
        planned: list[tuple[Revision, str]] = [
            (revision, _classify_for_revert(connection, revision))
            for revision in store.revisions_for_commit(connection, commit_id)
        ]
        revertable = [revision for revision, kind in planned if kind == "revertable"]

        if dry_run or not revertable:
            outcomes = [
                FileRevertOutcome(
                    file_id=revision.file_id,
                    target_version=revision.version - 1 if kind == "revertable" else None,
                    new_version=None,
                    status="reverted" if kind == "revertable" else kind,
                )
                for revision, kind in planned
            ]
            return _summarize_revert(
                commit_id=None,
                reverted_from=commit_id,
                dry_run=dry_run,
                outcomes=outcomes,
            )

        new_commit = commits.create_commit(
            connection,
            origin="revert",
            message=note,
            now=_utc_now(),
            reverted_from=commit_id,
        )
        connection.commit()  # commit row durable before any per-file work

        outcomes = []
        for revision, kind in planned:
            if kind != "revertable":
                outcomes.append(
                    FileRevertOutcome(
                        file_id=revision.file_id,
                        target_version=None,
                        new_version=None,
                        status=kind,
                    ),
                )
                continue
            try:
                new_version = _revert_file(
                    connection,
                    revision.file_id,
                    revision.version - 1,
                    note=note,
                    commit_id=new_commit,
                )
                connection.commit()  # disk already done inside; revision now durable
            except (OSError, ValueError, mutagen.MutagenError) as exc:  # type: ignore[attr-defined]
                connection.rollback()
                logger.warning(
                    "revert_commit %d: file_id=%d failed: %s",
                    commit_id,
                    revision.file_id,
                    exc,
                )
                outcomes.append(
                    FileRevertOutcome(
                        file_id=revision.file_id,
                        target_version=revision.version - 1,
                        new_version=None,
                        status="error",
                        detail=str(exc),
                    ),
                )
                continue
            outcomes.append(
                FileRevertOutcome(
                    file_id=revision.file_id,
                    target_version=revision.version - 1,
                    new_version=new_version,
                    status="reverted",
                ),
            )

        commits.set_commit_status(connection, new_commit, "applied")
        connection.commit()
    finally:
        connection.close()

    result = _summarize_revert(
        commit_id=new_commit,
        reverted_from=commit_id,
        dry_run=False,
        outcomes=outcomes,
    )
    logger.info(
        "revert of commit %d as commit %d: reverted=%d skipped=%d missing=%d errors=%d",
        commit_id,
        new_commit,
        result.reverted,
        result.skipped,
        result.missing,
        result.errors,
    )
    return result


def history(conn: sqlite3.Connection, file_id: int) -> list[Revision]:
    """Return *file_id*'s full revision log, oldest (version 0) first. Read-only."""
    return store.get_revisions(conn, file_id)


def history_for(settings: Settings, file_id: int) -> list[Revision]:
    """Conn-owning :func:`history`: open the ledger and return *file_id*'s log. Read-only."""
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        return history(connection, file_id)
    finally:
        connection.close()
