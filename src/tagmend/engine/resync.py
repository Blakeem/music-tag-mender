"""Re-sync the snapshot mirror to an audio file's bytes after TagMend writes the file.

:mod:`tagmend.engine.versioning` writes tags and :mod:`tagmend.engine.pictures` writes embedded
pictures. Both re-sync through :func:`resync_snapshot` and fail one file on
:data:`TAG_FILE_ERRORS`. They live here so that neither module imports the other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import mutagen

from tagmend.engine import acoustid, store
from tagmend.engine.tags import read_pictures, read_tags

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

# A locked, read-only or unreadable file, or a write the verifier refuses, fails that file alone.
TAG_FILE_ERRORS: Final[tuple[type[Exception], ...]] = (
    OSError,
    ValueError,
    mutagen.MutagenError,  # type: ignore[attr-defined]
)


def resync_snapshot(  # noqa: PLR0913 - cohesive keyword-only re-sync inputs
    conn: sqlite3.Connection,
    file_id: int,
    path: Path,
    *,
    before: tuple[int, int],
    audio_proven: bool,
    now: str,
) -> dict[str, list[str]]:
    """Re-sync the ledger to the bytes of *path*, which a write may have just changed.

    The live ``file_tags`` and ``file_pictures`` snapshots take the re-read tags and pictures,
    and the files-row signature the new stat, so the next incremental scan sees the file as
    unchanged. *before* is the ``(size, mtime_ns)`` the file had before the write. When
    *audio_proven* holds, the write proved the decoded audio unchanged, so the fingerprint row
    moves to the new signature. Returns the re-read tags. Does not commit.
    """
    fresh = read_tags(path).tags
    store.replace_tags(conn, file_id, fresh, now)
    store.replace_pictures(conn, file_id, store.picture_rows(read_pictures(path)))
    stat_result = path.stat()
    after = (stat_result.st_size, stat_result.st_mtime_ns)
    store.update_signature(conn, file_id, size_bytes=after[0], mtime_ns=after[1], now=now)
    if audio_proven:
        acoustid.rekey_fingerprint(conn, file_id, before=before, after=after)
    return fresh
