"""Tests for the SQLite schema DDL and version stamp (:mod:`tagmend.engine.schema`)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from typing import TYPE_CHECKING

import pytest

from tagmend.engine import path_keys
from tagmend.engine.schema import SCHEMA_VERSION, apply_append_only_triggers, apply_schema
from tagmend.engine.tags import MANAGED_SET_VERSION, TAG_READER_VERSION

if TYPE_CHECKING:
    from pathlib import Path


def _table_names(conn: sqlite3.Connection) -> set[str]:
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(row[0]) for row in cursor.fetchall()}


def test_apply_schema_stamps_current_version(db_conn: sqlite3.Connection) -> None:
    # db_conn already applied the schema; the stamp must match the constant the code ships.
    version = db_conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION
    assert SCHEMA_VERSION == 21


def test_apply_schema_creates_genre_tables(db_conn: sqlite3.Connection) -> None:
    tables = _table_names(db_conn)
    assert "lastfm_cache" in tables
    assert "file_genre_status" in tables


def test_apply_schema_creates_artist_status_table(db_conn: sqlite3.Connection) -> None:
    assert "file_artist_status" in _table_names(db_conn)


def test_apply_schema_creates_year_tables(db_conn: sqlite3.Connection) -> None:
    tables = _table_names(db_conn)
    assert "file_year_status" in tables
    assert "musicbrainz_release_group_cache" in tables


def test_apply_schema_creates_recording_cache_table(db_conn: sqlite3.Connection) -> None:
    assert "musicbrainz_recording_cache" in _table_names(db_conn)


def test_musicbrainz_recording_cache_columns(db_conn: sqlite3.Connection) -> None:
    cursor = db_conn.execute("PRAGMA table_info(musicbrainz_recording_cache)")
    columns = {str(row[1]): (str(row[2]), bool(row[3]), bool(row[5])) for row in cursor.fetchall()}
    # name -> (declared type, NOT NULL, is-primary-key)
    assert columns["request_key"] == ("TEXT", False, True)
    assert columns["found"] == ("INTEGER", True, False)
    assert columns["album_title"] == ("TEXT", False, False)
    assert columns["release_group_mbid"] == ("TEXT", False, False)
    assert columns["recording_mbid"] == ("TEXT", False, False)
    assert columns["fetched_at"] == ("TEXT", True, False)


def test_apply_schema_creates_no_voided_auto_table(db_conn: sqlite3.Connection) -> None:
    # The tag-axis value snapshots replaced the re-open watermark.
    assert "voided_auto" not in _table_names(db_conn)


def test_apply_schema_creates_mismatch_status_table(db_conn: sqlite3.Connection) -> None:
    assert "file_mismatch_status" in _table_names(db_conn)


def test_file_mismatch_status_columns(db_conn: sqlite3.Connection) -> None:
    cursor = db_conn.execute("PRAGMA table_info(file_mismatch_status)")
    columns = {str(row[1]): (str(row[2]), bool(row[3]), bool(row[5])) for row in cursor.fetchall()}
    # name -> (declared type, NOT NULL, is-primary-key)
    assert columns["file_id"] == ("INTEGER", False, True)
    assert columns["status"] == ("TEXT", True, False)
    assert columns["source_field"] == ("TEXT", False, False)
    assert columns["source_value"] == ("TEXT", False, False)
    assert columns["updated_at"] == ("TEXT", True, False)


def test_apply_schema_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        apply_schema(conn)  # second application must not raise
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_apply_schema_is_read_only_on_a_current_ledger(tmp_path: Path) -> None:
    # A running scan holds the write lock for its whole transaction. A read-only caller on a
    # current ledger must not need that lock just to confirm the schema.
    db_path = tmp_path / "ledger.sqlite3"
    setup = sqlite3.connect(db_path)
    try:
        apply_schema(setup)
        setup.commit()
    finally:
        setup.close()

    writer = sqlite3.connect(db_path)
    reader = sqlite3.connect(db_path, timeout=0.2)
    try:
        writer.execute("BEGIN IMMEDIATE")

        apply_schema(reader)  # must not raise "database is locked"

        assert reader.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    finally:
        reader.close()
        writer.rollback()
        writer.close()


def test_apply_schema_refuses_a_newer_ledger() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

        with pytest.raises(RuntimeError, match="newer than this tagmend"):
            apply_schema(conn)

        # Refused, not stamped down.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
    finally:
        conn.close()


def test_v10_ledger_gains_the_recording_cache_in_place() -> None:
    # A v10 ledger (no recording cache) gains the table + version bump additively, with the
    # existing tables/data intact.
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        conn.execute("PRAGMA user_version = 10")  # pretend this is a pre-v11 ledger
        conn.execute("DROP TABLE musicbrainz_recording_cache")  # the v11-only table
        conn.execute(
            """
            INSERT INTO musicbrainz_release_group_cache (request_key, found, fetched_at)
            VALUES ('k', 1, '2026-07-06T00:00:00+00:00')
            """,
        )
        conn.commit()

        apply_schema(conn)  # the in-place upgrade

        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "musicbrainz_recording_cache" in _table_names(conn)
        # Pre-existing data survives the additive upgrade.
        kept = conn.execute("SELECT COUNT(*) FROM musicbrainz_release_group_cache").fetchone()[0]
        assert kept == 1
    finally:
        conn.close()


def test_v11_ledger_upgrades_to_v12_in_place() -> None:
    # A v11 ledger names the year-axis table ``file_album_status``; v12 renames it to
    # ``file_year_status`` in place, carrying every stored disposition across.
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        file_id = _insert_file(conn)
        conn.execute("PRAGMA user_version = 11")  # pretend this is a pre-v12 ledger
        conn.execute("ALTER TABLE file_year_status RENAME TO file_album_status")
        conn.execute(
            """
            INSERT INTO file_album_status
              (file_id, status, source_artist, source_album, updated_at)
            VALUES (?, 'no_match', 'Obscure', 'Demos', '2026-07-06T00:00:00+00:00')
            """,
            (file_id,),
        )
        conn.commit()

        apply_schema(conn)  # the in-place upgrade

        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = _table_names(conn)
        assert "file_year_status" in tables
        assert "file_album_status" not in tables  # renamed, not copied
        # The disposition row survives the rename with every column intact.
        row = conn.execute(
            "SELECT status, source_artist, source_album FROM file_year_status WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        assert row == ("no_match", "Obscure", "Demos")
    finally:
        conn.close()


def _downgrade_to_v12(conn: sqlite3.Connection) -> None:
    """Turn a freshly-applied ledger back into a v12 one (no ``managed_set`` column)."""
    conn.execute("PRAGMA user_version = 12")
    # A real v12 ledger has no trigger, and SQLite refuses to drop a column a trigger names.
    conn.execute("DROP TRIGGER tag_revisions_managed_set_required")
    conn.execute("ALTER TABLE tag_revisions DROP COLUMN managed_set")


def _insert_revision_row(
    conn: sqlite3.Connection,
    file_id: int,
    version: int,
    created_at: str,
    *,
    managed_set: int | None = None,
) -> None:
    if managed_set is None:
        conn.execute(
            """
            INSERT INTO tag_revisions (file_id, version, created_at, origin, managed_tags, diff)
            VALUES (?, ?, ?, 'manual', '{}', '{}')
            """,
            (file_id, version, created_at),
        )
        return
    conn.execute(
        """
        INSERT INTO tag_revisions (
            file_id, version, created_at, origin, managed_tags, diff, managed_set
        )
        VALUES (?, ?, ?, 'manual', '{}', '{}', ?)
        """,
        (file_id, version, created_at, managed_set),
    )


def test_v12_ledger_stamps_managed_set_by_capture_date() -> None:
    # The widening shipped 2026-07-04: rows captured before it governed the original 5 tags
    # (managed set 1), rows from that date on the widened 18 (managed set 2).
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        file_id = _insert_file(conn)
        _downgrade_to_v12(conn)
        _insert_revision_row(conn, file_id, 0, "2026-07-03T23:59:59+00:00")
        _insert_revision_row(conn, file_id, 1, "2026-07-04T00:00:01+00:00")
        conn.commit()

        apply_schema(conn)  # the in-place upgrade

        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        rows = conn.execute(
            "SELECT version, managed_set FROM tag_revisions ORDER BY version",
        ).fetchall()
        assert [tuple(row) for row in rows] == [(0, 1), (1, 2)]
    finally:
        conn.close()


def test_v13_stamp_survives_a_caller_that_never_commits(tmp_path: Path) -> None:
    # Every engine entry point runs apply_schema straight after connecting, and the read-only
    # ones close without committing. The ADD COLUMN autocommits, so an uncommitted stamp would
    # leave the column present but NULL and unfixable on the next run.
    db_path = tmp_path / "ledger.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        apply_schema(conn)
        file_id = _insert_file(conn)
        _downgrade_to_v12(conn)
        _insert_revision_row(conn, file_id, 0, "2026-01-01T00:00:00+00:00")
        conn.commit()
    finally:
        conn.close()

    upgrading = sqlite3.connect(db_path)
    try:
        apply_schema(upgrading)  # a read-only caller: no commit of its own
    finally:
        upgrading.close()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT managed_set FROM tag_revisions").fetchone()[0] == 1
    finally:
        conn.close()


def test_fresh_ledger_takes_managed_set_from_the_ddl() -> None:
    # The v13 migration runs BEFORE the DDL, so on a fresh ledger ``tag_revisions`` does not
    # exist yet: the migration must skip rather than raise "no such table", and the column
    # comes from _TAG_REVISIONS_DDL.
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)

        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tag_revisions)")}
        assert "managed_set" in columns
    finally:
        conn.close()


def test_v13_migration_does_not_restamp_on_reapply() -> None:
    # Idempotence that matters: a second apply_schema must not re-run the date-based stamp
    # over rows whose marker is already set (here a pre-widening date carrying set 2).
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        file_id = _insert_file(conn)
        _downgrade_to_v12(conn)
        _insert_revision_row(conn, file_id, 0, "2026-01-01T00:00:00+00:00")
        conn.commit()

        apply_schema(conn)
        conn.execute("DROP TRIGGER tag_revisions_no_update")
        conn.execute("UPDATE tag_revisions SET managed_set = 2")
        apply_schema(conn)  # must not raise, must not restamp

        marker = conn.execute("SELECT managed_set FROM tag_revisions").fetchone()[0]
        assert marker == 2
    finally:
        conn.close()


def _downgrade_to_v13(conn: sqlite3.Connection) -> None:
    """Turn a freshly-applied ledger back into a v13 one (no ``reader_version`` column)."""
    conn.execute("PRAGMA user_version = 13")
    conn.execute("ALTER TABLE files DROP COLUMN reader_version")


def test_v13_ledger_gains_reader_version_defaulted_stale() -> None:
    # A v13 row was written by an unknown older reader, so it must land BELOW
    # TAG_READER_VERSION and be re-read once — with the row itself carried across intact.
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        file_id = _insert_file(conn)
        _downgrade_to_v13(conn)
        conn.commit()

        apply_schema(conn)  # the in-place upgrade

        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        row = conn.execute(
            "SELECT folder, filename, reader_version FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        assert row == ("/lib", "a.mp3", 0)
        assert TAG_READER_VERSION > 0  # so the backfilled 0 really does read as stale
    finally:
        conn.close()


def test_fresh_ledger_takes_reader_version_from_the_ddl() -> None:
    # The v14 migration runs BEFORE the DDL, so on a fresh ledger ``files`` does not exist
    # yet: the migration must skip rather than raise "no such table", and the column comes
    # from _FILES_DDL.
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)

        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(files)")}
        assert "reader_version" in columns
    finally:
        conn.close()


def test_v14_migration_does_not_reset_a_stamped_row() -> None:
    # Idempotence that matters: a second apply_schema must not re-add or re-default the
    # column over a row the scan has already stamped current.
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        file_id = _insert_file(conn)
        conn.execute(
            "UPDATE files SET reader_version = ? WHERE id = ?",
            (TAG_READER_VERSION, file_id),
        )
        conn.commit()

        apply_schema(conn)  # must not raise, must not reset

        stamped = conn.execute(
            "SELECT reader_version FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()[0]
        assert stamped == TAG_READER_VERSION
    finally:
        conn.close()


_APPEND_ONLY_TRIGGERS = (
    "tag_revisions_no_update",
    "tag_revisions_no_delete",
    "path_revisions_no_update",
    "path_revisions_no_delete",
)
_COMMIT_ID_INDEXES = ("idx_tag_revisions_commit_id", "idx_path_revisions_commit_id")


def _schema_objects(conn: sqlite3.Connection, kind: str) -> set[str]:
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    return {str(row[0]) for row in cursor.fetchall()}


def _insert_path_revision_row(conn: sqlite3.Connection, file_id: int) -> None:
    conn.execute(
        """
        INSERT INTO path_revisions (file_id, version, created_at, origin, from_path, to_path)
        VALUES (?, 0, '2026-06-08T00:00:00+00:00', 'manual', '/lib/a.mp3', '/lib/b.mp3')
        """,
        (file_id,),
    )


def test_tag_revisions_reject_update_and_delete(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)
    _insert_revision_row(
        db_conn, file_id, 0, "2026-06-08T00:00:00+00:00", managed_set=MANAGED_SET_VERSION
    )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db_conn.execute("UPDATE tag_revisions SET note = 'rewritten'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db_conn.execute("DELETE FROM tag_revisions")

    row = db_conn.execute("SELECT note FROM tag_revisions WHERE file_id = ?", (file_id,))
    assert row.fetchall() == [(None,)]


def test_path_revisions_reject_update_and_delete(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)
    _insert_path_revision_row(db_conn, file_id)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db_conn.execute("UPDATE path_revisions SET to_path = '/lib/c.mp3'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db_conn.execute("DELETE FROM path_revisions")

    row = db_conn.execute("SELECT to_path FROM path_revisions WHERE file_id = ?", (file_id,))
    assert row.fetchall() == [("/lib/b.mp3",)]


def test_revisions_by_commit_use_the_index(db_conn: sqlite3.Connection) -> None:
    plan = db_conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM tag_revisions WHERE commit_id = ?",
        (1,),
    ).fetchall()

    assert any("idx_tag_revisions_commit_id" in str(row[-1]) for row in plan)


def test_apply_append_only_triggers_is_idempotent(db_conn: sqlite3.Connection) -> None:
    apply_append_only_triggers(db_conn)  # already present: must not raise

    assert set(_APPEND_ONLY_TRIGGERS) <= _schema_objects(db_conn, "trigger")


def test_v16_ledger_gains_the_triggers_and_indexes_in_place() -> None:
    # A v16 ledger has neither. The upgrade creates both without rewriting a row.
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        file_id = _insert_file(conn)
        for trigger in _APPEND_ONLY_TRIGGERS:
            conn.execute(f"DROP TRIGGER {trigger}")
        for index in _COMMIT_ID_INDEXES:
            conn.execute(f"DROP INDEX {index}")
        conn.execute("PRAGMA user_version = 16")
        _insert_revision_row(
            conn, file_id, 0, "2026-06-08T00:00:00+00:00", managed_set=MANAGED_SET_VERSION
        )
        conn.commit()

        apply_schema(conn)  # the in-place upgrade

        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert set(_APPEND_ONLY_TRIGGERS) <= _schema_objects(conn, "trigger")
        assert set(_COMMIT_ID_INDEXES) <= _schema_objects(conn, "index")
        kept = conn.execute("SELECT file_id, version FROM tag_revisions").fetchall()
        assert kept == [(file_id, 0)]
    finally:
        conn.close()


def _staged_columns(conn: sqlite3.Connection) -> list[str]:
    return [str(row[1]) for row in conn.execute("PRAGMA table_info(tag_revisions_staged)")]


def test_v16_ledger_gains_the_staged_base_signature_in_place() -> None:
    # A v16 staged row predates the signature. It survives with a NULL pair, which the commit
    # reads as "skip the changed-since-stage check".
    fresh = sqlite3.connect(":memory:")
    try:
        apply_schema(fresh)
        fresh_columns = _staged_columns(fresh)
    finally:
        fresh.close()

    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        file_id = _insert_file(conn)
        conn.execute("ALTER TABLE tag_revisions_staged DROP COLUMN base_size_bytes")
        conn.execute("ALTER TABLE tag_revisions_staged DROP COLUMN base_mtime_ns")
        conn.execute("ALTER TABLE tag_revisions_staged DROP COLUMN changed_fields")
        conn.execute("PRAGMA user_version = 16")
        conn.execute(
            """
            INSERT INTO tag_revisions_staged (file_id, managed_tags, origin, note, staged_at)
            VALUES (?, '{"genre":["Rock"]}', 'manual', 'kept', '2026-06-08T00:00:00+00:00')
            """,
            (file_id,),
        )
        conn.commit()

        apply_schema(conn)  # the in-place upgrade
        apply_schema(conn)  # a second application must not re-add the columns

        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert _staged_columns(conn) == fresh_columns
        row = conn.execute(
            """
            SELECT managed_tags, note, base_size_bytes, base_mtime_ns, changed_fields
            FROM tag_revisions_staged WHERE file_id = ?
            """,
            (file_id,),
        ).fetchone()
        assert row == ('{"genre":["Rock"]}', "kept", None, None, None)
    finally:
        conn.close()


def _downgrade_to_v17(conn: sqlite3.Connection) -> None:
    """Turn a freshly-applied ledger back into a v17 one (no ``path_key`` or its index)."""
    conn.execute("DROP INDEX idx_files_path_key")
    conn.execute("ALTER TABLE files DROP COLUMN path_key")
    conn.execute("PRAGMA user_version = 17")


def _insert_file_at(conn: sqlite3.Connection, folder: str, filename: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO files (folder, filename, ext, first_seen_at, updated_at)
        VALUES (?, ?, '.mp3', '2026-06-08T00:00:00+00:00', '2026-06-08T00:00:00+00:00')
        """,
        (folder, filename),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _files_columns(conn: sqlite3.Connection) -> list[str]:
    return [str(row[1]) for row in conn.execute("PRAGMA table_info(files)")]


def test_previous_ledger_gains_path_key_backfilled() -> None:
    fresh = sqlite3.connect(":memory:")
    try:
        apply_schema(fresh)
        fresh_columns = _files_columns(fresh)
    finally:
        fresh.close()

    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        _downgrade_to_v17(conn)
        first = _insert_file_at(conn, "/lib/Album", "a.mp3")
        second = _insert_file_at(conn, "/lib/Other", "b.mp3")
        conn.commit()

        apply_schema(conn)  # the in-place upgrade

        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert _files_columns(conn) == fresh_columns
        keys = dict(conn.execute("SELECT id, path_key FROM files").fetchall())
        assert keys == {
            first: path_keys.file_path_key("/lib/Album", "a.mp3"),
            second: path_keys.file_path_key("/lib/Other", "b.mp3"),
        }
        assert "idx_files_path_key" in _schema_objects(conn, "index")
    finally:
        conn.close()


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS is case-insensitive")
def test_duplicate_path_keys_abort_the_upgrade_untouched() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        _downgrade_to_v17(conn)
        first = _insert_file_at(conn, r"C:\lib\Album", "a.mp3")
        second = _insert_file_at(conn, r"C:\lib\ALBUM", "a.mp3")
        conn.commit()

        with pytest.raises(RuntimeError) as excinfo:
            apply_schema(conn)

        assert f"id={first}" in str(excinfo.value)
        assert f"id={second}" in str(excinfo.value)
        assert "path_key" not in _files_columns(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 17
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2
    finally:
        conn.close()


def _insert_commit_row(conn: sqlite3.Connection, origin: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO commits (created_at, origin, status)
        VALUES ('2026-08-01T00:00:00+00:00', ?, 'applied')
        """,
        (origin,),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _insert_commit_revision(
    conn: sqlite3.Connection, file_id: int, version: int, commit_id: int, origin: str
) -> None:
    conn.execute(
        """
        INSERT INTO tag_revisions (
            file_id, version, created_at, origin, commit_id, managed_tags, diff, managed_set
        )
        VALUES (?, ?, '2026-08-01T00:00:00+00:00', ?, ?, '{}', '{}', ?)
        """,
        (file_id, version, origin, commit_id, MANAGED_SET_VERSION),
    )


def test_migrate_commit_origin_restamps_all_auto_commits() -> None:
    # Earlier builds stamped every MCP commit manual, so a resolver commit read as manual and
    # reopen_axes could void the work it had just written.
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        first_file = _insert_file_at(conn, "/lib/Album", "a.mp3")
        second_file = _insert_file_at(conn, "/lib/Album", "b.mp3")
        all_auto = _insert_commit_row(conn, "manual")
        mixed = _insert_commit_row(conn, "manual")
        all_manual = _insert_commit_row(conn, "manual")
        revert = _insert_commit_row(conn, "revert")
        _insert_commit_revision(conn, first_file, 1, all_auto, "auto")
        _insert_commit_revision(conn, second_file, 1, all_auto, "auto")
        _insert_commit_revision(conn, first_file, 2, mixed, "auto")
        _insert_commit_revision(conn, second_file, 2, mixed, "manual")
        _insert_commit_revision(conn, first_file, 3, all_manual, "manual")
        _insert_commit_revision(conn, first_file, 4, revert, "revert")
        conn.execute("PRAGMA user_version = 18")
        conn.commit()

        apply_schema(conn)  # the in-place upgrade

        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        origins = dict(conn.execute("SELECT id, origin FROM commits").fetchall())
        assert origins == {
            all_auto: "auto",
            mixed: "manual",
            all_manual: "manual",
            revert: "revert",
        }
        assert conn.execute("SELECT COUNT(*) FROM tag_revisions").fetchone()[0] == 6
    finally:
        conn.close()


_PREVIOUS_VERSION = SCHEMA_VERSION - 1


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def test_reverted_from_column_is_renamed_in_place() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        file_id = _insert_file(conn)
        conn.execute("ALTER TABLE tag_revisions RENAME COLUMN reverted_to_version TO reverted_from")
        conn.execute(f"PRAGMA user_version = {_PREVIOUS_VERSION}")
        conn.execute(
            """
            INSERT INTO tag_revisions (
                file_id, version, created_at, origin, reverted_from, managed_tags, diff,
                managed_set
            )
            VALUES (?, 1, '2026-08-01T00:00:00+00:00', 'revert', 0, '{}', '{}', ?)
            """,
            (file_id, MANAGED_SET_VERSION),
        )
        conn.commit()

        apply_schema(conn)  # the in-place upgrade

        columns = _columns(conn, "tag_revisions")
        assert "reverted_to_version" in columns
        assert "reverted_from" not in columns
        kept = conn.execute("SELECT reverted_to_version FROM tag_revisions").fetchone()[0]
        assert kept == 0
    finally:
        conn.close()


def test_musicbrainz_cache_is_renamed_in_place() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        conn.execute("ALTER TABLE musicbrainz_release_group_cache RENAME TO musicbrainz_cache")
        conn.execute(f"PRAGMA user_version = {_PREVIOUS_VERSION}")
        conn.execute(
            """
            INSERT INTO musicbrainz_cache (request_key, found, fetched_at)
            VALUES ('release-group k', 1, '2026-07-06T00:00:00+00:00')
            """,
        )
        conn.commit()

        apply_schema(conn)  # the in-place upgrade

        tables = _table_names(conn)
        assert "musicbrainz_cache" not in tables
        kept = conn.execute(
            "SELECT request_key FROM musicbrainz_release_group_cache",
        ).fetchall()
        assert kept == [("release-group k",)]
    finally:
        conn.close()


def test_release_group_id_columns_are_renamed_in_place() -> None:
    # The previous shape names the release-group cache musicbrainz_cache, and both caches
    # carry release_group_id.
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        conn.execute("ALTER TABLE musicbrainz_release_group_cache RENAME TO musicbrainz_cache")
        for table in ("musicbrainz_cache", "musicbrainz_recording_cache"):
            conn.execute(
                f"ALTER TABLE {table} RENAME COLUMN release_group_mbid TO release_group_id",
            )
            conn.execute(
                f"INSERT INTO {table} (request_key, found, release_group_id, fetched_at) "  # noqa: S608
                "VALUES ('k', 1, 'rg-1', '2026-07-06T00:00:00+00:00')",
            )
        conn.execute(f"PRAGMA user_version = {_PREVIOUS_VERSION}")
        conn.commit()

        apply_schema(conn)  # the in-place upgrade

        for table in ("musicbrainz_release_group_cache", "musicbrainz_recording_cache"):
            assert "release_group_id" not in _columns(conn, table)
            kept = conn.execute(f"SELECT release_group_mbid FROM {table}").fetchone()[0]  # noqa: S608
            assert kept == "rg-1"
    finally:
        conn.close()


def test_files_status_column_is_dropped_in_place() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        file_id = _insert_file(conn)
        conn.execute("ALTER TABLE files ADD COLUMN status TEXT NOT NULL DEFAULT 'scanned'")
        conn.execute(f"PRAGMA user_version = {_PREVIOUS_VERSION}")
        conn.commit()

        apply_schema(conn)  # the in-place upgrade

        assert "status" not in _files_columns(conn)
        row = conn.execute(
            "SELECT id, folder, filename FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        assert row == (file_id, "/lib", "a.mp3")
    finally:
        conn.close()


def test_fresh_ledger_has_no_files_status(db_conn: sqlite3.Connection) -> None:
    assert "status" not in _files_columns(db_conn)


def _correction_key(artist: str) -> str:
    payload = f"artist.getcorrection\x00artist={artist}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324 - cache key


def _build_previous_lastfm_cache(conn: sqlite3.Connection) -> None:
    """A previous-version ledger: one Daft Punk file, its correction row and a top-tags row."""
    apply_schema(conn)
    file_id = _insert_file(conn)
    conn.execute("DROP TABLE lastfm_correction_cache")
    conn.execute(
        "INSERT INTO file_tags (file_id, name, ordinal, value) VALUES (?, 'artist', 0, ?)",
        (file_id, "Daft Punk"),
    )
    conn.executemany(
        """
        INSERT INTO lastfm_cache (request_key, found, tags, fetched_at)
        VALUES (?, 1, ?, '2026-07-06T00:00:00+00:00')
        """,
        [
            (_correction_key("Daft Punk"), '[["Daft Punk",0],["mbid-1",0]]'),
            ("top-tags-key", '[["electronic",100]]'),
        ],
    )
    conn.execute(f"PRAGMA user_version = {_PREVIOUS_VERSION}")
    conn.commit()


def _lastfm_rows(conn: sqlite3.Connection) -> tuple[list[tuple[object, ...]], ...]:
    corrections = conn.execute(
        "SELECT request_key, found, name, mbid FROM lastfm_correction_cache",
    ).fetchall()
    top_tags = conn.execute("SELECT request_key, tags FROM lastfm_cache").fetchall()
    return corrections, top_tags


def test_correction_rows_move_out_of_lastfm_cache_in_place() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        _build_previous_lastfm_cache(conn)

        apply_schema(conn)  # the in-place upgrade

        corrections, top_tags = _lastfm_rows(conn)
        assert corrections == [(_correction_key("Daft Punk"), 1, "Daft Punk", "mbid-1")]
        assert top_tags == [("top-tags-key", '[["electronic",100]]')]
    finally:
        conn.close()


def test_correction_migration_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        _build_previous_lastfm_cache(conn)
        apply_schema(conn)
        upgraded = _lastfm_rows(conn)

        conn.execute(f"PRAGMA user_version = {_PREVIOUS_VERSION}")
        apply_schema(conn)  # the migration finds its table and moves nothing

        assert _lastfm_rows(conn) == upgraded
    finally:
        conn.close()


def test_revision_insert_without_managed_set_is_refused(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)

    with pytest.raises(sqlite3.IntegrityError, match="managed_set is required"):
        _insert_revision_row(db_conn, file_id, 0, "2026-06-08T00:00:00+00:00")


def test_previous_ledger_gains_the_managed_set_trigger() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        file_id = _insert_file(conn)
        conn.execute("DROP TRIGGER tag_revisions_managed_set_required")
        conn.execute(f"PRAGMA user_version = {_PREVIOUS_VERSION}")
        _insert_revision_row(
            conn, file_id, 0, "2026-06-08T00:00:00+00:00", managed_set=MANAGED_SET_VERSION
        )
        conn.commit()

        apply_schema(conn)  # the in-place upgrade

        assert "tag_revisions_managed_set_required" in _schema_objects(conn, "trigger")
        kept = conn.execute("SELECT file_id, version, managed_set FROM tag_revisions").fetchall()
        assert kept == [(file_id, 0, MANAGED_SET_VERSION)]
    finally:
        conn.close()


def test_upgrade_refuses_a_null_managed_set() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        file_id = _insert_file(conn)
        conn.execute("DROP TRIGGER tag_revisions_managed_set_required")
        conn.execute(f"PRAGMA user_version = {_PREVIOUS_VERSION}")
        _insert_revision_row(conn, file_id, 0, "2026-06-08T00:00:00+00:00")
        conn.commit()

        with pytest.raises(RuntimeError, match=rf"1 tag_revisions row.*\({file_id}, 0\)"):
            apply_schema(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == _PREVIOUS_VERSION
    finally:
        conn.close()


def test_file_mismatch_status_cascades_on_file_delete(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)
    db_conn.execute(
        """
        INSERT INTO file_mismatch_status
          (file_id, status, source_field, source_value, updated_at)
        VALUES (?, 'legit_ignore', 'albumartist', 'Jem', '2026-07-04T00:00:00+00:00')
        """,
        (file_id,),
    )

    db_conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    remaining = db_conn.execute(
        "SELECT COUNT(*) FROM file_mismatch_status WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    assert remaining[0] == 0


def test_file_genre_status_cascades_on_file_delete(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)
    db_conn.execute(
        """
        INSERT INTO file_genre_status (file_id, status, source_artist, source_album, updated_at)
        VALUES (?, 'no_match', 'A', NULL, '2026-06-08T00:00:00+00:00')
        """,
        (file_id,),
    )

    db_conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    remaining = db_conn.execute(
        "SELECT COUNT(*) FROM file_genre_status WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    assert remaining[0] == 0


def _insert_file(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        INSERT INTO files (folder, filename, ext, first_seen_at, updated_at)
        VALUES ('/lib', 'a.mp3', '.mp3', '2026-06-08T00:00:00+00:00',
                '2026-06-08T00:00:00+00:00')
        """,
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


# --- v21: tag-axis outcome rows ------------------------------------------------------

_AXIS_TABLES = ("file_genre_status", "file_artist_status", "file_year_status")


def _insert_tagged_file(conn: sqlite3.Connection, filename: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO files (folder, filename, ext, first_seen_at, updated_at)
        VALUES ('/lib', ?, '.mp3', '2026-06-08T00:00:00+00:00', '2026-06-08T00:00:00+00:00')
        """,
        (filename,),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _insert_history(  # noqa: PLR0913 - one revision row, every column spelled out
    conn: sqlite3.Connection,
    file_id: int,
    version: int,
    origin: str,
    managed_tags: dict[str, list[str]],
    diff: dict[str, dict[str, list[str]]],
    managed_set: int = MANAGED_SET_VERSION,
) -> None:
    conn.execute(
        """
        INSERT INTO tag_revisions (
            file_id, version, created_at, origin, managed_tags, diff, managed_set
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            version,
            f"2026-08-0{version + 1}T00:00:00+00:00",
            origin,
            json.dumps(managed_tags),
            json.dumps(diff),
            managed_set,
        ),
    )


def _status_rows(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    """Each tag-axis table as (file_id, status, value snapshot parsed, updated_at) rows."""
    rows: dict[str, list[tuple[object, ...]]] = {}
    for table in _AXIS_TABLES:
        cursor = conn.execute(
            f"SELECT file_id, status, source_value, updated_at FROM {table} ORDER BY file_id",  # noqa: S608
        )
        rows[table] = [
            (row[0], row[1], None if row[2] is None else json.loads(row[2]), row[3])
            for row in cursor.fetchall()
        ]
    return rows


def _previous_axis_schema(conn: sqlite3.Connection) -> None:
    """Turn a fresh ledger into the previous version's tables: no v21 columns, ``voided_auto``."""
    apply_schema(conn)
    for table in _AXIS_TABLES:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN source_value")
    conn.execute("ALTER TABLE tag_revisions_staged DROP COLUMN changed_fields")
    conn.execute(
        """
        CREATE TABLE voided_auto (
          file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
          field TEXT NOT NULL,
          voided_through_version INTEGER NOT NULL,
          voided_at TEXT NOT NULL,
          PRIMARY KEY (file_id, field)
        )
        """,
    )


def _build_previous_axis_ledger(conn: sqlite3.Connection) -> dict[str, int]:
    """A previous-version ledger: manual, auto and baseline revisions, old rows, watermarks."""
    _previous_axis_schema(conn)
    fixed = _insert_tagged_file(conn, "fixed.mp3")
    resolved = _insert_tagged_file(conn, "resolved.mp3")
    excluded = _insert_tagged_file(conn, "excluded.mp3")

    original = {"artist": ["Wrong"], "album": ["LP"], "genre": ["rock"]}
    renamed = {"artist": ["Right"], "album": ["LP"], "genre": ["rock"]}
    regenred = {"artist": ["Right"], "album": ["LP"], "genre": ["jazz"]}
    _insert_history(conn, fixed, 0, "scan", original, {})
    _insert_history(
        conn, fixed, 1, "manual", renamed, {"artist": {"from": ["Wrong"], "to": ["Right"]}}
    )
    _insert_history(
        conn, fixed, 2, "manual", regenred, {"genre": {"from": ["rock"], "to": ["jazz"]}}
    )
    _insert_history(conn, resolved, 0, "scan", {"artist": ["A"], "album": ["LP"]}, {})
    _insert_history(
        conn,
        resolved,
        1,
        "auto",
        {"artist": ["A"], "album": ["LP"], "originaldate": ["1999"]},
        {"originaldate": {"from": [], "to": ["1999"]}},
    )
    conn.execute(
        "INSERT INTO voided_auto VALUES (?, 'originaldate', 1, '2026-08-05T00:00:00+00:00')",
        (resolved,),
    )
    conn.execute(
        """
        INSERT INTO file_genre_status (file_id, status, source_artist, source_album, updated_at)
        VALUES (?, 'no_match', 'A', 'LP', '2026-08-03T00:00:00+00:00')
        """,
        (resolved,),
    )
    conn.execute(
        """
        INSERT INTO file_artist_status
          (file_id, status, source_artist, source_albumartist, updated_at)
        VALUES (?, 'manual', 'Someone', NULL, '2026-08-03T00:00:00+00:00')
        """,
        (excluded,),
    )
    conn.execute(
        """
        INSERT INTO tag_revisions_staged (file_id, managed_tags, origin, note, staged_at)
        VALUES (?, '{"genre":["pop"]}', 'manual', NULL, '2026-08-04T00:00:00+00:00')
        """,
        (excluded,),
    )
    conn.execute(f"PRAGMA user_version = {_PREVIOUS_VERSION}")
    conn.commit()
    return {"fixed": fixed, "resolved": resolved, "excluded": excluded}


def test_previous_ledger_gains_axis_outcomes_in_place() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        ids = _build_previous_axis_ledger(conn)

        apply_schema(conn)  # the in-place upgrade

        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "voided_auto" not in _table_names(conn)
        for table in _AXIS_TABLES:
            assert "source_value" in _columns(conn, table)
        rows = _status_rows(conn)
        # The manual artist fix and the later manual genre edit replay as manual rows, each
        # snapshotting its own revision's tags. The auto revision writes no done row.
        artist_values: dict[str, list[str]] = {
            name: []
            for name in (
                "albumartist",
                "albumartistsort",
                "artists",
                "artistsort",
                "musicbrainz_albumartistid",
                "musicbrainz_artistid",
            )
        }
        assert rows["file_artist_status"] == [
            (
                ids["fixed"],
                "manual",
                {**artist_values, "artist": ["Right"]},
                "2026-08-02T00:00:00+00:00",
            ),
            (ids["excluded"], "manual", None, "2026-08-03T00:00:00+00:00"),
        ]
        assert rows["file_genre_status"] == [
            (ids["fixed"], "manual", {"genre": ["jazz"]}, "2026-08-03T00:00:00+00:00"),
            (ids["resolved"], "no_match", None, "2026-08-03T00:00:00+00:00"),
        ]
        identities = conn.execute(
            "SELECT source_artist, source_album FROM file_genre_status ORDER BY file_id",
        ).fetchall()
        assert identities == [("Right", "LP"), ("A", "LP")]
        assert rows["file_year_status"] == []
        assert conn.execute("SELECT COUNT(*) FROM tag_revisions").fetchone()[0] == 5
        # A row staged before the column survives with NULL, and its commit falls back to
        # the diff against disk.
        staged = conn.execute(
            "SELECT file_id, managed_tags, changed_fields FROM tag_revisions_staged",
        ).fetchall()
        assert staged == [(ids["excluded"], '{"genre":["pop"]}', None)]
    finally:
        conn.close()


def test_axis_outcome_replay_ignores_widening_noise() -> None:
    # Both manual revisions carry an artistsort key. Only the one whose previous revision's
    # managed set governed artistsort is a human change. The other is widening noise.
    conn = sqlite3.connect(":memory:")
    try:
        _previous_axis_schema(conn)
        widened = _insert_tagged_file(conn, "widened.mp3")
        governed = _insert_tagged_file(conn, "governed.mp3")
        sort_added = {"artistsort": {"from": [], "to": ["Band, The"]}}
        _insert_history(conn, widened, 0, "scan", {"genre": ["rock"]}, {}, managed_set=1)
        _insert_history(
            conn,
            widened,
            1,
            "manual",
            {"genre": ["jazz"], "artistsort": ["Band, The"]},
            {"genre": {"from": ["rock"], "to": ["jazz"]}, **sort_added},
            managed_set=2,
        )
        _insert_history(conn, governed, 0, "scan", {"genre": ["rock"]}, {}, managed_set=2)
        _insert_history(
            conn,
            governed,
            1,
            "manual",
            {"genre": ["rock"], "artistsort": ["Band, The"]},
            sort_added,
            managed_set=2,
        )
        conn.execute(f"PRAGMA user_version = {_PREVIOUS_VERSION}")
        conn.commit()

        apply_schema(conn)

        rows = _status_rows(conn)
        assert [row[:2] for row in rows["file_genre_status"]] == [(widened, "manual")]
        assert [row[:2] for row in rows["file_artist_status"]] == [(governed, "manual")]
    finally:
        conn.close()


def test_axis_outcome_migration_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        _build_previous_axis_ledger(conn)
        apply_schema(conn)
        upgraded = _status_rows(conn)

        conn.execute(f"PRAGMA user_version = {_PREVIOUS_VERSION}")
        apply_schema(conn)  # every column exists and voided_auto is gone, so nothing runs

        assert _status_rows(conn) == upgraded
    finally:
        conn.close()


def test_fresh_ledger_takes_the_v21_columns_from_the_ddl(db_conn: sqlite3.Connection) -> None:
    for table in _AXIS_TABLES:
        assert "source_value" in _columns(db_conn, table)
    assert "changed_fields" in _staged_columns(db_conn)
