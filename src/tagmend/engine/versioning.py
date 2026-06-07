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

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tagmend.engine import db, schema, store
from tagmend.engine.tags import MANAGED_TAGS, read_tags, write_managed_tags
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


def revert(
    settings: Settings,
    file_id: int,
    target_version: int,
    *,
    note: str | None = None,
) -> int:
    """Restore *file_id* to *target_version* and append the revert as a new revision.

    Writes the target revision's managed tags to disk, refreshes the live ``file_tags``
    snapshot from the re-read file, then appends a new ``origin='revert'`` revision
    (``reverted_from=target_version``). Unlike :func:`append_revision`, a revert is
    **always** recorded even when the managed tags did not change — it is an explicit,
    audited action and is what makes "revert a revert" work. Returns the new version.

    Raises :class:`ValueError` if the target revision is unknown, the file is unknown,
    or the file is flagged missing (no on-disk target to write). Owns its transaction.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)

        target = store.get_revision(connection, file_id, target_version)
        if target is None:
            message = f"no revision {target_version} for file_id={file_id}"
            raise ValueError(message)
        file_row = store.get_file_by_id(connection, file_id)
        if file_row is None:
            message = f"unknown file_id={file_id}"
            raise ValueError(message)
        if file_row.is_missing:
            message = f"cannot revert a missing file (file_id={file_id})"
            raise ValueError(message)

        # Disk write first, before any DB append: a write failure aborts with no row.
        path = Path(file_row.folder) / file_row.filename
        write_managed_tags(path, target.managed_tags)

        # Refresh the live snapshot so file_tags reflects the actual on-disk state.
        now = _utc_now()
        reverted_tags = read_tags(path).tags
        store.replace_tags(connection, file_id, reverted_tags, now)

        # Append the revert (always, even on an empty diff).
        previous = store.max_version(connection, file_id)
        if previous is None:  # pragma: no cover - defensive; target existed
            message = f"no baseline for file_id={file_id}"
            raise RuntimeError(message)
        previous_revision = store.get_revision(connection, file_id, previous)
        if previous_revision is None:  # pragma: no cover - defensive
            message = f"revision {previous} vanished for file_id={file_id}"
            raise RuntimeError(message)

        reverted_snapshot = managed_subset(reverted_tags)
        version = previous + 1
        store.insert_revision(
            connection,
            file_id=file_id,
            version=version,
            origin="revert",
            managed_tags=reverted_snapshot,
            diff=compute_diff(previous_revision.managed_tags, reverted_snapshot),
            now=now,
            reverted_from=target_version,
            note=note,
        )
        connection.commit()
    finally:
        connection.close()

    logger.info(
        "reverted file_id=%d to version %d (new version %d)",
        file_id,
        target_version,
        version,
    )
    return version


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
