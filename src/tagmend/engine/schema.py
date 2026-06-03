"""SQLite schema for the library snapshot (M1).

Defines the tables that hold the read-path snapshot: ``files`` (one row per audio
file, anchored by its ``(folder, filename)`` at first scan and assigned a stable
integer ``id`` surrogate) and ``file_tags`` (normalized EAV rows for each tag value).
The ``id`` is the durable identity that all future history tables (M2/M3 onward)
reference, per PLAN.md §7.

Later milestones add their own tables (``artist_cache``, ``lastfm_cache``,
``tag_revisions``); they are *not* created here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

logger = get_logger(__name__)

SCHEMA_VERSION: Final = 1

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


def apply_schema(connection: sqlite3.Connection) -> None:
    """Create the M1 tables (idempotently) and stamp ``PRAGMA user_version``."""
    logger.debug("applying schema version %d", SCHEMA_VERSION)
    connection.execute(_FILES_DDL)
    connection.execute(_FILE_TAGS_DDL)
    connection.execute(_FILE_TAGS_INDEX_DDL)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
