"""SQLite schema of the ledger: its tables, indexes and triggers, and how an older one upgrades.

The tables, by role:

* Snapshot: ``files`` holds one row per audio file. Its integer ``id`` is the identity the
  revision, staging and status tables reference. Its ``path_key`` is the path's identity key
  from :mod:`tagmend.engine.path_keys`. The UNIQUE index ``idx_files_path_key`` keeps one row per
  key. The column is nullable. A NULL key never collides, so every engine insert sets it.
  ``file_tags`` holds the file's tag values, one row per value.
* Change tracking, modelled on git: ``commits`` holds one row per commit with its status. The
  statuses are documented beside ``_COMMIT_STATUSES`` in :mod:`tagmend.engine.commits`.
  ``tag_revisions`` and ``path_revisions`` are the per-file histories, keyed
  ``(file_id, version)``. Version 0 is the baseline. ``reverted_to_version``, on
  ``tag_revisions``, ``path_revisions`` and ``path_revisions_staged``, holds the version a revert
  restores. ``reverted_from``, on ``commits``, ``cover_writes`` and both sidecar tables, holds the
  row a revert undid.
* Staging, git's index: ``tag_revisions_staged`` and ``path_revisions_staged`` hold one pending
  target per file. Each row keeps the file's signature at stage time, which the commit checks.
* Sidecars: ``sidecar_moves`` logs each non-audio file that moved with its album folder. Its rows
  are keyed by their own paths, not by ``files.id``. Its ``unit_key`` is the key of the album
  folder the file sat under before the move. A revert finds the album's current folder from it
  by depth. ``sidecar_moves_staged`` holds the pending sidecar moves.
* Covers: ``cover_writes_staged`` holds one pending cover image per album folder, bytes
  included. ``cover_writes`` logs each cover file a commit created or removed.
* Axis status: ``file_genre_status``, ``file_artist_status``, ``file_year_status`` and
  ``file_song_status`` each hold at most one outcome row per file (:mod:`tagmend.engine.axis`).
  Each axis names its own two identity columns. ``file_mismatch_status`` holds one path decision
  per file as a JSON snapshot.
* Lookup caches: ``lastfm_cache``, ``lastfm_correction_cache``,
  ``musicbrainz_release_group_cache``, ``musicbrainz_recording_cache``,
  ``musicbrainz_artist_cache``, ``musicbrainz_release_cache``, ``acoustid_cache`` and
  ``coverart_cache`` are each keyed by a request hash. Their ``found`` column is the
  negative-cache sentinel.
  ``fingerprint_cache`` holds one fpcalc result per file at the signature it was taken at.

Every path in ``path_revisions``, ``path_revisions_staged``, ``sidecar_moves``,
``sidecar_moves_staged``, ``cover_writes`` and ``cover_writes_staged`` is relative to
``music_path``.

``tag_revisions``, ``path_revisions``, ``sidecar_moves`` and ``cover_writes`` are append-only.
:func:`apply_append_only_triggers` creates the triggers that abort an ``UPDATE`` or ``DELETE`` on
them. It also creates one that aborts a ``tag_revisions`` insert with no ``managed_set``.

:func:`apply_schema` acts only when ``PRAGMA user_version`` is older than
:data:`SCHEMA_VERSION`. Every schema change therefore bumps the version.
``CREATE TABLE IF NOT EXISTS`` never alters an existing table. A new table or a new plain index
needs only its DDL. Every other change needs an idempotent ``_migrate_*`` step: a renamed table,
a column added, renamed or dropped, a row rewrite, or a UNIQUE index that existing rows could
break. The steps run before the DDL. A step guards on ``_table_exists`` first, since a fresh
ledger has no tables when the steps run. A step whose writes share one guard makes them in one
:func:`_migration_transaction`, so a failure never leaves its guard column behind. A step that
loops over tables guards each table on its own. The triggers persist in a ledger. A migration
that updates or deletes rows of an append-only log first drops that log's triggers.
:func:`apply_schema` recreates them after the DDL. A column rename fires no trigger.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, cast

from tagmend.engine import axis, path_keys
from tagmend.engine.tags import governed_tags
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

logger = get_logger(__name__)

SCHEMA_VERSION: Final = 29


class LedgerSchemaError(RuntimeError):
    """This build cannot open the ledger until the owner acts on the message."""


_FILES_DDL: Final = """
CREATE TABLE IF NOT EXISTS files (
  id              INTEGER PRIMARY KEY,
  folder          TEXT NOT NULL,
  filename        TEXT NOT NULL,
  ext             TEXT NOT NULL,
  size_bytes      INTEGER,
  mtime_ns        INTEGER,
  is_missing      INTEGER NOT NULL DEFAULT 0,
  first_seen_at   TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  tags_updated_at TEXT,
  reader_version  INTEGER NOT NULL DEFAULT 0,
  path_key        TEXT,
  UNIQUE (folder, filename)
)
"""

_FILES_PATH_KEY_INDEX_DDL: Final = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_files_path_key ON files(path_key)"
)

_FILE_TAGS_DDL: Final = """
CREATE TABLE IF NOT EXISTS file_tags (
  file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  name      TEXT NOT NULL,
  ordinal   INTEGER NOT NULL DEFAULT 0,
  value     TEXT NOT NULL,
  PRIMARY KEY (file_id, name, ordinal)
)
"""

_FILE_TAGS_INDEX_DDL: Final = (
    "CREATE INDEX IF NOT EXISTS idx_file_tags_name_value ON file_tags(name, value)"
)

_COMMITS_DDL: Final = """
CREATE TABLE IF NOT EXISTS commits (
  id            INTEGER PRIMARY KEY,
  created_at    TEXT NOT NULL,
  origin        TEXT NOT NULL,
  message       TEXT,
  reverted_from INTEGER REFERENCES commits(id),
  status        TEXT NOT NULL DEFAULT 'applying'
)
"""

# ``managed_tags`` is a full snapshot, so any version restores without replaying the chain.
# ``managed_set`` tells a revert whether a tag the snapshot omits was empty or untracked then.
_TAG_REVISIONS_DDL: Final = """
CREATE TABLE IF NOT EXISTS tag_revisions (
  file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  version       INTEGER NOT NULL,
  commit_id     INTEGER REFERENCES commits(id),
  created_at    TEXT NOT NULL,
  origin        TEXT NOT NULL,
  reverted_to_version INTEGER,
  managed_tags  TEXT NOT NULL,
  diff          TEXT NOT NULL,
  note          TEXT,
  managed_set   INTEGER,
  PRIMARY KEY (file_id, version)
)
"""

# No ``kind`` column, since ``from_path`` and ``to_path`` already tell a rename from a move.
_PATH_REVISIONS_DDL: Final = """
CREATE TABLE IF NOT EXISTS path_revisions (
  file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  version       INTEGER NOT NULL,
  commit_id     INTEGER REFERENCES commits(id),
  created_at    TEXT NOT NULL,
  origin        TEXT NOT NULL,
  reverted_to_version INTEGER,
  from_path     TEXT NOT NULL,
  to_path       TEXT NOT NULL,
  note          TEXT,
  PRIMARY KEY (file_id, version)
)
"""

# The base signature lets a commit refuse a file edited since staging. ``changed_fields`` keeps
# the stage's own change, since a re-applied commit finds disk already at the target.
_TAG_REVISIONS_STAGED_DDL: Final = """
CREATE TABLE IF NOT EXISTS tag_revisions_staged (
  file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  managed_tags    TEXT NOT NULL,
  origin          TEXT NOT NULL,
  note            TEXT,
  staged_at       TEXT NOT NULL,
  base_size_bytes INTEGER,
  base_mtime_ns   INTEGER,
  changed_fields  TEXT,
  supplied_keys   TEXT,
  PRIMARY KEY (file_id)
)
"""

# Relative paths let a staged move survive the library's promotion to another folder. A
# migration appended the last four columns, so they stay nullable.
_PATH_REVISIONS_STAGED_DDL: Final = """
CREATE TABLE IF NOT EXISTS path_revisions_staged (
  file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  to_path         TEXT NOT NULL,
  origin          TEXT NOT NULL,
  note            TEXT,
  staged_at       TEXT NOT NULL,
  to_key          TEXT,
  base_size_bytes INTEGER,
  base_mtime_ns   INTEGER,
  reverted_to_version INTEGER,
  PRIMARY KEY (file_id)
)
"""

_PATH_REVISIONS_STAGED_TO_KEY_INDEX_DDL: Final = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_path_revisions_staged_to_key "
    "ON path_revisions_staged(to_key)"
)

# A sidecar has no ``files`` row, so its log is keyed by its own paths. The signature is how a
# commit recognises a sidecar that already landed at its target.
_SIDECAR_MOVES_DDL: Final = """
CREATE TABLE IF NOT EXISTS sidecar_moves (
  id            INTEGER PRIMARY KEY,
  commit_id     INTEGER NOT NULL REFERENCES commits(id),
  created_at    TEXT NOT NULL,
  origin        TEXT NOT NULL,
  reverted_from INTEGER REFERENCES sidecar_moves(id),
  from_path     TEXT NOT NULL,
  to_path       TEXT NOT NULL,
  from_key      TEXT NOT NULL,
  to_key        TEXT NOT NULL,
  unit_key      TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
  mtime_ns      INTEGER NOT NULL,
  note          TEXT
)
"""

_SIDECAR_MOVES_INDEX_DDL: Final = (
    "CREATE INDEX IF NOT EXISTS idx_sidecar_moves_commit_id ON sidecar_moves(commit_id)",
    "CREATE INDEX IF NOT EXISTS idx_sidecar_moves_from_key ON sidecar_moves(from_key)",
    "CREATE INDEX IF NOT EXISTS idx_sidecar_moves_to_key ON sidecar_moves(to_key)",
)

_SIDECAR_MOVES_STAGED_DDL: Final = """
CREATE TABLE IF NOT EXISTS sidecar_moves_staged (
  from_key        TEXT NOT NULL PRIMARY KEY,
  from_path       TEXT NOT NULL,
  to_path         TEXT NOT NULL,
  to_key          TEXT NOT NULL UNIQUE,
  unit_key        TEXT NOT NULL,
  base_size_bytes INTEGER NOT NULL,
  base_mtime_ns   INTEGER NOT NULL,
  origin          TEXT NOT NULL,
  reverted_from   INTEGER REFERENCES sidecar_moves(id),
  note            TEXT,
  staged_at       TEXT NOT NULL
)
"""

_SIDECAR_MOVES_STAGED_UNIT_INDEX_DDL: Final = (
    "CREATE INDEX IF NOT EXISTS idx_sidecar_moves_staged_unit_key ON sidecar_moves_staged(unit_key)"
)

# Each table precedes its indexes.
_SIDECAR_DDL: Final = (
    _SIDECAR_MOVES_DDL,
    *_SIDECAR_MOVES_INDEX_DDL,
    _SIDECAR_MOVES_STAGED_DDL,
    _SIDECAR_MOVES_STAGED_UNIT_INDEX_DDL,
)

# ``folder_key`` finds an album folder's row whatever the image format, since ``cover.jpg`` and
# ``cover.png`` differ in ``target_key``. The row holds the bytes the commit writes.
_COVER_WRITES_STAGED_DDL: Final = """
CREATE TABLE IF NOT EXISTS cover_writes_staged (
  target_key   TEXT PRIMARY KEY,
  target_path  TEXT NOT NULL,
  folder_key   TEXT NOT NULL,
  album_label  TEXT NOT NULL,
  file_ids     TEXT NOT NULL,
  source_kind  TEXT NOT NULL
    CHECK (source_kind IN ('owner_file', 'folder_image', 'release', 'release_group')),
  source_ref   TEXT NOT NULL,
  content      BLOB NOT NULL,
  sha256       TEXT NOT NULL,
  size_bytes   INTEGER NOT NULL,
  image_format TEXT NOT NULL,
  width        INTEGER NOT NULL,
  height       INTEGER NOT NULL,
  origin       TEXT NOT NULL,
  note         TEXT,
  staged_at    TEXT NOT NULL
)
"""

# A ``create`` row keeps its bytes, so a revert of a revert can write the file again.
_COVER_WRITES_DDL: Final = """
CREATE TABLE IF NOT EXISTS cover_writes (
  id            INTEGER PRIMARY KEY,
  commit_id     INTEGER NOT NULL REFERENCES commits(id),
  created_at    TEXT NOT NULL,
  origin        TEXT NOT NULL,
  action        TEXT NOT NULL CHECK (action IN ('create', 'remove')),
  reverted_from INTEGER REFERENCES cover_writes(id),
  path          TEXT NOT NULL,
  path_key      TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
  source_kind   TEXT NOT NULL,
  source_ref    TEXT NOT NULL,
  content       BLOB,
  note          TEXT
)
"""

# Each table precedes its indexes.
_COVER_DDL: Final = (
    _COVER_WRITES_STAGED_DDL,
    _COVER_WRITES_DDL,
    "CREATE INDEX IF NOT EXISTS idx_cover_writes_commit_id ON cover_writes(commit_id)",
    "CREATE INDEX IF NOT EXISTS idx_cover_writes_path_key ON cover_writes(path_key)",
)

# ``found = 0`` means Last.fm lacks the artist or album, unlike a found row with no tags.
# ``tags`` holds JSON ``[name, weight]`` pairs, ``[]`` when there are none. See PLAN.md §8.
_LASTFM_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS lastfm_cache (
  request_key TEXT PRIMARY KEY,
  found       INTEGER NOT NULL,
  tags        TEXT NOT NULL,
  fetched_at  TEXT NOT NULL
)
"""

# ``artist.getCorrection`` answers. ``found = 0`` means no correction. A found row holds the
# canonical ``name`` and the ``mbid`` when Last.fm gives one.
_LASTFM_CORRECTION_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS lastfm_correction_cache (
  request_key TEXT PRIMARY KEY,
  found       INTEGER NOT NULL,
  name        TEXT,
  mbid        TEXT,
  fetched_at  TEXT NOT NULL
)
"""

# ``source_value`` is the JSON of the axis field values a row settled. A row written before v21
# holds NULL there, so the column stays nullable.
_FILE_GENRE_STATUS_DDL: Final = """
CREATE TABLE IF NOT EXISTS file_genre_status (
  file_id       INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  status        TEXT NOT NULL,
  source_artist TEXT,
  source_album  TEXT,
  source_value  TEXT,
  updated_at    TEXT NOT NULL
)
"""

_FILE_ARTIST_STATUS_DDL: Final = """
CREATE TABLE IF NOT EXISTS file_artist_status (
  file_id             INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  status              TEXT NOT NULL,
  source_artist       TEXT,
  source_albumartist  TEXT,
  source_value        TEXT,
  updated_at          TEXT NOT NULL
)
"""

# Named ``file_album_status`` before v12.
_FILE_YEAR_STATUS_DDL: Final = """
CREATE TABLE IF NOT EXISTS file_year_status (
  file_id       INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  status        TEXT NOT NULL,
  source_artist TEXT,
  source_album  TEXT,
  source_value  TEXT,
  updated_at    TEXT NOT NULL
)
"""

# The identity is the release and release-track ids, blank as an empty string, so a file with no
# artist still carries a song status.
_FILE_SONG_STATUS_DDL: Final = """
CREATE TABLE IF NOT EXISTS file_song_status (
  file_id                   INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  status                    TEXT NOT NULL,
  source_album_mbid         TEXT,
  source_release_track_mbid TEXT,
  source_value              TEXT,
  updated_at                TEXT NOT NULL
)
"""

# ``found = 0`` means no usable Album release group. A found row holds the selected group's
# ``first-release-date`` and MBIDs. Named ``musicbrainz_cache`` before v20.
_MUSICBRAINZ_RELEASE_GROUP_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS musicbrainz_release_group_cache (
  request_key        TEXT PRIMARY KEY,
  found              INTEGER NOT NULL,
  album_title        TEXT,
  original_date      TEXT,
  release_mbid       TEXT,
  release_group_mbid TEXT,
  fetched_at         TEXT NOT NULL
)
"""

# ``found = 0`` means no release under that MBID. ``payload`` is one JSON document, since each
# caller reads a whole tracklist. Feeds ``detect_release_disagreements`` and ``resolve_songs``.
_MUSICBRAINZ_RELEASE_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS musicbrainz_release_cache (
  request_key TEXT PRIMARY KEY,
  found       INTEGER NOT NULL,
  payload     TEXT,
  fetched_at  TEXT NOT NULL
)
"""

# ``found = 0`` means no artist under that MBID. The alias set tells a name worth merging from a
# per-track credit in ``resolve_artists``' MusicBrainz tier.
_MUSICBRAINZ_ARTIST_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS musicbrainz_artist_cache (
  request_key    TEXT PRIMARY KEY,
  found          INTEGER NOT NULL,
  name           TEXT,
  sort_name      TEXT,
  disambiguation TEXT,
  aliases        TEXT,
  fetched_at     TEXT NOT NULL
)
"""

# ``(artist, title)`` recording searches for ``detect_album_gaps``' review-only tier.
# ``found = 0`` means the recording has no usable Album release group.
_MUSICBRAINZ_RECORDING_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS musicbrainz_recording_cache (
  request_key        TEXT PRIMARY KEY,
  found              INTEGER NOT NULL,
  album_title        TEXT,
  release_group_mbid TEXT,
  recording_mbid     TEXT,
  fetched_at         TEXT NOT NULL
)
"""

# The fingerprint lets an expired or failed lookup re-query without fpcalc. A NULL fingerprint
# and duration store a failure, so an undecodable file does not re-run fpcalc.
_FINGERPRINT_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS fingerprint_cache (
  file_id          INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  size_bytes       INTEGER NOT NULL,
  mtime_ns         INTEGER NOT NULL,
  fpcalc_exit      INTEGER NOT NULL,
  fingerprint      TEXT,
  duration         INTEGER,
  fingerprinted_at TEXT NOT NULL
)
"""

# ``found = 0`` means no match, served only while young since AcoustID learns new fingerprints.
# ``payload`` holds the parsed result as zlib-compressed JSON.
_ACOUSTID_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS acoustid_cache (
  request_key TEXT PRIMARY KEY,
  found       INTEGER NOT NULL,
  payload     BLOB,
  fetched_at  TEXT NOT NULL
)
"""

# ``found = 0`` means no approved front, served only while young since CAA gains art over time.
# ``payload`` holds the front's URLs as JSON.
_COVERART_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS coverart_cache (
  request_key TEXT PRIMARY KEY,
  found       INTEGER NOT NULL,
  payload     TEXT,
  fetched_at  TEXT NOT NULL
)
"""

# ``source_value`` stays nullable, since the v24 upgrade drops a column in place and cannot add
# NOT NULL.
_FILE_MISMATCH_STATUS_DDL: Final = """
CREATE TABLE IF NOT EXISTS file_mismatch_status (
  file_id       INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  status        TEXT NOT NULL,
  source_value  TEXT,
  updated_at    TEXT NOT NULL
)
"""


_REVISIONS_COMMIT_INDEX_DDL: Final = (
    "CREATE INDEX IF NOT EXISTS idx_tag_revisions_commit_id ON tag_revisions(commit_id)",
    "CREATE INDEX IF NOT EXISTS idx_path_revisions_commit_id ON path_revisions(commit_id)",
)

_APPEND_ONLY_LOGS: Final = ("tag_revisions", "path_revisions", "sidecar_moves", "cover_writes")

# Revert reads an omitted tag by the revision's managed set, so a NULL would silently change
# what a revert deletes. SQLite cannot add NOT NULL to an existing column, so a trigger does.
_MANAGED_SET_REQUIRED_TRIGGER_DDL: Final = (
    "CREATE TRIGGER IF NOT EXISTS tag_revisions_managed_set_required "
    "BEFORE INSERT ON tag_revisions WHEN NEW.managed_set IS NULL "
    "BEGIN SELECT RAISE(ABORT, 'tag_revisions.managed_set is required'); END"
)


def apply_append_only_triggers(connection: sqlite3.Connection) -> None:
    """Create the triggers that abort any ``UPDATE`` or ``DELETE`` on each append-only log.

    A BEFORE DELETE trigger also blocks the ``ON DELETE CASCADE`` from ``files``, so deleting a
    file row can never erase its history. One more trigger aborts a ``tag_revisions`` insert
    that carries no ``managed_set``. Idempotent.
    """
    for log in _APPEND_ONLY_LOGS:
        for event in ("update", "delete"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {log}_no_{event} "
                f"BEFORE {event.upper()} ON {log} "
                f"BEGIN SELECT RAISE(ABORT, '{log} is append-only'); END",
            )
    connection.execute(_MANAGED_SET_REQUIRED_TRIGGER_DDL)


def _log_schema_change(connection: sqlite3.Connection, current: int) -> None:
    """Log the schema step about to run: a new ledger is created, an older one is upgraded.

    A new ledger is one stamped 0 that holds no table yet, so a fresh ledger and the in-memory
    health probe never log a migration that did not happen.
    """
    tables = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1")
    if current == 0 and tables.fetchone() is None:
        logger.info("creating ledger schema v%d", SCHEMA_VERSION)
        return
    logger.info("upgrading ledger schema v%d to v%d", current, SCHEMA_VERSION)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    """Whether a table called *name* exists in this ledger (``sqlite_master`` lookup)."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether *table* already has a *column* (``PRAGMA table_info`` lookup)."""
    # PRAGMA takes no bound parameters, so the table name is interpolated (never user data).
    cursor = connection.execute(f"PRAGMA table_info({table})")
    return any(str(row[1]) == column for row in cursor.fetchall())


@contextmanager
def _migration_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Commit a step's writes together, or roll every one back and re-raise.

    ``db.connect`` keeps sqlite3's legacy transaction control, which opens no transaction for
    DDL, so each ``ALTER`` outside this block commits alone.
    """
    connection.execute("BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


def _migrate_v12_year_status(connection: sqlite3.Connection) -> None:
    """v12: rename ``file_album_status`` to ``file_year_status``, keeping every row."""
    if not _table_exists(connection, "file_album_status"):
        return
    if _table_exists(connection, "file_year_status"):
        return
    connection.execute("ALTER TABLE file_album_status RENAME TO file_year_status")
    logger.info("schema v12: renamed file_album_status to file_year_status")


# The date the managed set widened from 5 tags to 18 (managed-set version 1 to 2). A later
# widening adds a new version and never restamps rows through this constant.
_MANAGED_SET_WIDENING_DATE: Final = "2026-07-04"


def _migrate_v13_managed_set(connection: sqlite3.Connection) -> None:
    """v13: add ``tag_revisions.managed_set`` and stamp existing rows by capture date.

    Capture date is the only evidence a v12 row carries about which set governed it, and
    ``created_at`` is an ISO-8601 UTC string, so the comparison is lexicographic. The column and
    the stamp land in one transaction the step commits itself, since a read-only caller never
    commits.
    """
    if not _table_exists(connection, "tag_revisions"):
        return
    if _column_exists(connection, "tag_revisions", "managed_set"):
        return
    with _migration_transaction(connection):
        connection.execute("ALTER TABLE tag_revisions ADD COLUMN managed_set INTEGER")
        # Test ledgers are built by downgrading a fresh schema that already carries the trigger.
        connection.execute("DROP TRIGGER IF EXISTS tag_revisions_no_update")
        connection.execute(
            "UPDATE tag_revisions SET managed_set = CASE WHEN created_at >= ? THEN 2 ELSE 1 END",
            (_MANAGED_SET_WIDENING_DATE,),
        )
    logger.info("schema v13: stamped tag_revisions.managed_set on pre-existing rows")


def _migrate_v14_reader_version(connection: sqlite3.Connection) -> None:
    """v14: add ``files.reader_version``, defaulting pre-existing rows below any reader.

    The ``DEFAULT 0`` puts every existing row below :data:`TAG_READER_VERSION`. An unknown older
    reader read those rows, so the next incremental scan re-reads each of them once.
    """
    if not _table_exists(connection, "files"):
        return
    if _column_exists(connection, "files", "reader_version"):
        return
    connection.execute("ALTER TABLE files ADD COLUMN reader_version INTEGER NOT NULL DEFAULT 0")
    logger.info("schema v14: added files.reader_version (pre-existing rows default to 0)")


def _migrate_staged_base_signature(connection: sqlite3.Connection) -> None:
    """v17: add ``tag_revisions_staged.base_size_bytes`` / ``base_mtime_ns`` as NULL.

    A NULL pair marks a row staged before the signature existed, and the commit skips the
    changed-since-stage check for it.
    """
    if not _table_exists(connection, "tag_revisions_staged"):
        return
    if _column_exists(connection, "tag_revisions_staged", "base_size_bytes"):
        return
    with _migration_transaction(connection):
        connection.execute("ALTER TABLE tag_revisions_staged ADD COLUMN base_size_bytes INTEGER")
        connection.execute("ALTER TABLE tag_revisions_staged ADD COLUMN base_mtime_ns INTEGER")
    logger.info("schema v17: added tag_revisions_staged base signature columns")


def _migrate_staged_changed_fields(connection: sqlite3.Connection) -> None:
    """v21: add ``tag_revisions_staged.changed_fields`` as NULL."""
    if not _table_exists(connection, "tag_revisions_staged"):
        return
    if _column_exists(connection, "tag_revisions_staged", "changed_fields"):
        return
    connection.execute("ALTER TABLE tag_revisions_staged ADD COLUMN changed_fields TEXT")
    logger.info("schema v21: added tag_revisions_staged.changed_fields")


def _migrate_staged_supplied_keys(connection: sqlite3.Connection) -> None:
    """v23: add ``tag_revisions_staged.supplied_keys`` as NULL.

    A NULL marks a row staged before the column existed, which the stale-identity report reads
    as "no key confirmed".
    """
    if not _table_exists(connection, "tag_revisions_staged"):
        return
    if _column_exists(connection, "tag_revisions_staged", "supplied_keys"):
        return
    connection.execute("ALTER TABLE tag_revisions_staged ADD COLUMN supplied_keys TEXT")
    logger.info("schema v23: added tag_revisions_staged.supplied_keys")


def _migrate_path_staging(connection: sqlite3.Connection) -> None:
    """v25: add the path staging columns ``to_key``, the base signature and ``reverted_from``.

    The DDL phase adds the unique ``to_key`` index, which several NULL keys do not violate.
    """
    if not _table_exists(connection, "path_revisions_staged"):
        return
    if _column_exists(connection, "path_revisions_staged", "to_key"):
        return
    with _migration_transaction(connection):
        connection.execute("ALTER TABLE path_revisions_staged ADD COLUMN to_key TEXT")
        connection.execute("ALTER TABLE path_revisions_staged ADD COLUMN base_size_bytes INTEGER")
        connection.execute("ALTER TABLE path_revisions_staged ADD COLUMN base_mtime_ns INTEGER")
        connection.execute("ALTER TABLE path_revisions_staged ADD COLUMN reverted_from INTEGER")
    logger.info("schema v25: added the path staging columns")


def _migrate_path_reverted_to_version(connection: sqlite3.Connection) -> None:
    """v27: rename ``reverted_from`` to ``reverted_to_version`` on both path tables, keeping rows.

    Runs after :func:`_migrate_path_staging`, which adds the old name to a pre-v25 staging
    table.
    """
    for table in ("path_revisions", "path_revisions_staged"):
        if not _table_exists(connection, table):
            continue
        if _column_exists(connection, table, "reverted_to_version"):
            continue
        if not _column_exists(connection, table, "reverted_from"):
            continue
        connection.execute(
            f"ALTER TABLE {table} RENAME COLUMN reverted_from TO reverted_to_version",
        )
        logger.info("schema v27: renamed %s.reverted_from to reverted_to_version", table)


# Frozen history like :data:`_MANAGED_SET_WIDENING_DATE`: every pre-v24 row was set on the
# top-folder comparison, whatever that comparison is later named.
_LEGACY_MISMATCH_COVERS: Final = ("top_folder_artist",)


def _legacy_mismatch_snapshot(
    status: str,
    source_field: str | None,
    source_value: str | None,
    folder: str | None,
) -> str:
    """Return the v24 JSON snapshot of one pre-v24 ``file_mismatch_status`` row.

    A row with no source field was set on a file with neither artist tag, so it records both as
    blank and silences nothing until a tag appears. An ``artist`` row records ``albumartist`` as
    blank, since v23 read ``artist`` only while ``albumartist`` was blank. A keep binds to the
    folder the ledger holds now, which is the folder it was set on, since nothing moved files
    before v24.
    """
    tags: dict[str, str | None] = (
        {"albumartist": source_value}
        if source_field == "albumartist"
        else {"albumartist": None, "artist": source_value if source_field == "artist" else None}
    )
    snapshot: dict[str, object] = {
        "covers": list(_LEGACY_MISMATCH_COVERS),
        "tags": tags,
        "path_version": 0,
    }
    if status == "legit_ignore" and folder is not None:
        snapshot["folder_key"] = path_keys.path_key(folder)
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))


def _migrate_mismatch_covers(connection: sqlite3.Connection) -> None:
    """v24: rewrite each mismatch row as a JSON snapshot and drop ``source_field``."""
    if not _table_exists(connection, "file_mismatch_status"):
        return
    if not _column_exists(connection, "file_mismatch_status", "source_field"):
        return

    with _migration_transaction(connection):
        cursor = connection.execute(
            """
            SELECT m.file_id, m.status, m.source_field, m.source_value, f.folder
            FROM file_mismatch_status AS m LEFT JOIN files AS f ON f.id = m.file_id
            """,
        )
        rows = cursor.fetchall()
        connection.executemany(
            "UPDATE file_mismatch_status SET source_value = ? WHERE file_id = ?",
            [
                (
                    _legacy_mismatch_snapshot(
                        str(row[1]),
                        None if row[2] is None else str(row[2]),
                        None if row[3] is None else str(row[3]),
                        None if row[4] is None else str(row[4]),
                    ),
                    int(row[0]),
                )
                for row in rows
            ],
        )
        connection.execute("ALTER TABLE file_mismatch_status DROP COLUMN source_field")
    logger.info("schema v24: rewrote %d mismatch row(s) as covers snapshots", len(rows))


def _keyed_file_rows(connection: sqlite3.Connection) -> list[tuple[int, str, str, str]]:
    """Return every ``files`` row as ``(id, folder, filename, path key)``, in id order."""
    cursor = connection.execute("SELECT id, folder, filename FROM files ORDER BY id")
    rows: list[tuple[int, str, str, str]] = []
    for raw in cursor.fetchall():
        folder = str(raw[1])
        filename = str(raw[2])
        rows.append((int(raw[0]), folder, filename, path_keys.file_path_key(folder, filename)))
    return rows


def _refuse_path_key_collisions(rows: list[tuple[int, str, str, str]]) -> None:
    """Raise :class:`LedgerSchemaError` naming every path key two or more *rows* share."""
    by_key: dict[str, list[str]] = {}
    for file_id, folder, filename, key in rows:
        by_key.setdefault(key, []).append(f"id={file_id} ({folder!r}, {filename!r})")
    collisions = {key: members for key, members in by_key.items() if len(members) > 1}
    if not collisions:
        return
    details = "; ".join(f"{key}: {', '.join(ids)}" for key, ids in sorted(collisions.items()))
    message = (
        f"schema v18: {len(collisions)} path key(s) are held by more than one file row, so one "
        f"file is tracked twice. Keep one row per key, then restart: {details}"
    )
    raise LedgerSchemaError(message)


def _migrate_files_path_key(connection: sqlite3.Connection) -> None:
    """v18: add ``files.path_key``, backfill it and create its UNIQUE index, all or nothing.

    Two rows sharing a key are one file recorded twice, and only the owner can say which row
    keeps the history. The upgrade then rolls back, the ADD COLUMN included, and raises
    :class:`LedgerSchemaError` naming each collision.
    """
    if not _table_exists(connection, "files"):
        return
    if _column_exists(connection, "files", "path_key"):
        return

    with _migration_transaction(connection):
        connection.execute("ALTER TABLE files ADD COLUMN path_key TEXT")
        rows = _keyed_file_rows(connection)
        _refuse_path_key_collisions(rows)
        connection.executemany(
            "UPDATE files SET path_key = ? WHERE id = ?",
            [(key, file_id) for file_id, _, _, key in rows],
        )
        connection.execute(_FILES_PATH_KEY_INDEX_DDL)
    logger.info("schema v18: backfilled files.path_key on %d row(s)", len(rows))


def _migrate_commit_origin(connection: sqlite3.Connection) -> None:
    """v19: restamp ``auto`` on every ``manual`` commit whose revisions are all ``auto``.

    It updates ``commits`` only, which carries no append-only trigger. Commits itself, since a
    read-only caller would otherwise roll the restamp back.
    """
    if not _table_exists(connection, "commits"):
        return
    if not _table_exists(connection, "tag_revisions"):
        return
    cursor = connection.execute(
        """
        UPDATE commits SET origin = 'auto'
        WHERE origin = 'manual'
          AND EXISTS (SELECT 1 FROM tag_revisions r WHERE r.commit_id = commits.id)
          AND NOT EXISTS (
            SELECT 1 FROM tag_revisions r WHERE r.commit_id = commits.id AND r.origin != 'auto'
          )
        """,
    )
    restamped = cursor.rowcount
    connection.commit()
    logger.info("schema v19: restamped %d all-auto commit(s) as auto", restamped)


def _migrate_reverted_to_version(connection: sqlite3.Connection) -> None:
    """v20: rename ``tag_revisions.reverted_from`` to ``reverted_to_version``, keeping every row."""
    if not _table_exists(connection, "tag_revisions"):
        return
    if _column_exists(connection, "tag_revisions", "reverted_to_version"):
        return
    if not _column_exists(connection, "tag_revisions", "reverted_from"):
        return
    connection.execute(
        "ALTER TABLE tag_revisions RENAME COLUMN reverted_from TO reverted_to_version",
    )
    logger.info("schema v20: renamed tag_revisions.reverted_from to reverted_to_version")


# How many offending ``(file_id, version)`` pairs the managed-set refusal names.
_NULL_MANAGED_SET_SAMPLE: Final = 5


def _migrate_managed_set_required(connection: sqlite3.Connection) -> None:
    """v20: refuse a ledger holding a ``tag_revisions`` row with no ``managed_set``. No writes.

    Runs after :func:`_migrate_v13_managed_set`, so a pre-v13 row is already stamped. A NULL
    here means that stamp was lost, and only the owner can say which set governed the row, so
    the upgrade raises :class:`LedgerSchemaError` naming the count and the first few rows.
    """
    if not _table_exists(connection, "tag_revisions"):
        return
    if not _column_exists(connection, "tag_revisions", "managed_set"):
        return
    count = int(
        connection.execute(
            "SELECT COUNT(*) FROM tag_revisions WHERE managed_set IS NULL",
        ).fetchone()[0],
    )
    if count == 0:
        return
    sample = connection.execute(
        """
        SELECT file_id, version FROM tag_revisions
        WHERE managed_set IS NULL ORDER BY file_id, version LIMIT ?
        """,
        (_NULL_MANAGED_SET_SAMPLE,),
    ).fetchall()
    pairs = ", ".join(f"({int(row[0])}, {int(row[1])})" for row in sample)
    message = (
        f"schema v20: {count} tag_revisions row(s) carry no managed_set, so a revert cannot "
        f"tell which tags they governed. Stamp each one, then restart. "
        f"First (file_id, version) pairs: {pairs}"
    )
    raise LedgerSchemaError(message)


def _migrate_release_group_cache_name(connection: sqlite3.Connection) -> None:
    """v20: rename ``musicbrainz_cache`` to ``musicbrainz_release_group_cache``, keeping rows.

    Every request key already starts with ``release-group``, so no cached row is invalidated.
    """
    if not _table_exists(connection, "musicbrainz_cache"):
        return
    if _table_exists(connection, "musicbrainz_release_group_cache"):
        return
    connection.execute("ALTER TABLE musicbrainz_cache RENAME TO musicbrainz_release_group_cache")
    logger.info("schema v20: renamed musicbrainz_cache to musicbrainz_release_group_cache")


# Every cache that has carried a ``release_group_id`` column, under each name it has had.
_RELEASE_GROUP_ID_TABLES: Final = (
    "musicbrainz_release_group_cache",
    "musicbrainz_cache",
    "musicbrainz_recording_cache",
)


def _migrate_mbid_columns(connection: sqlite3.Connection) -> None:
    """v20: rename ``release_group_id`` to ``release_group_mbid`` in each cache, keeping rows.

    Runs after :func:`_migrate_release_group_cache_name`.
    """
    for table in _RELEASE_GROUP_ID_TABLES:
        if not _table_exists(connection, table):
            continue
        if not _column_exists(connection, table, "release_group_id"):
            continue
        if _column_exists(connection, table, "release_group_mbid"):
            continue
        connection.execute(
            f"ALTER TABLE {table} RENAME COLUMN release_group_id TO release_group_mbid",
        )
        logger.info("schema v20: renamed %s.release_group_id to release_group_mbid", table)


def _migrate_drop_files_status(connection: sqlite3.Connection) -> None:
    """v20: drop ``files.status``, which only its DEFAULT ever wrote and nothing read.

    No index, constraint, view or trigger names the column, so ``DROP COLUMN`` applies.
    """
    if not _table_exists(connection, "files"):
        return
    if not _column_exists(connection, "files", "status"):
        return
    connection.execute("ALTER TABLE files DROP COLUMN status")
    logger.info("schema v20: dropped files.status")


def _legacy_correction_key(artist: str) -> str:
    """Return the version-1 ``lastfm._request_key`` bytes for ``artist.getcorrection``.

    Frozen history like :data:`_MANAGED_SET_WIDENING_DATE`: the migration must find the rows
    the old key wrote, whatever the live key formula becomes.
    """
    payload = f"artist.getcorrection\x00artist={artist}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324 - cache key, not security


def _decode_legacy_correction(raw_tags: str) -> tuple[str | None, str | None]:
    """Return ``(name, mbid)`` from a correction stored as ``[[name, 0], [mbid, 0]]`` pairs."""
    pairs = json.loads(raw_tags)
    name = str(pairs[0][0]) if pairs else None
    mbid = str(pairs[1][0]) if len(pairs) > 1 and pairs[1][0] else None
    return name, mbid


def _move_correction_rows(connection: sqlite3.Connection) -> int:
    """Move each library artist's correction row out of ``lastfm_cache``. Returns the count."""
    values: list[str] = []
    if _table_exists(connection, "file_tags"):
        cursor = connection.execute(
            "SELECT DISTINCT value FROM file_tags WHERE name IN ('artist', 'albumartist')",
        )
        values = [str(row[0]) for row in cursor.fetchall()]

    moved = 0
    for request_key in sorted({_legacy_correction_key(value) for value in values}):
        row = connection.execute(
            "SELECT found, tags, fetched_at FROM lastfm_cache WHERE request_key = ?",
            (request_key,),
        ).fetchone()
        if row is None:
            continue
        name, mbid = _decode_legacy_correction(str(row[1]))
        connection.execute(
            """
            INSERT INTO lastfm_correction_cache (request_key, found, name, mbid, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (request_key, int(row[0]), name, mbid, str(row[2])),
        )
        connection.execute("DELETE FROM lastfm_cache WHERE request_key = ?", (request_key,))
        moved += 1
    return moved


def _migrate_lastfm_correction_cache(connection: sqlite3.Connection) -> None:
    """v20: create ``lastfm_correction_cache`` and move the library's correction rows into it.

    A correction row for a value the library no longer carries stays behind unreachable, which
    costs only a re-fetch if that value returns.
    """
    if not _table_exists(connection, "lastfm_cache"):
        return
    if _table_exists(connection, "lastfm_correction_cache"):
        return

    with _migration_transaction(connection):
        connection.execute(_LASTFM_CORRECTION_CACHE_DDL)
        moved = _move_correction_rows(connection)
    logger.info("schema v20: moved %d correction row(s) to lastfm_correction_cache", moved)


# Each tag-axis status table the v21 pass migrated, with the DDL that creates it in its v21 shape.
_AXIS_STATUS_TABLES: Final = (
    ("file_genre_status", _FILE_GENRE_STATUS_DDL),
    ("file_artist_status", _FILE_ARTIST_STATUS_DDL),
    ("file_year_status", _FILE_YEAR_STATUS_DDL),
)


# The axes the v21 replay covers. A later axis has no table yet when this migration runs.
_V21_TAG_AXES: Final = (axis.GENRE_AXIS, axis.ARTIST_AXIS, axis.YEAR_AXIS)


def _axis_outcomes_migrated(connection: sqlite3.Connection) -> bool:
    """Whether ``voided_auto`` is gone and every tag-axis status table has ``source_value``."""
    if _table_exists(connection, "voided_auto"):
        return False
    return all(
        _table_exists(connection, table) and _column_exists(connection, table, "source_value")
        for table, _ in _AXIS_STATUS_TABLES
    )


def _replay_manual_revisions(connection: sqlite3.Connection) -> int:
    """Write ``manual`` on each axis a manual revision's diff touched. Returns rows written.

    The commit writer's manual rule applied to history, in ``(file_id, version)`` order so the
    latest manual revision wins. Snapshots come from the revision's own ``managed_tags`` and
    the row is stamped with the revision's time, so a second run writes the same rows. A diff
    key the previous revision's managed set did not govern is widening noise, not a human
    change: the diff reads ``from: []`` there whether or not the field held a value.
    """
    cursor = connection.execute(
        """
        SELECT r.file_id, r.created_at, r.managed_tags, r.diff,
               COALESCE(p.managed_set, r.managed_set)
        FROM tag_revisions AS r
        LEFT JOIN tag_revisions AS p ON p.file_id = r.file_id AND p.version = r.version - 1
        WHERE r.origin = 'manual' ORDER BY r.file_id, r.version
        """,
    )
    written = 0
    for row in cursor.fetchall():
        managed_tags = cast("dict[str, list[str]]", json.loads(str(row[2])))
        diff = cast("dict[str, object]", json.loads(str(row[3])))
        previously_governed = governed_tags(int(row[4]))
        for tag_axis in _V21_TAG_AXES:
            if not any(name in diff and name in previously_governed for name in tag_axis.fields):
                continue
            axis.put_outcome(
                connection,
                tag_axis,
                file_id=int(row[0]),
                status="manual",
                tags=managed_tags,
                now=str(row[1]),
            )
            written += 1
    return written


def _migrate_axis_outcomes(connection: sqlite3.Connection) -> None:
    """v21: snapshot tag-axis status values, replay manual revisions, drop ``voided_auto``.

    It creates any tag-axis status table an older ledger never had, adds ``source_value`` where
    it is missing (existing rows keep NULL, which the classifier treats as matching), writes
    ``manual`` from the manual revisions (:func:`_replay_manual_revisions`) and drops
    ``voided_auto``, whose watermarks the snapshots replace. It writes no ``done`` row, so a
    file an auto revision settled reads ``pending`` until a resolver re-derives it from cache.
    """
    if not _table_exists(connection, "tag_revisions"):
        return
    if _axis_outcomes_migrated(connection):
        return

    with _migration_transaction(connection):
        for table, ddl in _AXIS_STATUS_TABLES:
            connection.execute(ddl)
            if not _column_exists(connection, table, "source_value"):
                connection.execute(f"ALTER TABLE {table} ADD COLUMN source_value TEXT")
        replayed = _replay_manual_revisions(connection)
        connection.execute("DROP TABLE IF EXISTS voided_auto")
    logger.info("schema v21: replayed %d manual status row(s), dropped voided_auto", replayed)


def _apply_migrations(connection: sqlite3.Connection) -> None:
    """Run every in-place migration of :func:`apply_schema`, oldest first."""
    _migrate_v12_year_status(connection)
    _migrate_v13_managed_set(connection)
    _migrate_v14_reader_version(connection)
    _migrate_staged_base_signature(connection)
    _migrate_files_path_key(connection)
    _migrate_commit_origin(connection)
    _migrate_reverted_to_version(connection)
    _migrate_managed_set_required(connection)
    _migrate_release_group_cache_name(connection)
    _migrate_mbid_columns(connection)
    _migrate_drop_files_status(connection)
    _migrate_lastfm_correction_cache(connection)
    _migrate_axis_outcomes(connection)
    _migrate_staged_changed_fields(connection)
    _migrate_staged_supplied_keys(connection)
    _migrate_mismatch_covers(connection)
    _migrate_path_staging(connection)
    _migrate_path_reverted_to_version(connection)


def apply_schema(connection: sqlite3.Connection) -> None:
    """Upgrade the ledger to :data:`SCHEMA_VERSION` when its stamp is older, else do nothing.

    A current ledger costs one ``PRAGMA user_version`` read and no write, so a read-only caller
    never waits on the write lock a running scan holds. A ledger stamped newer than this build
    raises :class:`LedgerSchemaError` rather than being stamped down.

    An older ledger, or a fresh one stamped 0, runs the in-place migrations first
    (:func:`_apply_migrations`). It then runs every ``CREATE ... IF NOT EXISTS``, then the
    append-only triggers, then the stamp. A migration keeps the ledger's rows unless its own
    docstring says otherwise. Two migrations refuse an older ledger with
    :class:`LedgerSchemaError`. :func:`_migrate_files_path_key` refuses one where two file rows
    share a path key. :func:`_migrate_managed_set_required` refuses one holding a revision with
    no managed set.

    ``commits`` is created before every table that references it.
    """
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current == SCHEMA_VERSION:
        return
    if current > SCHEMA_VERSION:
        message = (
            f"ledger schema v{current} is newer than this tagmend (v{SCHEMA_VERSION}). "
            "Upgrade tagmend."
        )
        raise LedgerSchemaError(message)

    _log_schema_change(connection, current)
    _apply_migrations(connection)
    connection.execute(_FILES_DDL)
    connection.execute(_FILES_PATH_KEY_INDEX_DDL)
    connection.execute(_FILE_TAGS_DDL)
    connection.execute(_FILE_TAGS_INDEX_DDL)
    connection.execute(_COMMITS_DDL)
    connection.execute(_TAG_REVISIONS_DDL)
    connection.execute(_PATH_REVISIONS_DDL)
    connection.execute(_TAG_REVISIONS_STAGED_DDL)
    connection.execute(_PATH_REVISIONS_STAGED_DDL)
    connection.execute(_PATH_REVISIONS_STAGED_TO_KEY_INDEX_DDL)
    connection.execute(_LASTFM_CACHE_DDL)
    connection.execute(_LASTFM_CORRECTION_CACHE_DDL)
    connection.execute(_FILE_GENRE_STATUS_DDL)
    connection.execute(_FILE_ARTIST_STATUS_DDL)
    connection.execute(_FILE_YEAR_STATUS_DDL)
    connection.execute(_FILE_SONG_STATUS_DDL)
    connection.execute(_MUSICBRAINZ_RELEASE_GROUP_CACHE_DDL)
    connection.execute(_MUSICBRAINZ_RECORDING_CACHE_DDL)
    connection.execute(_MUSICBRAINZ_ARTIST_CACHE_DDL)
    connection.execute(_MUSICBRAINZ_RELEASE_CACHE_DDL)
    connection.execute(_FILE_MISMATCH_STATUS_DDL)
    connection.execute(_FINGERPRINT_CACHE_DDL)
    connection.execute(_ACOUSTID_CACHE_DDL)
    connection.execute(_COVERART_CACHE_DDL)
    for ddl in (*_SIDECAR_DDL, *_COVER_DDL, *_REVISIONS_COMMIT_INDEX_DDL):
        connection.execute(ddl)
    apply_append_only_triggers(connection)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
