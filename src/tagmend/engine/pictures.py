"""Embedded pictures: stage, unstage, diff, commit and revert the removal of embedded pictures.

:func:`stage_pictures` stages the removal of embedded pictures in ``picture_writes_staged``, one
row per file and SHA-256, with the picture's bytes and attributes read from disk.
:func:`unstage_pictures` drops staged rows and :func:`diff_pictures` shows them with their state
on disk now. None of the three writes to the library. :func:`commit_pictures` removes the staged
pictures as one commit and logs each removal in ``picture_writes``.
:func:`revert_picture_commit` undoes such a commit for
:func:`tagmend.engine.versioning.revert_commit`.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import mutagen

from tagmend.engine import clock, commits, db, ledger_lock, path_keys, resync, schema, store
from tagmend.engine.serialize import FieldDict
from tagmend.engine.tags import (
    PictureData,
    ensure_pictures_writable,
    read_picture_data,
    read_pictures,
    write_pictures,
)
from tagmend.engine.validation import check_limit
from tagmend.log import get_logger

if TYPE_CHECKING:
    import os
    import sqlite3

    from tagmend.config import Settings

logger = get_logger(__name__)

SKIP_UNREADABLE: Final = "unreadable"
SKIP_UNWRITABLE: Final = "unwritable"

STATE_MISSING: Final = "missing"
STATE_LANDED: Final = "landed"
STATE_READY: Final = "ready"

_ORIGIN_MANUAL: Final = "manual"
_SHA256_PATTERN: Final = re.compile(r"[0-9a-fA-F]{64}")
_READ_ERRORS: Final = (mutagen.MutagenError, OSError)  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class PictureStaged(FieldDict):
    """One picture removal :func:`stage_pictures` staged, or would stage on a dry run.

    ``path`` is the file's absolute path.
    """

    file_id: int
    path: str
    sha256: str
    picture_type: int | None
    mime: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PictureSkipped(FieldDict):
    """One file :func:`stage_pictures` staged nothing for. ``path`` is the file's absolute path."""

    file_id: int
    path: str
    reason: str
    detail: str | None


@dataclass(frozen=True, slots=True)
class StagePicturesResult(FieldDict):
    """What one :func:`stage_pictures` call staged and skipped."""

    dry_run: bool
    staged: list[PictureStaged]
    skipped: list[PictureSkipped]
    summary: str


@dataclass(frozen=True, slots=True)
class PictureDiffView(FieldDict):
    """One staged picture removal as :func:`diff_pictures` shows it, without its bytes.

    ``path`` is the file's absolute path now. ``state`` is ``missing``, ``landed`` or ``ready``.
    """

    file_id: int
    path: str
    sha256: str
    picture_type: int | None
    mime: str
    size_bytes: int
    state: str
    origin: str
    note: str | None
    staged_at: str


@dataclass(frozen=True, slots=True)
class _Pick:
    """One selected picture of one file, with the bytes and attributes its staged row holds."""

    file_id: int
    path: Path
    sha256: str
    ordinal: int
    picture: PictureData


@dataclass(frozen=True, slots=True)
class _OnDisk:
    """One staged file as the commit will meet it. ``digests`` is ``None`` when it is unreadable."""

    path: str
    missing: bool
    digests: frozenset[str] | None

    def state(self, sha256: str) -> str:
        """Return the state of the staged removal of *sha256* from this file.

        An unreadable file is ``ready``, since a picture write replaces a file whole and never
        leaves it unreadable, so the read failure is no landed removal.
        """
        if self.missing:
            return STATE_MISSING
        if self.digests is not None and sha256 not in self.digests:
            return STATE_LANDED
        return STATE_READY


def _normalize_sha256(sha256: str) -> str:
    """Return *sha256* lowercased, or raise :class:`ValueError` unless it is 64 hex characters."""
    if not _SHA256_PATTERN.fullmatch(sha256):
        message = f"sha256 must be 64 hex characters, got {sha256!r}"
        raise ValueError(message)
    return sha256.lower()


def _digest(picture: PictureData) -> str:
    """Return the SHA-256 of *picture*'s bytes, the key every picture row names it by."""
    return hashlib.sha256(picture.data).hexdigest()


# --- stage_pictures --------------------------------------------------------------------


def _skip(row: store.FileRow, path: Path, reason: str, exc: Exception) -> PictureSkipped:
    """Build the result row of one file skipped for *exc*."""
    return PictureSkipped(file_id=row.id, path=str(path), reason=reason, detail=str(exc))


def _pick_file(
    row: store.FileRow,
    digest: str | None,
    droppable_frames: frozenset[str],
) -> list[_Pick] | PictureSkipped:
    """Return the pictures of one file that *digest* selects, read from disk, or why it is skipped.

    Two copies of one digest make one pick. A file holding no selected picture is never checked
    for writability, so it is never reported.
    """
    # Input
    path = Path(row.folder) / row.filename
    picks: dict[str, _Pick] = {}
    try:
        pictures = read_picture_data(path)
    except _READ_ERRORS as exc:
        return _skip(row, path, SKIP_UNREADABLE, exc)

    # Process
    for ordinal, picture in enumerate(pictures):
        sha256 = _digest(picture)
        if digest is None or sha256 == digest:
            picks.setdefault(
                sha256,
                _Pick(file_id=row.id, path=path, sha256=sha256, ordinal=ordinal, picture=picture),
            )
    if not picks:
        return []
    try:
        ensure_pictures_writable(path, droppable_frames=droppable_frames)
    except _READ_ERRORS as exc:
        return _skip(row, path, SKIP_UNREADABLE, exc)
    except ValueError as exc:
        return _skip(row, path, SKIP_UNWRITABLE, exc)

    # Output
    return list(picks.values())


def _write_picks(conn: sqlite3.Connection, picks: list[_Pick], note: str | None) -> None:
    """Stage every pick, replacing the row already staged for its file and digest."""
    now = clock.utc_now()
    for pick in picks:
        store.upsert_staged_picture(
            conn,
            store.StagedPicture(
                file_id=pick.file_id,
                sha256=pick.sha256,
                ordinal=pick.ordinal,
                picture_type=pick.picture.picture_type,
                mime=pick.picture.mime,
                description=pick.picture.description,
                size_bytes=len(pick.picture.data),
                origin=_ORIGIN_MANUAL,
                note=note,
                staged_at=now,
            ),
            attributes=pick.picture.to_json(),
            content=pick.picture.data,
        )


def _staged_view(pick: _Pick) -> PictureStaged:
    """Build the result row of one pick."""
    return PictureStaged(
        file_id=pick.file_id,
        path=str(pick.path),
        sha256=pick.sha256,
        picture_type=pick.picture.picture_type,
        mime=pick.picture.mime,
        size_bytes=len(pick.picture.data),
    )


def _stage_summary(*, dry_run: bool, picks: list[_Pick], skipped: list[PictureSkipped]) -> str:
    """Build a short, plain human summary of one :func:`stage_pictures` call."""
    verb = "Would stage" if dry_run else "Staged"
    files = len({pick.file_id for pick in picks})
    reasons = Counter(row.reason for row in skipped)
    counted = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
    text = (
        f"{verb} the removal of {len(picks)} picture(s) from {files} file(s). "
        f"Skipped {len(skipped)} file(s)"
    )
    text += f": {counted}." if counted else "."
    return text


@ledger_lock.mutating
def stage_pictures(
    settings: Settings,
    *,
    path: str | os.PathLike[str],
    sha256: str | None = None,
    dry_run: bool = False,
    note: str | None = None,
) -> StagePicturesResult:
    """Stage the removal of pictures embedded in the files in *path* (writes no library file).

    *path* is a folder, and the present files in it and in every folder under it are read from
    disk, never from the snapshot, so each staged row holds the current bytes. Without *sha256*
    every embedded picture of those files is selected, with it only the pictures of that digest.
    A real run upserts one row per file and digest with origin ``manual`` and *note*. Two copies
    of one digest in a file make one row, and the commit removes both. A file that cannot be read
    is skipped as ``unreadable``, and one holding a selected picture that
    :func:`tagmend.engine.tags.ensure_pictures_writable` refuses as ``unwritable``. A dry run
    reads the same and writes nothing.

    Raises :class:`ValueError` for a *sha256* that is not 64 hex characters and a *path* outside
    ``music_path``.
    """
    # Input
    digest = None if sha256 is None else _normalize_sha256(sha256)
    root_key = path_keys.folder_arg_key(settings, path)
    droppable_frames = frozenset(settings.id3_droppable_frames)
    picks: list[_Pick] = []
    skipped: list[PictureSkipped] = []
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        files = [
            row for row in store.tracked_files_under(connection, root_key) if not row.is_missing
        ]

        # Process
        for row in files:
            picked = _pick_file(row, digest, droppable_frames)
            if isinstance(picked, PictureSkipped):
                skipped.append(picked)
            else:
                picks.extend(picked)
        if not dry_run:
            _write_picks(connection, picks, note)
            connection.commit()
    finally:
        connection.close()

    # Output
    logger.info("stage_pictures dry_run=%s staged=%d skipped=%d", dry_run, len(picks), len(skipped))
    return StagePicturesResult(
        dry_run=dry_run,
        staged=[_staged_view(pick) for pick in picks],
        skipped=skipped,
        summary=_stage_summary(dry_run=dry_run, picks=picks, skipped=skipped),
    )


# --- unstage_pictures and diff_pictures ------------------------------------------------


def _read_on_disk(conn: sqlite3.Connection, file_id: int) -> _OnDisk:
    """Return what the file *file_id* holds on disk now."""
    row = store.get_file_by_id(conn, file_id)
    if row is None:  # pragma: no cover - defensive, a files delete cascades to its staged rows
        message = f"a staged picture names unknown file_id={file_id}"
        raise RuntimeError(message)
    path = Path(row.folder) / row.filename
    if row.is_missing or not path.is_file():
        return _OnDisk(path=str(path), missing=True, digests=None)
    try:
        digests = frozenset(picture.sha256 for picture in read_pictures(path))
    except _READ_ERRORS as exc:
        logger.warning("picture state: cannot read %s: %s", path, exc)
        return _OnDisk(path=str(path), missing=False, digests=None)
    return _OnDisk(path=str(path), missing=False, digests=digests)


def _files_on_disk(
    conn: sqlite3.Connection,
    rows: list[store.StagedPicture],
) -> dict[int, _OnDisk]:
    """Return what each file of *rows* holds on disk now, each file read once."""
    return {file_id: _read_on_disk(conn, file_id) for file_id in {row.file_id for row in rows}}


def _refuse_landed(on_disk: dict[int, _OnDisk], rows: list[store.StagedPicture]) -> None:
    """Refuse the unstage when a matched row's removal already landed on disk."""
    landed = [
        f"{on_disk[row.file_id].path} ({row.sha256})"
        for row in rows
        if on_disk[row.file_id].state(row.sha256) == STATE_LANDED
    ]
    if not landed:
        return
    message = (
        f"picture removal(s) {', '.join(landed)} already landed on disk, and each staged row "
        "holds the only copy of its picture. Run commit_pictures to log those removals. Nothing "
        "was unstaged"
    )
    raise ValueError(message)


@ledger_lock.mutating
def unstage_pictures(
    settings: Settings,
    *,
    path: str | os.PathLike[str] | None = None,
    sha256: str | None = None,
) -> int:
    """Drop the staged picture removals of the files in *path* or any folder under it, else all.

    *sha256* keeps only the rows of that digest. Returns the count dropped. Raises
    :class:`ValueError` for a *sha256* that is not 64 hex characters and a *path* outside
    ``music_path``. It also raises and drops nothing when a matched row's file is present and no
    longer holds the digest, a removal a crash cut before its log, since that row holds the only
    copy of the removed picture until ``commit_pictures``.
    """
    # Input
    digest = None if sha256 is None else _normalize_sha256(sha256)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = [
            row
            for row in store.list_staged_pictures(connection, root_key)
            if digest is None or row.sha256 == digest
        ]

        # Process
        _refuse_landed(_files_on_disk(connection, rows), rows)
        for row in rows:
            store.delete_staged_picture(connection, row.file_id, row.sha256)
        connection.commit()
    finally:
        connection.close()

    # Output
    logger.info("unstage_pictures removed=%d", len(rows))
    return len(rows)


def diff_pictures(
    settings: Settings,
    *,
    path: str | os.PathLike[str] | None = None,
    limit: int | None = None,
) -> list[PictureDiffView]:
    """Return every staged picture removal, or those of the files in *path* and under it. Read-only.

    ``state`` is the first that holds, as the commit will meet it: ``missing`` (the file is gone
    or flagged missing), ``landed`` (the file no longer holds the digest, so the commit logs the
    removal with no write), else ``ready``. The rows come in file id then digest order. *limit*
    caps the rows. Raises :class:`ValueError` for a negative *limit* and a *path* outside
    ``music_path``.
    """
    # Input
    check_limit(limit)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = store.list_staged_pictures(connection, root_key)
        capped = rows if limit is None else rows[:limit]

        # Process
        on_disk = _files_on_disk(connection, capped)
    finally:
        connection.close()

    # Output
    return [
        PictureDiffView(
            file_id=row.file_id,
            path=on_disk[row.file_id].path,
            sha256=row.sha256,
            picture_type=row.picture_type,
            mime=row.mime,
            size_bytes=row.size_bytes,
            state=on_disk[row.file_id].state(row.sha256),
            origin=row.origin,
            note=row.note,
            staged_at=row.staged_at,
        )
        for row in capped
    ]


# --- commit_pictures -------------------------------------------------------------------

ACTION_REMOVE: Final = "remove"
ACTION_RESTORE: Final = "restore"

_ORIGIN_REVERT: Final = "revert"
_CHANGED_SINCE_STAGE_DETAIL: Final = (
    "the file changed since staging. Review it with diff_pictures, or drop its rows with "
    "unstage_pictures"
)


@dataclass(frozen=True, slots=True)
class _LogEntry:
    """One ``picture_writes`` row a file appends, before :func:`_append_log` numbers it."""

    action: str
    ordinal: int
    picture: PictureData
    origin: str
    note: str | None
    reverted_from: int | None = None


@dataclass(frozen=True, slots=True)
class _FileChange:
    """What one file's picture write and every row it logs under one commit share."""

    commit_id: int
    file_id: int
    path: Path
    now: str
    droppable_frames: frozenset[str]


def _write_and_resync(
    conn: sqlite3.Connection,
    change: _FileChange,
    target: list[PictureData] | None,
) -> None:
    """Write *target* as the file's pictures unless it is ``None``, then re-sync the snapshot."""
    before = change.path.stat()
    audio_proven = False
    if target is not None:
        audio_proven = write_pictures(
            change.path, target, droppable_frames=change.droppable_frames
        ).audio_proven
    resync.resync_snapshot(
        conn,
        change.file_id,
        change.path,
        before=(before.st_size, before.st_mtime_ns),
        audio_proven=audio_proven,
        now=change.now,
    )


def _append_log(
    conn: sqlite3.Connection, change: _FileChange, entries: list[_LogEntry]
) -> int | None:
    """Append *entries* in order, each one version above the last, and return the last version.

    Returns ``None`` when *entries* is empty. Does not commit.
    """
    version = store.latest_picture_version(conn, change.file_id)
    for entry in entries:
        version += 1
        store.insert_picture_write(
            conn,
            store.PictureWrite(
                commit_id=change.commit_id,
                created_at=change.now,
                origin=entry.origin,
                action=entry.action,
                reverted_from=entry.reverted_from,
                file_id=change.file_id,
                version=version,
                path=str(change.path),
                sha256=_digest(entry.picture),
                ordinal=entry.ordinal,
                picture_type=entry.picture.picture_type,
                mime=entry.picture.mime,
                description=entry.picture.description,
                attributes=entry.picture.to_json(),
                content=entry.picture.data,
                size_bytes=len(entry.picture.data),
                note=entry.note,
            ),
        )
    return version if entries else None


def _removal(row: store.StagedPicture, ordinal: int, picture: PictureData) -> _LogEntry:
    """Return the ``remove`` row of one copy of the staged *row*'s picture at *ordinal*."""
    return _LogEntry(
        action=ACTION_REMOVE, ordinal=ordinal, picture=picture, origin=row.origin, note=row.note
    )


def _landed_removal(conn: sqlite3.Connection, row: store.StagedPicture) -> _LogEntry:
    """Return the ``remove`` row of a staged removal that landed before a crash cut its log.

    The file no longer holds the picture, so the row is built from the staged copy.
    """
    payload = store.staged_picture_payload(conn, row.file_id, row.sha256)
    if payload is None:  # pragma: no cover - defensive, the staged row was just read
        message = f"file_id={row.file_id} has no staged picture {row.sha256}"
        raise RuntimeError(message)
    return _removal(row, row.ordinal, PictureData.from_json(*payload))


def _refuse_overlaps(conn: sqlite3.Connection) -> None:
    """Refuse the commit while a file with a staged picture row has a staged tag change or move.

    A picture write changes the size and mtime that ``commit_tags`` and ``commit_paths`` check.
    """
    tagged, moved = store.staged_picture_overlaps(conn)
    problems: list[str] = []
    if tagged:
        problems.append(
            f"File(s) {', '.join(map(str, tagged))} also have a staged tag change. "
            "Run commit_tags or unstage_tags first."
        )
    if moved:
        problems.append(
            f"File(s) {', '.join(map(str, moved))} also have a staged move. "
            "Run commit_paths or unstage_paths first."
        )
    if not problems:
        return
    message = (
        f"{' '.join(problems)} Then run commit_pictures, since a picture write changes the size "
        "and mtime those commits check"
    )
    raise ValueError(message)


@dataclass(frozen=True, slots=True)
class PictureDomain:
    """The pictures :class:`tagmend.engine.commits.RevisionDomain` driven by ``run_commit``.

    ``droppable_frames`` is the ID3 frame id set every write is given.
    """

    droppable_frames: frozenset[str] = frozenset()

    @property
    def changed_since_stage_detail(self) -> str:
        """Name the review and the unstage tool, though :meth:`changed_since_stage` never holds."""
        return _CHANGED_SINCE_STAGE_DETAIL

    @property
    def per_file_errors(self) -> tuple[type[Exception], ...]:
        """A locked, read-only or unreadable file, or a refused write, touches no other file."""
        return resync.TAG_FILE_ERRORS

    def plan_order(self, conn: sqlite3.Connection, file_ids: list[int]) -> list[int]:  # noqa: ARG002
        """Pictures have no ordering: iterate in the given order."""
        return file_ids

    def resolve_path(self, conn: sqlite3.Connection, file_id: int) -> Path | None:
        """Return the file's current path, or ``None`` if it is unknown or flagged missing."""
        row = store.get_file_by_id(conn, file_id)
        if row is None or row.is_missing:
            return None
        return Path(row.folder) / row.filename

    def changed_since_stage(self, conn: sqlite3.Connection, file_id: int, path: Path) -> bool:  # noqa: ARG002
        """Return ``False``, since a staged row names its picture by digest.

        A later write to the file, such as a tag commit, leaves that picture's bytes as they
        were, so it does not void the removal.
        """
        return False

    def apply_to_disk(
        self,
        conn: sqlite3.Connection,
        file_id: int,
        path: Path,
        *,
        commit_id: int,
        now: str,
    ) -> int | None:
        """Remove every copy of each staged digest from the file, log each removal, drop the rows.

        The file is written before any database write, and every database write stays in the
        open transaction ``run_commit`` commits. Each copy removed appends a ``remove`` row read
        from disk. A staged digest the file no longer holds is a removal that landed before a
        crash, so its staged copy appends the ``remove`` row with no write. Returns the highest
        version appended.
        """
        # Input
        staged = store.staged_pictures_of_file(conn, file_id)
        current = read_picture_data(path)
        change = _FileChange(
            commit_id=commit_id,
            file_id=file_id,
            path=path,
            now=now,
            droppable_frames=self.droppable_frames,
        )
        by_digest = {row.sha256: row for row in staged}
        digests = [_digest(picture) for picture in current]

        # Process
        removed = [
            _removal(by_digest[digest], ordinal, picture)
            for ordinal, (digest, picture) in enumerate(zip(digests, current, strict=True))
            if digest in by_digest
        ]
        landed = [_landed_removal(conn, row) for row in staged if row.sha256 not in digests]
        kept = [
            picture
            for digest, picture in zip(digests, current, strict=True)
            if digest not in by_digest
        ]
        _write_and_resync(conn, change, kept if removed else None)
        version = _append_log(conn, change, [*removed, *landed])
        for row in staged:
            store.delete_staged_picture(conn, file_id, row.sha256)

        # Output
        return version

    def flag_and_drop_missing(self, conn: sqlite3.Connection, file_id: int) -> None:
        """Flag the file missing and drop its staged picture rows (it vanished from disk)."""
        store.flag_missing(conn, file_id, clock.utc_now())
        for row in store.staged_pictures_of_file(conn, file_id):
            store.delete_staged_picture(conn, file_id, row.sha256)

    def post_commit_file(self, conn: sqlite3.Connection, file_id: int) -> None:
        """Pictures have no per-file filesystem follow-up."""


@ledger_lock.mutating
def commit_pictures(settings: Settings) -> commits.CommitResult:
    """Remove every staged picture from its file as one commit and log each removal.

    Any commit left ``applying`` is marked ``interrupted`` first, and its leftover rows are swept
    into this one. The files run in file id order through
    :func:`tagmend.engine.commits.run_commit`. Each file loses every copy of each staged digest
    and keeps every other entry, and each copy removed appends a ``remove`` row with its bytes to
    ``picture_writes``. A staged digest the file no longer holds is logged with no write. A file
    gone from disk is flagged missing and its rows dropped, and a file that fails to write keeps
    its rows and reports ``error``. The commit's origin is ``manual``.
    ``versioning.revert_commit`` undoes the commit through :func:`revert_picture_commit`.

    ``commit_id`` is ``None`` when nothing is staged. Raises :class:`ValueError` while a file
    with a staged picture row also has a staged tag change or move.
    """
    # Input
    domain = PictureDomain(droppable_frames=frozenset(settings.id3_droppable_frames))
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        commits.mark_interrupted(connection)
        connection.commit()
        staged = store.list_staged_pictures(connection)
        file_ids = list(dict.fromkeys(row.file_id for row in staged))
        if not file_ids:
            return commits.summarize(commit_id=None, applied=[])
        _refuse_overlaps(connection)

        # Process
        commit_id = commits.create_commit(
            connection,
            origin=_ORIGIN_MANUAL,
            message=f"remove {len(staged)} staged picture(s) from {len(file_ids)} file(s)",
            now=clock.utc_now(),
        )
        connection.commit()  # commit row durable before any disk work
        applied = commits.run_commit(connection, domain, commit_id=commit_id, file_ids=file_ids)
        commits.set_commit_status(connection, commit_id, "applied")
        connection.commit()
    finally:
        connection.close()

    # Output
    result = commits.summarize(commit_id=commit_id, applied=applied)
    logger.info(
        "picture commit %d: committed=%d missing=%d errors=%d",
        commit_id,
        result.committed,
        result.missing,
        result.errors,
    )
    return result


# --- revert_commit of a picture commit -------------------------------------------------

_REVERTABLE: Final = "revertable"
_STATUS_REVERTED: Final = "reverted"
_STATUS_NOOP: Final = "noop"
_STATUS_MISSING: Final = "missing"
_STATUS_SKIPPED: Final = "skipped_later_changes"
_STATUS_ERROR: Final = "error"


@dataclass(frozen=True, slots=True)
class _Undo:
    """The picture list one file's revert sets, the inverse rows it logs, and whether it writes."""

    target: list[PictureData]
    entries: list[_LogEntry]
    writes: bool


@dataclass(frozen=True, slots=True)
class _RevertRun:
    """What every file of one picture revert shares. ``commit_id`` is ``None`` on a dry run."""

    commit_id: int | None
    note: str | None
    droppable_frames: frozenset[str]


def _rows_by_file(rows: list[store.PictureWriteRow]) -> list[list[store.PictureWriteRow]]:
    """Group *rows*, which come in file id then version order, into one list per file."""
    grouped: dict[int, list[store.PictureWriteRow]] = {}
    for row in rows:
        grouped.setdefault(row.file_id, []).append(row)
    return list(grouped.values())


def _classify_revert(
    conn: sqlite3.Connection,
    rows: list[store.PictureWriteRow],
) -> tuple[str, Path | None]:
    """Classify one file's rows of the reverted commit, with the file's path when revertable.

    The kind is ``missing`` (the file is gone or flagged missing), ``skipped_later_changes`` (a
    later ``picture_writes`` row of the file exists), else ``revertable``.
    """
    file_id = rows[0].file_id
    file_row = store.get_file_by_id(conn, file_id)
    if file_row is None or file_row.is_missing:
        return _STATUS_MISSING, None
    path = Path(file_row.folder) / file_row.filename
    if not path.is_file():
        return _STATUS_MISSING, None
    if store.latest_picture_version(conn, file_id) > rows[-1].version:
        return _STATUS_SKIPPED, None
    return _REVERTABLE, path


def _logged_picture(conn: sqlite3.Connection, row: store.PictureWriteRow) -> PictureData:
    """Return the picture the ``picture_writes`` row *row* logged, with its bytes."""
    payload = store.picture_write_payload(conn, row.id)
    if payload is None:  # pragma: no cover - defensive, the row was just read
        message = f"picture_writes row {row.id} is gone"
        raise RuntimeError(message)
    return PictureData.from_json(*payload)


def _plan_undo(
    current: list[PictureData],
    logged: list[tuple[store.PictureWriteRow, PictureData]],
    note: str | None,
) -> _Undo:
    """Return how one file undoes its *logged* rows, given its *current* pictures. Pure.

    A ``restore`` row is undone by removing every copy of its digest. A ``remove`` row is undone
    by inserting its picture at its logged ordinal, clamped to the list length, the rows taken in
    ordinal order. A digest already in the target state needs no write, and its inverse row is
    still logged.
    """
    held = {_digest(picture) for picture in current}
    dropped = {
        row.sha256 for row, _ in logged if row.action == ACTION_RESTORE and row.sha256 in held
    }
    target = [picture for picture in current if _digest(picture) not in dropped]
    entries: list[_LogEntry] = []
    for row, picture in sorted(logged, key=lambda pair: pair[0].ordinal):
        ordinal = row.ordinal
        if row.action == ACTION_REMOVE and row.sha256 not in held:
            ordinal = min(row.ordinal, len(target))
            target.insert(ordinal, picture)
        entries.append(
            _LogEntry(
                action=ACTION_RESTORE if row.action == ACTION_REMOVE else ACTION_REMOVE,
                ordinal=ordinal,
                picture=picture,
                origin=_ORIGIN_REVERT,
                note=note,
                reverted_from=row.id,
            )
        )
    return _Undo(target=target, entries=entries, writes=target != current)


def _revert_file(
    conn: sqlite3.Connection,
    run: _RevertRun,
    rows: list[store.PictureWriteRow],
    path: Path,
) -> commits.FileRevertOutcome:
    """Undo one file's rows of the reverted commit and commit them, or preview that on a dry run.

    The write and the re-sync land before the inverse rows are appended. A failure rolls this
    file back alone and reports ``error``.
    """
    # Input
    file_id = rows[0].file_id
    target_version = rows[0].version - 1
    new_version: int | None = None
    try:
        logged = [(row, _logged_picture(conn, row)) for row in rows]
        undo = _plan_undo(read_picture_data(path), logged, run.note)

        # Process
        if run.commit_id is not None:
            change = _FileChange(
                commit_id=run.commit_id,
                file_id=file_id,
                path=path,
                now=clock.utc_now(),
                droppable_frames=run.droppable_frames,
            )
            _write_and_resync(conn, change, undo.target if undo.writes else None)
            new_version = _append_log(conn, change, undo.entries)
            conn.commit()  # the disk write is done, so the inverse rows are now durable
    except resync.TAG_FILE_ERRORS as exc:
        conn.rollback()
        logger.warning("picture revert: file_id=%d failed: %s", file_id, exc)
        return commits.FileRevertOutcome(
            file_id=file_id,
            target_version=target_version,
            new_version=None,
            status=_STATUS_ERROR,
            detail=str(exc),
        )

    # Output
    return commits.FileRevertOutcome(
        file_id=file_id,
        target_version=target_version,
        new_version=new_version,
        status=_STATUS_REVERTED if undo.writes else _STATUS_NOOP,
    )


def revert_picture_commit(
    conn: sqlite3.Connection,
    settings: Settings,
    commit_id: int,
    *,
    note: str | None,
    dry_run: bool,
) -> commits.RevertCommitResult:
    """Undo every embedded picture *commit_id* removed or restored, under ONE new ``revert`` commit.

    The picture half of :func:`tagmend.engine.versioning.revert_commit`, which has already checked
    the target commit and that nothing is staged. The files run in file id order. A file gone or
    flagged missing is ``missing``, and one with a later ``picture_writes`` row is
    ``skipped_later_changes``. Otherwise a ``remove`` row is undone by writing its picture back at
    its logged ordinal, clamped to the list length, from its logged attributes and bytes, and a
    ``restore`` row by removing its digest. Each file is written once with
    :func:`tagmend.engine.tags.write_pictures` and re-synced before its inverse rows (``restore``
    for ``remove``, ``remove`` for ``restore``) are appended with origin ``revert``. A digest
    already in the target state is logged with no write, which also resumes a revert a crash cut
    between its write and its log. A file that needed no write is ``noop``, and a failed write is
    ``error`` while the rest continue. ``target_version`` is the file's ``picture_writes``
    version before the commit. *dry_run* classifies and writes nothing. With nothing revertable,
    no commit is created. Commits per file on the caller's open *conn*.
    """
    # Input
    planned = [
        (rows, *_classify_revert(conn, rows))
        for rows in _rows_by_file(store.picture_writes_for_commit(conn, commit_id))
    ]
    new_commit: int | None = None

    # Process
    if not dry_run and any(path is not None for _, _, path in planned):
        new_commit = commits.create_commit(
            conn, origin=_ORIGIN_REVERT, message=note, now=clock.utc_now(), reverted_from=commit_id
        )
        conn.commit()  # commit row durable before any disk work
    run = _RevertRun(
        commit_id=new_commit, note=note, droppable_frames=frozenset(settings.id3_droppable_frames)
    )
    outcomes = [
        _revert_file(conn, run, rows, path)
        if path is not None
        else commits.FileRevertOutcome(
            file_id=rows[0].file_id, target_version=None, new_version=None, status=kind
        )
        for rows, kind, path in planned
    ]
    if new_commit is not None:
        commits.set_commit_status(conn, new_commit, "applied")
        conn.commit()

    # Output
    result = commits.summarize_revert(
        commit_id=new_commit, reverted_from=commit_id, dry_run=dry_run, outcomes=outcomes
    )
    logger.info(
        "picture revert of commit %d as commit %s: reverted=%d noop=%d skipped=%d missing=%d "
        "errors=%d",
        commit_id,
        new_commit,
        result.reverted,
        result.noop,
        result.skipped,
        result.missing,
        result.errors,
    )
    return result
