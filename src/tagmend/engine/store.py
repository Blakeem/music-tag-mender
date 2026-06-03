"""Pure data access for the ``files`` / ``file_tags`` snapshot (M1).

Every function takes an open :class:`sqlite3.Connection` and does one focused thing:
no scanning, no tag reading, no commit policy — that orchestration lives in
:mod:`tagmend.engine.library`. SQLite hands back ``Any``; this module casts at the
boundary so the rest of the engine stays strictly typed.

All SQL uses ``?`` placeholders (never string-formatted values).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, SupportsInt, cast

from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

logger = get_logger(__name__)


def _as_int(value: object) -> int:
    """Coerce a sqlite-returned ``Any``/``object`` scalar to ``int`` for strict typing."""
    return int(cast("SupportsInt", value))


@dataclass(frozen=True, slots=True)
class FileRow:
    """One row from the ``files`` table, typed for engine use."""

    id: int
    folder: str
    filename: str
    ext: str
    size_bytes: int | None
    mtime_ns: int | None
    is_missing: bool
    tags_updated_at: str | None


_FILE_COLUMNS = "id, folder, filename, ext, size_bytes, mtime_ns, is_missing, tags_updated_at"


def _row_to_file(row: tuple[object, ...]) -> FileRow:
    """Build a typed :class:`FileRow` from a raw sqlite tuple."""
    return FileRow(
        id=_as_int(row[0]),
        folder=str(row[1]),
        filename=str(row[2]),
        ext=str(row[3]),
        size_bytes=None if row[4] is None else _as_int(row[4]),
        mtime_ns=None if row[5] is None else _as_int(row[5]),
        is_missing=bool(row[6]),
        tags_updated_at=None if row[7] is None else str(row[7]),
    )


def get_file(conn: sqlite3.Connection, folder: str, filename: str) -> FileRow | None:
    """Return the file row anchored at ``(folder, filename)``, or ``None``."""
    cursor = conn.execute(
        f"SELECT {_FILE_COLUMNS} FROM files WHERE folder = ? AND filename = ?",  # noqa: S608
        (folder, filename),
    )
    row = cursor.fetchone()
    return None if row is None else _row_to_file(tuple(row))


def insert_file(  # noqa: PLR0913 - keyword-only insert payload, all columns required
    conn: sqlite3.Connection,
    *,
    folder: str,
    filename: str,
    ext: str,
    size_bytes: int | None,
    mtime_ns: int | None,
    now: str,
) -> int:
    """Insert a new file row (tags unread) and return its assigned ``id``."""
    cursor = conn.execute(
        """
        INSERT INTO files (
            folder, filename, ext, size_bytes, mtime_ns,
            is_missing, first_seen_at, updated_at, tags_updated_at
        )
        VALUES (?, ?, ?, ?, ?, 0, ?, ?, NULL)
        """,
        (folder, filename, ext, size_bytes, mtime_ns, now, now),
    )
    new_id = cursor.lastrowid
    if new_id is None:  # pragma: no cover - defensive; INTEGER PK always assigns one
        message = "insert_file did not return a row id"
        raise RuntimeError(message)
    return int(new_id)


def update_signature(
    conn: sqlite3.Connection,
    file_id: int,
    *,
    size_bytes: int | None,
    mtime_ns: int | None,
    now: str,
) -> None:
    """Record a new size/mtime signature and bump ``updated_at``."""
    conn.execute(
        "UPDATE files SET size_bytes = ?, mtime_ns = ?, updated_at = ? WHERE id = ?",
        (size_bytes, mtime_ns, now, file_id),
    )


def clear_missing(conn: sqlite3.Connection, file_id: int, now: str) -> None:
    """Mark a previously-missing file as present again."""
    conn.execute(
        "UPDATE files SET is_missing = 0, updated_at = ? WHERE id = ?",
        (now, file_id),
    )


def flag_missing(conn: sqlite3.Connection, file_id: int, now: str) -> None:
    """Mark a file as missing (its path is no longer on disk)."""
    conn.execute(
        "UPDATE files SET is_missing = 1, updated_at = ? WHERE id = ?",
        (now, file_id),
    )


def get_tags(conn: sqlite3.Connection, file_id: int) -> dict[str, list[str]]:
    """Return the stored tags for *file_id* as canonical name -> ordered values."""
    cursor = conn.execute(
        "SELECT name, value FROM file_tags WHERE file_id = ? ORDER BY name, ordinal",
        (file_id,),
    )
    tags: dict[str, list[str]] = {}
    for row in cursor.fetchall():
        name = str(row[0])
        value = str(row[1])
        tags.setdefault(name, []).append(value)
    return tags


def replace_tags(
    conn: sqlite3.Connection,
    file_id: int,
    tags: dict[str, list[str]],
    now: str,
) -> None:
    """Replace all stored tags for *file_id* and stamp ``tags_updated_at``."""
    conn.execute("DELETE FROM file_tags WHERE file_id = ?", (file_id,))
    rows = [
        (file_id, name, ordinal, value)
        for name, values in tags.items()
        for ordinal, value in enumerate(values)
    ]
    if rows:
        conn.executemany(
            "INSERT INTO file_tags (file_id, name, ordinal, value) VALUES (?, ?, ?, ?)",
            rows,
        )
    conn.execute(
        "UPDATE files SET tags_updated_at = ? WHERE id = ?",
        (now, file_id),
    )


def tracked_files_under(conn: sqlite3.Connection, root: Path) -> list[FileRow]:
    """Return every tracked file whose folder is *root* or nested under it.

    Filtering happens in Python via :meth:`Path.is_relative_to` to avoid ``LIKE``
    wildcard pitfalls when paths contain ``_`` or ``%``.
    """
    cursor = conn.execute(f"SELECT {_FILE_COLUMNS} FROM files")  # noqa: S608
    result: list[FileRow] = []
    for raw in cursor.fetchall():
        row = _row_to_file(tuple(raw))
        from_path = Path(row.folder)
        if from_path == root or from_path.is_relative_to(root):
            result.append(row)
    return result


def compute_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """Return library-wide counts for the ``stats`` command / MCP tool."""
    total_files = _scalar_int(conn, "SELECT COUNT(*) FROM files")
    missing = _scalar_int(conn, "SELECT COUNT(*) FROM files WHERE is_missing = 1")
    present = total_files - missing
    unprocessed = _scalar_int(
        conn,
        "SELECT COUNT(*) FROM files WHERE tags_updated_at IS NULL AND is_missing = 0",
    )
    total_tag_values = _scalar_int(conn, "SELECT COUNT(*) FROM file_tags")

    by_ext: dict[str, int] = {}
    for row in conn.execute("SELECT ext, COUNT(*) FROM files GROUP BY ext ORDER BY ext"):
        by_ext[str(row[0])] = _as_int(row[1])

    return {
        "total_files": total_files,
        "missing": missing,
        "present": present,
        "unprocessed": unprocessed,
        "total_tag_values": total_tag_values,
        "by_ext": by_ext,
    }


def _scalar_int(conn: sqlite3.Connection, sql: str) -> int:
    """Run a single-value COUNT query and return it as an ``int``."""
    row = conn.execute(sql).fetchone()
    return 0 if row is None else _as_int(row[0])
