"""Pure data access for the snapshot, the revision logs, and the staging areas.

Covers the ``files`` / ``file_tags`` snapshot, the append-only ``tag_revisions``,
``path_revisions``, ``sidecar_moves`` and ``cover_writes`` histories, and the
``tag_revisions_staged``, ``path_revisions_staged``, ``sidecar_moves_staged`` and
``cover_writes_staged`` staging areas (git's index). It also holds the Last.fm, MusicBrainz and
Cover Art Archive lookup caches, the tag-axis derived status, the ``file_mismatch_status``
decisions and the scope selector. Every function takes an open :class:`sqlite3.Connection` and
does one focused thing, with no scanning, tag reading or commit policy. That orchestration
lives in :mod:`tagmend.engine.library`, :mod:`tagmend.engine.versioning`,
:mod:`tagmend.engine.staging` and :mod:`tagmend.engine.paths`.
The ``commits``-table ops and the shared commit loop live in :mod:`tagmend.engine.commits`.
SQLite hands back ``Any``, so this module casts at the boundary and the rest of the engine stays
strictly typed.

All SQL uses ``?`` placeholders (never string-formatted values).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from tagmend.engine import axis, db, path_keys
from tagmend.engine.tags import MANAGED_SET_VERSION, TAG_READER_VERSION

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Collection


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
    reader_version: int


_FILE_COLUMNS = (
    "id, folder, filename, ext, size_bytes, mtime_ns, is_missing, tags_updated_at, reader_version"
)


def _row_to_file(row: tuple[object, ...]) -> FileRow:
    """Build a typed :class:`FileRow` from a raw sqlite tuple."""
    return FileRow(
        id=db.as_int(row[0]),
        folder=str(row[1]),
        filename=str(row[2]),
        ext=str(row[3]),
        size_bytes=None if row[4] is None else db.as_int(row[4]),
        mtime_ns=None if row[5] is None else db.as_int(row[5]),
        is_missing=bool(row[6]),
        tags_updated_at=None if row[7] is None else str(row[7]),
        reader_version=db.as_int(row[8]),
    )


def get_file(conn: sqlite3.Connection, folder: str, filename: str) -> FileRow | None:
    """Return the file row at ``(folder, filename)``, or ``None``.

    The lookup is by the platform path key (:func:`tagmend.engine.path_keys.file_path_key`), so
    on Windows a spelling that differs only by case or separator finds the same row.
    """
    cursor = conn.execute(
        f"SELECT {_FILE_COLUMNS} FROM files WHERE path_key = ?",  # noqa: S608
        (path_keys.file_path_key(folder, filename),),
    )
    row = cursor.fetchone()
    return None if row is None else _row_to_file(tuple(row))


def get_file_by_id(conn: sqlite3.Connection, file_id: int) -> FileRow | None:
    """Return the file row with the given stable ``id``, or ``None``."""
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
    """Insert a new file row (tags unread) and return its assigned ``id``.

    Stamps the current :data:`TAG_READER_VERSION` even though the tags are not read yet:
    a newly discovered file is not a leftover of an older reader, and its NULL
    ``tags_updated_at`` already makes the next scan read it.
    """
    cursor = conn.execute(
        """
        INSERT INTO files (
            folder, filename, ext, size_bytes, mtime_ns,
            is_missing, first_seen_at, updated_at, tags_updated_at, reader_version, path_key
        )
        VALUES (?, ?, ?, ?, ?, 0, ?, ?, NULL, ?, ?)
        """,
        (
            folder,
            filename,
            ext,
            size_bytes,
            mtime_ns,
            now,
            now,
            TAG_READER_VERSION,
            path_keys.file_path_key(folder, filename),
        ),
    )
    new_id = cursor.lastrowid
    if new_id is None:  # pragma: no cover - defensive, since an INTEGER PK always assigns one
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


def update_location(
    conn: sqlite3.Connection,
    file_id: int,
    *,
    folder: str,
    filename: str,
    now: str,
) -> None:
    """Record a new display spelling of the same path and bump ``updated_at``.

    ``path_key`` is left alone: the caller found this row by that key, so the new spelling
    names the same file.
    """
    conn.execute(
        "UPDATE files SET folder = ?, filename = ?, updated_at = ? WHERE id = ?",
        (folder, filename, now, file_id),
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


def stamp_reader_version(conn: sqlite3.Connection, file_id: int) -> None:
    """Mark *file_id*'s stored tags as produced by the current tag reader.

    Deliberately separate from :func:`replace_tags`: the scan stamps after every successful
    read, including the ~99% that find identical tags and write nothing. Folding it into
    :func:`replace_tags` would leave those rows stale forever and re-read them every scan.
    """
    conn.execute(
        "UPDATE files SET reader_version = ? WHERE id = ?",
        (TAG_READER_VERSION, file_id),
    )


def mark_reader_stale(conn: sqlite3.Connection, file_id: int) -> None:
    """Make the next incremental scan re-read *file_id*, whose new signature had no tag re-read."""
    conn.execute("UPDATE files SET reader_version = 0 WHERE id = ?", (file_id,))


def tracked_files_under(conn: sqlite3.Connection, root_key: str) -> list[FileRow]:
    """Return every tracked file whose folder is the folder keyed *root_key* or nested under it.

    One key-range query on ``idx_files_path_key`` (:func:`tagmend.engine.path_keys.subtree_bounds`),
    so ``_`` and ``%`` in a path are ordinary characters. Rows come back in id order.
    """
    low, high = path_keys.subtree_bounds(root_key)
    cursor = conn.execute(
        f"SELECT {_FILE_COLUMNS} FROM files WHERE path_key >= ? AND path_key < ? ORDER BY id",  # noqa: S608
        (low, high),
    )
    return [_row_to_file(tuple(row)) for row in cursor.fetchall()]


def list_files(conn: sqlite3.Connection, *, limit: int | None = None) -> list[FileRow]:
    """Return tracked files in stable id order, optionally capped at *limit* rows."""
    sql = f"SELECT {_FILE_COLUMNS} FROM files ORDER BY id"  # noqa: S608
    cursor = conn.execute(sql) if limit is None else conn.execute(f"{sql} LIMIT ?", (limit,))
    return [_row_to_file(tuple(row)) for row in cursor.fetchall()]


def compute_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """Return library-wide counts for ``get_library_stats``.

    The mismatch block needs the path comparator and the settings, so
    :func:`tagmend.engine.library.get_library_stats` adds it.
    """
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
        by_ext[str(row[0])] = db.as_int(row[1])

    return {
        "total_files": total_files,
        "missing": missing,
        "present": present,
        "unprocessed": unprocessed,
        "total_tag_values": total_tag_values,
        "by_ext": by_ext,
        **{tag_axis.name: status_counts(conn, tag_axis) for tag_axis in axis.TAG_AXES},
    }


def _scalar_int(conn: sqlite3.Connection, sql: str) -> int:
    """Run a single-value COUNT query and return it as an ``int``."""
    row = conn.execute(sql).fetchone()
    return 0 if row is None else db.as_int(row[0])


# --- tag_revisions (append-only history, PLAN.md §7) -------------------------------

# Valid ``origin`` values. ``scan`` = an observation of the file on disk (the version-0
# baseline, or a re-baseline under a newer managed set), ``auto``/``manual`` = normal writes,
# ``revert`` = a revert (which is itself an appended revision).
_REVISION_ORIGINS: Final = frozenset({"scan", "auto", "manual", "revert"})

_REVISION_COLUMNS = (
    "file_id, version, created_at, origin, reverted_to_version, commit_id, managed_tags, diff, "
    "note, managed_set"
)


@dataclass(frozen=True, slots=True)
class Revision:
    """One row from ``tag_revisions``, with its JSON columns parsed to typed maps."""

    file_id: int
    version: int
    created_at: str
    origin: str
    reverted_to_version: int | None
    commit_id: int | None
    managed_tags: dict[str, list[str]]
    diff: dict[str, dict[str, list[str]]]
    note: str | None
    managed_set: int

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for ``history_tags``."""
        return {
            "version": self.version,
            "created_at": self.created_at,
            "origin": self.origin,
            "reverted_to_version": self.reverted_to_version,
            "commit_id": self.commit_id,
            "managed_tags": self.managed_tags,
            "diff": self.diff,
            "note": self.note,
            "managed_set": self.managed_set,
        }


def _row_to_revision(row: tuple[object, ...]) -> Revision:
    """Build a typed :class:`Revision` from a raw sqlite tuple."""
    return Revision(
        file_id=db.as_int(row[0]),
        version=db.as_int(row[1]),
        created_at=str(row[2]),
        origin=str(row[3]),
        reverted_to_version=None if row[4] is None else db.as_int(row[4]),
        commit_id=None if row[5] is None else db.as_int(row[5]),
        managed_tags=_parse_tag_map(str(row[6])),
        diff=_parse_diff(str(row[7])),
        note=None if row[8] is None else str(row[8]),
        managed_set=db.as_int(row[9]),
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
    reverted_to_version: int | None = None,
    commit_id: int | None = None,
    note: str | None = None,
) -> None:
    """Append one revision row. It never updates or deletes.

    Every row is stamped with the CURRENT
    :data:`~tagmend.engine.tags.MANAGED_SET_VERSION`, which is what lets revert read a tag
    omitted from the snapshot as "empty then" rather than "not tracked then".

    *commit_id* groups this change with the other files in the same commit. It is ``None``
    for a ``scan`` observation (the version-0 baseline or a re-baseline), which no commit
    makes. Raises :class:`ValueError` for an unknown *origin*. The ``(file_id, version)`` PK
    rejects a duplicate version with :class:`sqlite3.IntegrityError`.
    """
    if origin not in _REVISION_ORIGINS:
        message = f"unknown revision origin: {origin!r}"
        raise ValueError(message)
    conn.execute(
        """
        INSERT INTO tag_revisions (
            file_id, version, created_at, origin, reverted_to_version,
            commit_id, managed_tags, diff, note, managed_set
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            version,
            now,
            origin,
            reverted_to_version,
            commit_id,
            _dump_json(managed_tags),
            _dump_json(diff),
            note,
            MANAGED_SET_VERSION,
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

    The commit's per-file change set: what :func:`tagmend.engine.versioning.revert_commit`
    classifies and undoes. Baselines (``commit_id`` NULL) never appear here.
    """
    cursor = conn.execute(
        f"SELECT {_REVISION_COLUMNS} FROM tag_revisions WHERE commit_id = ? ORDER BY file_id",  # noqa: S608
        (commit_id,),
    )
    return [_row_to_revision(tuple(row)) for row in cursor.fetchall()]


def latest_revision(conn: sqlite3.Connection, file_id: int) -> Revision | None:
    """Return *file_id*'s highest-version revision, or ``None`` if it has none yet."""
    cursor = conn.execute(
        f"SELECT {_REVISION_COLUMNS} FROM tag_revisions WHERE file_id = ? "  # noqa: S608
        "ORDER BY version DESC LIMIT 1",
        (file_id,),
    )
    row = cursor.fetchone()
    return None if row is None else _row_to_revision(tuple(row))


def revisions_after(conn: sqlite3.Connection, file_id: int, version: int) -> list[Revision]:
    """Return every revision of *file_id* newer than *version*, oldest first."""
    cursor = conn.execute(
        f"SELECT {_REVISION_COLUMNS} FROM tag_revisions "  # noqa: S608
        "WHERE file_id = ? AND version > ? ORDER BY version",
        (file_id, version),
    )
    return [_row_to_revision(tuple(row)) for row in cursor.fetchall()]


def max_version(conn: sqlite3.Connection, file_id: int) -> int | None:
    """Return *file_id*'s highest revision number, or ``None`` if it has none yet.

    ``None`` means no baseline has been captured. :func:`latest_revision` reads the current
    revision.
    """
    row = conn.execute(
        "SELECT MAX(version) FROM tag_revisions WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return db.as_int(row[0])


# --- staged tags (the git "index", PLAN.md §7) --------------------------------------

_STAGED_TAG_FIELDS: Final = (
    "file_id",
    "managed_tags",
    "origin",
    "note",
    "staged_at",
    "base_size_bytes",
    "base_mtime_ns",
    "changed_fields",
    "supplied_keys",
)
_STAGED_TAG_COLUMNS: Final = ", ".join(_STAGED_TAG_FIELDS)


@dataclass(frozen=True, slots=True)
class StagedTag:
    """One pending change from ``tag_revisions_staged`` (the staging area).

    ``base_size_bytes``/``base_mtime_ns`` are the file's signature when it was staged, so a
    commit can refuse a file edited since. Both are ``None`` on a row staged before v17.
    ``changed_fields`` names the fields the target changes against the tags on disk at stage
    time, ``None`` on a row staged before v21. ``supplied_keys`` names the keys whose values
    the caller supplied, ``None`` on a row staged before v23.
    """

    file_id: int
    managed_tags: dict[str, list[str]]
    origin: str
    note: str | None
    staged_at: str
    base_size_bytes: int | None
    base_mtime_ns: int | None
    changed_fields: frozenset[str] | None
    supplied_keys: frozenset[str] | None


def _json_key_set(raw: object) -> frozenset[str] | None:
    """Decode a nullable JSON list column into a key set."""
    return None if raw is None else frozenset(cast("list[str]", json.loads(str(raw))))


def _row_to_staged_tag(row: tuple[object, ...]) -> StagedTag:
    """Build a typed :class:`StagedTag` from a raw sqlite tuple."""
    return StagedTag(
        file_id=db.as_int(row[0]),
        managed_tags=_parse_tag_map(str(row[1])),
        origin=str(row[2]),
        note=None if row[3] is None else str(row[3]),
        staged_at=str(row[4]),
        base_size_bytes=None if row[5] is None else db.as_int(row[5]),
        base_mtime_ns=None if row[6] is None else db.as_int(row[6]),
        changed_fields=_json_key_set(row[7]),
        supplied_keys=_json_key_set(row[8]),
    )


def upsert_staged_tag(  # noqa: PLR0913 - cohesive keyword-only staging payload
    conn: sqlite3.Connection,
    *,
    file_id: int,
    managed_tags: dict[str, list[str]],
    origin: str,
    now: str,
    note: str | None = None,
    base_size_bytes: int | None = None,
    base_mtime_ns: int | None = None,
    changed_fields: Collection[str] | None = None,
    supplied_keys: Collection[str] | None = None,
) -> None:
    """Insert or replace the single pending change for *file_id*.

    A re-stage overwrites any prior pending change. The ``file_id`` PK keeps exactly one
    pending change per file (the latest staged target wins). *base_size_bytes* and
    *base_mtime_ns* record the file's signature at stage time. A ``None`` pair skips the
    commit's changed-since-stage check. *changed_fields* names the fields the target changes
    against the tags on disk at stage time. *supplied_keys* names the keys the caller supplied.
    """
    changed_json = None if changed_fields is None else _dump_json(sorted(changed_fields))
    supplied_json = None if supplied_keys is None else _dump_json(sorted(supplied_keys))
    placeholders = ", ".join("?" for _ in _STAGED_TAG_FIELDS)
    conn.execute(
        f"INSERT OR REPLACE INTO tag_revisions_staged ({_STAGED_TAG_COLUMNS}) "  # noqa: S608
        f"VALUES ({placeholders})",
        (
            file_id,
            _dump_json(managed_tags),
            origin,
            note,
            now,
            base_size_bytes,
            base_mtime_ns,
            changed_json,
            supplied_json,
        ),
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


def list_staged_tags_under(conn: sqlite3.Connection, root_key: str) -> list[StagedTag]:
    """Return pending changes whose file lives in the folder keyed *root_key* or under it.

    Joins ``files`` on the same key range as :func:`tracked_files_under`, in file_id order.
    """
    low, high = path_keys.subtree_bounds(root_key)
    columns = ", ".join(f"s.{name}" for name in _STAGED_TAG_FIELDS)
    cursor = conn.execute(
        f"SELECT {columns} FROM tag_revisions_staged s JOIN files f ON f.id = s.file_id "  # noqa: S608
        "WHERE f.path_key >= ? AND f.path_key < ? ORDER BY s.file_id",
        (low, high),
    )
    return [_row_to_staged_tag(tuple(row)) for row in cursor.fetchall()]


def delete_staged_tag(
    conn: sqlite3.Connection,
    file_id: int,
    *,
    staged_at: str | None = None,
) -> None:
    """Remove the pending change for *file_id* (no-op if none).

    *staged_at* removes it only while it is still the row staged then, so a row another
    process staged since survives.
    """
    if staged_at is None:
        conn.execute("DELETE FROM tag_revisions_staged WHERE file_id = ?", (file_id,))
        return
    conn.execute(
        "DELETE FROM tag_revisions_staged WHERE file_id = ? AND staged_at = ?",
        (file_id, staged_at),
    )


def staged_origins(conn: sqlite3.Connection, file_ids: list[int]) -> set[str]:
    """Return the distinct origins of the pending changes for *file_ids*."""
    if not file_ids:
        return set()
    placeholders = ",".join("?" for _ in file_ids)
    cursor = conn.execute(
        f"SELECT DISTINCT origin FROM tag_revisions_staged WHERE file_id IN ({placeholders})",  # noqa: S608
        tuple(file_ids),
    )
    return {str(row[0]) for row in cursor.fetchall()}


# --- lastfm_cache (persistent parsed-tag cache, PLAN.md Last.fm genre tagging) --------


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
    ``[name, weight]`` pairs. Pass ``found=False`` with ``tags=[]`` to negative-cache.
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
    return [(str(name), db.as_int(weight)) for name, weight in parsed]


# --- lastfm_correction_cache (persistent artist.getCorrection cache) ------------------


@dataclass(frozen=True, slots=True)
class LastfmCorrectionRow:
    """One cached ``artist.getCorrection`` answer: the canonical name and its MBID."""

    name: str | None
    mbid: str | None


def get_cached_correction(
    conn: sqlite3.Connection,
    request_key: str,
) -> tuple[bool, LastfmCorrectionRow] | None:
    """Return the cached correction for *request_key*, or ``None`` on a cache miss.

    A hit is ``(found, row)``. ``found=False`` is the negative-cache sentinel (no correction),
    while ``found=True`` carries the canonical name and optional MBID on *row*.
    """
    cursor = conn.execute(
        "SELECT found, name, mbid FROM lastfm_correction_cache WHERE request_key = ?",
        (request_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    correction = LastfmCorrectionRow(
        name=None if row[1] is None else str(row[1]),
        mbid=None if row[2] is None else str(row[2]),
    )
    return (bool(row[0]), correction)


def put_cached_correction(  # noqa: PLR0913 - cohesive keyword-only cache payload
    conn: sqlite3.Connection,
    *,
    request_key: str,
    found: bool,
    name: str | None,
    mbid: str | None,
    now: str,
) -> None:
    """Insert or replace the cached correction for *request_key*.

    Pass ``found=False`` with *name* and *mbid* ``None`` to negative-cache.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO lastfm_correction_cache
          (request_key, found, name, mbid, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (request_key, 1 if found else 0, name, mbid, now),
    )


# --- musicbrainz_release_group_cache (persistent release-group lookup cache) ----------


@dataclass(frozen=True, slots=True)
class MBReleaseGroupRow:
    """One cached MusicBrainz release-group lookup (a found album's resolved fields)."""

    album_title: str | None
    original_date: str | None
    release_group_mbid: str | None


def get_cached_mb_release_group(
    conn: sqlite3.Connection,
    request_key: str,
) -> tuple[bool, MBReleaseGroupRow] | None:
    """Return the cached MusicBrainz lookup for *request_key*, or ``None`` on a cache miss.

    ``None`` distinguishes a never-cached key from a cached negative result. A hit is
    ``(found, row)``: ``found=False`` is the negative-cache sentinel (no usable Album
    release group), while ``found=True`` carries the resolved fields on *row*.
    """
    cursor = conn.execute(
        """
        SELECT found, album_title, original_date, release_group_mbid
        FROM musicbrainz_release_group_cache WHERE request_key = ?
        """,
        (request_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    found = bool(row[0])
    album = MBReleaseGroupRow(
        album_title=None if row[1] is None else str(row[1]),
        original_date=None if row[2] is None else str(row[2]),
        release_group_mbid=None if row[3] is None else str(row[3]),
    )
    return (found, album)


def put_cached_mb_release_group(  # noqa: PLR0913 - cohesive keyword-only cache payload
    conn: sqlite3.Connection,
    *,
    request_key: str,
    found: bool,
    album_title: str | None,
    original_date: str | None,
    release_group_mbid: str | None,
    now: str,
) -> None:
    """Insert or replace the cached MusicBrainz lookup for *request_key*.

    A re-fetch overwrites any prior cached value. Pass ``found=False`` with the resolved
    columns ``None`` to negative-cache (no usable Album release group).
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO musicbrainz_release_group_cache (
            request_key, found, album_title, original_date, release_group_mbid, fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            request_key,
            1 if found else 0,
            album_title,
            original_date,
            release_group_mbid,
            now,
        ),
    )


# --- musicbrainz_recording_cache (persistent recording-search cache, album-gaps tier) ----


@dataclass(frozen=True, slots=True)
class MBRecordingRow:
    """One cached MusicBrainz recording-search lookup (a found recording's resolved fields)."""

    album_title: str | None


def get_cached_mb_recording(
    conn: sqlite3.Connection,
    request_key: str,
) -> tuple[bool, MBRecordingRow] | None:
    """Return the cached recording-search lookup for *request_key*, or ``None`` on a miss.

    ``None`` distinguishes a never-cached key from a cached negative result. A hit is
    ``(found, row)``: ``found=False`` is the negative-cache sentinel (no usable Album release
    group for the recording), while ``found=True`` carries the resolved fields on *row*.
    """
    cursor = conn.execute(
        """
        SELECT found, album_title FROM musicbrainz_recording_cache WHERE request_key = ?
        """,
        (request_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    found = bool(row[0])
    recording = MBRecordingRow(album_title=None if row[1] is None else str(row[1]))
    return (found, recording)


def put_cached_mb_recording(
    conn: sqlite3.Connection,
    *,
    request_key: str,
    found: bool,
    album_title: str | None,
    now: str,
) -> None:
    """Insert or replace the cached recording-search lookup for *request_key*.

    A re-fetch overwrites any prior cached value. Pass ``found=False`` with the resolved
    columns ``None`` to negative-cache (no usable Album release group for the recording).
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO musicbrainz_recording_cache (
            request_key, found, album_title, fetched_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (request_key, 1 if found else 0, album_title, now),
    )


# --- musicbrainz_artist_cache (persistent MBID -> canonical-name lookups) --------------


@dataclass(frozen=True, slots=True)
class MBArtistRow:
    """One cached MusicBrainz artist lookup (the canonical name + its alias set)."""

    name: str | None
    sort_name: str | None
    disambiguation: str | None
    aliases: tuple[str, ...]


def get_cached_mb_artist(
    conn: sqlite3.Connection,
    request_key: str,
) -> tuple[bool, MBArtistRow] | None:
    """Return the cached artist lookup for *request_key*, or ``None`` on a miss.

    ``None`` distinguishes a never-cached key from a cached negative result. A hit is
    ``(found, row)``: ``found=False`` is the negative-cache sentinel (MusicBrainz has no
    artist under that MBID), while ``found=True`` carries the resolved fields on *row*.
    """
    cursor = conn.execute(
        """
        SELECT found, name, sort_name, disambiguation, aliases
        FROM musicbrainz_artist_cache WHERE request_key = ?
        """,
        (request_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return (
        bool(row[0]),
        MBArtistRow(
            name=None if row[1] is None else str(row[1]),
            sort_name=None if row[2] is None else str(row[2]),
            disambiguation=None if row[3] is None else str(row[3]),
            aliases=_decode_aliases(row[4]),
        ),
    )


def _decode_aliases(raw: object) -> tuple[str, ...]:
    """Decode the stored alias JSON array, tolerating a null or malformed column."""
    if raw is None:
        return ()
    try:
        decoded = json.loads(str(raw))
    except json.JSONDecodeError:
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(str(name) for name in decoded)


def put_cached_mb_artist(  # noqa: PLR0913 - cohesive keyword-only cache payload
    conn: sqlite3.Connection,
    *,
    request_key: str,
    found: bool,
    name: str | None,
    sort_name: str | None,
    disambiguation: str | None,
    aliases: tuple[str, ...],
    now: str,
) -> None:
    """Insert or replace the cached artist lookup for *request_key*.

    Pass ``found=False`` with the resolved columns empty to negative-cache an MBID
    MusicBrainz does not know.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO musicbrainz_artist_cache (
            request_key, found, name, sort_name, disambiguation, aliases, fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_key,
            1 if found else 0,
            name,
            sort_name,
            disambiguation,
            json.dumps(list(aliases)),
            now,
        ),
    )


# --- musicbrainz_release_cache (persistent release + tracklist lookups) ---------------
#
# A release is a nested document (media, each with tracks), so the parsed form is stored as
# one JSON payload rather than shredded across three tables. Nothing queries inside it: the
# key is always the release MBID, and the caller wants the whole tracklist.


def get_cached_mb_release(
    conn: sqlite3.Connection,
    request_key: str,
) -> tuple[bool, str | None] | None:
    """Return the cached release lookup for *request_key*, or ``None`` on a miss.

    A hit is ``(found, payload)``: ``found=False`` is the negative-cache sentinel (no release
    under that MBID), while ``found=True`` carries the parsed release as a JSON string.
    """
    cursor = conn.execute(
        "SELECT found, payload FROM musicbrainz_release_cache WHERE request_key = ?",
        (request_key,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return (bool(row[0]), None if row[1] is None else str(row[1]))


def put_cached_mb_release(
    conn: sqlite3.Connection,
    *,
    request_key: str,
    found: bool,
    payload: str | None,
    now: str,
) -> None:
    """Insert or replace the cached release lookup for *request_key*."""
    conn.execute(
        """
        INSERT OR REPLACE INTO musicbrainz_release_cache (request_key, found, payload, fetched_at)
        VALUES (?, ?, ?, ?)
        """,
        (request_key, 1 if found else 0, payload, now),
    )


# --- coverart_cache (persistent Cover Art Archive listing lookups) --------------------


def get_cached_coverart(
    conn: sqlite3.Connection,
    request_key: str,
) -> tuple[bool, str | None, str] | None:
    """Return the cached listing lookup for *request_key*, or ``None`` on a miss.

    A hit is ``(found, payload, fetched_at)``. The caller expires a not-found row by its age.
    """
    row = conn.execute(
        "SELECT found, payload, fetched_at FROM coverart_cache WHERE request_key = ?",
        (request_key,),
    ).fetchone()
    if row is None:
        return None
    return (bool(row[0]), None if row[1] is None else str(row[1]), str(row[2]))


def put_cached_coverart(
    conn: sqlite3.Connection,
    *,
    request_key: str,
    found: bool,
    payload: str | None,
    now: str,
) -> None:
    """Insert or replace the cached listing lookup for *request_key*."""
    conn.execute(
        """
        INSERT OR REPLACE INTO coverart_cache (request_key, found, payload, fetched_at)
        VALUES (?, ?, ?, ?)
        """,
        (request_key, 1 if found else 0, payload, now),
    )


# --- staging-area guards (every staging table) ---------------------------------------


def is_staged(conn: sqlite3.Connection, file_id: int) -> bool:
    """Return whether *file_id* has a pending change in ``tag_revisions_staged``."""
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM tag_revisions_staged WHERE file_id = ?)",
        (file_id,),
    ).fetchone()
    return bool(row[0])


def any_staged(conn: sqlite3.Connection) -> bool:
    """Return whether ANY tag change, file move, sidecar move or cover is staged.

    The clean-staging-area guard for commit-level revert and the resolvers: rolling back with
    work still staged would interleave a revert with half-staged intent, so the revert refuses
    (git's "commit or stash first").
    """
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM tag_revisions_staged) "
        "OR EXISTS(SELECT 1 FROM path_revisions_staged) "
        "OR EXISTS(SELECT 1 FROM sidecar_moves_staged) "
        "OR EXISTS(SELECT 1 FROM cover_writes_staged)",
    ).fetchone()
    return bool(row[0])


def any_move_staged(conn: sqlite3.Connection) -> bool:
    """Return whether any file or sidecar has a pending move."""
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM path_revisions_staged) "
        "OR EXISTS(SELECT 1 FROM sidecar_moves_staged)",
    ).fetchone()
    return bool(row[0])


# --- file_<axis>_status: the tag-axis classifier (genre, artist, year, song) ----------
#
# The row access lives in :mod:`tagmend.engine.axis`. This section derives the user-facing
# status from a row, the staging area and the current tags of a present file.


def _staged_alters(
    staged: StagedTag | None,
    current: dict[str, list[str]],
    fields: tuple[str, ...],
) -> bool:
    """Whether *staged*'s target differs from *current* on any of *fields*."""
    if staged is None:
        return False
    return any(staged.managed_tags.get(name, []) != current.get(name, []) for name in fields)


# Re-exports of each tag axis's workflow states for the ``list_files(<axis>_status=...)`` filters.
GENRE_WORKFLOW_STATUSES: Final = axis.GENRE_AXIS.workflow_statuses
ARTIST_WORKFLOW_STATUSES: Final = axis.ARTIST_AXIS.workflow_statuses
YEAR_WORKFLOW_STATUSES: Final = axis.YEAR_AXIS.workflow_statuses
SONG_WORKFLOW_STATUSES: Final = axis.SONG_AXIS.workflow_statuses


def _outcome_holds(
    axis_: axis.Axis,
    row: axis.OutcomeRow,
    identity: axis.Identity,
    tags: dict[str, list[str]],
) -> bool:
    """Whether a resolver outcome still describes the file: both snapshots match its tags."""
    if row.status not in axis.RESOLVER_OUTCOMES or row.identity != identity:
        return False
    return row.values is None or row.values == axis.field_values(axis_, tags)


def derived_status(conn: sqlite3.Connection, axis_: axis.Axis, file_id: int) -> str:
    """Return *file_id*'s workflow status on the tag *axis_*. First match wins.

    1. A staged change alters one of :attr:`~tagmend.engine.axis.Axis.fields`: ``staged``.
    2. The row is ``manual``: ``manual``, sticky until ``reset_<axis>_status``.
    3. The axis has no identity for the current tags: ``no_identity``.
    4. The row is ``done`` or ``no_match`` and both its snapshots match the current tags (a
       NULL value snapshot matches): the row's status.
    5. Otherwise: ``pending``.

    A revert, a rescan after an external edit, an unstage and an identity change therefore
    re-open a file with no writer. Callers restrict the domain to present files.
    """
    tags = get_tags(conn, file_id)
    if _staged_alters(get_staged_tag(conn, file_id), tags, axis_.fields):
        return "staged"
    row = axis.get_outcome(conn, axis_, file_id)
    if row is not None and row.status == "manual":
        return "manual"
    identity = axis.identity_of(axis_, tags)
    if identity is None:
        return "no_identity"
    if row is not None and _outcome_holds(axis_, row, identity, tags):
        return row.status
    return "pending"


def present_file_ids(conn: sqlite3.Connection) -> list[int]:
    """Return the id of every file the last scan found on disk, in ascending id order."""
    cursor = conn.execute("SELECT id FROM files WHERE is_missing = 0 ORDER BY id")
    return [db.as_int(row[0]) for row in cursor.fetchall()]


def status_counts(conn: sqlite3.Connection, axis_: axis.Axis) -> dict[str, int]:
    """Tally :func:`derived_status` over every present file for the tag *axis_*.

    Every workflow-status key is present (zero-filled), and the counts sum to the present-file
    count, because a missing file has no axis status.
    """
    counts = dict.fromkeys(sorted(axis_.workflow_statuses), 0)
    for file_id in present_file_ids(conn):
        counts[derived_status(conn, axis_, file_id)] += 1
    return counts


def pending_file_ids(
    conn: sqlite3.Connection,
    axis_: axis.Axis,
    file_ids: list[int],
) -> list[int]:
    """Return the present files of *file_ids* that derive ``pending`` on *axis_*, in id order.

    The one selection every resolver draws from, and its recount of what is left.
    """
    missing = missing_file_ids(conn)
    return [
        file_id
        for file_id in sorted(file_ids)
        if file_id not in missing and derived_status(conn, axis_, file_id) == "pending"
    ]


def record_outcome(
    conn: sqlite3.Connection,
    axis_: axis.Axis,
    *,
    file_id: int,
    status: str,
    now: str,
) -> None:
    """Write a resolver outcome snapshotting the tags the resolver judged plus what it staged.

    The snapshot is the mirror tags overlaid with the staged row's ``supplied_keys`` values. A
    field or identity the file changed on disk since the scan then differs from the commit's
    read-back, so the commit does not re-stamp the row and the next rescan re-opens the file. A
    row staged before v23 names no supplied keys, so its whole staged target is the snapshot.
    """
    mirror = get_tags(conn, file_id)
    staged = get_staged_tag(conn, file_id)
    supplied_keys = None if staged is None else staged.supplied_keys
    tags = mirror
    if staged is not None and supplied_keys is None:
        tags = staged.managed_tags
    elif staged is not None and supplied_keys is not None:
        supplied = {key: staged.managed_tags.get(key, []) for key in supplied_keys}
        tags = mirror | supplied
    axis.put_outcome(conn, axis_, file_id=file_id, status=status, tags=tags, now=now)


# --- file_mismatch_status (one path decision per file) -------------------------------

# The mismatch axis's statuses. The two decisions are stored rows, and ``pending`` means no
# decision in force. :mod:`tagmend.engine.mismatch` derives which one a file reads.
MISMATCH_WORKFLOW_STATUSES: Final = axis.MISMATCH_AXIS.workflow_statuses


@dataclass(frozen=True, slots=True)
class MismatchStatusRow:
    """One ``file_mismatch_status`` row, holding a decision and the snapshot that binds it.

    ``covers`` names the comparisons and classes the file flagged when the decision was set.
    ``tags`` holds their tag values then, a blank tag as ``None``. ``path_version`` is the
    file's highest ``path_revisions`` version then, and ``folder_key`` the key of the folder a
    ``legit_ignore`` row keeps. Both are ``None`` on a row with no snapshot, which binds nothing.
    """

    status: str
    covers: tuple[str, ...]
    tags: dict[str, str | None]
    path_version: int | None
    folder_key: str | None

    def snapshot(self) -> dict[str, object]:
        """Return the JSON snapshot ``source_value`` holds, ``folder_key`` only when set."""
        snapshot: dict[str, object] = {
            "covers": list(self.covers),
            "tags": self.tags,
            "path_version": self.path_version,
        }
        if self.folder_key is not None:
            snapshot["folder_key"] = self.folder_key
        return snapshot


def _mismatch_row(status: object, raw_snapshot: object) -> MismatchStatusRow:
    """Build a :class:`MismatchStatusRow` from a raw status and ``source_value``."""
    snapshot = (
        {} if raw_snapshot is None else cast("dict[str, object]", json.loads(str(raw_snapshot)))
    )
    covers = cast("list[str]", snapshot.get("covers", []))
    tags = cast("dict[str, str | None]", snapshot.get("tags", {}))
    path_version = snapshot.get("path_version")
    folder_key = snapshot.get("folder_key")
    return MismatchStatusRow(
        status=str(status),
        covers=tuple(covers),
        tags=dict(tags),
        path_version=None if path_version is None else db.as_int(path_version),
        folder_key=None if folder_key is None else str(folder_key),
    )


def get_mismatch_status(conn: sqlite3.Connection, file_id: int) -> MismatchStatusRow | None:
    """Return *file_id*'s stored path decision, or ``None`` if it has none."""
    row = conn.execute(
        "SELECT status, source_value FROM file_mismatch_status WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    return None if row is None else _mismatch_row(row[0], row[1])


def set_mismatch_status(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    row: MismatchStatusRow,
    now: str,
) -> None:
    """Insert or replace *file_id*'s stored path decision with *row*."""
    conn.execute(
        """
        INSERT OR REPLACE INTO file_mismatch_status (file_id, status, source_value, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (file_id, row.status, _dump_json(row.snapshot()), now),
    )


def delete_mismatch_status(conn: sqlite3.Connection, file_id: int) -> None:
    """Remove *file_id*'s stored path decision (no-op if none)."""
    axis.delete_status(conn, axis.MISMATCH_AXIS, file_id)


def load_mismatch_statuses(conn: sqlite3.Connection) -> dict[int, MismatchStatusRow]:
    """Return every stored path decision keyed by ``file_id``, in one ``SELECT``."""
    cursor = conn.execute("SELECT file_id, status, source_value FROM file_mismatch_status")
    return {db.as_int(row[0]): _mismatch_row(row[1], row[2]) for row in cursor.fetchall()}


# --- path_revisions (append-only location history) ---------------------------------


def insert_path_revision(  # noqa: PLR0913 - cohesive append-only revision payload
    conn: sqlite3.Connection,
    *,
    file_id: int,
    version: int,
    commit_id: int | None,
    origin: str,
    from_path: str,
    to_path: str,
    now: str,
    reverted_to_version: int | None = None,
    note: str | None = None,
) -> None:
    """Append one ``path_revisions`` row. The append-only triggers refuse any rewrite."""
    conn.execute(
        """
        INSERT INTO path_revisions
          (file_id, version, commit_id, created_at, origin, reverted_to_version, from_path, to_path,
           note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (file_id, version, commit_id, now, origin, reverted_to_version, from_path, to_path, note),
    )


_PATH_REVISION_COLUMNS = (
    "file_id, version, commit_id, created_at, origin, reverted_to_version, from_path, to_path, note"
)


@dataclass(frozen=True, slots=True)
class PathRevision:
    """One ``path_revisions`` row. Both paths are relative to ``music_path``."""

    file_id: int
    version: int
    commit_id: int | None
    created_at: str
    origin: str
    reverted_to_version: int | None
    from_path: str
    to_path: str
    note: str | None

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for ``history_paths``."""
        return {
            "version": self.version,
            "created_at": self.created_at,
            "origin": self.origin,
            "reverted_to_version": self.reverted_to_version,
            "commit_id": self.commit_id,
            "from_path": self.from_path,
            "to_path": self.to_path,
            "note": self.note,
        }


def _row_to_path_revision(row: tuple[object, ...]) -> PathRevision:
    """Build a typed :class:`PathRevision` from a raw sqlite tuple."""
    return PathRevision(
        file_id=db.as_int(row[0]),
        version=db.as_int(row[1]),
        commit_id=None if row[2] is None else db.as_int(row[2]),
        created_at=str(row[3]),
        origin=str(row[4]),
        reverted_to_version=None if row[5] is None else db.as_int(row[5]),
        from_path=str(row[6]),
        to_path=str(row[7]),
        note=None if row[8] is None else str(row[8]),
    )


def get_path_revisions(conn: sqlite3.Connection, file_id: int) -> list[PathRevision]:
    """Return *file_id*'s location history, oldest (version 0) first."""
    cursor = conn.execute(
        f"SELECT {_PATH_REVISION_COLUMNS} FROM path_revisions "  # noqa: S608
        "WHERE file_id = ? ORDER BY version",
        (file_id,),
    )
    return [_row_to_path_revision(tuple(row)) for row in cursor.fetchall()]


def path_revisions_for_commit(conn: sqlite3.Connection, commit_id: int) -> list[PathRevision]:
    """Return every ``path_revisions`` row *commit_id* appended, in ``file_id`` order."""
    cursor = conn.execute(
        f"SELECT {_PATH_REVISION_COLUMNS} FROM path_revisions "  # noqa: S608
        "WHERE commit_id = ? ORDER BY file_id",
        (commit_id,),
    )
    return [_row_to_path_revision(tuple(row)) for row in cursor.fetchall()]


def commit_log_counts(conn: sqlite3.Connection, commit_id: int) -> dict[str, int]:
    """Return the rows each revision log holds for *commit_id*, naming only non-empty logs."""
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM tag_revisions WHERE commit_id = ?), "
        "(SELECT COUNT(*) FROM path_revisions WHERE commit_id = ?), "
        "(SELECT COUNT(*) FROM sidecar_moves WHERE commit_id = ?), "
        "(SELECT COUNT(*) FROM cover_writes WHERE commit_id = ?)",
        (commit_id, commit_id, commit_id, commit_id),
    ).fetchone()
    counts = {
        "tag_revisions": db.as_int(row[0]),
        "path_revisions": db.as_int(row[1]),
        "sidecar_moves": db.as_int(row[2]),
        "cover_writes": db.as_int(row[3]),
    }
    return {log: count for log, count in counts.items() if count}


def path_versions(conn: sqlite3.Connection) -> dict[int, int]:
    """Return each file's highest ``path_revisions`` version. A file with none is absent."""
    cursor = conn.execute("SELECT file_id, MAX(version) FROM path_revisions GROUP BY file_id")
    return {db.as_int(row[0]): db.as_int(row[1]) for row in cursor.fetchall()}


def path_revision_sources(conn: sqlite3.Connection) -> list[str]:
    """Return every distinct ``from_path`` the ``path_revisions`` log holds."""
    cursor = conn.execute("SELECT DISTINCT from_path FROM path_revisions")
    return [str(row[0]) for row in cursor.fetchall()]


def relocate_file(
    conn: sqlite3.Connection,
    file_id: int,
    *,
    folder: str,
    filename: str,
    now: str,
) -> None:
    """Point *file_id*'s row at a new path, its path key included, and bump ``updated_at``.

    ``idx_files_path_key`` raises :class:`sqlite3.IntegrityError` when another row holds the key.
    """
    conn.execute(
        "UPDATE files SET folder = ?, filename = ?, path_key = ?, updated_at = ? WHERE id = ?",
        (folder, filename, path_keys.file_path_key(folder, filename), now, file_id),
    )


def file_id_at_key(conn: sqlite3.Connection, key: str) -> int | None:
    """Return the id of the file row whose ``path_key`` is *key*, or ``None``."""
    row = conn.execute("SELECT id FROM files WHERE path_key = ?", (key,)).fetchone()
    return None if row is None else db.as_int(row[0])


# --- path_revisions_staged (the path staging area) -----------------------------------

_STAGED_PATH_FIELDS: Final = (
    "file_id",
    "to_path",
    "to_key",
    "origin",
    "note",
    "staged_at",
    "base_size_bytes",
    "base_mtime_ns",
    "reverted_to_version",
)
_STAGED_PATH_COLUMNS: Final = ", ".join(_STAGED_PATH_FIELDS)


@dataclass(frozen=True, slots=True)
class StagedPath:
    """One pending move. ``to_path`` and ``to_key`` are relative to ``music_path``.

    ``base_size_bytes``/``base_mtime_ns`` are the file's signature at stage time. Every write
    fills them and ``to_key``, so ``None`` appears only on a row an earlier schema held.
    """

    file_id: int
    to_path: str
    to_key: str | None
    origin: str
    note: str | None
    staged_at: str
    base_size_bytes: int | None
    base_mtime_ns: int | None
    reverted_to_version: int | None


def _row_to_staged_path(row: tuple[object, ...]) -> StagedPath:
    """Build a typed :class:`StagedPath` from a raw sqlite tuple."""
    return StagedPath(
        file_id=db.as_int(row[0]),
        to_path=str(row[1]),
        to_key=None if row[2] is None else str(row[2]),
        origin=str(row[3]),
        note=None if row[4] is None else str(row[4]),
        staged_at=str(row[5]),
        base_size_bytes=None if row[6] is None else db.as_int(row[6]),
        base_mtime_ns=None if row[7] is None else db.as_int(row[7]),
        reverted_to_version=None if row[8] is None else db.as_int(row[8]),
    )


def upsert_staged_path(conn: sqlite3.Connection, staged: StagedPath) -> None:
    """Insert *staged*, or replace the pending move of its file.

    An upsert on ``file_id`` rather than ``INSERT OR REPLACE``, which would silently delete
    another file's row holding the same ``to_key``. That conflict raises
    :class:`sqlite3.IntegrityError` instead.
    """
    placeholders = ", ".join("?" for _ in _STAGED_PATH_FIELDS)
    conn.execute(
        f"""
        INSERT INTO path_revisions_staged ({_STAGED_PATH_COLUMNS})
        VALUES ({placeholders})
        ON CONFLICT(file_id) DO UPDATE SET
          to_path = excluded.to_path, to_key = excluded.to_key, origin = excluded.origin,
          note = excluded.note, staged_at = excluded.staged_at,
          base_size_bytes = excluded.base_size_bytes, base_mtime_ns = excluded.base_mtime_ns,
          reverted_to_version = excluded.reverted_to_version
        """,  # noqa: S608
        (
            staged.file_id,
            staged.to_path,
            staged.to_key,
            staged.origin,
            staged.note,
            staged.staged_at,
            staged.base_size_bytes,
            staged.base_mtime_ns,
            staged.reverted_to_version,
        ),
    )


def get_staged_path(conn: sqlite3.Connection, file_id: int) -> StagedPath | None:
    """Return the pending move for *file_id*, or ``None``."""
    row = conn.execute(
        f"SELECT {_STAGED_PATH_COLUMNS} FROM path_revisions_staged WHERE file_id = ?",  # noqa: S608
        (file_id,),
    ).fetchone()
    return None if row is None else _row_to_staged_path(tuple(row))


def list_staged_paths(conn: sqlite3.Connection) -> list[StagedPath]:
    """Return every pending move, in file_id order."""
    cursor = conn.execute(
        f"SELECT {_STAGED_PATH_COLUMNS} FROM path_revisions_staged ORDER BY file_id",  # noqa: S608
    )
    return [_row_to_staged_path(tuple(row)) for row in cursor.fetchall()]


def list_staged_paths_under(conn: sqlite3.Connection, root_key: str) -> list[StagedPath]:
    """Return pending moves whose file sits in the folder keyed *root_key* or under it.

    The ``files`` row reads the source until the move commits, so this matches the source.
    """
    low, high = path_keys.subtree_bounds(root_key)
    columns = ", ".join(f"s.{name}" for name in _STAGED_PATH_FIELDS)
    cursor = conn.execute(
        f"SELECT {columns} FROM path_revisions_staged s JOIN files f ON f.id = s.file_id "  # noqa: S608
        "WHERE f.path_key >= ? AND f.path_key < ? ORDER BY s.file_id",
        (low, high),
    )
    return [_row_to_staged_path(tuple(row)) for row in cursor.fetchall()]


def staged_file_id_at_target(conn: sqlite3.Connection, to_key: str) -> int | None:
    """Return the file whose pending move targets the relative key *to_key*, or ``None``."""
    row = conn.execute(
        "SELECT file_id FROM path_revisions_staged WHERE to_key = ?",
        (to_key,),
    ).fetchone()
    return None if row is None else db.as_int(row[0])


def delete_staged_path(conn: sqlite3.Connection, file_id: int) -> None:
    """Remove the pending move for *file_id* (no-op if none)."""
    conn.execute("DELETE FROM path_revisions_staged WHERE file_id = ?", (file_id,))


def staged_path_file_ids(conn: sqlite3.Connection, file_ids: list[int]) -> list[int]:
    """Return the ids of *file_ids* that hold a ``path_revisions_staged`` row, in id order."""
    if not file_ids:
        return []
    placeholders = ",".join("?" for _ in file_ids)
    cursor = conn.execute(
        f"SELECT file_id FROM path_revisions_staged WHERE file_id IN ({placeholders}) "  # noqa: S608
        "ORDER BY file_id",
        tuple(file_ids),
    )
    return [db.as_int(row[0]) for row in cursor.fetchall()]


# --- sidecar_moves_staged (the non-audio files of a moving album) ----------------------

_SIDECAR_STAGED_FIELDS: Final = (
    "from_key",
    "from_path",
    "to_path",
    "to_key",
    "unit_key",
    "base_size_bytes",
    "base_mtime_ns",
    "origin",
    "reverted_from",
    "note",
    "staged_at",
)
_SIDECAR_STAGED_COLUMNS: Final = ", ".join(_SIDECAR_STAGED_FIELDS)


@dataclass(frozen=True, slots=True)
class StagedSidecar:
    """One pending sidecar move. Paths and keys are relative to ``music_path``.

    ``unit_key`` is the key of the album folder the sidecar sits under, whose audio it follows.
    ``base_size_bytes``/``base_mtime_ns`` are its signature when staged. ``reverted_from`` is the
    ``sidecar_moves`` id a revert row undoes.
    """

    from_key: str
    from_path: str
    to_path: str
    to_key: str
    unit_key: str
    base_size_bytes: int
    base_mtime_ns: int
    origin: str
    reverted_from: int | None
    note: str | None
    staged_at: str


def _row_to_staged_sidecar(row: tuple[object, ...]) -> StagedSidecar:
    """Build a typed :class:`StagedSidecar` from a raw sqlite tuple."""
    return StagedSidecar(
        from_key=str(row[0]),
        from_path=str(row[1]),
        to_path=str(row[2]),
        to_key=str(row[3]),
        unit_key=str(row[4]),
        base_size_bytes=db.as_int(row[5]),
        base_mtime_ns=db.as_int(row[6]),
        origin=str(row[7]),
        reverted_from=None if row[8] is None else db.as_int(row[8]),
        note=None if row[9] is None else str(row[9]),
        staged_at=str(row[10]),
    )


def insert_staged_sidecar(conn: sqlite3.Connection, staged: StagedSidecar) -> None:
    """Stage one sidecar move. A second row with the same source or target raises."""
    placeholders = ", ".join("?" for _ in _SIDECAR_STAGED_FIELDS)
    conn.execute(
        f"INSERT INTO sidecar_moves_staged ({_SIDECAR_STAGED_COLUMNS}) "  # noqa: S608
        f"VALUES ({placeholders})",
        (
            staged.from_key,
            staged.from_path,
            staged.to_path,
            staged.to_key,
            staged.unit_key,
            staged.base_size_bytes,
            staged.base_mtime_ns,
            staged.origin,
            staged.reverted_from,
            staged.note,
            staged.staged_at,
        ),
    )


def list_staged_sidecars(conn: sqlite3.Connection) -> list[StagedSidecar]:
    """Return every pending sidecar move, in source-key order."""
    cursor = conn.execute(
        f"SELECT {_SIDECAR_STAGED_COLUMNS} FROM sidecar_moves_staged ORDER BY from_key",  # noqa: S608
    )
    return [_row_to_staged_sidecar(tuple(row)) for row in cursor.fetchall()]


def set_staged_sidecar_signature(
    conn: sqlite3.Connection,
    from_key: str,
    signature: tuple[int, int],
) -> None:
    """Record *signature* as the staged signature of the sidecar move keyed *from_key*."""
    conn.execute(
        "UPDATE sidecar_moves_staged SET base_size_bytes = ?, base_mtime_ns = ? WHERE from_key = ?",
        (*signature, from_key),
    )


def delete_staged_sidecar(conn: sqlite3.Connection, from_key: str) -> None:
    """Remove the pending sidecar move keyed *from_key* (no-op if none)."""
    conn.execute("DELETE FROM sidecar_moves_staged WHERE from_key = ?", (from_key,))


# --- cover_writes_staged (one pending cover image per album folder) ----------------------

_STAGED_COVER_FIELDS: Final = (
    "target_key",
    "target_path",
    "folder_key",
    "album_label",
    "file_ids",
    "source_kind",
    "source_ref",
    "sha256",
    "size_bytes",
    "image_format",
    "width",
    "height",
    "origin",
    "note",
    "staged_at",
)
_STAGED_COVER_COLUMNS: Final = ", ".join(_STAGED_COVER_FIELDS)


@dataclass(frozen=True, slots=True)
class StagedCover:
    """One pending cover image, without its bytes. Paths and keys are relative to ``music_path``.

    ``folder_key`` is the key of the album's target folder. ``file_ids`` are the album's files
    when it was staged.
    """

    target_key: str
    target_path: str
    folder_key: str
    album_label: str
    file_ids: tuple[int, ...]
    source_kind: str
    source_ref: str
    sha256: str
    size_bytes: int
    image_format: str
    width: int
    height: int
    origin: str
    note: str | None
    staged_at: str


def _row_to_staged_cover(row: tuple[object, ...]) -> StagedCover:
    """Build a typed :class:`StagedCover` from a raw sqlite tuple."""
    file_ids = cast("list[int]", json.loads(str(row[4])))
    return StagedCover(
        target_key=str(row[0]),
        target_path=str(row[1]),
        folder_key=str(row[2]),
        album_label=str(row[3]),
        file_ids=tuple(file_ids),
        source_kind=str(row[5]),
        source_ref=str(row[6]),
        sha256=str(row[7]),
        size_bytes=db.as_int(row[8]),
        image_format=str(row[9]),
        width=db.as_int(row[10]),
        height=db.as_int(row[11]),
        origin=str(row[12]),
        note=None if row[13] is None else str(row[13]),
        staged_at=str(row[14]),
    )


def insert_staged_cover(conn: sqlite3.Connection, staged: StagedCover, content: bytes) -> None:
    """Stage one cover image with its *content*. A second row with the same target raises."""
    placeholders = ", ".join("?" for _ in (*_STAGED_COVER_FIELDS, "content"))
    conn.execute(
        f"INSERT INTO cover_writes_staged ({_STAGED_COVER_COLUMNS}, content) "  # noqa: S608
        f"VALUES ({placeholders})",
        (
            staged.target_key,
            staged.target_path,
            staged.folder_key,
            staged.album_label,
            _dump_json(list(staged.file_ids)),
            staged.source_kind,
            staged.source_ref,
            staged.sha256,
            staged.size_bytes,
            staged.image_format,
            staged.width,
            staged.height,
            staged.origin,
            staged.note,
            staged.staged_at,
            content,
        ),
    )


def list_staged_covers(conn: sqlite3.Connection) -> list[StagedCover]:
    """Return every pending cover, without its bytes, in target-key order."""
    cursor = conn.execute(
        f"SELECT {_STAGED_COVER_COLUMNS} FROM cover_writes_staged ORDER BY target_key",  # noqa: S608
    )
    return [_row_to_staged_cover(tuple(row)) for row in cursor.fetchall()]


def delete_staged_cover(conn: sqlite3.Connection, target_key: str) -> None:
    """Remove the pending cover keyed *target_key* (no-op if none)."""
    conn.execute("DELETE FROM cover_writes_staged WHERE target_key = ?", (target_key,))


def staged_cover_content(conn: sqlite3.Connection, target_key: str) -> bytes | None:
    """Return the image bytes of the pending cover keyed *target_key*, ``None`` if none."""
    row = conn.execute(
        "SELECT content FROM cover_writes_staged WHERE target_key = ?", (target_key,)
    ).fetchone()
    return None if row is None else bytes(row[0])


# --- cover_writes (the append-only log of cover files created or removed) ---------------


@dataclass(frozen=True, slots=True)
class CoverWrite:
    """One ``cover_writes`` row to append. ``path`` and ``path_key`` are relative to ``music_path``.

    ``content`` holds the bytes a ``create`` row wrote, and is ``None`` on a ``remove`` row.
    """

    commit_id: int
    created_at: str
    origin: str
    action: str
    reverted_from: int | None
    path: str
    path_key: str
    sha256: str
    size_bytes: int
    source_kind: str
    source_ref: str
    content: bytes | None
    note: str | None


def insert_cover_write(conn: sqlite3.Connection, write: CoverWrite) -> None:
    """Append one ``cover_writes`` row. The append-only triggers refuse rewrites."""
    conn.execute(
        """
        INSERT INTO cover_writes
          (commit_id, created_at, origin, action, reverted_from, path, path_key, sha256,
           size_bytes, source_kind, source_ref, content, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            write.commit_id,
            write.created_at,
            write.origin,
            write.action,
            write.reverted_from,
            write.path,
            write.path_key,
            write.sha256,
            write.size_bytes,
            write.source_kind,
            write.source_ref,
            write.content,
            write.note,
        ),
    )


_COVER_WRITE_ROW_COLUMNS: Final = (
    "id, commit_id, action, reverted_from, path, path_key, sha256, size_bytes, source_kind, "
    "source_ref"
)


@dataclass(frozen=True, slots=True)
class CoverWriteRow:
    """One logged ``cover_writes`` row, without its bytes. Paths are relative to ``music_path``."""

    id: int
    commit_id: int
    action: str
    reverted_from: int | None
    path: str
    path_key: str
    sha256: str
    size_bytes: int
    source_kind: str
    source_ref: str


def _row_to_cover_write(row: tuple[object, ...]) -> CoverWriteRow:
    """Build a typed :class:`CoverWriteRow` from a raw sqlite tuple."""
    return CoverWriteRow(
        id=db.as_int(row[0]),
        commit_id=db.as_int(row[1]),
        action=str(row[2]),
        reverted_from=None if row[3] is None else db.as_int(row[3]),
        path=str(row[4]),
        path_key=str(row[5]),
        sha256=str(row[6]),
        size_bytes=db.as_int(row[7]),
        source_kind=str(row[8]),
        source_ref=str(row[9]),
    )


def cover_writes_for_commit(conn: sqlite3.Connection, commit_id: int) -> list[CoverWriteRow]:
    """Return every ``cover_writes`` row *commit_id* appended, without bytes, in path-key order."""
    cursor = conn.execute(
        f"SELECT {_COVER_WRITE_ROW_COLUMNS} FROM cover_writes "  # noqa: S608
        "WHERE commit_id = ? ORDER BY path_key",
        (commit_id,),
    )
    return [_row_to_cover_write(tuple(row)) for row in cursor.fetchall()]


def cover_written_later(conn: sqlite3.Connection, write_id: int, path_key: str) -> bool:
    """Whether a ``cover_writes`` row after *write_id* has the path key *path_key*."""
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM cover_writes WHERE id > ? AND path_key = ?)",
        (write_id, path_key),
    ).fetchone()
    return bool(row[0])


def cover_write_content(conn: sqlite3.Connection, write_id: int) -> bytes | None:
    """Return the bytes the ``cover_writes`` row *write_id* holds, ``None`` when it holds none."""
    row = conn.execute("SELECT content FROM cover_writes WHERE id = ?", (write_id,)).fetchone()
    return None if row is None or row[0] is None else bytes(row[0])


# --- sidecar_moves (the append-only log of sidecar moves) -------------------------------

_SIDECAR_MOVE_COLUMNS: Final = (
    "id, commit_id, created_at, origin, reverted_from, from_path, to_path, from_key, to_key, "
    "unit_key, size_bytes, mtime_ns, note"
)


@dataclass(frozen=True, slots=True)
class SidecarMove:
    """One ``sidecar_moves`` row. Paths and keys are relative to ``music_path``."""

    id: int
    commit_id: int
    created_at: str
    origin: str
    reverted_from: int | None
    from_path: str
    to_path: str
    from_key: str
    to_key: str
    unit_key: str
    size_bytes: int
    mtime_ns: int
    note: str | None


def _row_to_sidecar_move(row: tuple[object, ...]) -> SidecarMove:
    """Build a typed :class:`SidecarMove` from a raw sqlite tuple."""
    return SidecarMove(
        id=db.as_int(row[0]),
        commit_id=db.as_int(row[1]),
        created_at=str(row[2]),
        origin=str(row[3]),
        reverted_from=None if row[4] is None else db.as_int(row[4]),
        from_path=str(row[5]),
        to_path=str(row[6]),
        from_key=str(row[7]),
        to_key=str(row[8]),
        unit_key=str(row[9]),
        size_bytes=db.as_int(row[10]),
        mtime_ns=db.as_int(row[11]),
        note=None if row[12] is None else str(row[12]),
    )


def insert_sidecar_move(
    conn: sqlite3.Connection,
    staged: StagedSidecar,
    *,
    commit_id: int,
    now: str,
) -> None:
    """Append the log row of the staged move *staged*. The append-only triggers refuse rewrites."""
    conn.execute(
        """
        INSERT INTO sidecar_moves
          (commit_id, created_at, origin, reverted_from, from_path, to_path, from_key, to_key,
           unit_key, size_bytes, mtime_ns, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            commit_id,
            now,
            staged.origin,
            staged.reverted_from,
            staged.from_path,
            staged.to_path,
            staged.from_key,
            staged.to_key,
            staged.unit_key,
            staged.base_size_bytes,
            staged.base_mtime_ns,
            staged.note,
        ),
    )


def sidecar_moves_for_commit(conn: sqlite3.Connection, commit_id: int) -> list[SidecarMove]:
    """Return every ``sidecar_moves`` row *commit_id* appended, in source-key order."""
    cursor = conn.execute(
        f"SELECT {_SIDECAR_MOVE_COLUMNS} FROM sidecar_moves "  # noqa: S608
        "WHERE commit_id = ? ORDER BY from_key",
        (commit_id,),
    )
    return [_row_to_sidecar_move(tuple(row)) for row in cursor.fetchall()]


def sidecar_moved_later(conn: sqlite3.Connection, move_id: int, key: str) -> bool:
    """Whether a ``sidecar_moves`` row after *move_id* moved a sidecar from or to *key*."""
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM sidecar_moves WHERE id > ? AND (from_key = ? OR to_key = ?))",
        (move_id, key, key),
    ).fetchone()
    return bool(row[0])


def sidecar_moved_from_after(conn: sqlite3.Connection, commit_id: int, key: str) -> bool:
    """Whether a ``sidecar_moves`` row of a commit after *commit_id* moved a sidecar from *key*."""
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM sidecar_moves WHERE commit_id > ? AND from_key = ?)",
        (commit_id, key),
    ).fetchone()
    return bool(row[0])


# --- files and file_tags readers, and the scope selector --------------------------------


def distinct_artists(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Return each distinct ``artist`` tag value with its present-file count, ordered by value."""
    cursor = conn.execute(
        """
        SELECT t.value, COUNT(DISTINCT t.file_id)
        FROM file_tags t
        JOIN files f ON f.id = t.file_id
        WHERE t.name = 'artist' AND f.is_missing = 0
        GROUP BY t.value
        ORDER BY t.value
        """,
    )
    return [(str(row[0]), db.as_int(row[1])) for row in cursor.fetchall()]


def load_tag_values(
    conn: sqlite3.Connection,
    names: tuple[str, ...],
) -> dict[int, dict[str, str]]:
    """Return ``{file_id: {name: value}}`` for the ordinal-0 value of each requested tag.

    One ``SELECT`` over ``file_tags`` filtered to *names* at ``ordinal = 0`` (each tag's
    primary value), so a caller needing a few scalar fields across the whole library avoids
    the N+1 :func:`get_tags` loop. A file missing every requested tag is absent from the
    result. A file with only some carries only those.
    """
    if not names:
        return {}
    placeholders = ",".join("?" for _ in names)
    cursor = conn.execute(
        "SELECT file_id, name, value FROM file_tags "  # noqa: S608 - '?' bind markers only
        f"WHERE name IN ({placeholders}) AND ordinal = 0",
        names,
    )
    result: dict[int, dict[str, str]] = {}
    for row in cursor.fetchall():
        result.setdefault(db.as_int(row[0]), {})[str(row[1])] = str(row[2])
    return result


def load_tag_lists(
    conn: sqlite3.Connection,
    names: tuple[str, ...],
) -> dict[int, dict[str, list[str]]]:
    """Return ``{file_id: {name: values}}`` with every value of each requested tag, in order.

    The multi-value sibling of :func:`load_tag_values`, for a tag whose later values matter.
    """
    if not names:
        return {}
    placeholders = ",".join("?" for _ in names)
    cursor = conn.execute(
        "SELECT file_id, name, value FROM file_tags "  # noqa: S608 - '?' bind markers only
        f"WHERE name IN ({placeholders}) ORDER BY file_id, name, ordinal",
        names,
    )
    result: dict[int, dict[str, list[str]]] = {}
    for row in cursor.fetchall():
        result.setdefault(db.as_int(row[0]), {}).setdefault(str(row[1]), []).append(str(row[2]))
    return result


def missing_file_ids(conn: sqlite3.Connection) -> set[int]:
    """Return the ids of every file the last scan flagged missing from disk."""
    cursor = conn.execute("SELECT id FROM files WHERE is_missing = 1")
    return {db.as_int(row[0]) for row in cursor.fetchall()}


def files_in_scope(
    conn: sqlite3.Connection,
    *,
    value_fields: tuple[str, ...] = (),
    value: str | None = None,
    album: str | None = None,
    file_ids: list[int] | None = None,
) -> list[int]:
    """Return the file ids in scope, in ascending id order.

    * *file_ids* given: those ids. An unknown id raises :class:`ValueError` naming it, so a
      typo is never a silent no-op. An empty list returns ``[]``.
    * else *value* given: files carrying *value* exactly in any of *value_fields*, narrowed to
      files whose ``album`` equals *album* when that is given too.
    * else: every tracked file.

    *album* without *value* (and no *file_ids*) raises :class:`ValueError`, since it only narrows
    a value scope.
    """
    if file_ids is not None:
        _require_known_file_ids(conn, file_ids)
        return sorted(set(file_ids))
    if album is not None and value is None:
        message = "album narrows a value scope, so it needs value"
        raise ValueError(message)
    if value is not None:
        return _files_in_scope_by_value(conn, value_fields, value=value, album=album)
    cursor = conn.execute("SELECT id FROM files ORDER BY id")
    return [db.as_int(row[0]) for row in cursor.fetchall()]


def _require_known_file_ids(conn: sqlite3.Connection, file_ids: list[int]) -> None:
    """Raise :class:`ValueError` naming every id in *file_ids* that ``files`` does not hold."""
    if not file_ids:
        return
    placeholders = ",".join("?" for _ in file_ids)
    cursor = conn.execute(
        f"SELECT id FROM files WHERE id IN ({placeholders})",  # noqa: S608
        tuple(file_ids),
    )
    known = {db.as_int(row[0]) for row in cursor.fetchall()}
    unknown = sorted(set(file_ids) - known)
    if unknown:
        message = f"unknown file_id(s): {unknown}"
        raise ValueError(message)


def _files_in_scope_by_value(
    conn: sqlite3.Connection,
    value_fields: tuple[str, ...],
    *,
    value: str,
    album: str | None,
) -> list[int]:
    """Return file ids carrying *value* in any of *value_fields* (and *album*, if given)."""
    if not value_fields:
        message = "a value scope needs at least one tag to match"
        raise ValueError(message)
    placeholders = ",".join("?" for _ in value_fields)
    album_clause = (
        ""
        if album is None
        else "AND EXISTS (SELECT 1 FROM file_tags b "
        "WHERE b.file_id = a.file_id AND b.name = 'album' AND b.value = ?)"
    )
    params: tuple[str, ...] = (*value_fields, value)
    if album is not None:
        params = (*params, album)
    cursor = conn.execute(
        "SELECT DISTINCT a.file_id FROM file_tags a "  # noqa: S608 - '?' markers + fixed clause
        f"WHERE a.name IN ({placeholders}) AND a.value = ? {album_clause} "
        "ORDER BY a.file_id",
        params,
    )
    return [db.as_int(row[0]) for row in cursor.fetchall()]
