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

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import mutagen

from tagmend.engine import axis, commits, db, schema, store, versioning
from tagmend.engine.tags import MANAGED_TAGS, read_tags, write_managed_tags
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from tagmend.config import Settings
    from tagmend.engine.commits import CommitResult

logger = get_logger(__name__)

# A staged change originates from automated resolution (``auto``) or a manual/LLM
# decision (``manual``); ``revert`` is never staged. The chosen origin flows into the
# revision the commit appends.
_STAGED_ORIGINS = frozenset({"auto", "manual"})

# A name and the ids that name the same thing. Rewriting one member of a group while leaving
# another in place is how a file ends up reading "Alice in Chains" with Linkin Park's MBID
# still attached — 118 files across 8 folders went that way in the 2026-08-25 run, because
# staging merges onto the file's current tags and silently keeps whatever the caller omitted.
# Reported on the diff for review; never blocked, never auto-changed.
_IDENTITY_GROUPS: Final[tuple[tuple[str, ...], ...]] = (
    ("artist", "musicbrainz_artistid"),
    ("albumartist", "musicbrainz_albumartistid"),
    ("album", "musicbrainz_albumid", "musicbrainz_releasegroupid"),
    ("title", "musicbrainz_trackid", "musicbrainz_releasetrackid"),
)


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
    stale_identity: list[dict[str, object]] = field(default_factory=list)

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
            "stale_identity": self.stale_identity,
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

        # Baseline (version 0) is captured at stage time; this is a defensive no-op. It reads
        # disk for the same reason stage time does — a baseline is what a revert restores, so
        # it can never come from the snapshot mirror, which may lag the file.
        versioning.ensure_baseline(
            conn,
            file_id,
            managed_tags=read_tags(path).tags,
            now=now,
        )

        # Disk first, before any DB append.
        write_managed_tags(path, staged.managed_tags)

        # Refresh the live snapshot, append the revision, delete the staged row.
        fresh = read_tags(path).tags
        store.replace_tags(conn, file_id, fresh, now)
        # Re-sync the files-row signature to the just-written bytes (same fields the
        # scanner stats), so the next incremental scan sees this file as unchanged
        # rather than spuriously re-flagging every committed file as updated.
        stat_result = path.stat()
        store.update_signature(
            conn,
            file_id,
            size_bytes=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            now=now,
        )
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


def _stage_one(  # noqa: PLR0913 - cohesive keyword-only per-file staging payload
    conn: sqlite3.Connection,
    *,
    file_id: int,
    managed_tags: dict[str, list[str]],
    origin: str,
    note: str | None,
    now: str,
) -> None:
    """Validate + stage one file's change on an OPEN connection (no commit). Never drifts.

    The shared per-file core of :func:`stage_tags` and :func:`stage_tags_batch`: rejects
    unmanaged keys, rejects an unknown or missing *file_id*, lazily captures the version-0
    baseline, merges *managed_tags* onto the file's current managed subset (P0 — omitted keys
    are preserved), and upserts the staged row. Raises :class:`ValueError` naming *file_id* on
    any invalid input; leaves the transaction for the caller to commit or roll back.
    """
    unmanaged = sorted(set(managed_tags) - MANAGED_TAGS)
    if unmanaged:
        message = f"cannot stage non-managed tag(s) for file_id={file_id}: {', '.join(unmanaged)}"
        raise ValueError(message)

    file_row = store.get_file_by_id(conn, file_id)
    if file_row is None:
        message = f"unknown file_id={file_id}"
        raise ValueError(message)
    if file_row.is_missing:
        message = f"cannot stage a missing file (file_id={file_id})"
        raise ValueError(message)

    # Disk, not the snapshot mirror. The mirror can lag the file (an older tag reader wrote
    # the row, or nothing rescanned after an upgrade), and BOTH things derived here are
    # delete-on-absent: the commit removes every managed key missing from the target, and the
    # baseline is what a revert restores. A stale mirror therefore destroyed a tag the caller
    # never mentioned, unrecoverably.
    path = Path(file_row.folder) / file_row.filename
    try:
        current = read_tags(path).tags
    except (mutagen.MutagenError, OSError) as exc:  # type: ignore[attr-defined]
        # Reject at the boundary rather than staging a target built from nothing: the file
        # vanished or turned unreadable since the scan that wrote its row.
        message = f"cannot read tags from disk for file_id={file_id} ({path}): {exc}"
        raise ValueError(message) from exc

    # Capture v0 now (resume-free model): freeze the true original before any commit.
    if store.max_version(conn, file_id) is None:
        versioning.ensure_baseline(conn, file_id, managed_tags=current, now=now)

    # No accidental deletion (P0): merge onto the current managed subset so omitted managed
    # keys are preserved through the commit's delete-on-absent write. The caller's values
    # win; an explicit empty list still deletes a field.
    target = versioning.managed_subset(current)
    target.update(managed_tags)

    store.upsert_staged_tag(
        conn,
        file_id=file_id,
        managed_tags=target,
        origin=origin,
        now=now,
        note=note,
    )


def stage_tags(
    settings: Settings,
    *,
    file_id: int,
    managed_tags: dict[str, list[str]],
    origin: str = "manual",
    note: str | None = None,
) -> None:
    """Record the desired target managed tags for *file_id* (replacing any pending one).

    Validates *origin* (``auto``/``manual``) then, via the shared :func:`_stage_one` core,
    that every key is a managed tag and the file is known and not flagged missing. Also
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
    if origin not in _STAGED_ORIGINS:
        message = f"invalid staged origin: {origin!r} (expected auto|manual)"
        raise ValueError(message)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        _stage_one(
            connection,
            file_id=file_id,
            managed_tags=managed_tags,
            origin=origin,
            note=note,
            now=_utc_now(),
        )
        connection.commit()
    finally:
        connection.close()

    logger.info("staged tags for file_id=%d (origin=%s)", file_id, origin)


_BATCH_ENTRY_WIDTH = 2


def _validate_batch_entries(entries: Sequence[object]) -> list[tuple[int, dict[str, list[str]]]]:
    """Narrow an untyped *entries* sequence to ``(file_id, managed_tags)`` pairs, or raise.

    The parameter is deliberately untyped: under mypy strict these checks would be
    unreachable against the declared pair type, yet an engine-side caller handing over the
    MCP-shaped ``{"file_id": .., "tags": ..}`` dict gets its two KEYS destructured instead,
    so the shape error surfaces as a nonsense ``duplicate file_id=file_id in batch``.
    Every message names the offending index.
    """
    validated: list[tuple[int, dict[str, list[str]]]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, tuple):
            message = f"entry {index}: expected a (file_id, tags) tuple, got {type(entry).__name__}"
            raise ValueError(message)  # noqa: TRY004 - batch rejections are uniformly ValueError
        if len(entry) != _BATCH_ENTRY_WIDTH:
            message = f"entry {index}: expected a (file_id, tags) tuple of 2, got {len(entry)}"
            raise ValueError(message)
        file_id, tags = entry
        if not isinstance(file_id, int) or isinstance(file_id, bool):
            message = f"entry {index}: file_id must be an integer, got {type(file_id).__name__}"
            raise ValueError(message)  # noqa: TRY004 - batch rejections are uniformly ValueError
        if not isinstance(tags, dict):
            message = (
                f"entry {index} (file_id={file_id}): tags must be a dict of "
                f"name -> list of values, got {type(tags).__name__}"
            )
            raise ValueError(message)  # noqa: TRY004 - batch rejections are uniformly ValueError
        for name, values in tags.items():
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                message = (
                    f"entry {index} (file_id={file_id}): tags[{name!r}] must be a list of strings"
                )
                raise ValueError(message)
        validated.append((file_id, cast("dict[str, list[str]]", tags)))
    return validated


def stage_tags_batch(
    settings: Settings,
    *,
    entries: Sequence[object],
    note: str | None = None,
) -> list[int]:
    """Stage N files' managed-tag changes in ONE connection / ONE transaction (all-or-nothing).

    *entries* is a sequence of ``(file_id, managed_tags)`` tuples. Every entry's SHAPE is
    checked first by :func:`_validate_batch_entries` (a :class:`ValueError` naming the index
    and what was wrong), then each is validated and staged through the SAME :func:`_stage_one`
    core as :func:`stage_tags` (unmanaged-key rejection, unknown/missing-file rejection, lazy
    v0 baseline, merge-onto-current-subset), so single and batch can never drift. The
    ``origin`` is hardcoded ``"manual"`` (this flow never auto-stages — no origin parameter is
    exposed). A malformed entry, a duplicate ``file_id`` in one batch, or any invalid entry
    raises :class:`ValueError` and NOTHING is staged (the shared transaction is rolled back on
    close). A later :func:`commit_tags` groups the whole batch into ONE revertible commit.
    Returns the staged file ids in input order.

    ``tracknumber``/``discnumber`` values are staged VERBATIM (callers supply the full
    ``"n/total"`` strings); this helper never parses or computes them.
    """
    validated = _validate_batch_entries(entries)

    seen: set[int] = set()
    for file_id, _ in validated:
        if file_id in seen:
            message = f"duplicate file_id={file_id} in batch"
            raise ValueError(message)
        seen.add(file_id)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        now = _utc_now()
        for file_id, managed_tags in validated:
            _stage_one(
                connection,
                file_id=file_id,
                managed_tags=managed_tags,
                origin="manual",
                note=note,
                now=now,
            )
        connection.commit()
    finally:
        connection.close()

    staged_ids = [file_id for file_id, _ in validated]
    logger.info("staged batch of %d file(s)", len(staged_ids))
    return staged_ids


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


def _current_managed(
    conn: sqlite3.Connection,
    file_row: store.FileRow,
) -> dict[str, list[str]]:
    """Return the file's current managed tags, from DISK when it can be read.

    The staged target is built from disk, so comparing it against the snapshot mirror would
    render a field the mirror merely lacks as an addition — a review surface inventing changes
    that will not happen. The mirror is the fallback for a file that is gone or unreadable,
    which is the only case where nothing better exists.
    """
    if not file_row.is_missing:
        try:
            return versioning.managed_subset(
                read_tags(Path(file_row.folder) / file_row.filename).tags,
            )
        except (mutagen.MutagenError, OSError):  # type: ignore[attr-defined]
            logger.warning("diff: unreadable file_id=%s, falling back to snapshot", file_row.id)
    return versioning.managed_subset(store.get_tags(conn, file_row.id))


def _stale_identity(
    diff: dict[str, dict[str, list[str]]],
    target: dict[str, list[str]],
) -> list[dict[str, object]]:
    """Report coupled identity fields this change rewrites one half of.

    A group's name and its MusicBrainz ids describe the same thing, so changing the name while
    keeping the old id leaves the file naming one entity and pointing at another.
    """
    stale: list[dict[str, object]] = []
    for group in _IDENTITY_GROUPS:
        changed = [field_name for field_name in group if field_name in diff]
        if not changed:
            continue
        for field_name in group:
            retained = target.get(field_name)
            if field_name not in diff and retained:
                stale.append(
                    {
                        "changed": changed[0],
                        "stale_field": field_name,
                        "stale_value": retained,
                    },
                )
    return stale


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
            current = _current_managed(connection, file_row)
            target = staged.managed_tags
            diff = versioning.compute_diff(current, target)
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
                    diff=diff,
                    stale_identity=_stale_identity(diff, target),
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


@dataclass(frozen=True, slots=True)
class ReopenResult:
    """Immutable summary of one :func:`reopen_axes` call, JSON-ready for the MCP tool."""

    commit_id: int
    files: int
    artist_status_cleared: int

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "commit_id": self.commit_id,
            "files": self.files,
            "artist_status_cleared": self.artist_status_cleared,
        }


def reopen_axes(settings: Settings, *, commit_id: int) -> ReopenResult:
    """Re-open the derived axes for every file a manual identity-fix commit changed (NN7).

    The explicit post-fix coherence step: after committing a manual identity fix (the
    mismatch-fix flow), the file's stale auto-resolved genre/year no longer describe its NEW
    identity, and any sticky ``file_artist_status`` was tied to the OLD name. For every DISTINCT
    file with a ``tag_revisions`` row in *commit_id* this, in ONE transaction:

    * voids the derived-axis fields (:data:`tagmend.engine.axis.GENRE_AXIS.fields` +
      :data:`~tagmend.engine.axis.YEAR_AXIS.fields`) via
      :func:`tagmend.engine.store.void_auto_changes`, so ``derived_genre_status`` /
      ``derived_year_status`` flip ``done`` → ``pending`` and the axes re-open (a LATER fresh
      auto commit reads ``done`` again — the Run-1 watermark semantics); and
    * deletes any :func:`tagmend.engine.store.delete_artist_status` row.

    Noop/missing files have no revision row in the commit and are correctly untouched. Raises
    :class:`ValueError` for an unknown *commit_id* or a commit with ``origin='auto'`` (voiding
    fresh auto work is a foot-gun); ``manual``/``revert`` commits are allowed. Returns the
    affected file count and how many artist rows were cleared. Owns its transaction; never
    touches the ``commits`` table or the append-only ``tag_revisions`` history.
    """
    void_fields = axis.GENRE_AXIS.fields + axis.YEAR_AXIS.fields

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)

        commit = commits.get_commit(connection, commit_id)
        if commit is None:
            message = f"unknown commit_id={commit_id}"
            raise ValueError(message)
        if commit.origin == "auto":
            message = (
                f"cannot reopen an auto commit (commit_id={commit_id}); "
                "reopen targets manual identity-fix commits"
            )
            raise ValueError(message)

        file_ids = sorted({r.file_id for r in store.revisions_for_commit(connection, commit_id)})
        artist_cleared = 0
        for fid in file_ids:
            store.void_auto_changes(connection, fid, void_fields)
            if store.get_artist_status(connection, fid) is not None:
                artist_cleared += 1
                store.delete_artist_status(connection, fid)
        connection.commit()
    finally:
        connection.close()

    logger.info(
        "reopen commit %d: %d file(s) re-opened, %d artist status row(s) cleared",
        commit_id,
        len(file_ids),
        artist_cleared,
    )
    return ReopenResult(
        commit_id=commit_id,
        files=len(file_ids),
        artist_status_cleared=artist_cleared,
    )
