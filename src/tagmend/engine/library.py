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

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

import mutagen

from tagmend.engine import axis, clock, db, mismatch, path_keys, scan, schema, store, versioning
from tagmend.engine.detector_core import FieldDict
from tagmend.engine.tags import TAG_READER_VERSION, read_tags
from tagmend.engine.validation import check_limit, require_choice
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from tagmend.config import Settings

logger = get_logger(__name__)

# The year axis manages one field, the one ``list_albums`` counts blanks of.
_YEAR_FIELD: Final = axis.YEAR_AXIS.fields[0]


@dataclass(frozen=True, slots=True)
class FileView(FieldDict):
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
    song_status: str = "pending"
    song_source_album_mbid: str | None = None  # release ids a done/manual was recorded against
    song_source_release_track_mbid: str | None = None
    mismatch_status: str = "pending"
    mismatch_source_value: dict[str, object] | None = None  # the decision's snapshot


def _axis_view(
    conn: sqlite3.Connection,
    tag_axis: axis.Axis,
    file_id: int,
) -> tuple[str, str | None, str | None]:
    """Return *file_id*'s derived status on *tag_axis* plus the identity a counting row holds.

    The identity rides along only when the derived status IS the stored row's status, so a
    stale row never shows as the reason for a status it no longer decides.
    """
    status = store.derived_status(conn, tag_axis, file_id)
    row = axis.get_outcome(conn, tag_axis, file_id)
    if row is None or row.status != status:
        return status, None, None
    return status, row.identity.primary, row.identity.secondary


def _mismatch_view(
    state: mismatch.MismatchState | None,
) -> tuple[str, dict[str, object] | None]:
    """Return a file's mismatch status plus the snapshot of the decision that sets it.

    A missing file has no reading (``None``) and shows ``pending``. The snapshot rides along
    only while the decision is the status the file reads.
    """
    if state is None:
        return "pending", None
    row = state.row
    if row is None or row.status != state.status:
        return state.status, None
    return state.status, row.snapshot()


def _to_view(
    conn: sqlite3.Connection,
    row: store.FileRow,
    mismatch_state: mismatch.MismatchState | None,
) -> FileView:
    """Build a :class:`FileView` from a file row, reading its managed-tag subset.

    Also resolves the file's genre, artist, year and song statuses, and takes its mismatch
    reading from *mismatch_state*. For a stored decision the source values it was recorded
    against ride along so a reviewer can compare them with the current ``managed_tags``.
    """
    genre_status, genre_artist, genre_album = _axis_view(conn, axis.GENRE_AXIS, row.id)
    artist_status, artist_artist, artist_albumartist = _axis_view(conn, axis.ARTIST_AXIS, row.id)
    year_status, year_artist, year_album = _axis_view(conn, axis.YEAR_AXIS, row.id)
    song_status, song_album_mbid, song_release_track_mbid = _axis_view(conn, axis.SONG_AXIS, row.id)
    mismatch_status, mismatch_source = _mismatch_view(mismatch_state)
    return FileView(
        file_id=row.id,
        folder=row.folder,
        filename=row.filename,
        ext=row.ext,
        is_missing=row.is_missing,
        managed_tags=versioning.managed_subset(store.get_tags(conn, row.id)),
        genre_status=genre_status,
        genre_source_artist=genre_artist,
        genre_source_album=genre_album,
        artist_status=artist_status,
        artist_source_artist=artist_artist,
        artist_source_albumartist=artist_albumartist,
        year_status=year_status,
        year_source_artist=year_artist,
        year_source_album=year_album,
        song_status=song_status,
        song_source_album_mbid=song_album_mbid,
        song_source_release_track_mbid=song_release_track_mbid,
        mismatch_status=mismatch_status,
        mismatch_source_value=mismatch_source,
    )


def _views(
    conn: sqlite3.Connection,
    settings: Settings,
    rows: list[store.FileRow],
    mismatch_states: Mapping[int, mismatch.MismatchState] | None = None,
) -> list[FileView]:
    """Build the views of *rows*, reading their mismatch states unless given them."""
    states = (
        mismatch.file_states(conn, settings, [row.id for row in rows])
        if mismatch_states is None
        else mismatch_states
    )
    return [_to_view(conn, row, states.get(row.id)) for row in rows]


def _row_matches_status(  # noqa: PLR0913 - cohesive keyword-only status filters
    conn: sqlite3.Connection,
    row: store.FileRow,
    *,
    genre_status: str | None,
    artist_status: str | None,
    year_status: str | None,
    song_status: str | None,
    mismatch_status: str | None,
    mismatch_states: Mapping[int, mismatch.MismatchState],
) -> bool:
    """Return whether *row* satisfies every requested workflow-status filter.

    Each non-``None`` filter must match the file's derived status on that axis (the axes are
    independent and field-aware), and a ``None`` filter is ignored. A missing file has no
    status on any axis, so any filter excludes it. *mismatch_states* holds the present files'
    mismatch readings.
    """
    tag_filters = (
        (axis.GENRE_AXIS, genre_status),
        (axis.ARTIST_AXIS, artist_status),
        (axis.YEAR_AXIS, year_status),
        (axis.SONG_AXIS, song_status),
    )
    for tag_axis, wanted in tag_filters:
        if wanted is None:
            continue
        if row.is_missing or store.derived_status(conn, tag_axis, row.id) != wanted:
            return False
    if mismatch_status is None:
        return True
    state = mismatch_states.get(row.id)
    return state is not None and state.status == mismatch_status


def list_files(  # noqa: PLR0913 - cohesive keyword-only discovery filters
    settings: Settings,
    *,
    path: Path | None = None,
    limit: int | None = None,
    genre_status: str | None = None,
    artist_status: str | None = None,
    year_status: str | None = None,
    song_status: str | None = None,
    mismatch_status: str | None = None,
) -> list[FileView]:
    """Return tracked files (id order) with their managed tags, for discovery.

    Optionally limited to files in the folder *path* or under it (resolved by
    :func:`tagmend.engine.path_keys.folder_arg_key`, so case and separators do not matter on
    Windows and a relative *path* resolves under ``music_path``), filtered to one genre, artist
    and/or year workflow status (``pending`` | ``no_identity`` | ``no_match`` | ``manual`` |
    ``staged`` | ``done``), one song workflow status (``pending`` | ``manual`` | ``staged`` |
    ``done``), one mismatch status (``pending`` | ``legit_ignore`` | ``misfiled_deferred``,
    read by :func:`tagmend.engine.mismatch.file_states`), and/or capped at *limit* rows.
    ``genre_status="no_match"`` is the "fix by hand" worklist. ``no_identity`` lists the files
    the axis has no identity for, which no resolver selects. No status filter returns a missing
    file. With NO status filter the cap is applied before reading tags, so a large library
    stays cheap to browse. With any filter, all candidate rows are examined, ALL filters are
    applied, and the cap counts the *matching* files. Raises :class:`ValueError` for an unknown
    status, a negative *limit* or a *path* outside ``music_path``. Read-only.
    """
    check_limit(limit)
    require_choice("genre_status", genre_status, store.GENRE_WORKFLOW_STATUSES)
    require_choice("artist_status", artist_status, store.ARTIST_WORKFLOW_STATUSES)
    require_choice("year_status", year_status, store.YEAR_WORKFLOW_STATUSES)
    require_choice("song_status", song_status, store.SONG_WORKFLOW_STATUSES)
    require_choice("mismatch_status", mismatch_status, store.MISMATCH_WORKFLOW_STATUSES)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)

    filtered = (
        genre_status is not None
        or artist_status is not None
        or year_status is not None
        or song_status is not None
        or mismatch_status is not None
    )

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = (
            store.tracked_files_under(connection, root_key)
            if root_key is not None
            else store.list_files(connection, limit=None if filtered else limit)
        )

        if not filtered:
            if root_key is not None and limit is not None:
                rows = rows[:limit]
            return _views(connection, settings, rows)

        # The mismatch filter reads every candidate in one comparator pass. Without it, only
        # the matched rows are read, below.
        states = (
            {}
            if mismatch_status is None
            else mismatch.file_states(connection, settings, [row.id for row in rows])
        )
        # Status filter(s): the cap counts MATCHING files, so examine rows until it fills.
        # When several are set, a file must satisfy ALL to match.
        matched: list[store.FileRow] = []
        for row in rows:
            if not _row_matches_status(
                connection,
                row,
                genre_status=genre_status,
                artist_status=artist_status,
                year_status=year_status,
                song_status=song_status,
                mismatch_status=mismatch_status,
                mismatch_states=states,
            ):
                continue
            matched.append(row)
            if limit is not None and len(matched) >= limit:
                break
        return _views(
            connection,
            settings,
            matched,
            None if mismatch_status is None else states,
        )
    finally:
        connection.close()


def get_file(settings: Settings, file_id: int) -> FileView | None:
    """Return one tracked file with its managed tags, or ``None`` if unknown. Read-only."""
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        row = store.get_file_by_id(connection, file_id)
        return None if row is None else _views(connection, settings, [row])[0]
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class ArtistRow(FieldDict):
    """One distinct ``artist`` tag value with its file count, for ``list_artists``."""

    artist: str
    file_count: int


def list_artists(settings: Settings, *, limit: int | None = None) -> list[ArtistRow]:
    """Return each distinct ``artist`` tag value with its file count (value order).

    A discovery aid for scoping ``resolve_genres`` by artist. Read-only. *limit* (when given)
    caps the number of rows returned, applied AFTER the value ordering so the cap is
    deterministic. Raises :class:`ValueError` for a negative *limit*.
    """
    check_limit(limit)
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = store.distinct_artists(connection)
    finally:
        connection.close()
    result = [ArtistRow(artist=value, file_count=count) for value, count in rows]
    if limit is not None:
        result = result[:limit]
    return result


@dataclass(frozen=True, slots=True)
class AlbumRow(FieldDict):
    """One distinct ``(albumartist-else-artist, album)`` group with its status, for listing."""

    artist: str | None
    album: str
    file_count: int
    year_status: str
    blank_originaldate: int


def list_albums(
    settings: Settings,
    *,
    year_status: str | None = None,
    actionable: bool = False,
    limit: int | None = None,
) -> list[AlbumRow]:
    """Return each distinct album group with its file count + a representative status.

    Groups present files by ``(albumartist-else-artist, album)`` (the album identity) and
    reports the derived year status of the group's first file plus ``blank_originaldate``,
    the count of the group's files whose ``originaldate`` tag is empty. Those are the files
    :func:`tagmend.engine.years.resolve_years` can fill, and ``> 0`` marks an actionable
    group. A discovery aid for scoping ``resolve_years``. Read-only.

    *year_status* (when given) keeps only groups whose derived status matches. *actionable*
    keeps only the actionable groups, those with ``blank_originaldate > 0``. The two
    compose, and both are applied AFTER ordering and BEFORE *limit*. *limit* (when given)
    caps the number of rows returned so a large library stays context-cheap. Raises
    :class:`ValueError` for a negative *limit*.
    """
    check_limit(limit)
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        groups: dict[tuple[str | None, str], list[int]] = {}
        blanks: dict[tuple[str | None, str], int] = {}
        for fid in store.present_file_ids(connection):
            tags = store.get_tags(connection, fid)
            identity = axis.lookup_identity(tags)
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
                year_status=store.derived_status(connection, axis.YEAR_AXIS, fids[0]),
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


class ScanMode(StrEnum):
    """How aggressively a scan re-reads tags from disk."""

    INCREMENTAL = "incremental"
    FULL = "full"
    PRESENCE = "presence"


@dataclass(frozen=True, slots=True)
class ScanResult(FieldDict):
    """Immutable summary of one scan run."""

    total_seen: int
    added: int
    updated: int
    unchanged: int
    tags_read: int
    missing_flagged: int
    restored: int
    errors: int
    respelled: int
    pending_commit: int


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
    respelled: int = 0
    pending_commit: int = 0
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
            respelled=self.respelled,
            pending_commit=self.pending_commit,
        )


def scan_library(
    settings: Settings,
    *,
    path: Path | None = None,
    mode: ScanMode = ScanMode.INCREMENTAL,
) -> ScanResult:
    """Scan *path* (or the configured ``music_path``) into the snapshot.

    A relative *path* resolves under ``music_path``. A *path* under ``music_path`` is walked as
    the configured ``music_path`` spelling plus each deeper folder's on-disk name, so every scan
    stores one spelling per file. A file found under a new spelling of a known path (a
    case-only rename) keeps its id and history and is counted ``respelled``.

    The scan guard keeps a staged move's file under its id. A disk path whose key equals a
    staged move's target is skipped and counted ``pending_commit``, and a file with a staged
    move is never flagged missing, so a move that landed before a crash adds no second row.

    Raises :class:`ValueError` when no music path is configured, the path is not an existing
    directory, the path lies outside ``music_path``, or a relative path has no ``music_path``
    to resolve under.
    """
    # Input / validation
    music_path = settings.music_path
    # Only an explicit path resolves against music_path. A relative music_path is itself
    # relative to the working directory, so joining it onto itself would name a missing folder.
    if path is not None:
        root = path_keys.resolve_folder_arg(settings, path)
    elif music_path is not None:
        root = music_path
    else:
        message = "music_path not configured. Run `tagmend config-set music_path <dir>`"
        raise ValueError(message)
    if not root.exists():
        message = f"music path does not exist: {root}"
        raise ValueError(message)
    if not root.is_dir():
        message = f"music path is not a directory: {root}"
        raise ValueError(message)
    if music_path is not None:
        root = _spell_under_music_path(root, music_path)

    # Process
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        counters = _Counters()
        guard = _scan_guard(connection, music_path)
        for audio_path in scan.iter_audio_files(root):
            _process_file(connection, audio_path, mode, counters, guard)
        _reconcile_missing(connection, root, counters, guard)
        connection.commit()
    finally:
        connection.close()

    # Output
    result = counters.to_result()
    logger.info(
        "scan complete: mode=%s seen=%d added=%d updated=%d unchanged=%d "
        "tags_read=%d restored=%d missing=%d errors=%d respelled=%d pending_commit=%d",
        mode.value,
        result.total_seen,
        result.added,
        result.updated,
        result.unchanged,
        result.tags_read,
        result.restored,
        result.missing_flagged,
        result.errors,
        result.respelled,
        result.pending_commit,
    )
    return result


@dataclass(frozen=True, slots=True)
class _ScanGuard:
    """The staged moves a scan must not disturb: their target keys and their file ids."""

    target_keys: frozenset[str]
    file_ids: frozenset[int]


def _scan_guard(conn: sqlite3.Connection, music_path: Path | None) -> _ScanGuard:
    """Return the scan guard of the moves staged now. Staged targets are relative to music_path."""
    staged = store.list_staged_paths(conn)
    if music_path is None:
        return _ScanGuard(target_keys=frozenset(), file_ids=frozenset(s.file_id for s in staged))
    return _ScanGuard(
        target_keys=frozenset(path_keys.path_key(music_path / s.to_path) for s in staged),
        file_ids=frozenset(s.file_id for s in staged),
    )


def _spell_under_music_path(root: Path, music_path: Path) -> Path:
    """Return *root* spelled as *music_path* plus each deeper folder's on-disk name.

    NTFS answers to any casing and the walk keeps the root's casing in every stored path, so a
    root typed another way would otherwise re-add each file under a second spelling. *root* is
    already known to lie within *music_path* (:func:`tagmend.engine.path_keys.resolve_folder_arg`).
    """
    music_depth = len(Path(os.path.normpath(music_path)).parts)
    spelled = music_path
    for part in Path(os.path.normpath(root)).parts[music_depth:]:
        spelled = spelled / _on_disk_name(spelled, part)
    return spelled


def _on_disk_name(parent: Path, name: str) -> str:
    """Return the entry of *parent* whose name has the same path key as *name*, else *name*."""
    wanted = path_keys.path_key(name)
    with os.scandir(parent) as entries:
        return next((e.name for e in entries if path_keys.path_key(e.name) == wanted), name)


def _process_file(
    conn: sqlite3.Connection,
    path: Path,
    mode: ScanMode,
    counters: _Counters,
    guard: _ScanGuard,
) -> None:
    """Reconcile a single on-disk file against the snapshot, updating *counters*."""
    counters.total_seen += 1
    folder = str(path.parent)
    filename = path.name
    ext = path.suffix.lower()
    # Runs before the key lookup and the respell. A landed move's target would otherwise be
    # added as a new file, or respell the source of a case-only rename.
    if path_keys.file_path_key(folder, filename) in guard.target_keys:
        counters.pending_commit += 1
        return

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
        return
    if (existing.folder, existing.filename) != (folder, filename):
        store.update_location(
            conn, existing.id, folder=folder, filename=filename, now=clock.utc_now()
        )
        counters.respelled += 1
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
    now = clock.utc_now()
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
    now = clock.utc_now()

    if existing.is_missing:
        store.clear_missing(conn, existing.id, now)
        counters.restored += 1

    sig_changed = existing.size_bytes != size_bytes or existing.mtime_ns != mtime_ns
    if sig_changed:
        store.update_signature(conn, existing.id, size_bytes=size_bytes, mtime_ns=mtime_ns, now=now)
        store.mark_reader_stale(conn, existing.id)
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
    store.replace_tags(conn, file_id, new_tags, clock.utc_now())
    # tags_read counts files whose tags were re-read AND actually differed from the
    # stored snapshot (i.e. re-persisted this run); an identical re-read is an honest
    # no-op and is not tallied (see test_full_mode_honest_noop_then_reread). This is
    # distinct from `updated`, which counts size/mtime signature changes.
    counters.tags_read += 1


def _reconcile_missing(
    conn: sqlite3.Connection,
    root: Path,
    counters: _Counters,
    guard: _ScanGuard,
) -> None:
    """Flag tracked files under *root* that were not seen on this pass, except staged moves."""
    now = clock.utc_now()
    for row in store.tracked_files_under(conn, path_keys.path_key(root)):
        if row.id in counters.seen_ids or row.is_missing or row.id in guard.file_ids:
            continue
        store.flag_missing(conn, row.id, now)
        counters.missing_flagged += 1


def get_library_stats(settings: Settings) -> dict[str, object]:
    """Return library-wide counts from the snapshot, the mismatch block read by the comparator."""
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        stats = store.compute_stats(connection)
        stats["mismatch"] = mismatch.status_counts(connection, settings)
        return stats
    finally:
        connection.close()
