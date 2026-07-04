"""Tags staging area + commit orchestration (M3 write path).

The git "index → commit" step that sits above the per-file revision log in
:mod:`tagmend.engine.versioning`. A *staged* change records the desired target managed
tags for one file (``tag_revisions_staged`` — one pending row per file); a *commit*
groups every currently-staged change under one ``commits`` row, applies each to disk via
the shared crash-safe loop in :mod:`tagmend.engine.commits`, and appends a real
``tag_revisions`` row, turning the staged row into history and deleting it.

This module supplies the **tags** :class:`TagDomain` (a concrete
:class:`tagmend.engine.commits.RevisionDomain`) and the conn-owning orchestrators
(:func:`stage_tags` / :func:`unstage_tags` / :func:`diff_tags` / :func:`commit_tags`).
The domain-neutral commit machinery (the ``commits`` table, the result dataclasses, and
``run_commit``) lives in :mod:`tagmend.engine.commits`.

Crash-safety follows PLAN.md §7 (the resume-free model): the staged table *is* the
journal. The baseline (version 0) is captured at **stage** time, so a crash mid-commit
followed by a rescan can never capture the wrong v0. A commit flips any lingering
``applying`` commit to ``interrupted`` (a crash remnant), then sweeps every still-staged
row under a **new** commit. Per file, the disk write happens first; then — in ONE DB
transaction — the revision is appended *and* the staged row deleted, so a revision never
exists without its staged row already gone. Anything still staged was not durably
committed; the next commit re-applies it idempotently.

Like :func:`tagmend.engine.versioning.revert` and
:func:`tagmend.engine.library.scan_library`, every public function here owns its own
connection and commit; the building blocks in :mod:`tagmend.engine.store` never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tagmend.engine import commits, db, schema, store, versioning
from tagmend.engine.tags import MANAGED_TAGS, read_tags, write_managed_tags
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.commits import CommitResult

logger = get_logger(__name__)

# A staged change originates from automated resolution (``auto``) or a manual/LLM
# decision (``manual``); ``revert`` is never staged. The chosen origin flows into the
# revision the commit appends.
_STAGED_ORIGINS = frozenset({"auto", "manual"})


def _utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class TagDiffView:
    """A staged tag change enriched with the current→target diff (``git diff --staged``)."""

    file_id: int
    folder: str
    filename: str
    is_missing: bool
    origin: str
    note: str | None
    staged_at: str
    current: dict[str, list[str]]
    target: dict[str, list[str]]
    diff: dict[str, dict[str, list[str]]]

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "file_id": self.file_id,
            "folder": self.folder,
            "filename": self.filename,
            "is_missing": self.is_missing,
            "origin": self.origin,
            "note": self.note,
            "staged_at": self.staged_at,
            "current": self.current,
            "target": self.target,
            "diff": self.diff,
        }


@dataclass(frozen=True, slots=True)
class TagDomain:
    """The tags :class:`tagmend.engine.commits.RevisionDomain` driven by ``run_commit``.

    Frozen and stateless: it reads each staged file's payload from
    :mod:`tagmend.engine.store` by ``file_id``. ``plan_order`` and ``post_commit_file``
    are identity/no-op — tag commits have no ordering or filesystem-cleanup concerns.
    """

    name: str = "tags"

    def list_staged_file_ids(self, conn: sqlite3.Connection) -> list[int]:
        """Return every staged file id, in file_id order."""
        return [s.file_id for s in store.list_staged_tags(conn)]

    def list_staged_file_ids_under(self, conn: sqlite3.Connection, root: Path) -> list[int]:
        """Return staged file ids whose file lives at *root* or nested under it."""
        return [s.file_id for s in store.list_staged_tags_under(conn, root)]

    def plan_order(self, conn: sqlite3.Connection, file_ids: list[int]) -> list[int]:  # noqa: ARG002
        """Tags have no move ordering: iterate in the given order."""
        return file_ids

    def resolve_path(self, conn: sqlite3.Connection, file_id: int) -> Path | None:
        """Return the on-disk path for *file_id*, or ``None`` if unknown/missing."""
        file_row = store.get_file_by_id(conn, file_id)
        if file_row is None or file_row.is_missing:
            return None
        return Path(file_row.folder) / file_row.filename

    def apply_to_disk(
        self,
        conn: sqlite3.Connection,
        file_id: int,
        path: Path,
        *,
        commit_id: int,
        now: str,
    ) -> int | None:
        """Write the staged tags to disk, then append the revision + delete the staged row.

        The disk write happens *before* any DB append, so a write failure aborts this
        file's transaction with the staged row intact for a later retry. The revision
        append and the staged-row delete are left in the open transaction (``run_commit``
        commits them together) — the invariant that prevents double-applying.
        """
        staged = store.get_staged_tag(conn, file_id)
        if staged is None:  # pragma: no cover - defensive; file_id came from the staged list
            message = f"staged row vanished for file_id={file_id}"
            raise RuntimeError(message)

        # Baseline (version 0) is captured at stage time; this is a defensive no-op.
        versioning.ensure_baseline(
            conn,
            file_id,
            managed_tags=store.get_tags(conn, file_id),
            now=now,
        )

        # Disk first, before any DB append.
        write_managed_tags(path, staged.managed_tags)

        # Refresh the live snapshot, append the revision, delete the staged row.
        fresh = read_tags(path).tags
        store.replace_tags(conn, file_id, fresh, now)
        version = versioning.append_revision(
            conn,
            file_id,
            managed_tags=fresh,
            origin=staged.origin,
            now=now,
            note=staged.note,
            commit_id=commit_id,
        )
        store.delete_staged_tag(conn, file_id)
        return version

    def flag_and_drop_missing(self, conn: sqlite3.Connection, file_id: int) -> None:
        """Flag the file missing and drop its staged row (it vanished from disk)."""
        store.flag_missing(conn, file_id, _utc_now())
        store.delete_staged_tag(conn, file_id)

    def post_commit_file(self, conn: sqlite3.Connection, file_id: int) -> None:
        """Tags have no per-file filesystem follow-up."""


def stage_tags(
    settings: Settings,
    *,
    file_id: int,
    managed_tags: dict[str, list[str]],
    origin: str = "manual",
    note: str | None = None,
) -> None:
    """Record the desired target managed tags for *file_id* (replacing any pending one).

    Validates *origin* (``auto``/``manual``), that every key is a managed tag, and that
    the file is known and not flagged missing — failing before any row is written. Also
    captures the version-0 baseline now (from the current snapshot) if the file has none,
    so a later crash-then-rescan can never record the wrong original. Nothing on disk
    changes and no further history is recorded until :func:`commit_tags`. Owns its
    transaction.

    **No accidental deletion (P0).** *managed_tags* is merged *onto* the file's current
    managed subset, so an omitted managed key means "leave it alone", not "delete it":
    staging ``{"genre": [...]}`` on a file rich in title/album/track/MB-id fields cannot
    wipe them at commit time (``commit_tags`` writes the staged target verbatim, and
    :func:`tagmend.engine.tags.write_managed_tags` deletes every managed key *absent* from
    that target). An explicit empty list still deletes a field, and the resolve flows —
    which already stage ``managed_subset(current) | {field}`` — are unaffected.
    """
    # Input / validation (cheap checks first, before opening the ledger).
    if origin not in _STAGED_ORIGINS:
        message = f"invalid staged origin: {origin!r} (expected auto|manual)"
        raise ValueError(message)
    unmanaged = sorted(set(managed_tags) - MANAGED_TAGS)
    if unmanaged:
        message = f"cannot stage non-managed tag(s): {', '.join(unmanaged)}"
        raise ValueError(message)

    # Process
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        file_row = store.get_file_by_id(connection, file_id)
        if file_row is None:
            message = f"unknown file_id={file_id}"
            raise ValueError(message)
        if file_row.is_missing:
            message = f"cannot stage a missing file (file_id={file_id})"
            raise ValueError(message)

        current = store.get_tags(connection, file_id)

        # Capture v0 now (resume-free model): freeze the true original before any commit.
        if store.max_version(connection, file_id) is None:
            versioning.ensure_baseline(
                connection,
                file_id,
                managed_tags=current,
                now=_utc_now(),
            )

        # No accidental deletion (P0): merge onto the current managed subset so omitted
        # managed keys are preserved through the commit's delete-on-absent write. The
        # caller's values win; an explicit empty list still deletes a field.
        target = versioning.managed_subset(current)
        target.update(managed_tags)

        store.upsert_staged_tag(
            connection,
            file_id=file_id,
            managed_tags=target,
            origin=origin,
            now=_utc_now(),
            note=note,
        )
        connection.commit()
    finally:
        connection.close()

    logger.info("staged tags for file_id=%d (origin=%s)", file_id, origin)


def unstage_tags(settings: Settings, *, file_id: int) -> bool:
    """Drop the pending change for *file_id*. Returns ``True`` if a row was removed.

    A baseline captured at stage time stays (history is proportional to staged intent);
    it is harmless and never re-applied.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        removed = store.get_staged_tag(connection, file_id) is not None
        if removed:
            store.delete_staged_tag(connection, file_id)
            connection.commit()
    finally:
        connection.close()
    return removed


def diff_tags(settings: Settings, *, root: Path | None = None) -> list[TagDiffView]:
    """Return staged-but-uncommitted tag changes enriched with the current→target diff.

    This is ``git diff --staged`` (staged-vs-snapshot), **not** working-tree: ``current``
    is the last committed/scanned managed-tag snapshot for the file and may lag the actual
    disk state after an interrupted commit. ``target`` is the staged managed tags and
    ``diff`` is :func:`tagmend.engine.versioning.compute_diff` between them (a no-op stage
    yields ``diff == {}`` but the row still appears). Optionally limited to files under
    *root*. Owns its transaction (read-only).
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        staged_rows = (
            store.list_staged_tags(connection)
            if root is None
            else store.list_staged_tags_under(connection, root)
        )
        views: list[TagDiffView] = []
        for staged in staged_rows:
            file_row = store.get_file_by_id(connection, staged.file_id)
            if file_row is None:  # pragma: no cover - staged FK guarantees the file row
                continue
            current = versioning.managed_subset(store.get_tags(connection, staged.file_id))
            target = staged.managed_tags
            views.append(
                TagDiffView(
                    file_id=staged.file_id,
                    folder=file_row.folder,
                    filename=file_row.filename,
                    is_missing=file_row.is_missing,
                    origin=staged.origin,
                    note=staged.note,
                    staged_at=staged.staged_at,
                    current=current,
                    target=target,
                    diff=versioning.compute_diff(current, target),
                ),
            )
        return views
    finally:
        connection.close()


def commit_tags(
    settings: Settings,
    *,
    message: str | None = None,
    origin: str = "manual",
    root: Path | None = None,
) -> CommitResult:
    """Apply every currently-staged tag change as one revertible commit; return a summary.

    First flips any lingering ``applying`` commit to ``interrupted`` (crash recovery in
    the resume-free model), then sweeps every still-staged row (optionally limited to
    *root*) under a fresh commit and applies them file by file via
    :func:`tagmend.engine.commits.run_commit`. A file gone from disk is flagged missing,
    its staged row dropped, and reported in ``missing_files`` — the rest of the commit
    still completes. ``commit_id`` is ``None`` when nothing was staged. Owns its
    transaction.
    """
    if origin not in _STAGED_ORIGINS:
        message_text = f"invalid commit origin: {origin!r} (expected auto|manual)"
        raise ValueError(message_text)

    domain = TagDomain()
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)

        # Recovery first: any pre-existing 'applying' commit is a crash remnant.
        commits.mark_interrupted(connection)
        connection.commit()

        file_ids = (
            domain.list_staged_file_ids_under(connection, root)
            if root is not None
            else domain.list_staged_file_ids(connection)
        )
        if not file_ids:
            connection.commit()
            return commits._summarize(commit_id=None, applied=[])  # noqa: SLF001

        now = _utc_now()
        commit_id = commits.create_commit(connection, origin=origin, message=message, now=now)
        connection.commit()  # commit row durable before any per-file work

        applied = commits.run_commit(
            connection,
            domain,
            commit_id=commit_id,
            file_ids=file_ids,
        )

        commits.set_commit_status(connection, commit_id, "applied")
        connection.commit()
    finally:
        connection.close()

    result = commits._summarize(commit_id=commit_id, applied=applied)  # noqa: SLF001
    logger.info(
        "commit %d: committed=%d noop=%d missing=%d",
        commit_id,
        result.committed,
        result.noop,
        result.missing,
    )
    return result
