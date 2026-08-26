"""Tests for the SQLite schema DDL and version stamp (:mod:`tagmend.engine.schema`)."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from tagmend.engine.schema import SCHEMA_VERSION, apply_schema
from tagmend.engine.tags import TAG_READER_VERSION

if TYPE_CHECKING:
    from pathlib import Path


def _table_names(conn: sqlite3.Connection) -> set[str]:
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(row[0]) for row in cursor.fetchall()}


def test_apply_schema_stamps_current_version(db_conn: sqlite3.Connection) -> None:
    # db_conn already applied the schema; the stamp must match the constant the code ships.
    version = db_conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION
    assert SCHEMA_VERSION == 14


def test_apply_schema_creates_genre_tables(db_conn: sqlite3.Connection) -> None:
    tables = _table_names(db_conn)
    assert "lastfm_cache" in tables
    assert "file_genre_status" in tables


def test_apply_schema_creates_artist_status_table(db_conn: sqlite3.Connection) -> None:
    assert "file_artist_status" in _table_names(db_conn)


def test_apply_schema_creates_year_tables(db_conn: sqlite3.Connection) -> None:
    tables = _table_names(db_conn)
    assert "file_year_status" in tables
    assert "musicbrainz_cache" in tables


def test_apply_schema_creates_recording_cache_table(db_conn: sqlite3.Connection) -> None:
    assert "musicbrainz_recording_cache" in _table_names(db_conn)


def test_musicbrainz_recording_cache_columns(db_conn: sqlite3.Connection) -> None:
    cursor = db_conn.execute("PRAGMA table_info(musicbrainz_recording_cache)")
    columns = {str(row[1]): (str(row[2]), bool(row[3]), bool(row[5])) for row in cursor.fetchall()}
    # name -> (declared type, NOT NULL, is-primary-key)
    assert columns["request_key"] == ("TEXT", False, True)
    assert columns["found"] == ("INTEGER", True, False)
    assert columns["album_title"] == ("TEXT", False, False)
    assert columns["release_group_id"] == ("TEXT", False, False)
    assert columns["recording_mbid"] == ("TEXT", False, False)
    assert columns["fetched_at"] == ("TEXT", True, False)


def test_apply_schema_creates_voided_auto_table(db_conn: sqlite3.Connection) -> None:
    assert "voided_auto" in _table_names(db_conn)


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
            INSERT INTO musicbrainz_cache (request_key, found, fetched_at)
            VALUES ('k', 1, '2026-07-06T00:00:00+00:00')
            """,
        )
        conn.commit()

        apply_schema(conn)  # the in-place upgrade

        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "musicbrainz_recording_cache" in _table_names(conn)
        # Pre-existing data survives the additive upgrade.
        kept = conn.execute("SELECT COUNT(*) FROM musicbrainz_cache").fetchone()[0]
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
    conn.execute("ALTER TABLE tag_revisions DROP COLUMN managed_set")


def _insert_revision_row(
    conn: sqlite3.Connection, file_id: int, version: int, created_at: str
) -> None:
    conn.execute(
        """
        INSERT INTO tag_revisions (file_id, version, created_at, origin, managed_tags, diff)
        VALUES (?, ?, ?, 'manual', '{}', '{}')
        """,
        (file_id, version, created_at),
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
