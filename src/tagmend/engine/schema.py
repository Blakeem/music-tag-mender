"""SQLite schema for the library snapshot + staging/commit/revision logs (M1 + M3).

Defines the tables that hold the read-path snapshot: ``files`` (one row per audio
file, anchored by its ``(folder, filename)`` at first scan and assigned a stable
integer ``id`` surrogate) and ``file_tags`` (normalized EAV rows for each tag value).
The ``id`` is the durable identity that all history tables reference, per PLAN.md §7.

The change-tracking model mirrors git (PLAN.md §7):

* ``commits`` — one row per *commit*: a group of individual changes applied together.
  Its ``id`` is the ``commit_id`` the revision rows reference. Holds the group's
  message/time/origin and a status (``applying``→``applied``, or a terminal
  ``interrupted`` left by a crashed run). A lingering ``applying`` row means an
  interrupted run; recovery is just running the commit again (no resume machinery).
* ``tag_revisions_staged`` / ``path_revisions_staged`` — the staging area (git's
  index). One pending change per file (PK ``file_id``); holds the *desired target*.
  Staged rows no longer carry a ``commit_id`` (no claiming): a commit turns each
  staged row into a real revision row, then deletes it.
* ``tag_revisions`` — the managed-tag content history (PLAN.md §7). Logic lives in
  :mod:`tagmend.engine.versioning`.
* ``path_revisions`` — the location history (PLAN.md §18). DDL is locked here for
  schema symmetry, but the move/rename logic is deferred to M6.

The two revision logs are append-only, keyed by ``files.id`` with a composite PK
``(file_id, version)`` (version 0 = baseline with ``commit_id`` NULL, +1 per change;
never updated/deleted). Last.fm caches (``artist_cache``, ``lastfm_cache``) land in M2
and are *not* here yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

logger = get_logger(__name__)

SCHEMA_VERSION: Final = 5

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
  status          TEXT NOT NULL DEFAULT 'scanned',
  UNIQUE (folder, filename)
)
"""

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

# One row per commit: a group of individual changes applied together (git's commit).
# ``id`` is the ``commit_id`` the revision rows reference. ``status`` is the crash
# marker: a row stuck in 'applying' is an interrupted commit (the next commit flips it
# to 'interrupted' and sweeps any leftover staged rows into a new commit).
# ``reverted_from`` (origin='revert') points at the commit this one undoes. See PLAN.md §7.
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

# Append-only managed-tag content history. One row per file per change; never updated
# or deleted. ``version`` (0 = baseline) is both the ordering key and the restore
# handle — ``created_at`` is display-only. ``commit_id`` groups the change with the
# other files in the same commit (NULL for the version-0 baseline, which precedes any
# commit). ``managed_tags`` is a FULL JSON snapshot, so any version is restorable
# without replaying the chain. See PLAN.md §7 / §22.
_TAG_REVISIONS_DDL: Final = """
CREATE TABLE IF NOT EXISTS tag_revisions (
  file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  version       INTEGER NOT NULL,
  commit_id     INTEGER REFERENCES commits(id),
  created_at    TEXT NOT NULL,
  origin        TEXT NOT NULL,
  reverted_from INTEGER,
  managed_tags  TEXT NOT NULL,
  diff          TEXT NOT NULL,
  note          TEXT,
  PRIMARY KEY (file_id, version)
)
"""

# Append-only location history (file/folder renames + moves). DDL is locked now for
# symmetry with ``tag_revisions``; the move logic is deferred to M6 (PLAN.md §18).
# No ``kind`` column: folders emerge from per-file paths (rename vs move is derivable
# from ``from_path``/``to_path``), and empty source folders are pruned on move. The
# old ``plan_id`` grouping is now ``commit_id`` (shared with ``commits``).
_PATH_REVISIONS_DDL: Final = """
CREATE TABLE IF NOT EXISTS path_revisions (
  file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  version       INTEGER NOT NULL,
  commit_id     INTEGER REFERENCES commits(id),
  created_at    TEXT NOT NULL,
  origin        TEXT NOT NULL,
  reverted_from INTEGER,
  from_path     TEXT NOT NULL,
  to_path       TEXT NOT NULL,
  note          TEXT,
  PRIMARY KEY (file_id, version)
)
"""

# Staging area (git's index): one pending change per file, holding the desired TARGET
# state. There is no ``commit_id`` (no claiming): a staged row stays staged until a
# commit turns it into a real revision row and deletes it. A crash leaves leftover rows
# staged, which the next commit sweeps into a new commit. See PLAN.md §7.
_TAG_REVISIONS_STAGED_DDL: Final = """
CREATE TABLE IF NOT EXISTS tag_revisions_staged (
  file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  managed_tags TEXT NOT NULL,
  origin       TEXT NOT NULL,
  note         TEXT,
  staged_at    TEXT NOT NULL,
  PRIMARY KEY (file_id)
)
"""

_PATH_REVISIONS_STAGED_DDL: Final = """
CREATE TABLE IF NOT EXISTS path_revisions_staged (
  file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  to_path   TEXT NOT NULL,
  origin    TEXT NOT NULL,
  note      TEXT,
  staged_at TEXT NOT NULL,
  PRIMARY KEY (file_id)
)
"""


def apply_schema(connection: sqlite3.Connection) -> None:
    """Create all tables (idempotently) and stamp ``PRAGMA user_version``.

    Creating tables on a fresh ledger needs no migration: the ``CREATE TABLE IF NOT
    EXISTS`` statements create whatever is missing and the ``PRAGMA user_version``
    re-stamp advances the version. NOTE: this does **not** alter columns of tables that
    already exist, nor rename them, so a *pre-release* dev ledger from an earlier schema
    must be deleted and rescanned. In particular **a v4 ledger must be deleted before
    v5**: v5 drops the ``commit_id`` column from both staged tables
    (``tag_revisions_staged`` / ``path_revisions_staged``) and ``apply_schema`` cannot
    drop a column. (v4 renamed the staging tables
    ``staged_tag_revisions``→``tag_revisions_staged`` and
    ``staged_path_revisions``→``path_revisions_staged``; v3 renamed
    ``batch_id``→``commit_id`` and dropped ``kind``/``plan_id``.) There is no production
    data to migrate yet.

    Staged rows no longer carry a ``commit_id``; a lingering ``'applying'`` commit means
    an interrupted run, recovered by simply committing again.

    ``commits`` is created before the revision/staging tables that reference it.
    """
    logger.debug("applying schema version %d", SCHEMA_VERSION)
    connection.execute(_FILES_DDL)
    connection.execute(_FILE_TAGS_DDL)
    connection.execute(_FILE_TAGS_INDEX_DDL)
    connection.execute(_COMMITS_DDL)
    connection.execute(_TAG_REVISIONS_DDL)
    connection.execute(_PATH_REVISIONS_DDL)
    connection.execute(_TAG_REVISIONS_STAGED_DDL)
    connection.execute(_PATH_REVISIONS_STAGED_DDL)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
