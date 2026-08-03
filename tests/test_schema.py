"""Tests for the SQLite schema DDL and version stamp (:mod:`tagmend.engine.schema`)."""

from __future__ import annotations

import sqlite3

from tagmend.engine.schema import SCHEMA_VERSION, apply_schema


def _table_names(conn: sqlite3.Connection) -> set[str]:
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(row[0]) for row in cursor.fetchall()}


def test_apply_schema_stamps_version_12(db_conn: sqlite3.Connection) -> None:
    # db_conn already applied the schema; confirm the stamped user_version is v12.
    version = db_conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 12
    assert SCHEMA_VERSION == 12


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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
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

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
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

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
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
