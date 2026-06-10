"""Library scan orchestration + stats (M1).

The single entry point both frontends call: walk the configured (or supplied) music
folder, reconcile each file against the ``files`` snapshot, read & store tags as the
chosen :class:`ScanMode` dictates, and flag anything that has disappeared from disk.
This is the only module here that owns transaction/commit policy and stitches together
:mod:`scan`, :mod:`tags`, :mod:`store`, :mod:`schema`, and :mod:`db`.

Scan modes:

* ``incremental`` (default) — read tags only when the size/mtime signature changed or
  the file has never had its tags read.
* ``full`` — re-read tags for every file regardless of signature.
* ``presence`` — only reconcile existence (added/missing/restored); never read tags.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import mutagen

from tagmend.engine import db, scan, schema, store, versioning
from tagmend.engine.tags import read_tags
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from tagmend.config import Settings

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FileView:
    """One tracked file plus its current managed tags, for the discovery tools."""

    file_id: int
    folder: str
    filename: str
    ext: str
    is_missing: bool
    managed_tags: dict[str, list[str]]
    genre_status: str = "pending"
    genre_source_artist: str | None = None  # identity a no_match/manual was recorded against
    genre_source_album: str | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "file_id": self.file_id,
            "folder": self.folder,
            "filename": self.filename,
            "ext": self.ext,
            "is_missing": self.is_missing,
            "managed_tags": self.managed_tags,
            "genre_status": self.genre_status,
            "genre_source_artist": self.genre_source_artist,
            "genre_source_album": self.genre_source_album,
        }


def _to_view(conn: sqlite3.Connection, row: store.FileRow) -> FileView:
    """Build a :class:`FileView` from a file row, reading its managed-tag subset.

    Also resolves the file's genre workflow status; for a stored ``no_match``/``manual``
    decision, the source identity it was recorded against rides along so a reviewer can
    compare it with the current ``managed_tags`` and judge staleness.
    """
    genre_status = store.derived_genre_status(conn, row.id)
    decision = store.get_genre_status(conn, row.id)
    has_stored_status = decision is not None and genre_status == decision.status
    return FileView(
        file_id=row.id,
        folder=row.folder,
        filename=row.filename,
        ext=row.ext,
        is_missing=row.is_missing,
        managed_tags=versioning.managed_subset(store.get_tags(conn, row.id)),
        genre_status=genre_status,
        genre_source_artist=decision.source_artist if has_stored_status and decision else None,
        genre_source_album=decision.source_album if has_stored_status and decision else None,
    )


def list_files(
    settings: Settings,
    *,
    root: Path | None = None,
    limit: int | None = None,
    genre_status: str | None = None,
) -> list[FileView]:
    """Return tracked files (id order) with their managed tags, for discovery.

    Optionally limited to files under *root*, filtered to one genre workflow status
    (``pending`` | ``no_match`` | ``manual`` | ``staged`` | ``done``), and/or capped at
    *limit* rows. ``genre_status="no_match"`` is the "fix by hand" worklist. Without a
    filter the cap is applied before reading tags, so a large library stays cheap to
    browse; with a filter, all candidate rows are examined and the cap applies to the
    *matching* files. Raises :class:`ValueError` for an unknown status. Read-only.
    """
    if genre_status is not None and genre_status not in store.GENRE_WORKFLOW_STATUSES:
        message = (
            f"unknown genre_status: {genre_status!r} "
            f"(expected one of {sorted(store.GENRE_WORKFLOW_STATUSES)})"
        )
        raise ValueError(message)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = (
            store.tracked_files_under(connection, root)
            if root is not None
            else store.list_files(connection, limit=limit if genre_status is None else None)
        )

        if genre_status is None:
            if root is not None and limit is not None:
                rows = rows[:limit]
            return [_to_view(connection, row) for row in rows]

        # Status filter: the cap counts MATCHING files, so examine rows until it fills.
        views: list[FileView] = []
        for row in rows:
            if store.derived_genre_status(connection, row.id) != genre_status:
                continue
            views.append(_to_view(connection, row))
            if limit is not None and len(views) >= limit:
                break
        return views
    finally:
        connection.close()


def get_file_view(settings: Settings, file_id: int) -> FileView | None:
    """Return one tracked file with its managed tags, or ``None`` if unknown. Read-only."""
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        row = store.get_file_by_id(connection, file_id)
        return None if row is None else _to_view(connection, row)
    finally:
        connection.close()


class ScanMode(StrEnum):
    """How aggressively a scan re-reads tags from disk."""

    INCREMENTAL = "incremental"
    FULL = "full"
    PRESENCE = "presence"


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Immutable summary of one scan run."""

    total_seen: int
    added: int
    updated: int
    unchanged: int
    tags_read: int
    missing_flagged: int
    restored: int
    errors: int

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "total_seen": self.total_seen,
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "tags_read": self.tags_read,
            "missing_flagged": self.missing_flagged,
            "restored": self.restored,
            "errors": self.errors,
        }


@dataclass(slots=True)
class _Counters:
    """Mutable tally accumulated during a scan, frozen into a ScanResult at the end."""

    total_seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    tags_read: int = 0
    missing_flagged: int = 0
    restored: int = 0
    errors: int = 0
    seen_ids: set[int] = field(default_factory=set)

    def to_result(self) -> ScanResult:
        """Snapshot the counters into the public, frozen result."""
        return ScanResult(
            total_seen=self.total_seen,
            added=self.added,
            updated=self.updated,
            unchanged=self.unchanged,
            tags_read=self.tags_read,
            missing_flagged=self.missing_flagged,
            restored=self.restored,
            errors=self.errors,
        )


def _utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    return datetime.now(UTC).isoformat()


def scan_library(
    settings: Settings,
    *,
    path: Path | None = None,
    mode: ScanMode = ScanMode.INCREMENTAL,
) -> ScanResult:
    """Scan *path* (or the configured ``music_path``) into the snapshot.

    Raises :class:`ValueError` when no music path is configured or the path is not an
    existing directory.
    """
    # Input / validation
    root = path or settings.music_path
    if root is None:
        message = "music_path not configured — run `tagmend config-set music_path <dir>`"
        raise ValueError(message)
    if not root.exists():
        message = f"music path does not exist: {root}"
        raise ValueError(message)
    if not root.is_dir():
        message = f"music path is not a directory: {root}"
        raise ValueError(message)

    # Process
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        counters = _Counters()
        for audio_path in scan.iter_audio_files(root):
            _process_file(connection, audio_path, mode, counters)
        _reconcile_missing(connection, root, counters)
        connection.commit()
    finally:
        connection.close()

    # Output
    result = counters.to_result()
    logger.info(
        "scan complete: mode=%s seen=%d added=%d updated=%d unchanged=%d "
        "tags_read=%d restored=%d missing=%d errors=%d",
        mode.value,
        result.total_seen,
        result.added,
        result.updated,
        result.unchanged,
        result.tags_read,
        result.restored,
        result.missing_flagged,
        result.errors,
    )
    return result


def _process_file(
    conn: sqlite3.Connection,
    path: Path,
    mode: ScanMode,
    counters: _Counters,
) -> None:
    """Reconcile a single on-disk file against the snapshot, updating *counters*."""
    counters.total_seen += 1
    folder = str(path.parent)
    filename = path.name
    ext = path.suffix.lower()

    try:
        stat_result = path.stat()
    except OSError:
        logger.warning("could not stat %s; skipping", path)
        counters.errors += 1
        return

    size_bytes = stat_result.st_size
    mtime_ns = stat_result.st_mtime_ns
    existing = store.get_file(conn, folder, filename)

    if existing is None:
        _process_new_file(conn, path, mode, counters, folder, filename, ext, size_bytes, mtime_ns)
    else:
        _process_existing_file(conn, path, mode, counters, existing, size_bytes, mtime_ns)


def _process_new_file(  # noqa: PLR0913 - cohesive insert payload, all required
    conn: sqlite3.Connection,
    path: Path,
    mode: ScanMode,
    counters: _Counters,
    folder: str,
    filename: str,
    ext: str,
    size_bytes: int,
    mtime_ns: int,
) -> None:
    """Insert a never-seen file and, unless in PRESENCE mode, read its tags."""
    now = _utc_now()
    file_id = store.insert_file(
        conn,
        folder=folder,
        filename=filename,
        ext=ext,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        now=now,
    )
    counters.added += 1
    counters.seen_ids.add(file_id)

    if mode is ScanMode.PRESENCE:
        return
    _try_read_and_store(conn, path, file_id, counters, current=None)


def _process_existing_file(  # noqa: PLR0913 - cohesive reconcile inputs, all required
    conn: sqlite3.Connection,
    path: Path,
    mode: ScanMode,
    counters: _Counters,
    existing: store.FileRow,
    size_bytes: int,
    mtime_ns: int,
) -> None:
    """Reconcile a previously-seen file: restore, update signature, maybe re-read tags."""
    counters.seen_ids.add(existing.id)
    now = _utc_now()

    if existing.is_missing:
        store.clear_missing(conn, existing.id, now)
        counters.restored += 1

    sig_changed = existing.size_bytes != size_bytes or existing.mtime_ns != mtime_ns
    if sig_changed:
        store.update_signature(conn, existing.id, size_bytes=size_bytes, mtime_ns=mtime_ns, now=now)
        counters.updated += 1
    else:
        counters.unchanged += 1

    tags_unread = existing.tags_updated_at is None
    if not _should_read_tags(mode, sig_changed=sig_changed, tags_unread=tags_unread):
        return
    current = store.get_tags(conn, existing.id)
    _try_read_and_store(conn, path, existing.id, counters, current=current)


def _should_read_tags(mode: ScanMode, *, sig_changed: bool, tags_unread: bool) -> bool:
    """Decide whether tags should be (re-)read for an existing file."""
    if mode is ScanMode.PRESENCE:
        return False
    if mode is ScanMode.FULL:
        return True
    return sig_changed or tags_unread


def _try_read_and_store(
    conn: sqlite3.Connection,
    path: Path,
    file_id: int,
    counters: _Counters,
    *,
    current: dict[str, list[str]] | None,
) -> None:
    """Read tags from disk and persist them only if they actually changed.

    *current* is the already-stored tag map for an existing file (to avoid a no-op
    write that would dishonestly bump ``tags_updated_at``), or ``None`` for a brand
    new file that has no stored tags yet.
    """
    try:
        new_tags = read_tags(path).tags
    except (mutagen.MutagenError, OSError) as exc:  # type: ignore[attr-defined]
        logger.warning("could not read tags from %s: %s", path, exc)
        counters.errors += 1
        return

    if new_tags == current:
        return
    store.replace_tags(conn, file_id, new_tags, _utc_now())
    counters.tags_read += 1


def _reconcile_missing(conn: sqlite3.Connection, root: Path, counters: _Counters) -> None:
    """Flag tracked files under *root* that were not seen on this pass."""
    now = _utc_now()
    for row in store.tracked_files_under(conn, root):
        if row.id in counters.seen_ids or row.is_missing:
            continue
        store.flag_missing(conn, row.id, now)
        counters.missing_flagged += 1


def library_stats(settings: Settings) -> dict[str, object]:
    """Return library-wide counts from the snapshot."""
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        return store.compute_stats(connection)
    finally:
        connection.close()
