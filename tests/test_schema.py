"""Tests for the SQLite schema DDL and version stamp (:mod:`tagmend.engine.schema`)."""

from __future__ import annotations

import sqlite3

from tagmend.engine.schema import SCHEMA_VERSION, apply_schema


def _table_names(conn: sqlite3.Connection) -> set[str]:
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(row[0]) for row in cursor.fetchall()}


def test_apply_schema_stamps_version_8(db_conn: sqlite3.Connection) -> None:
    # db_conn already applied the schema; confirm the stamped user_version is v8.
    version = db_conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 8
    assert SCHEMA_VERSION == 8


def test_apply_schema_creates_genre_tables(db_conn: sqlite3.Connection) -> None:
    tables = _table_names(db_conn)
    assert "lastfm_cache" in tables
    assert "file_genre_status" in tables


def test_apply_schema_creates_artist_status_table(db_conn: sqlite3.Connection) -> None:
    assert "file_artist_status" in _table_names(db_conn)


def test_apply_schema_creates_album_tables(db_conn: sqlite3.Connection) -> None:
    tables = _table_names(db_conn)
    assert "file_album_status" in tables
    assert "musicbrainz_cache" in tables


def test_apply_schema_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        apply_schema(conn)  # second application must not raise
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    finally:
        conn.close()


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
