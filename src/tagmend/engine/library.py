"""Library scan orchestration + stats (M1).

The single entry point both frontends call: walk the configured (or supplied) music
folder, reconcile each file against the ``files`` snapshot, read & store tags as the
chosen :class:`ScanMode` dictates, and flag anything that has disappeared from disk.
This is the only module here that owns transaction/commit policy and stitches together
:mod:`scan`, :mod:`tags`, :mod:`store`, :mod:`schema`, and :mod:`db`.

Scan modes:

* ``incremental`` (default) — read tags only when the size/mtime signature changed, the
  file has never had its tags read, or an older tag reader wrote the stored row.
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
from tagmend.engine.tags import TAG_READER_VERSION, read_tags
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
    artist_status: str = "pending"
    artist_source_artist: str | None = None  # values a manual exclusion was recorded against
    artist_source_albumartist: str | None = None
    year_status: str = "pending"
    year_source_artist: str | None = None  # identity a no_match/manual was recorded against
    year_source_album: str | None = None
    mismatch_status: str = "pending"
    mismatch_source_field: str | None = None  # which tag a disposition was recorded against
    mismatch_source_value: str | None = None  # that tag's value at decision time

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
            "artist_status": self.artist_status,
            "artist_source_artist": self.artist_source_artist,
            "artist_source_albumartist": self.artist_source_albumartist,
            "year_status": self.year_status,
            "year_source_artist": self.year_source_artist,
            "year_source_album": self.year_source_album,
            "mismatch_status": self.mismatch_status,
            "mismatch_source_field": self.mismatch_source_field,
            "mismatch_source_value": self.mismatch_source_value,
        }


def _to_view(conn: sqlite3.Connection, row: store.FileRow) -> FileView:
    """Build a :class:`FileView` from a file row, reading its managed-tag subset.

    Also resolves the file's genre and artist workflow statuses (each FIELD-AWARE on its
    own tag). For a stored ``no_match``/``manual`` decision the source values it was
    recorded against ride along so a reviewer can compare them with the current
    ``managed_tags`` and judge staleness.
    """
    genre_status = store.derived_genre_status(conn, row.id)
    genre_decision = store.get_genre_status(conn, row.id)
    has_stored_genre = genre_decision is not None and genre_status == genre_decision.status

    artist_status = store.derived_artist_status(conn, row.id)
    artist_decision = store.get_artist_status(conn, row.id)
    has_stored_artist = artist_decision is not None and artist_status == artist_decision.status

    year_status = store.derived_year_status(conn, row.id)
    year_decision = store.get_year_status(conn, row.id)
    has_stored_year = year_decision is not None and year_status == year_decision.status

    mismatch_status = store.derived_mismatch_status(conn, row.id)
    mismatch_decision = store.get_mismatch_status(conn, row.id)
    has_stored_mismatch = (
        mismatch_decision is not None and mismatch_status == mismatch_decision.status
    )
    return FileView(
        file_id=row.id,
        folder=row.folder,
        filename=row.filename,
        ext=row.ext,
        is_missing=row.is_missing,
        managed_tags=versioning.managed_subset(store.get_tags(conn, row.id)),
        genre_status=genre_status,
        genre_source_artist=genre_decision.source_artist
        if has_stored_genre and genre_decision
        else None,
        genre_source_album=genre_decision.source_album
        if has_stored_genre and genre_decision
        else None,
        artist_status=artist_status,
        artist_source_artist=artist_decision.source_artist
        if has_stored_artist and artist_decision
        else None,
        artist_source_albumartist=artist_decision.source_albumartist
        if has_stored_artist and artist_decision
        else None,
        year_status=year_status,
        year_source_artist=year_decision.source_artist
        if has_stored_year and year_decision
        else None,
        year_source_album=year_decision.source_album if has_stored_year and year_decision else None,
        mismatch_status=mismatch_status,
        mismatch_source_field=mismatch_decision.source_field
        if has_stored_mismatch and mismatch_decision
        else None,
        mismatch_source_value=mismatch_decision.source_value
        if has_stored_mismatch and mismatch_decision
        else None,
    )


def _row_matches_status(  # noqa: PLR0913 - cohesive keyword-only status filters
    conn: sqlite3.Connection,
    file_id: int,
    *,
    genre_status: str | None,
    artist_status: str | None,
    year_status: str | None,
    mismatch_status: str | None,
) -> bool:
    """Return whether *file_id* satisfies every requested workflow-status filter.

    Each non-``None`` filter must match the file's derived status on that axis (the axes are
    independent and field-aware); a ``None`` filter is ignored.
    """
    if genre_status is not None and store.derived_genre_status(conn, file_id) != genre_status:
        return False
    if artist_status is not None and store.derived_artist_status(conn, file_id) != artist_status:
        return False
    if year_status is not None and store.derived_year_status(conn, file_id) != year_status:
        return False
    return not (
        mismatch_status is not None
        and store.derived_mismatch_status(conn, file_id) != mismatch_status
    )


def list_files(  # noqa: PLR0913 - cohesive keyword-only discovery filters
    settings: Settings,
    *,
    root: Path | None = None,
    limit: int | None = None,
    genre_status: str | None = None,
    artist_status: str | None = None,
    year_status: str | None = None,
    mismatch_status: str | None = None,
) -> list[FileView]:
    """Return tracked files (id order) with their managed tags, for discovery.

    Optionally limited to files under *root*, filtered to one genre workflow status
    (``pending`` | ``no_identity`` | ``no_match`` | ``manual`` | ``staged`` | ``done``), one
    artist workflow status (``pending`` | ``no_identity`` | ``manual`` | ``staged`` |
    ``done``), one year workflow status (``pending`` | ``no_identity`` | ``no_match`` |
    ``manual`` | ``staged`` | ``done``), one mismatch disposition
    (``pending`` | ``legit_ignore`` | ``misfiled_deferred``), and/or capped at *limit* rows.
    ``genre_status="no_match"`` is the "fix by hand" worklist; ``no_identity`` lists the files
    that carry neither ``artist`` nor ``albumartist`` (every resolver skips them). With NO
    status filter the cap is applied before reading tags, so a large library stays cheap to
    browse; with any filter, all candidate rows are examined, ALL filters are applied, and the
    cap counts the *matching* files. Raises :class:`ValueError` for an unknown status. Read-only.
    """
    if genre_status is not None and genre_status not in store.GENRE_WORKFLOW_STATUSES:
        message = (
            f"unknown genre_status: {genre_status!r} "
            f"(expected one of {sorted(store.GENRE_WORKFLOW_STATUSES)})"
        )
        raise ValueError(message)
    if artist_status is not None and artist_status not in store.ARTIST_WORKFLOW_STATUSES:
        message = (
            f"unknown artist_status: {artist_status!r} "
            f"(expected one of {sorted(store.ARTIST_WORKFLOW_STATUSES)})"
        )
        raise ValueError(message)
    if year_status is not None and year_status not in store.YEAR_WORKFLOW_STATUSES:
        message = (
            f"unknown year_status: {year_status!r} "
            f"(expected one of {sorted(store.YEAR_WORKFLOW_STATUSES)})"
        )
        raise ValueError(message)
    if mismatch_status is not None and mismatch_status not in store.MISMATCH_WORKFLOW_STATUSES:
        message = (
            f"unknown mismatch_status: {mismatch_status!r} "
            f"(expected one of {sorted(store.MISMATCH_WORKFLOW_STATUSES)})"
        )
        raise ValueError(message)

    filtered = (
        genre_status is not None
        or artist_status is not None
        or year_status is not None
        or mismatch_status is not None
    )

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = (
            store.tracked_files_under(connection, root)
            if root is not None
            else store.list_files(connection, limit=None if filtered else limit)
        )

        if not filtered:
            if root is not None and limit is not None:
                rows = rows[:limit]
            return [_to_view(connection, row) for row in rows]

        # Status filter(s): the cap counts MATCHING files, so examine rows until it fills.
        # When several are set, a file must satisfy ALL to match.
        views: list[FileView] = []
        for row in rows:
            if not _row_matches_status(
                connection,
                row.id,
                genre_status=genre_status,
                artist_status=artist_status,
                year_status=year_status,
                mismatch_status=mismatch_status,
            ):
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
    reader_stale = existing.reader_version < TAG_READER_VERSION
    if not _should_read_tags(
        mode,
        sig_changed=sig_changed,
        tags_unread=tags_unread,
        reader_stale=reader_stale,
    ):
        return
    current = store.get_tags(conn, existing.id)
    _try_read_and_store(conn, path, existing.id, counters, current=current)


def _should_read_tags(
    mode: ScanMode,
    *,
    sig_changed: bool,
    tags_unread: bool,
    reader_stale: bool,
) -> bool:
    """Decide whether tags should be (re-)read for an existing file.

    *reader_stale* is the third incremental trigger: an unchanged file whose row an older
    tag reader wrote reads correct-looking but stale, and no signature change will ever
    refresh it. ``presence`` still reads nothing, stale reader or not.
    """
    if mode is ScanMode.PRESENCE:
        return False
    if mode is ScanMode.FULL:
        return True
    return sig_changed or tags_unread or reader_stale


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

    # Stamped before the identical-tags early return below: an unchanged re-read still
    # refreshed the row with the current reader, and stamping only where replace_tags runs
    # would leave the ~99% that match stale and re-read on every incremental scan.
    store.stamp_reader_version(conn, file_id)
    if new_tags == current:
        return
    store.replace_tags(conn, file_id, new_tags, _utc_now())
    # tags_read counts files whose tags were re-read AND actually differed from the
    # stored snapshot (i.e. re-persisted this run); an identical re-read is an honest
    # no-op and is not tallied (see test_full_mode_honest_noop_then_reread). This is
    # distinct from `updated`, which counts size/mtime signature changes.
    counters.tags_read += 1


def _reconcile_missing(conn: sqlite3.Connection, root: Path, counters: _Counters) -> None:
    """Flag tracked files under *root* that were not seen on this pass."""
    now = _utc_now()
    for row in store.tracked_files_under(conn, root):
        if row.id in counters.seen_ids or row.is_missing:
            continue
        store.flag_missing(conn, row.id, now)
        counters.missing_flagged += 1


def get_library_stats(settings: Settings) -> dict[str, object]:
    """Return library-wide counts from the snapshot."""
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        return store.compute_stats(connection)
    finally:
        connection.close()
