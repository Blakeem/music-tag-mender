"""Unit tests for the M2 genre data-access layer in :mod:`tagmend.engine.store`.

Covers the persistent Last.fm tag cache, the terminal ``file_genre_status`` decisions,
the "done"-derivation predicates, and the artist/scope selection helpers.
"""

from __future__ import annotations

import sqlite3

from tagmend.engine import store

_NOW = "2026-06-08T00:00:00+00:00"
_LATER = "2026-06-08T01:00:00+00:00"


def _insert(
    conn: sqlite3.Connection,
    *,
    folder: str = "/lib",
    filename: str = "a.mp3",
) -> int:
    return store.insert_file(
        conn,
        folder=folder,
        filename=filename,
        ext=".mp3",
        size_bytes=100,
        mtime_ns=1_000,
        now=_NOW,
    )


# --- lastfm_cache -------------------------------------------------------------------


def test_cache_miss_returns_none(db_conn: sqlite3.Connection) -> None:
    assert store.get_cached_tags(db_conn, "missing-key") is None


def test_cache_negative_sentinel_round_trip(db_conn: sqlite3.Connection) -> None:
    store.put_cached_tags(db_conn, request_key="k", found=False, tags=[], now=_NOW)

    cached = store.get_cached_tags(db_conn, "k")
    assert cached == (False, [])


def test_cache_found_round_trip(db_conn: sqlite3.Connection) -> None:
    tags = [("rock", 100), ("indie", 42)]
    store.put_cached_tags(db_conn, request_key="k", found=True, tags=tags, now=_NOW)

    cached = store.get_cached_tags(db_conn, "k")
    assert cached == (True, [("rock", 100), ("indie", 42)])


def test_cache_found_with_empty_tags_distinct_from_negative(
    db_conn: sqlite3.Connection,
) -> None:
    store.put_cached_tags(db_conn, request_key="k", found=True, tags=[], now=_NOW)
    assert store.get_cached_tags(db_conn, "k") == (True, [])


def test_cache_put_replaces_prior(db_conn: sqlite3.Connection) -> None:
    store.put_cached_tags(db_conn, request_key="k", found=False, tags=[], now=_NOW)
    store.put_cached_tags(
        db_conn,
        request_key="k",
        found=True,
        tags=[("rock", 5)],
        now=_LATER,
    )
    assert store.get_cached_tags(db_conn, "k") == (True, [("rock", 5)])


# --- file_genre_status --------------------------------------------------------------


def test_genre_status_absent_returns_none(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    assert store.get_genre_status(db_conn, file_id) is None


def test_genre_status_set_get_round_trip(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    store.set_genre_status(
        db_conn,
        file_id=file_id,
        status="no_match",
        source_artist="Boards of Canada",
        source_album="Geogaddi",
        now=_NOW,
    )

    row = store.get_genre_status(db_conn, file_id)
    assert row is not None
    assert row.status == "no_match"
    assert row.source_artist == "Boards of Canada"
    assert row.source_album == "Geogaddi"


def test_genre_status_set_replaces_and_allows_null_sources(
    db_conn: sqlite3.Connection,
) -> None:
    file_id = _insert(db_conn)
    store.set_genre_status(
        db_conn,
        file_id=file_id,
        status="no_match",
        source_artist="A",
        source_album="B",
        now=_NOW,
    )
    store.set_genre_status(
        db_conn,
        file_id=file_id,
        status="manual",
        source_artist=None,
        source_album=None,
        now=_LATER,
    )

    row = store.get_genre_status(db_conn, file_id)
    assert row is not None
    assert row.status == "manual"
    assert row.source_artist is None
    assert row.source_album is None


def test_genre_status_delete(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    store.set_genre_status(
        db_conn,
        file_id=file_id,
        status="manual",
        source_artist=None,
        source_album=None,
        now=_NOW,
    )

    store.delete_genre_status(db_conn, file_id)
    assert store.get_genre_status(db_conn, file_id) is None
    # Deleting again is a harmless no-op.
    store.delete_genre_status(db_conn, file_id)


# --- "done" derivation --------------------------------------------------------------


def test_is_staged_reflects_staging_area(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    assert store.is_staged(db_conn, file_id) is False

    store.upsert_staged_tag(
        db_conn,
        file_id=file_id,
        managed_tags={"genre": ["Rock"]},
        origin="auto",
        now=_NOW,
    )
    assert store.is_staged(db_conn, file_id) is True


def test_has_auto_revision_only_for_auto_origin(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    assert store.has_auto_revision(db_conn, file_id) is False

    # A non-auto revision must NOT count as an auto revision.
    store.insert_revision(
        db_conn,
        file_id=file_id,
        version=0,
        origin="scan",
        managed_tags={},
        diff={},
        now=_NOW,
    )
    assert store.has_auto_revision(db_conn, file_id) is False

    store.insert_revision(
        db_conn,
        file_id=file_id,
        version=1,
        origin="auto",
        managed_tags={"genre": ["Rock"]},
        diff={},
        now=_LATER,
    )
    assert store.has_auto_revision(db_conn, file_id) is True


# --- distinct_artists + files_in_scope ----------------------------------------------


def _seed_library(conn: sqlite3.Connection) -> dict[str, int]:
    """Seed three files: two by 'A' (one in album 'X'), one by 'B'. Returns id map."""
    a1 = _insert(conn, filename="a1.mp3")
    a2 = _insert(conn, filename="a2.mp3")
    b1 = _insert(conn, filename="b1.mp3")
    store.replace_tags(conn, a1, {"artist": ["A"], "album": ["X"]}, _NOW)
    store.replace_tags(conn, a2, {"artist": ["A"], "album": ["Y"]}, _NOW)
    store.replace_tags(conn, b1, {"artist": ["B"], "album": ["X"]}, _NOW)
    return {"a1": a1, "a2": a2, "b1": b1}


def test_distinct_artists_counts_files(db_conn: sqlite3.Connection) -> None:
    _seed_library(db_conn)
    assert store.distinct_artists(db_conn) == [("A", 2), ("B", 1)]


def test_files_in_scope_all(db_conn: sqlite3.Connection) -> None:
    ids = _seed_library(db_conn)
    assert store.files_in_scope(db_conn) == sorted(ids.values())


def test_files_in_scope_by_artist(db_conn: sqlite3.Connection) -> None:
    ids = _seed_library(db_conn)
    assert store.files_in_scope(db_conn, artist="A") == [ids["a1"], ids["a2"]]


def test_files_in_scope_by_artist_and_album(db_conn: sqlite3.Connection) -> None:
    ids = _seed_library(db_conn)
    assert store.files_in_scope(db_conn, artist="A", album="X") == [ids["a1"]]


def test_files_in_scope_by_file_ids_keeps_existing_in_order(
    db_conn: sqlite3.Connection,
) -> None:
    ids = _seed_library(db_conn)
    # Pass out of order with a bogus id; result is the existing ones in ascending order.
    requested = [ids["b1"], 9999, ids["a1"]]
    assert store.files_in_scope(db_conn, file_ids=requested) == [ids["a1"], ids["b1"]]


def test_files_in_scope_empty_file_ids_returns_empty(db_conn: sqlite3.Connection) -> None:
    _seed_library(db_conn)
    assert store.files_in_scope(db_conn, file_ids=[]) == []
