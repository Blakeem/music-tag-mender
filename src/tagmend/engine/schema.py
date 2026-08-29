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
never updated/deleted).

The M2 Last.fm genre path adds two side tables (PLAN — Last.fm genre tagging, phase 1):

* ``lastfm_cache`` — a persistent cache of parsed Last.fm tag lists keyed by a request
  hash, surviving MCP restarts. ``found`` is the negative-cache sentinel (0 = the
  artist/album genuinely is not on Last.fm; 1 = found), distinct from ``found=1`` with an
  empty ``tags`` array.
* ``file_genre_status`` — stores ONLY the terminal/negative per-file decisions
  (``'no_match'`` / ``'manual'``). "Done" is *derived* elsewhere from the staged/committed
  revision tables, so there is deliberately no ``'tagged'`` state to desync.

The M4 artist normalization path adds one more side table (schema v7, purely additive):

* ``file_artist_status`` — the artist-axis twin of ``file_genre_status``, storing ONLY the
  sticky ``'manual'`` exclusion (no ``'no_match'`` state on this axis). "Done"/"staged" are
  *derived* field-awarely from the revision tables (keyed on ``artist``/``albumartist``).

The album original-year path adds two more side tables (schema v8, purely additive — a v7
ledger upgrades in place with no data loss):

* ``file_year_status`` (named ``file_album_status`` until v12) — the year-axis twin of
  ``file_genre_status`` (same identity: ``(albumartist-else-artist, album)`` →
  ``source_artist``/``source_album``), storing the ``'no_match'`` / ``'manual'`` decisions;
  "done"/"staged" are *derived* field-awarely (keyed on ``originaldate``).
* ``musicbrainz_cache`` — a persistent cache of MusicBrainz release-group lookups keyed by a
  request hash. ``found`` is the negative-cache sentinel (0 = no usable Album release group),
  mirroring ``lastfm_cache``.

The mismatch-fix re-pend path adds one more side table (schema v9, purely additive — a v8
ledger upgrades in place with no data loss):

* ``voided_auto`` — a per-``(file_id, field)`` watermark that "voids" stale auto-resolved
  values without mutating the append-only history: after a manual identity fix,
  :func:`tagmend.engine.store.void_auto_changes` stamps the field's current
  ``MAX(version)`` here, and :func:`~tagmend.engine.store.has_auto_change_for` ignores auto
  revisions at or below the watermark, so the genre/originaldate re-pend. A LATER auto
  revision (``version`` above the watermark) counts again, so re-processing is safe.

The mismatch-fix review surface adds one more side table (schema v10, purely additive — a v9
ledger upgrades in place with no data loss):

* ``file_mismatch_status`` — the 4th per-file status table (the mismatch-axis twin of the
  three shipped status tables). Stores ONLY the sticky per-file dispositions
  (``'legit_ignore'`` — a false positive to silence; ``'misfiled_deferred'`` — a misfiled
  file deferred for later). An accepted fix needs NO row: once the tag agrees with the path,
  the detector stops flagging it. ``source_field``/``source_value`` snapshot the disagreeing
  tag (which of ``albumartist``/``artist`` + its value at decision time) so a later tag change
  makes the disposition stale and the file re-surfaces. Unlike the other three axes this one
  has NO ``staged``/``done`` derivation, so it never routes through ``derived_status``.

The album-gaps MusicBrainz recording-search tier adds one more side table (schema v11, purely
additive — a v10 ledger upgrades in place with no data loss):

* ``musicbrainz_recording_cache`` — a persistent cache of MusicBrainz recording-search
  lookups keyed by a request hash (the ``(artist, title)`` twin of ``musicbrainz_cache``,
  mirroring ``lastfm_cache``). ``found`` is the negative-cache sentinel (0 = no usable Album
  release group for the recording; 1 = found). The found columns hold the selected recording's
  release-group title/id + the recording MBID. Feeds ``detect_album_gaps``' review-only tier;
  cache writes are its ONLY ledger writes.

The tool-naming pass renames one table (schema v12, no new tables — a v11 ledger upgrades in
place with no data loss):

* ``file_album_status`` → ``file_year_status``. The album *axis* became the **year** axis
  (it fills ``originaldate``, never the ``album`` tag), so its status table follows the axis
  name. :func:`apply_schema` performs a SQLite-native ``ALTER TABLE ... RENAME TO`` before the
  DDL runs, so every stored ``'no_match'`` / ``'manual'`` disposition is preserved. Nothing
  about the album *entity* changes (``musicbrainz_cache``, the ``album`` tag, ``list_albums``).

The revert-fidelity pass adds one column (schema v13, no new tables — a v12 ledger upgrades in
place with every row preserved):

* ``tag_revisions.managed_set`` — which managed-tag set governed that revision (the versions in
  :data:`tagmend.engine.tags.MANAGED_SETS`). Without it, revert cannot tell a snapshot that
  omits a tag because it was empty from one that omits it because the set did not track it yet,
  so it preserved every widened field on every snapshot and reported success while changing
  nothing. :func:`_migrate_v13_managed_set` stamps pre-existing rows by capture date.

The scan-staleness pass adds one column (schema v14, no new tables — a v13 ledger upgrades in
place with every row preserved):

* ``files.reader_version`` — which tag reader produced that snapshot row (the value of
  :data:`tagmend.engine.tags.TAG_READER_VERSION` at read time). An incremental scan re-reads
  only on a size/mtime change, so a row written by an older reader would never refresh and
  every detector would keep reading it. :func:`_migrate_v14_reader_version` defaults
  pre-existing rows to 0, below any real reader version, so the next incremental scan
  re-reads each of them exactly once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

logger = get_logger(__name__)

SCHEMA_VERSION: Final = 15

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
  reader_version  INTEGER NOT NULL DEFAULT 0,
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
# without replaying the chain. ``managed_set`` records WHICH managed-tag set that
# snapshot governed (:data:`tagmend.engine.tags.MANAGED_SETS`), so revert knows whether an
# omitted tag means "empty then" (delete it) or "not tracked then" (keep it).
# See PLAN.md §7 / §22.
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
  managed_set   INTEGER,
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

# Persistent cache of parsed Last.fm tag lists, keyed by a request hash (so it survives
# MCP restarts and inspector re-launches). ``found`` is the negative-cache sentinel
# (0 = artist/album genuinely absent from Last.fm; 1 = found), distinct from ``found=1``
# with an empty ``tags`` array. ``tags`` is a JSON array of ``[name, weight]`` pairs
# (``[]`` when found-but-empty, or when not found). See PLAN — Last.fm genre tagging §
# "Caching & pacing".
_LASTFM_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS lastfm_cache (
  request_key TEXT PRIMARY KEY,
  found       INTEGER NOT NULL,
  tags        TEXT NOT NULL,
  fetched_at  TEXT NOT NULL
)
"""

# Per-file terminal/negative genre decisions. Stores ONLY the two outcomes that are not
# otherwise represented by the revision tables: ``'no_match'`` (nothing usable on
# Last.fm) and ``'manual'`` (user/LLM excluded it). "Done" is DERIVED elsewhere from the
# staged/committed revision tables, so there is no ``'tagged'`` state to desync.
# ``source_artist``/``source_album`` record what the decision was computed against, so a
# later tag change makes a ``'no_match'`` stale and re-processable. See PLAN —
# Last.fm genre tagging § "Status model".
_FILE_GENRE_STATUS_DDL: Final = """
CREATE TABLE IF NOT EXISTS file_genre_status (
  file_id       INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  status        TEXT NOT NULL,
  source_artist TEXT,
  source_album  TEXT,
  updated_at    TEXT NOT NULL
)
"""

# Per-file sticky artist-name exclusion (the artist-axis twin of ``file_genre_status``).
# Stores ONLY the ``'manual'`` decision (the artist model has no ``'no_match'`` state):
# a user/LLM excludes a file so :func:`tagmend.engine.artists.resolve_artists` always
# skips it. "Done"/"staged" are DERIVED elsewhere from the field-aware revision tables, so
# there is no state here to desync. ``source_artist``/``source_albumartist`` record the
# values the exclusion was taken against, for audit. See PLAN — artist normalization.
_FILE_ARTIST_STATUS_DDL: Final = """
CREATE TABLE IF NOT EXISTS file_artist_status (
  file_id             INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  status              TEXT NOT NULL,
  source_artist       TEXT,
  source_albumartist  TEXT,
  updated_at          TEXT NOT NULL
)
"""

# Per-file terminal/negative year decisions (the year-axis twin of ``file_genre_status``,
# identical shape). Stores ONLY ``'no_match'`` (no usable MusicBrainz Album release group)
# and ``'manual'`` (user/LLM excluded it). "Done"/"staged" are DERIVED elsewhere from the
# field-aware revision tables (keyed on ``originaldate``), so there is no state here to
# desync. ``source_artist``/``source_album`` record the resolved identity
# (``albumartist``-else-``artist`` + ``album``) the decision was taken against, so a later
# tag change makes a ``'no_match'`` stale and re-processable. Named ``file_album_status``
# before v12. See PLAN — year axis.
_FILE_YEAR_STATUS_DDL: Final = """
CREATE TABLE IF NOT EXISTS file_year_status (
  file_id       INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  status        TEXT NOT NULL,
  source_artist TEXT,
  source_album  TEXT,
  updated_at    TEXT NOT NULL
)
"""

# Persistent cache of MusicBrainz release-group lookups, keyed by a request hash (so it
# survives MCP restarts), mirroring ``lastfm_cache``. ``found`` is the negative-cache
# sentinel (0 = no usable Album release group; 1 = found). The found columns hold the
# selected release group's original ``first-release-date`` and ids. See PLAN — year axis.
_MUSICBRAINZ_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS musicbrainz_cache (
  request_key      TEXT PRIMARY KEY,
  found            INTEGER NOT NULL,
  album_title      TEXT,
  original_date    TEXT,
  release_mbid     TEXT,
  release_group_id TEXT,
  fetched_at       TEXT NOT NULL
)
"""

# Persistent cache of MusicBrainz recording-search lookups, keyed by a request hash (so it
# survives MCP restarts), mirroring ``musicbrainz_cache`` for the ``(artist, title)`` axis.
# ``found`` is the negative-cache sentinel (0 = no usable Album release group for the
# recording; 1 = found). The found columns hold the selected recording's release-group
# title/id + the recording MBID. Feeds ``detect_album_gaps``' review-only tier. See PLAN —
# album-gaps recording tier.
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

_MUSICBRAINZ_RECORDING_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS musicbrainz_recording_cache (
  request_key      TEXT PRIMARY KEY,
  found            INTEGER NOT NULL,
  album_title      TEXT,
  release_group_id TEXT,
  recording_mbid   TEXT,
  fetched_at       TEXT NOT NULL
)
"""

# Per-``(file_id, field)`` watermark that voids stale auto-resolved values WITHOUT touching
# the append-only ``tag_revisions`` history. ``voided_through_version`` records the field's
# ``MAX(version)`` at the moment of a manual identity fix; ``has_auto_change_for`` then
# ignores auto revisions whose ``version`` is at or below it, so the field re-pends. A later
# auto revision (``version`` above the watermark) counts again — re-processing is safe. See
# PLAN — mismatch-fix (NN7 re-pend primitive).
_VOIDED_AUTO_DDL: Final = """
CREATE TABLE IF NOT EXISTS voided_auto (
  file_id               INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  field                 TEXT NOT NULL,
  voided_through_version INTEGER NOT NULL,
  voided_at             TEXT NOT NULL,
  PRIMARY KEY (file_id, field)
)
"""

# Per-file mismatch disposition (the 4th per-file status table; the mismatch-axis twin of
# ``file_genre_status``). Stores ONLY the sticky dispositions the detector honours:
# ``'legit_ignore'`` (a false positive to silence) and ``'misfiled_deferred'`` (a misfiled
# file deferred for later). An accepted fix needs NO row — once the tag agrees with the path
# the detector stops flagging it. ``source_field`` (``'albumartist'``/``'artist'``) +
# ``source_value`` snapshot the disagreeing tag at decision time, so a later tag change makes
# the disposition stale and the file re-surfaces. See PLAN — mismatch-fix (review surface).
_FILE_MISMATCH_STATUS_DDL: Final = """
CREATE TABLE IF NOT EXISTS file_mismatch_status (
  file_id       INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  status        TEXT NOT NULL,
  source_field  TEXT,
  source_value  TEXT,
  updated_at    TEXT NOT NULL
)
"""


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


def _migrate_v12_year_status(connection: sqlite3.Connection) -> None:
    """v12: rename ``file_album_status`` → ``file_year_status``, preserving every row.

    Runs BEFORE the DDL so the ``CREATE TABLE IF NOT EXISTS`` below finds the renamed table
    and does not create an empty second one. The rename is SQLite-native (``ALTER TABLE ...
    RENAME TO``), so all stored ``'no_match'``/``'manual'`` dispositions survive. Idempotent:
    it fires only when the old table exists and the new one does not (a fresh ledger has
    neither, a v12+ ledger has only the new one).
    """
    if not _table_exists(connection, "file_album_status"):
        return
    if _table_exists(connection, "file_year_status"):
        return
    connection.execute("ALTER TABLE file_album_status RENAME TO file_year_status")
    logger.info("schema v12: renamed file_album_status to file_year_status")


# The date the managed set widened from 5 tags to 18 (managed-set version 1 -> 2 in
# :data:`tagmend.engine.tags.MANAGED_SETS`). Frozen history: a later widening adds a new
# version and never restamps rows through this constant.
_MANAGED_SET_WIDENING_DATE: Final = "2026-07-04"


def _migrate_v13_managed_set(connection: sqlite3.Connection) -> None:
    """v13: add ``tag_revisions.managed_set`` and stamp existing rows by capture date.

    Runs BEFORE the DDL, so the guard leads with ``_table_exists``: on a fresh ledger
    ``tag_revisions`` does not exist yet and a bare ``ALTER TABLE`` would raise "no such
    table" on every first run. A fresh ledger skips this and takes the column from
    :data:`_TAG_REVISIONS_DDL` instead. Idempotent: the ``_column_exists`` half stops a
    second application.

    Capture date is the only evidence a v12 row carries about which set governed it, and
    ``created_at`` is an ISO-8601 UTC string, so the comparison is lexicographic. Commits
    itself, since a read-only caller would otherwise discard the stamp (see below).
    """
    if not _table_exists(connection, "tag_revisions"):
        return
    if _column_exists(connection, "tag_revisions", "managed_set"):
        return
    connection.execute("ALTER TABLE tag_revisions ADD COLUMN managed_set INTEGER")
    connection.execute(
        "UPDATE tag_revisions SET managed_set = CASE WHEN created_at >= ? THEN 2 ELSE 1 END",
        (_MANAGED_SET_WIDENING_DATE,),
    )
    # The stamp is DML, so sqlite3 opens an implicit transaction for it. Every caller runs
    # apply_schema straight after db.connect and many never commit (read-only paths), which
    # would roll the stamp back while the autocommitted ADD COLUMN survives — leaving the
    # column present but NULL, so the migration could never run again. Commit it here.
    connection.commit()
    logger.info("schema v13: stamped tag_revisions.managed_set on pre-existing rows")


def _migrate_v14_reader_version(connection: sqlite3.Connection) -> None:
    """v14: add ``files.reader_version``, defaulting pre-existing rows below any reader.

    Runs BEFORE the DDL, so the guard leads with ``_table_exists``: on a fresh ledger
    ``files`` does not exist yet and a bare ``ALTER TABLE`` would raise "no such table" on
    every first run. A fresh ledger skips this and takes the column from :data:`_FILES_DDL`
    instead. Idempotent: the ``_column_exists`` half stops a second application.

    The ``DEFAULT 0`` backfills every existing row below :data:`TAG_READER_VERSION`, which is
    the point — those rows were read by an unknown older reader, so the next incremental scan
    must re-read each of them once. No DML follows, so unlike v13 there is no stamp to commit.
    """
    if not _table_exists(connection, "files"):
        return
    if _column_exists(connection, "files", "reader_version"):
        return
    connection.execute("ALTER TABLE files ADD COLUMN reader_version INTEGER NOT NULL DEFAULT 0")
    logger.info("schema v14: added files.reader_version (pre-existing rows default to 0)")


def apply_schema(connection: sqlite3.Connection) -> None:
    """Create all tables (idempotently) and stamp ``PRAGMA user_version``.

    Creating tables on a fresh ledger needs no migration: the ``CREATE TABLE IF NOT
    EXISTS`` statements create whatever is missing and the ``PRAGMA user_version``
    re-stamp advances the version. NOTE: this does **not** alter columns of tables that
    already exist, so a *pre-release* dev ledger from an earlier schema must be deleted and
    rescanned. In particular **a v4 ledger must be deleted before
    v5**: v5 drops the ``commit_id`` column from both staged tables
    (``tag_revisions_staged`` / ``path_revisions_staged``) and ``apply_schema`` cannot
    drop a column. (v4 renamed the staging tables
    ``staged_tag_revisions``→``tag_revisions_staged`` and
    ``staged_path_revisions``→``path_revisions_staged``; v3 renamed
    ``batch_id``→``commit_id`` and dropped ``kind``/``plan_id``.) There is no production
    data to migrate yet.

    The in-place migrations are v12's table rename (``file_album_status`` →
    ``file_year_status``), applied by :func:`_migrate_v12_year_status` before any DDL runs so
    a v11 ledger upgrades with its dispositions intact. v13 adds
    ``tag_revisions.managed_set`` the same way (:func:`_migrate_v13_managed_set`), and v14
    adds ``files.reader_version`` (:func:`_migrate_v14_reader_version`).

    Staged rows no longer carry a ``commit_id``; a lingering ``'applying'`` commit means
    an interrupted run, recovered by simply committing again.

    ``commits`` is created before the revision/staging tables that reference it.
    """
    logger.debug("applying schema version %d", SCHEMA_VERSION)
    _migrate_v12_year_status(connection)
    _migrate_v13_managed_set(connection)
    _migrate_v14_reader_version(connection)
    connection.execute(_FILES_DDL)
    connection.execute(_FILE_TAGS_DDL)
    connection.execute(_FILE_TAGS_INDEX_DDL)
    connection.execute(_COMMITS_DDL)
    connection.execute(_TAG_REVISIONS_DDL)
    connection.execute(_PATH_REVISIONS_DDL)
    connection.execute(_TAG_REVISIONS_STAGED_DDL)
    connection.execute(_PATH_REVISIONS_STAGED_DDL)
    connection.execute(_LASTFM_CACHE_DDL)
    connection.execute(_FILE_GENRE_STATUS_DDL)
    connection.execute(_FILE_ARTIST_STATUS_DDL)
    connection.execute(_FILE_YEAR_STATUS_DDL)
    connection.execute(_MUSICBRAINZ_CACHE_DDL)
    connection.execute(_MUSICBRAINZ_RECORDING_CACHE_DDL)
    connection.execute(_MUSICBRAINZ_ARTIST_CACHE_DDL)
    connection.execute(_VOIDED_AUTO_DDL)
    connection.execute(_FILE_MISMATCH_STATUS_DDL)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
