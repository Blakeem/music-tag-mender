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
* ``path_revisions`` — the location history (PLAN.md §18), written by
  :mod:`tagmend.engine.paths`. Its paths are relative to ``music_path``.

The two revision logs are append-only, keyed by ``files.id`` with a composite PK
``(file_id, version)`` (version 0 = baseline with ``commit_id`` NULL, +1 per change).
Triggers enforce it: an ``UPDATE`` or ``DELETE`` on either log aborts, and so does a
``DELETE FROM files`` that would cascade into one.

:func:`apply_schema` runs the migrations and the DDL only when the stored
``PRAGMA user_version`` is older than :data:`SCHEMA_VERSION`, so every later schema change
must bump :data:`SCHEMA_VERSION`.

The M2 Last.fm genre path adds two side tables (PLAN — Last.fm genre tagging, phase 1):

* ``lastfm_cache`` — a persistent cache of parsed Last.fm tag lists keyed by a request
  hash, surviving MCP restarts. ``found`` is the negative-cache sentinel (0 = the
  artist/album genuinely is not on Last.fm; 1 = found), distinct from ``found=1`` with an
  empty ``tags`` array.
* ``file_genre_status``: the genre axis's per-file status row. v21 below gives its shape.

The M4 artist normalization path adds one more side table (schema v7, purely additive):

* ``file_artist_status``: the artist-axis twin of ``file_genre_status``.

The album original-year path adds two more side tables (schema v8, purely additive — a v7
ledger upgrades in place with no data loss):

* ``file_year_status`` (named ``file_album_status`` until v12): the year-axis twin of
  ``file_genre_status``.
* ``musicbrainz_release_group_cache`` (named ``musicbrainz_cache`` until v20) is a persistent
  cache of MusicBrainz release-group lookups keyed by a request hash. ``found`` is the
  negative-cache sentinel (0 = no usable Album release group), mirroring ``lastfm_cache``.

The mismatch-fix re-pend path adds one more side table (schema v9, purely additive — a v8
ledger upgrades in place with no data loss):

* ``voided_auto``: a per-``(file_id, field)`` watermark that re-opened auto-resolved values
  after a manual identity fix. v21 drops it.

The mismatch-fix review surface adds one more side table (schema v10, purely additive — a v9
ledger upgrades in place with no data loss):

* ``file_mismatch_status``: one path decision per file (``legit_ignore`` or
  ``misfiled_deferred``). An accepted fix needs no row. v24 below gives its shape.

The album-gaps MusicBrainz recording-search tier adds one more side table (schema v11, purely
additive — a v10 ledger upgrades in place with no data loss):

* ``musicbrainz_recording_cache`` — a persistent cache of MusicBrainz recording-search
  lookups keyed by a request hash (the ``(artist, title)`` twin of the release-group cache,
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
  about the album *entity* changes (the release-group cache, the ``album`` tag, ``list_albums``).

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

The artist-by-MBID lookup adds one cache (schema v15, purely additive, created by its DDL with
no migration):

* ``musicbrainz_artist_cache`` holds the canonical name, sort name, disambiguation and alias
  set per artist MBID. ``found`` is the negative-cache sentinel. Feeds ``resolve_artists``'
  MusicBrainz tier.

The release-by-MBID lookup adds one cache (schema v16, purely additive, created by its DDL with
no migration):

* ``musicbrainz_release_cache`` holds the parsed release and tracklist as one JSON
  ``payload``, because nothing queries inside it. Feeds ``detect_release_disagreements``.

The data-safety pass adds two columns, two indexes and four triggers (schema v17, no new
tables. A v16 ledger upgrades in place with every row preserved):

* ``tag_revisions_staged.base_size_bytes`` / ``base_mtime_ns``: the file's signature when it
  was staged. A commit refuses a file whose signature moved since, because writing the stored
  target would overwrite the external edit. :func:`_migrate_staged_base_signature` adds both as
  NULL on pre-existing rows, which skips the check for them.
* ``idx_tag_revisions_commit_id`` / ``idx_path_revisions_commit_id``: the per-commit change
  set that ``revert_commit`` and ``reopen_axes`` read.
* The four append-only triggers from :func:`apply_append_only_triggers`. A migration that
  rebuilds or updates a log drops its triggers first and relies on the DDL phase, which runs
  after every migration, to recreate them.

The path-identity pass adds one column and one index (schema v18, no new tables. A v17 ledger
upgrades in place with every row preserved):

* ``files.path_key`` + ``idx_files_path_key`` (UNIQUE): the platform identity key of the file's
  path (:mod:`tagmend.engine.path_keys`). NTFS ignores case, so ``(folder, filename)`` alone let
  one file on disk become two rows. The column is nullable in both shapes, and every engine
  insert sets it. :func:`_migrate_files_path_key` backfills it and refuses a ledger in which two
  rows already share a key.

The commit-origin pass changes no shape (schema v19. A v18 ledger upgrades in place with every
row preserved):

* ``commits.origin`` is derived from the staged rows a commit sweeps, so a commit is ``auto``
  only when every change in it came from a resolver. Earlier builds stamped every MCP commit
  ``manual``. :func:`_migrate_commit_origin` restamps each ``manual`` commit whose revisions
  are all ``auto``.

The naming and schema-hygiene pass renames, moves and drops in place (schema v20. A v19 ledger
upgrades in place with every row preserved):

* ``tag_revisions.reverted_from`` becomes ``reverted_to_version``, since it holds the version a
  revert restored. ``commits.reverted_from`` keeps its name, since it holds the undone commit.
* ``musicbrainz_cache`` becomes ``musicbrainz_release_group_cache``, and ``release_group_id``
  becomes ``release_group_mbid`` in both MusicBrainz caches that carry it.
* ``files.status`` is dropped. Only its DEFAULT ever wrote it, and nothing read it.
* ``lastfm_correction_cache`` holds ``artist.getCorrection`` answers in typed columns.
  :func:`_migrate_lastfm_correction_cache` moves the library's correction rows out of
  ``lastfm_cache``, which now holds top tags only.
* The ``tag_revisions_managed_set_required`` trigger refuses a revision without a managed set.
  :func:`_migrate_managed_set_required` refuses to upgrade a ledger already holding one.

The axis-status pass adds one column to four tables and drops one table (schema v21. A v20
ledger upgrades in place with every status and staged row preserved):

* ``file_genre_status``, ``file_artist_status`` and ``file_year_status`` each hold at most one
  outcome row per file (``done``, ``no_match`` or ``manual``) with two snapshots: the identity
  it was decided against in the two ``source_*`` columns and the new ``source_value``, the JSON
  of the axis fields' values. :func:`tagmend.engine.store.derived_status` derives every status
  from that row (:mod:`tagmend.engine.axis`).
* :func:`_migrate_axis_outcomes` adds ``source_value`` (NULL on existing rows), writes
  ``manual`` rows from the manual revisions and drops ``voided_auto``.
* ``tag_revisions_staged.changed_fields``: the JSON list of fields a staged target changes
  against the tags on disk at stage time. The commit records ``manual`` on the axes it names.
  :func:`_migrate_staged_changed_fields` adds it as NULL, and the commit then falls back to
  the diff against the tags on disk at commit time.

The audio-identification layer adds two caches (schema v22, purely additive, created by its DDL
with no migration):

* ``fingerprint_cache`` holds one fpcalc outcome per file at the files-row signature it was
  taken at: the fingerprint and duration, or the failing exit code with both NULL.
* ``acoustid_cache`` holds one AcoustID lookup per request hash, the parsed result as
  zlib-compressed JSON. ``found`` is the negative-cache sentinel (0 = no match).

The song axis adds one table and one column (schema v23. A v22 ledger upgrades in place):

* ``file_song_status``: the song-axis twin of ``file_genre_status``. Its identity snapshot is
  the release and release-track ids, so an identity fix that rebinds a file re-opens it.
* ``tag_revisions_staged.supplied_keys``: the JSON list of the keys the caller supplied for a
  staged file. :func:`_migrate_staged_supplied_keys` adds it as NULL.

The path-decision pass reshapes one table (schema v24. A v23 ledger upgrades in place with every
row preserved):

* ``file_mismatch_status.source_value`` holds one JSON snapshot per decision. It records the
  names the decision covers, the values of their tags, the file's path version and, on a
  ``legit_ignore`` row, the key of the folder it keeps. ``source_field`` is dropped.
  :func:`_migrate_mismatch_covers` rewrites each earlier row as a decision on the top-folder
  comparison alone.

The path executor gives the path staging area its commit inputs (schema v25. A v24 ledger
upgrades in place with every row preserved):

* ``path_revisions_staged.to_key``: the identity key of the target, relative to ``music_path``,
  with the UNIQUE ``idx_path_revisions_staged_to_key``, so two staged moves never share a target.
* ``base_size_bytes`` / ``base_mtime_ns``: the file's signature at stage time. A move keeps both,
  which is how a commit recognises a file that already landed at its target.
* ``reverted_from``: on a revert row, the version whose location it restores.
  :func:`_migrate_path_staging` adds the four columns as NULL on earlier rows.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Final, cast

from tagmend.engine import axis, path_keys
from tagmend.engine.tags import governed_tags
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

logger = get_logger(__name__)

SCHEMA_VERSION: Final = 25

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

# Append-only managed-tag content history. One row per file per change. The
# :func:`apply_append_only_triggers` triggers abort any UPDATE or DELETE, the cascade from
# ``files`` included. ``version`` (0 = baseline) is both the ordering key and the restore
# handle. ``created_at`` is display-only. ``commit_id`` groups the change with the
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
  reverted_to_version INTEGER,
  managed_tags  TEXT NOT NULL,
  diff          TEXT NOT NULL,
  note          TEXT,
  managed_set   INTEGER,
  PRIMARY KEY (file_id, version)
)
"""

# Append-only location history (file/folder renames + moves), enforced by the same
# :func:`apply_append_only_triggers` triggers as ``tag_revisions``. ``reverted_from`` holds the
# version whose location a revert restored.
# No ``kind`` column: rename and move are derivable from ``from_path``/``to_path``.
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
# ``base_size_bytes``/``base_mtime_ns`` are the file's signature at stage time, so a commit can
# refuse a file edited since. ``changed_fields`` keeps the stage's own change because a re-applied
# commit finds disk already equal to the target. ``supplied_keys`` tells a value the caller
# confirmed from one the merge kept. Migrations append these, so they sit last.
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

# ``to_path`` and ``to_key`` are relative to ``music_path``, so a staged move survives the
# library being promoted to another folder. A migration appends the last four columns, so they
# stay nullable.
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
  reverted_from   INTEGER,
  PRIMARY KEY (file_id)
)
"""

_PATH_REVISIONS_STAGED_TO_KEY_INDEX_DDL: Final = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_path_revisions_staged_to_key "
    "ON path_revisions_staged(to_key)"
)

# Persistent cache of parsed Last.fm top-tag lists only, keyed by a request hash (so it
# survives MCP restarts and inspector re-launches). ``found`` is the negative-cache sentinel
# (0 = artist/album genuinely absent from Last.fm; 1 = found), distinct from ``found=1``
# with an empty ``tags`` array. ``tags`` is a JSON array of ``[name, weight]`` pairs
# (``[]`` when found-but-empty, or when not found). See PLAN, Last.fm genre tagging,
# "Caching & pacing".
_LASTFM_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS lastfm_cache (
  request_key TEXT PRIMARY KEY,
  found       INTEGER NOT NULL,
  tags        TEXT NOT NULL,
  fetched_at  TEXT NOT NULL
)
"""

# Persistent cache of ``artist.getCorrection`` answers, keyed like ``lastfm_cache``. ``found``
# is the negative-cache sentinel (0 = no correction). A found row carries the canonical
# ``name`` and the ``mbid`` when Last.fm gives one.
_LASTFM_CORRECTION_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS lastfm_correction_cache (
  request_key TEXT PRIMARY KEY,
  found       INTEGER NOT NULL,
  name        TEXT,
  mbid        TEXT,
  fetched_at  TEXT NOT NULL
)
"""

# The three tag-axis status tables share one shape (:mod:`tagmend.engine.axis`). Each row is
# one outcome (``done``/``no_match``/``manual``), the identity it was decided against in the
# two ``source_*`` columns, and ``source_value``: the JSON of the axis fields' values it
# describes, NULL on a row written before v21. A ``done``/``no_match`` row counts only while
# both snapshots match the file, so no writer is needed to re-open one.
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

# Persistent cache of MusicBrainz release-group lookups, keyed by a request hash (so it
# survives MCP restarts), mirroring ``lastfm_cache``. ``found`` is the negative-cache
# sentinel (0 = no usable Album release group; 1 = found). The found columns hold the
# selected release group's original ``first-release-date`` and MBIDs. Named
# ``musicbrainz_cache`` before v20. See PLAN, year axis.
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

# Persistent cache of MusicBrainz release lookups by release MBID, keyed by a request hash
# carrying its own parse-rule version. ``found`` is the negative-cache sentinel (0 = no
# release under that id; 1 = found). A release is a nested document (media, each with
# tracks), so the parsed form is one JSON ``payload`` rather than three shredded tables:
# nothing queries inside it, since the key is always the MBID and the caller wants the whole
# tracklist. Feeds ``detect_release_disagreements``.
_MUSICBRAINZ_RELEASE_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS musicbrainz_release_cache (
  request_key TEXT PRIMARY KEY,
  found       INTEGER NOT NULL,
  payload     TEXT,
  fetched_at  TEXT NOT NULL
)
"""

# Persistent cache of MusicBrainz artist lookups by artist MBID, keyed by a request hash
# carrying its own parse-rule version. ``found`` is the negative-cache sentinel (0 = no
# artist under that id; 1 = found). The found columns hold the canonical name, the sort name
# and the alias set, which is what tells a name worth merging from a per-track credit.
# Feeds ``resolve_artists``' MusicBrainz tier.
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

# Persistent cache of MusicBrainz recording-search lookups, keyed by a request hash (so it
# survives MCP restarts), mirroring ``musicbrainz_release_group_cache`` for the
# ``(artist, title)`` axis.
# ``found`` is the negative-cache sentinel (0 = no usable Album release group for the
# recording; 1 = found). The found columns hold the selected recording's release-group
# title/id + the recording MBID. Feeds ``detect_album_gaps``' review-only tier. See PLAN —
# album-gaps recording tier.
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

# One fpcalc outcome per file, reused while ``size_bytes`` and ``mtime_ns`` equal the files row.
# The raw fingerprint is kept so an expired or failed lookup re-queries without re-running fpcalc,
# and a stored failure (NULL fingerprint and duration) keeps an undecodable file from re-running.
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

# Persistent cache of AcoustID lookups, keyed by a hash of the fingerprint, the duration, the
# requested meta and a parse version. ``found`` is the negative-cache sentinel (0 = no match),
# served only while young because AcoustID learns new fingerprints over time.
_ACOUSTID_CACHE_DDL: Final = """
CREATE TABLE IF NOT EXISTS acoustid_cache (
  request_key TEXT PRIMARY KEY,
  found       INTEGER NOT NULL,
  payload     BLOB,
  fetched_at  TEXT NOT NULL
)
"""

# One path decision per file. ``legit_ignore`` keeps the file's folder, and
# ``misfiled_deferred`` lets the tags render the entire path. ``source_value`` is the JSON
# snapshot that binds the decision to its tags and location
# (:class:`tagmend.engine.store.MismatchStatusRow`). It stays nullable because the v24 upgrade
# drops a column in place and cannot add NOT NULL.
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

_APPEND_ONLY_LOGS: Final = ("tag_revisions", "path_revisions")

# Revert reads an omitted tag by the revision's managed set, so a NULL would silently change
# what a revert deletes. SQLite cannot add NOT NULL to an existing column, so a trigger does.
_MANAGED_SET_REQUIRED_TRIGGER_DDL: Final = (
    "CREATE TRIGGER IF NOT EXISTS tag_revisions_managed_set_required "
    "BEFORE INSERT ON tag_revisions WHEN NEW.managed_set IS NULL "
    "BEGIN SELECT RAISE(ABORT, 'tag_revisions.managed_set is required'); END"
)


def apply_append_only_triggers(connection: sqlite3.Connection) -> None:
    """Create the triggers that abort any ``UPDATE`` or ``DELETE`` on the two revision logs.

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
    # Test ledgers are built by downgrading a fresh schema that already carries the trigger.
    connection.execute("DROP TRIGGER IF EXISTS tag_revisions_no_update")
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


def _migrate_staged_base_signature(connection: sqlite3.Connection) -> None:
    """v17: add ``tag_revisions_staged.base_size_bytes`` / ``base_mtime_ns`` as NULL.

    Runs BEFORE the DDL, so the guard leads with ``_table_exists``: on a fresh ledger the table
    does not exist yet and takes both columns from :data:`_TAG_REVISIONS_STAGED_DDL` instead.
    Idempotent: the ``_column_exists`` half stops a second application. A NULL pair marks a row
    staged before the signature existed, and the commit skips the changed-since-stage check
    for it.
    """
    if not _table_exists(connection, "tag_revisions_staged"):
        return
    if _column_exists(connection, "tag_revisions_staged", "base_size_bytes"):
        return
    connection.execute("ALTER TABLE tag_revisions_staged ADD COLUMN base_size_bytes INTEGER")
    connection.execute("ALTER TABLE tag_revisions_staged ADD COLUMN base_mtime_ns INTEGER")
    logger.info("schema v17: added tag_revisions_staged base signature columns")


def _migrate_staged_changed_fields(connection: sqlite3.Connection) -> None:
    """v21: add ``tag_revisions_staged.changed_fields`` as NULL.

    Runs BEFORE the DDL, so a fresh ledger takes the column from
    :data:`_TAG_REVISIONS_STAGED_DDL` instead. Idempotent through ``_column_exists``.
    """
    if not _table_exists(connection, "tag_revisions_staged"):
        return
    if _column_exists(connection, "tag_revisions_staged", "changed_fields"):
        return
    connection.execute("ALTER TABLE tag_revisions_staged ADD COLUMN changed_fields TEXT")
    logger.info("schema v21: added tag_revisions_staged.changed_fields")


def _migrate_staged_supplied_keys(connection: sqlite3.Connection) -> None:
    """v23: add ``tag_revisions_staged.supplied_keys`` as NULL.

    Runs BEFORE the DDL, so a fresh ledger takes the column from
    :data:`_TAG_REVISIONS_STAGED_DDL` instead. Idempotent through ``_column_exists``. A NULL
    marks a row staged before the column existed, which the stale-identity report reads as
    "no key confirmed".
    """
    if not _table_exists(connection, "tag_revisions_staged"):
        return
    if _column_exists(connection, "tag_revisions_staged", "supplied_keys"):
        return
    connection.execute("ALTER TABLE tag_revisions_staged ADD COLUMN supplied_keys TEXT")
    logger.info("schema v23: added tag_revisions_staged.supplied_keys")


def _migrate_path_staging(connection: sqlite3.Connection) -> None:
    """v25: add the path staging columns ``to_key``, the base signature and ``reverted_from``.

    Runs BEFORE the DDL, so a fresh ledger takes the columns from
    :data:`_PATH_REVISIONS_STAGED_DDL` instead. Idempotent through ``_column_exists``. The DDL
    phase adds the unique ``to_key`` index, which several NULL keys do not violate.
    """
    if not _table_exists(connection, "path_revisions_staged"):
        return
    if _column_exists(connection, "path_revisions_staged", "to_key"):
        return
    connection.execute("ALTER TABLE path_revisions_staged ADD COLUMN to_key TEXT")
    connection.execute("ALTER TABLE path_revisions_staged ADD COLUMN base_size_bytes INTEGER")
    connection.execute("ALTER TABLE path_revisions_staged ADD COLUMN base_mtime_ns INTEGER")
    connection.execute("ALTER TABLE path_revisions_staged ADD COLUMN reverted_from INTEGER")
    logger.info("schema v25: added the path staging columns")


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
    """v24: rewrite each mismatch row as a JSON snapshot and drop ``source_field``.

    Runs BEFORE the DDL, so a fresh ledger takes the v24 shape from
    :data:`_FILE_MISMATCH_STATUS_DDL`. Idempotent: it fires only while ``source_field`` exists.
    The rewrite and the drop land in one transaction.
    """
    if not _table_exists(connection, "file_mismatch_status"):
        return
    if not _column_exists(connection, "file_mismatch_status", "source_field"):
        return

    connection.execute("BEGIN")
    try:
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
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
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
    """Raise :class:`RuntimeError` naming every path key two or more *rows* share."""
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
    raise RuntimeError(message)


def _migrate_files_path_key(connection: sqlite3.Connection) -> None:
    """v18: add ``files.path_key``, backfill it and create its UNIQUE index, all or nothing.

    Runs BEFORE the DDL, so the guard leads with ``_table_exists``: a fresh ledger takes the
    column from :data:`_FILES_DDL`. Idempotent: the ``_column_exists`` half stops a second
    application. Two rows sharing a key are one file recorded twice, and only the owner can say
    which row keeps the history, so the upgrade rolls back (the ADD COLUMN included, SQLite DDL
    being transactional) and raises :class:`RuntimeError` naming each collision.
    """
    if not _table_exists(connection, "files"):
        return
    if _column_exists(connection, "files", "path_key"):
        return

    connection.execute("BEGIN")
    try:
        connection.execute("ALTER TABLE files ADD COLUMN path_key TEXT")
        rows = _keyed_file_rows(connection)
        _refuse_path_key_collisions(rows)
        connection.executemany(
            "UPDATE files SET path_key = ? WHERE id = ?",
            [(key, file_id) for file_id, _, _, key in rows],
        )
        connection.execute(_FILES_PATH_KEY_INDEX_DDL)
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
    logger.info("schema v18: backfilled files.path_key on %d row(s)", len(rows))


def _migrate_commit_origin(connection: sqlite3.Connection) -> None:
    """v19: restamp ``auto`` on every ``manual`` commit whose revisions are all ``auto``.

    Runs BEFORE the DDL, so the guard leads with ``_table_exists``: a fresh ledger has no
    commits to restamp. Idempotent: a second run finds no ``manual`` commit left to match. It
    updates ``commits`` only, which carries no append-only trigger. Commits itself like v13,
    since a read-only caller would otherwise roll the restamp back.
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
    """v20: rename ``tag_revisions.reverted_from`` to ``reverted_to_version``, keeping every row.

    Runs BEFORE the DDL, so a fresh ledger skips this and takes the column from
    :data:`_TAG_REVISIONS_DDL`. Idempotent: it fires only while the old column exists and the
    new one does not. A rename fires no UPDATE trigger, so the append-only triggers stay.
    """
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
    the upgrade raises :class:`RuntimeError` naming the count and the first few rows.
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
    raise RuntimeError(message)


def _migrate_release_group_cache_name(connection: sqlite3.Connection) -> None:
    """v20: rename ``musicbrainz_cache`` to ``musicbrainz_release_group_cache``, keeping rows.

    Runs BEFORE the DDL so the ``CREATE TABLE IF NOT EXISTS`` finds the renamed table and does
    not create an empty second one. Idempotent: it fires only when the old table exists and
    the new one does not. Every request key already starts with ``release-group``, so no
    cached row is invalidated.
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

    Runs after :func:`_migrate_release_group_cache_name`. Idempotent: each table renames only
    while it has the old column and lacks the new one.
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

    Runs BEFORE the DDL, so a fresh ledger skips this and its DDL has no such column.
    Idempotent: it fires only while the column exists. No index, constraint, view or trigger
    names the column, so ``DROP COLUMN`` applies.
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

    Runs BEFORE the DDL. A fresh ledger has no ``lastfm_cache`` and takes the table from
    :data:`_LASTFM_CORRECTION_CACHE_DDL`. Idempotent: an existing ``lastfm_correction_cache``
    stops a second run. The CREATE and the row moves land in one transaction. A correction
    row for a value the library no longer carries stays behind unreachable, which costs only
    a re-fetch if that value returns.
    """
    if not _table_exists(connection, "lastfm_cache"):
        return
    if _table_exists(connection, "lastfm_correction_cache"):
        return

    connection.execute("BEGIN")
    try:
        connection.execute(_LASTFM_CORRECTION_CACHE_DDL)
        moved = _move_correction_rows(connection)
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
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

    Runs BEFORE the DDL, so a fresh ledger (no ``tag_revisions``) skips this and takes every
    table from its DDL. In one transaction it creates any tag-axis status table an older
    ledger never had, adds ``source_value`` where it is missing (existing rows keep NULL, which
    the classifier treats as matching), writes ``manual`` from the manual revisions
    (:func:`_replay_manual_revisions`) and drops ``voided_auto``, whose watermarks the
    snapshots replace. It writes no ``done`` row, so a file an auto revision settled reads
    ``pending`` until a resolver re-derives it from cache. Idempotent: it fires only while
    ``voided_auto`` exists or a status table lacks ``source_value``.
    """
    if not _table_exists(connection, "tag_revisions"):
        return
    if _axis_outcomes_migrated(connection):
        return

    connection.execute("BEGIN")
    try:
        for table, ddl in _AXIS_STATUS_TABLES:
            connection.execute(ddl)
            if not _column_exists(connection, table, "source_value"):
                connection.execute(f"ALTER TABLE {table} ADD COLUMN source_value TEXT")
        replayed = _replay_manual_revisions(connection)
        connection.execute("DROP TABLE IF EXISTS voided_auto")
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
    logger.info("schema v21: replayed %d manual status row(s), dropped voided_auto", replayed)


def apply_schema(connection: sqlite3.Connection) -> None:
    """Upgrade the ledger to :data:`SCHEMA_VERSION` when its stamp is older, else do nothing.

    A current ledger costs one ``PRAGMA user_version`` read and no write, so a read-only caller
    never waits on the write lock a running scan holds. A ledger stamped newer than this build
    raises :class:`RuntimeError` rather than being stamped down.

    An older ledger, or a fresh one stamped 0, runs the in-place migrations first, then every
    ``CREATE ... IF NOT EXISTS``, then the append-only triggers, then the stamp. The migrations
    preserve real data: v12 renames ``file_album_status`` to ``file_year_status``
    (:func:`_migrate_v12_year_status`), v13 adds and stamps ``tag_revisions.managed_set``
    (:func:`_migrate_v13_managed_set`), v14 adds ``files.reader_version``
    (:func:`_migrate_v14_reader_version`), v17 adds the staged base signature
    (:func:`_migrate_staged_base_signature`), v18 adds and backfills ``files.path_key``
    (:func:`_migrate_files_path_key`), v19 restamps all-auto commits
    (:func:`_migrate_commit_origin`) and v20 renames, moves and drops in place
    (:func:`_migrate_reverted_to_version`, :func:`_migrate_managed_set_required`,
    :func:`_migrate_release_group_cache_name`, :func:`_migrate_mbid_columns`,
    :func:`_migrate_drop_files_status`, :func:`_migrate_lastfm_correction_cache`) and v21
    snapshots the tag-axis status rows (:func:`_migrate_axis_outcomes`) and adds the staged
    changed fields (:func:`_migrate_staged_changed_fields`), v23 adds the staged supplied
    keys (:func:`_migrate_staged_supplied_keys`), v24 rewrites the mismatch rows as covers
    snapshots (:func:`_migrate_mismatch_covers`) and v25 adds the path staging columns
    (:func:`_migrate_path_staging`). v15, v16 and v22 add cache tables only, and
    v23 adds ``file_song_status``, which the DDL creates, so they need no migration step. The
    triggers come after every migration, so a migration that updates a log runs before they
    exist.

    ``commits`` is created before the revision/staging tables that reference it.
    """
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current == SCHEMA_VERSION:
        return
    if current > SCHEMA_VERSION:
        message = (
            f"ledger schema v{current} is newer than this tagmend (v{SCHEMA_VERSION}); "
            "upgrade tagmend"
        )
        raise RuntimeError(message)

    logger.info("upgrading ledger schema v%d to v%d", current, SCHEMA_VERSION)
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
    for index_ddl in _REVISIONS_COMMIT_INDEX_DDL:
        connection.execute(index_ddl)
    apply_append_only_triggers(connection)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
