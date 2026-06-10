"""Pure data access for the snapshot, tag-revision log, and staged-tag area (M1 + M3).

Covers the ``files`` / ``file_tags`` snapshot, the append-only ``tag_revisions`` history,
and the ``tag_revisions_staged`` staging area (git's index). Every function takes an open
:class:`sqlite3.Connection` and does one focused thing: no scanning, no tag reading, no
commit policy — that orchestration lives in :mod:`tagmend.engine.library`,
:mod:`tagmend.engine.versioning`, and :mod:`tagmend.engine.staging`. The ``commits``-table
ops and the shared commit loop live in :mod:`tagmend.engine.commits`. SQLite hands back
``Any``; this module casts at the boundary so the rest of the engine stays strictly typed.

All SQL uses ``?`` placeholders (never string-formatted values).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, SupportsInt, cast

from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

logger = get_logger(__name__)


def _as_int(value: object) -> int:
    """Coerce a sqlite-returned ``Any``/``object`` scalar to ``int`` for strict typing."""
    return int(cast("SupportsInt", value))


def _dump_json(obj: object) -> str:
    """Serialize *obj* deterministically (sorted keys, compact) for a revision column."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _parse_tag_map(raw: str) -> dict[str, list[str]]:
    """Parse a stored ``managed_tags`` blob back into the typed tag map."""
    return cast("dict[str, list[str]]", json.loads(raw))


def _parse_diff(raw: str) -> dict[str, dict[str, list[str]]]:
    """Parse a stored ``diff`` blob back into the typed ``{tag: {from, to}}`` map."""
    return cast("dict[str, dict[str, list[str]]]", json.loads(raw))


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


def get_file_by_id(conn: sqlite3.Connection, file_id: int) -> FileRow | None:
    """Return the file row with the given stable ``id``, or ``None``.

    Used by the revert path, which knows a file by its durable ``file_id`` and needs
    its current on-disk ``(folder, filename)`` to write tags back.
    """
    cursor = conn.execute(
        f"SELECT {_FILE_COLUMNS} FROM files WHERE id = ?",  # noqa: S608
        (file_id,),
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


def list_files(conn: sqlite3.Connection, *, limit: int | None = None) -> list[FileRow]:
    """Return tracked files in stable id order, optionally capped at *limit* rows."""
    sql = f"SELECT {_FILE_COLUMNS} FROM files ORDER BY id"  # noqa: S608
    cursor = conn.execute(sql) if limit is None else conn.execute(f"{sql} LIMIT ?", (limit,))
    return [_row_to_file(tuple(row)) for row in cursor.fetchall()]


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
        "genre": genre_status_counts(conn),
    }


def _scalar_int(conn: sqlite3.Connection, sql: str) -> int:
    """Run a single-value COUNT query and return it as an ``int``."""
    row = conn.execute(sql).fetchone()
    return 0 if row is None else _as_int(row[0])


# --- tag_revisions (append-only history; PLAN.md §7) -------------------------------

# Valid ``origin`` values. ``scan`` = the version-0 baseline, ``auto``/``manual`` =
# normal writes, ``revert`` = a revert (which is itself an appended revision).
_REVISION_ORIGINS: Final = frozenset({"scan", "auto", "manual", "revert"})

_REVISION_COLUMNS = (
    "file_id, version, created_at, origin, reverted_from, commit_id, managed_tags, diff, note"
)


@dataclass(frozen=True, slots=True)
class Revision:
    """One row from ``tag_revisions``, with its JSON columns parsed to typed maps."""

    file_id: int
    version: int
    created_at: str
    origin: str
    reverted_from: int | None
    commit_id: int | None
    managed_tags: dict[str, list[str]]
    diff: dict[str, dict[str, list[str]]]
    note: str | None


def _row_to_revision(row: tuple[object, ...]) -> Revision:
    """Build a typed :class:`Revision` from a raw sqlite tuple."""
    return Revision(
        file_id=_as_int(row[0]),
        version=_as_int(row[1]),
        created_at=str(row[2]),
        origin=str(row[3]),
        reverted_from=None if row[4] is None else _as_int(row[4]),
        commit_id=None if row[5] is None else _as_int(row[5]),
        managed_tags=_parse_tag_map(str(row[6])),
        diff=_parse_diff(str(row[7])),
        note=None if row[8] is None else str(row[8]),
    )


def insert_revision(  # noqa: PLR0913 - cohesive append-only revision payload
    conn: sqlite3.Connection,
    *,
    file_id: int,
    version: int,
    origin: str,
    managed_tags: dict[str, list[str]],
    diff: dict[str, dict[str, list[str]]],
    now: str,
    reverted_from: int | None = None,
    commit_id: int | None = None,
    note: str | None = None,
) -> None:
    """Append one revision row. Append-only — never updates or deletes.

    *commit_id* groups this change with the other files in the same commit; it is
    ``None`` for the version-0 baseline, which precedes any commit. Raises
    :class:`ValueError` for an unknown *origin*. The ``(file_id, version)`` PK rejects a
    duplicate version with :class:`sqlite3.IntegrityError`.
    """
    if origin not in _REVISION_ORIGINS:
        message = f"unknown revision origin: {origin!r}"
        raise ValueError(message)
    conn.execute(
        """
        INSERT INTO tag_revisions (
            file_id, version, created_at, origin, reverted_from,
            commit_id, managed_tags, diff, note
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            version,
            now,
            origin,
            reverted_from,
            commit_id,
            _dump_json(managed_tags),
            _dump_json(diff),
            note,
        ),
    )


def get_revisions(conn: sqlite3.Connection, file_id: int) -> list[Revision]:
    """Return *file_id*'s full revision history, oldest (version 0) first."""
    cursor = conn.execute(
        f"SELECT {_REVISION_COLUMNS} FROM tag_revisions WHERE file_id = ? ORDER BY version",  # noqa: S608
        (file_id,),
    )
    return [_row_to_revision(tuple(row)) for row in cursor.fetchall()]


def get_revision(conn: sqlite3.Connection, file_id: int, version: int) -> Revision | None:
    """Return one specific revision of *file_id*, or ``None`` if it does not exist."""
    cursor = conn.execute(
        f"SELECT {_REVISION_COLUMNS} FROM tag_revisions WHERE file_id = ? AND version = ?",  # noqa: S608
        (file_id, version),
    )
    row = cursor.fetchone()
    return None if row is None else _row_to_revision(tuple(row))


def revisions_for_commit(conn: sqlite3.Connection, commit_id: int) -> list[Revision]:
    """Return every revision row created by *commit_id*, in ``file_id`` order.

    The commit's per-file change set — what :func:`tagmend.engine.versioning.revert_commit`
    classifies and undoes. Baselines (``commit_id`` NULL) never appear here.
    """
    cursor = conn.execute(
        f"SELECT {_REVISION_COLUMNS} FROM tag_revisions WHERE commit_id = ? ORDER BY file_id",  # noqa: S608
        (commit_id,),
    )
    return [_row_to_revision(tuple(row)) for row in cursor.fetchall()]


def max_version(conn: sqlite3.Connection, file_id: int) -> int | None:
    """Return *file_id*'s highest revision number, or ``None`` if it has none yet.

    This is the single source of truth for "has this file been versioned?" and for the
    current revision (the latest row). ``None`` means no baseline has been captured.
    """
    row = conn.execute(
        "SELECT MAX(version) FROM tag_revisions WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return _as_int(row[0])


# --- staged tags (the git "index"; PLAN.md §7) --------------------------------------

# The ``commits`` table ops and the shared commit loop live in
# :mod:`tagmend.engine.commits`; staged rows here no longer carry a ``commit_id``.
_STAGED_TAG_COLUMNS = "file_id, managed_tags, origin, note, staged_at"


@dataclass(frozen=True, slots=True)
class StagedTag:
    """One pending change from ``tag_revisions_staged`` (the staging area)."""

    file_id: int
    managed_tags: dict[str, list[str]]
    origin: str
    note: str | None
    staged_at: str


def _row_to_staged_tag(row: tuple[object, ...]) -> StagedTag:
    """Build a typed :class:`StagedTag` from a raw sqlite tuple."""
    return StagedTag(
        file_id=_as_int(row[0]),
        managed_tags=_parse_tag_map(str(row[1])),
        origin=str(row[2]),
        note=None if row[3] is None else str(row[3]),
        staged_at=str(row[4]),
    )


def upsert_staged_tag(  # noqa: PLR0913 - cohesive keyword-only staging payload
    conn: sqlite3.Connection,
    *,
    file_id: int,
    managed_tags: dict[str, list[str]],
    origin: str,
    now: str,
    note: str | None = None,
) -> None:
    """Insert or replace the single pending change for *file_id*.

    A re-stage overwrites any prior pending change; the ``file_id`` PK keeps exactly one
    pending change per file (the latest staged target wins).
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO tag_revisions_staged (
            file_id, managed_tags, origin, note, staged_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (file_id, _dump_json(managed_tags), origin, note, now),
    )


def get_staged_tag(conn: sqlite3.Connection, file_id: int) -> StagedTag | None:
    """Return the pending change for *file_id*, or ``None``."""
    cursor = conn.execute(
        f"SELECT {_STAGED_TAG_COLUMNS} FROM tag_revisions_staged WHERE file_id = ?",  # noqa: S608
        (file_id,),
    )
    row = cursor.fetchone()
    return None if row is None else _row_to_staged_tag(tuple(row))


def list_staged_tags(conn: sqlite3.Connection) -> list[StagedTag]:
    """Return every pending change, in file_id order."""
    cursor = conn.execute(
        f"SELECT {_STAGED_TAG_COLUMNS} FROM tag_revisions_staged ORDER BY file_id",  # noqa: S608
    )
    return [_row_to_staged_tag(tuple(row)) for row in cursor.fetchall()]


def list_staged_tags_under(conn: sqlite3.Connection, root: Path) -> list[StagedTag]:
    """Return pending changes whose file lives at *root* or nested under it.

    Joins ``files`` for each staged row's folder and filters in Python via
    :meth:`Path.is_relative_to` (same approach as :func:`tracked_files_under`).
    """
    cursor = conn.execute(
        """
        SELECT s.file_id, s.managed_tags, s.origin, s.note, s.staged_at,
               f.folder
        FROM tag_revisions_staged s
        JOIN files f ON f.id = s.file_id
        ORDER BY s.file_id
        """,
    )
    result: list[StagedTag] = []
    for raw in cursor.fetchall():
        row = tuple(raw)
        folder = Path(str(row[5]))
        if folder == root or folder.is_relative_to(root):
            result.append(_row_to_staged_tag(row[:5]))
    return result


def delete_staged_tag(conn: sqlite3.Connection, file_id: int) -> None:
    """Remove the pending change for *file_id* (no-op if none)."""
    conn.execute("DELETE FROM tag_revisions_staged WHERE file_id = ?", (file_id,))


# --- lastfm_cache (persistent parsed-tag cache; PLAN — Last.fm genre tagging) --------


def get_cached_tags(
    conn: sqlite3.Connection,
    request_key: str,
) -> tuple[bool, list[tuple[str, int]]] | None:
    """Return the cached lookup for *request_key*, or ``None`` on a cache miss.

    ``None`` distinguishes a never-cached key from a cached negative result. A hit is
    ``(found, tags)``: ``(False, [])`` is the negative-cache sentinel (genuinely absent
    from Last.fm), while ``(True, [...])`` is a found result (possibly with an empty
    list when found-but-no-tags). Tags come back as ``(name, weight)`` pairs.
    """
    cursor = conn.execute(
        "SELECT found, tags FROM lastfm_cache WHERE request_key = ?",
        (request_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    found = bool(row[0])
    tags = _parse_tag_pairs(str(row[1]))
    return (found, tags)


def put_cached_tags(
    conn: sqlite3.Connection,
    *,
    request_key: str,
    found: bool,
    tags: list[tuple[str, int]],
    now: str,
) -> None:
    """Insert or replace the cached lookup for *request_key*.

    A re-fetch overwrites any prior cached value. ``tags`` is stored as a JSON array of
    ``[name, weight]`` pairs; pass ``found=False`` with ``tags=[]`` to negative-cache.
    """
    payload = [[name, weight] for name, weight in tags]
    conn.execute(
        """
        INSERT OR REPLACE INTO lastfm_cache (request_key, found, tags, fetched_at)
        VALUES (?, ?, ?, ?)
        """,
        (request_key, 1 if found else 0, _dump_json(payload), now),
    )


def _parse_tag_pairs(raw: str) -> list[tuple[str, int]]:
    """Parse a stored ``tags`` blob (JSON ``[name, weight]`` array) into typed pairs."""
    parsed = cast("list[list[object]]", json.loads(raw))
    return [(str(name), _as_int(weight)) for name, weight in parsed]


# --- file_genre_status (terminal/negative decisions; PLAN — Status model) -----------


@dataclass(frozen=True, slots=True)
class GenreStatusRow:
    """One row from ``file_genre_status`` (a ``'no_match'`` / ``'manual'`` decision)."""

    status: str
    source_artist: str | None
    source_album: str | None


def get_genre_status(conn: sqlite3.Connection, file_id: int) -> GenreStatusRow | None:
    """Return *file_id*'s terminal genre decision, or ``None`` if it has none."""
    cursor = conn.execute(
        "SELECT status, source_artist, source_album FROM file_genre_status WHERE file_id = ?",
        (file_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return GenreStatusRow(
        status=str(row[0]),
        source_artist=None if row[1] is None else str(row[1]),
        source_album=None if row[2] is None else str(row[2]),
    )


def set_genre_status(  # noqa: PLR0913 - cohesive keyword-only status payload
    conn: sqlite3.Connection,
    *,
    file_id: int,
    status: str,
    source_artist: str | None,
    source_album: str | None,
    now: str,
) -> None:
    """Insert or replace *file_id*'s terminal genre decision.

    ``source_artist``/``source_album`` record what the decision was computed against, so
    a later tag change can mark a ``'no_match'`` stale and re-processable.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO file_genre_status (
            file_id, status, source_artist, source_album, updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (file_id, status, source_artist, source_album, now),
    )


def delete_genre_status(conn: sqlite3.Connection, file_id: int) -> None:
    """Remove *file_id*'s terminal genre decision (no-op if none)."""
    conn.execute("DELETE FROM file_genre_status WHERE file_id = ?", (file_id,))


# --- "done" derivation + scope selection (PLAN — Selection set) ---------------------


def is_staged(conn: sqlite3.Connection, file_id: int) -> bool:
    """Return whether *file_id* has a pending change in ``tag_revisions_staged``."""
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM tag_revisions_staged WHERE file_id = ?)",
        (file_id,),
    ).fetchone()
    return bool(row[0])


def any_staged(conn: sqlite3.Connection) -> bool:
    """Return whether ANY file has a pending change in ``tag_revisions_staged``.

    The clean-staging-area guard for commit-level revert: rolling back with work still
    staged would interleave a revert with half-staged intent, so the revert refuses
    (git's "commit or stash first").
    """
    row = conn.execute("SELECT EXISTS(SELECT 1 FROM tag_revisions_staged)").fetchone()
    return bool(row[0])


def has_auto_revision(conn: sqlite3.Connection, file_id: int) -> bool:
    """Return whether *file_id* has a committed ``origin='auto'`` revision."""
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM tag_revisions WHERE file_id = ? AND origin = 'auto')",
        (file_id,),
    ).fetchone()
    return bool(row[0])


# The five user-facing genre workflow states. Two are stored (`no_match`/`manual`,
# rows in file_genre_status), three are derived (`staged`/`done` from the revision
# tables, `pending` = none of the above). Single source of truth for the
# ``list_files(genre_status=...)`` filter and the stats counts.
GENRE_WORKFLOW_STATUSES: Final = frozenset({"pending", "no_match", "manual", "staged", "done"})


def derived_genre_status(conn: sqlite3.Connection, file_id: int) -> str:
    """Return *file_id*'s genre workflow status: staged | done | no_match | manual | pending.

    THE canonical derivation, composed from the same predicates
    :func:`tagmend.engine.genres._select` skips on (keep the two in sync): a staged
    change wins, then a committed ``origin='auto'`` revision (``done``), then a stored
    ``file_genre_status`` row, else ``pending``. Reports the STORED status even when
    it is stale (identity changed since) — ``stage_genres`` still retries stale
    ``no_match`` rows; the listing surfaces the recorded decision so a human can
    judge it. Files with no artist tag count as ``pending`` (unprocessable until
    tagged, but not terminal).
    """
    if is_staged(conn, file_id):
        return "staged"
    if has_auto_revision(conn, file_id):
        return "done"
    decision = get_genre_status(conn, file_id)
    if decision is not None:
        return decision.status
    return "pending"


def genre_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Return file counts per genre workflow status, all five keys always present.

    One SQL pass over every tracked file. The ``CASE`` mirrors
    :func:`derived_genre_status` exactly (a test cross-checks the two — keep them in
    lockstep when either changes).
    """
    counts = dict.fromkeys(sorted(GENRE_WORKFLOW_STATUSES), 0)
    cursor = conn.execute(
        """
        SELECT
          CASE
            WHEN EXISTS (
              SELECT 1 FROM tag_revisions_staged s WHERE s.file_id = f.id
            ) THEN 'staged'
            WHEN EXISTS (
              SELECT 1 FROM tag_revisions r WHERE r.file_id = f.id AND r.origin = 'auto'
            ) THEN 'done'
            WHEN g.status IS NOT NULL THEN g.status
            ELSE 'pending'
          END AS workflow_status,
          COUNT(*)
        FROM files f
        LEFT JOIN file_genre_status g ON g.file_id = f.id
        GROUP BY workflow_status
        """,
    )
    for row in cursor.fetchall():
        counts[str(row[0])] = _as_int(row[1])
    return counts


def distinct_artists(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Return each distinct ``artist`` tag value with its file count, ordered by value."""
    cursor = conn.execute(
        """
        SELECT value, COUNT(DISTINCT file_id)
        FROM file_tags
        WHERE name = 'artist'
        GROUP BY value
        ORDER BY value
        """,
    )
    return [(str(row[0]), _as_int(row[1])) for row in cursor.fetchall()]


def files_in_scope(
    conn: sqlite3.Connection,
    *,
    artist: str | None = None,
    album: str | None = None,
    file_ids: list[int] | None = None,
) -> list[int]:
    """Return the file ids in scope, in ascending id order.

    Precedence mirrors the ``stage_genres`` scope rules:

    * *file_ids* given → the subset of those that actually exist (others dropped);
    * else *artist* given → files whose ``artist`` tag equals it, narrowed by *album*
      when given (both via ``file_tags`` joins on ``idx_file_tags_name_value``);
    * else → every tracked file.
    """
    if file_ids is not None:
        return _files_in_scope_by_ids(conn, file_ids)
    if artist is not None:
        return _files_in_scope_by_tags(conn, artist=artist, album=album)
    cursor = conn.execute("SELECT id FROM files ORDER BY id")
    return [_as_int(row[0]) for row in cursor.fetchall()]


def _files_in_scope_by_ids(conn: sqlite3.Connection, file_ids: list[int]) -> list[int]:
    """Return the subset of *file_ids* that exist in ``files``, in ascending id order."""
    if not file_ids:
        return []
    placeholders = ",".join("?" for _ in file_ids)
    cursor = conn.execute(
        f"SELECT id FROM files WHERE id IN ({placeholders}) ORDER BY id",  # noqa: S608
        tuple(file_ids),
    )
    return [_as_int(row[0]) for row in cursor.fetchall()]


def _files_in_scope_by_tags(
    conn: sqlite3.Connection,
    *,
    artist: str,
    album: str | None,
) -> list[int]:
    """Return file ids matching the ``artist`` tag (and ``album`` if given), id order."""
    if album is None:
        cursor = conn.execute(
            """
            SELECT DISTINCT a.file_id
            FROM file_tags a
            WHERE a.name = 'artist' AND a.value = ?
            ORDER BY a.file_id
            """,
            (artist,),
        )
        return [_as_int(row[0]) for row in cursor.fetchall()]
    cursor = conn.execute(
        """
        SELECT DISTINCT a.file_id
        FROM file_tags a
        JOIN file_tags b ON b.file_id = a.file_id
        WHERE a.name = 'artist' AND a.value = ?
          AND b.name = 'album' AND b.value = ?
        ORDER BY a.file_id
        """,
        (artist, album),
    )
    return [_as_int(row[0]) for row in cursor.fetchall()]
