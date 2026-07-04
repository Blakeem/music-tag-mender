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


# --- derived_genre_status precedence matrix -----------------------------------------


def _set_status(
    conn: sqlite3.Connection,
    file_id: int,
    status: str,
    *,
    source_artist: str | None = None,
    source_album: str | None = None,
) -> None:
    """Write a terminal ``file_genre_status`` row for *file_id*."""
    store.set_genre_status(
        conn,
        file_id=file_id,
        status=status,
        source_artist=source_artist,
        source_album=source_album,
        now=_NOW,
    )


def _stage(conn: sqlite3.Connection, file_id: int) -> None:
    """Stage a pending genre change for *file_id* (puts it in the staging area)."""
    store.upsert_staged_tag(
        conn,
        file_id=file_id,
        managed_tags={"genre": ["Rock"]},
        origin="auto",
        now=_NOW,
    )


def _commit_auto(conn: sqlite3.Connection, file_id: int) -> None:
    """Append a committed ``origin='auto'`` GENRE revision for *file_id* (derives 'done').

    The ``diff`` records a ``genre`` change so the field-aware ``derived_genre_status``
    reads this as genre-``done``.
    """
    store.insert_revision(
        conn,
        file_id=file_id,
        version=0,
        origin="auto",
        managed_tags={"genre": ["Rock"]},
        diff={"genre": {"from": [], "to": ["Rock"]}},
        now=_NOW,
    )


def test_derived_status_pending_when_nothing(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    assert store.derived_genre_status(db_conn, file_id) == "pending"


def test_derived_status_no_match_row(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _set_status(db_conn, file_id, "no_match", source_artist="A")
    assert store.derived_genre_status(db_conn, file_id) == "no_match"


def test_derived_status_manual_row(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _set_status(db_conn, file_id, "manual")
    assert store.derived_genre_status(db_conn, file_id) == "manual"


def test_derived_status_staged(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _stage(db_conn, file_id)
    assert store.derived_genre_status(db_conn, file_id) == "staged"


def test_derived_status_done_from_auto_revision(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _commit_auto(db_conn, file_id)
    assert store.derived_genre_status(db_conn, file_id) == "done"


def test_derived_status_staged_beats_done(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _commit_auto(db_conn, file_id)
    _stage(db_conn, file_id)
    assert store.derived_genre_status(db_conn, file_id) == "staged"


def test_derived_status_staged_beats_manual(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _set_status(db_conn, file_id, "manual")
    _stage(db_conn, file_id)
    assert store.derived_genre_status(db_conn, file_id) == "staged"


def test_derived_status_staged_beats_no_match(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _set_status(db_conn, file_id, "no_match", source_artist="A")
    _stage(db_conn, file_id)
    assert store.derived_genre_status(db_conn, file_id) == "staged"


def test_derived_status_done_beats_manual(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _set_status(db_conn, file_id, "manual")
    _commit_auto(db_conn, file_id)
    assert store.derived_genre_status(db_conn, file_id) == "done"


def test_derived_status_done_beats_no_match(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _set_status(db_conn, file_id, "no_match", source_artist="A")
    _commit_auto(db_conn, file_id)
    assert store.derived_genre_status(db_conn, file_id) == "done"


# --- genre_status_counts ------------------------------------------------------------


def _seed_status_matrix(conn: sqlite3.Connection) -> dict[str, int]:
    """Seed one file in each of the five workflow states. Returns name -> file id."""
    pending = _insert(conn, filename="pending.mp3")

    no_match = _insert(conn, filename="no_match.mp3")
    _set_status(conn, no_match, "no_match", source_artist="A")

    manual = _insert(conn, filename="manual.mp3")
    _set_status(conn, manual, "manual")

    staged = _insert(conn, filename="staged.mp3")
    _stage(conn, staged)

    done = _insert(conn, filename="done.mp3")
    _commit_auto(conn, done)

    return {
        "pending": pending,
        "no_match": no_match,
        "manual": manual,
        "staged": staged,
        "done": done,
    }


def test_genre_status_counts_all_five_keys_present_when_empty(
    db_conn: sqlite3.Connection,
) -> None:
    counts = store.genre_status_counts(db_conn)
    assert set(counts) == store.GENRE_WORKFLOW_STATUSES
    assert all(value == 0 for value in counts.values())


def test_genre_status_counts_matrix(db_conn: sqlite3.Connection) -> None:
    _seed_status_matrix(db_conn)
    counts = store.genre_status_counts(db_conn)
    assert counts == {
        "pending": 1,
        "no_match": 1,
        "manual": 1,
        "staged": 1,
        "done": 1,
    }


def test_genre_status_counts_sql_vs_python_drift_guard(
    db_conn: sqlite3.Connection,
) -> None:
    """SQL-vs-Python drift guard: the CASE query must bucket exactly as derived_genre_status.

    Recompute the counts in Python by calling ``derived_genre_status`` per file and
    assert dict equality with the single-pass SQL ``genre_status_counts`` result; any
    divergence between the two implementations fails here.
    """
    ids = _seed_status_matrix(db_conn)
    # Add a couple of duplicates so the counts are not all 1 (catches mis-bucketing).
    extra_pending = _insert(db_conn, filename="pending2.mp3")
    extra_done = _insert(db_conn, filename="done2.mp3")
    _commit_auto(db_conn, extra_done)
    all_ids = [*ids.values(), extra_pending, extra_done]

    expected = dict.fromkeys(store.GENRE_WORKFLOW_STATUSES, 0)
    for file_id in all_ids:
        expected[store.derived_genre_status(db_conn, file_id)] += 1

    assert store.genre_status_counts(db_conn) == expected


# --- compute_stats genre block ------------------------------------------------------


def test_compute_stats_includes_genre_block(db_conn: sqlite3.Connection) -> None:
    _seed_status_matrix(db_conn)
    stats = store.compute_stats(db_conn)
    assert "genre" in stats
    assert stats["genre"] == store.genre_status_counts(db_conn)


# --- field-aware staged/done predicates ---------------------------------------------


def _commit_auto_field(
    conn: sqlite3.Connection,
    file_id: int,
    field_name: str,
) -> None:
    """Append a committed ``auto`` revision whose ``diff`` changed only *field_name*."""
    store.insert_revision(
        conn,
        file_id=file_id,
        version=0,
        origin="auto",
        managed_tags={field_name: ["X"]},
        diff={field_name: {"from": [], "to": ["X"]}},
        now=_NOW,
    )


def _stage_field(conn: sqlite3.Connection, file_id: int, field_name: str) -> None:
    """Stage a pending change that alters only *field_name* (current tags are empty)."""
    store.upsert_staged_tag(
        conn,
        file_id=file_id,
        managed_tags={field_name: ["X"]},
        origin="auto",
        now=_NOW,
    )


def test_has_auto_change_for_is_field_specific(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _commit_auto_field(db_conn, file_id, "genre")
    assert store.has_auto_change_for(db_conn, file_id, ("genre",)) is True
    assert store.has_auto_change_for(db_conn, file_id, ("artist", "albumartist")) is False


def test_has_staged_change_for_is_field_specific(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _stage_field(db_conn, file_id, "artist")
    assert store.has_staged_change_for(db_conn, file_id, ("artist", "albumartist")) is True
    assert store.has_staged_change_for(db_conn, file_id, ("genre",)) is False


def test_field_aware_split_done(db_conn: sqlite3.Connection) -> None:
    """A genre-only auto commit reads as genre-done but artist-pending, and vice versa."""
    genre_file = _insert(db_conn, filename="g.mp3")
    _commit_auto_field(db_conn, genre_file, "genre")
    assert store.derived_genre_status(db_conn, genre_file) == "done"
    assert store.derived_artist_status(db_conn, genre_file) == "pending"

    artist_file = _insert(db_conn, filename="a.mp3")
    _commit_auto_field(db_conn, artist_file, "artist")
    assert store.derived_artist_status(db_conn, artist_file) == "done"
    assert store.derived_genre_status(db_conn, artist_file) == "pending"


def test_field_aware_split_staged(db_conn: sqlite3.Connection) -> None:
    """A genre-only staged change reads as genre-staged but artist-pending, and vice versa."""
    genre_file = _insert(db_conn, filename="g.mp3")
    _stage_field(db_conn, genre_file, "genre")
    assert store.derived_genre_status(db_conn, genre_file) == "staged"
    assert store.derived_artist_status(db_conn, genre_file) == "pending"

    artist_file = _insert(db_conn, filename="a.mp3")
    _stage_field(db_conn, artist_file, "albumartist")
    assert store.derived_artist_status(db_conn, artist_file) == "staged"
    assert store.derived_genre_status(db_conn, artist_file) == "pending"


# --- file_artist_status (sticky manual exclusion) -----------------------------------


def test_artist_status_absent_returns_none(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    assert store.get_artist_status(db_conn, file_id) is None


def test_artist_status_set_get_delete_round_trip(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    store.set_artist_status(
        db_conn,
        file_id=file_id,
        status="manual",
        source_artist="Miami Nights 84",
        source_albumartist="VA",
        now=_NOW,
    )
    row = store.get_artist_status(db_conn, file_id)
    assert row is not None
    assert row.status == "manual"
    assert row.source_artist == "Miami Nights 84"
    assert row.source_albumartist == "VA"

    store.delete_artist_status(db_conn, file_id)
    assert store.get_artist_status(db_conn, file_id) is None
    store.delete_artist_status(db_conn, file_id)  # idempotent no-op


def test_derived_artist_status_manual_below_staged_and_done(
    db_conn: sqlite3.Connection,
) -> None:
    file_id = _insert(db_conn)
    store.set_artist_status(
        db_conn,
        file_id=file_id,
        status="manual",
        source_artist="A",
        source_albumartist=None,
        now=_NOW,
    )
    assert store.derived_artist_status(db_conn, file_id) == "manual"

    _commit_auto_field(db_conn, file_id, "artist")
    assert store.derived_artist_status(db_conn, file_id) == "done"

    _stage_field(db_conn, file_id, "albumartist")
    assert store.derived_artist_status(db_conn, file_id) == "staged"


# --- artist_status_counts + compute_stats artist block ------------------------------


def _seed_artist_matrix(conn: sqlite3.Connection) -> dict[str, int]:
    """Seed one file in each of the four artist workflow states. Returns name -> id."""
    pending = _insert(conn, filename="ap.mp3")

    manual = _insert(conn, filename="am.mp3")
    store.set_artist_status(
        conn,
        file_id=manual,
        status="manual",
        source_artist="A",
        source_albumartist=None,
        now=_NOW,
    )

    staged = _insert(conn, filename="as.mp3")
    _stage_field(conn, staged, "artist")

    done = _insert(conn, filename="ad.mp3")
    _commit_auto_field(conn, done, "albumartist")

    return {"pending": pending, "manual": manual, "staged": staged, "done": done}


def test_artist_status_counts_all_keys_present_when_empty(
    db_conn: sqlite3.Connection,
) -> None:
    counts = store.artist_status_counts(db_conn)
    assert set(counts) == store.ARTIST_WORKFLOW_STATUSES
    assert all(value == 0 for value in counts.values())


def test_artist_status_counts_matrix(db_conn: sqlite3.Connection) -> None:
    _seed_artist_matrix(db_conn)
    assert store.artist_status_counts(db_conn) == {
        "pending": 1,
        "manual": 1,
        "staged": 1,
        "done": 1,
    }


def test_artist_status_counts_match_per_file_derivation(
    db_conn: sqlite3.Connection,
) -> None:
    ids = _seed_artist_matrix(db_conn)
    extra_done = _insert(db_conn, filename="ad2.mp3")
    _commit_auto_field(db_conn, extra_done, "artist")
    all_ids = [*ids.values(), extra_done]

    expected = dict.fromkeys(store.ARTIST_WORKFLOW_STATUSES, 0)
    for file_id in all_ids:
        expected[store.derived_artist_status(db_conn, file_id)] += 1
    assert store.artist_status_counts(db_conn) == expected


def test_compute_stats_includes_artist_block(db_conn: sqlite3.Connection) -> None:
    _seed_artist_matrix(db_conn)
    stats = store.compute_stats(db_conn)
    assert "artist" in stats
    assert stats["artist"] == store.artist_status_counts(db_conn)


# --- file_album_status (album-axis twin of genre) -----------------------------------


def test_album_status_set_get_delete_round_trip(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    store.set_album_status(
        db_conn,
        file_id=file_id,
        status="no_match",
        source_artist="Black Sabbath",
        source_album="Paranoid",
        now=_NOW,
    )
    row = store.get_album_status(db_conn, file_id)
    assert row is not None
    assert row.status == "no_match"
    assert row.source_artist == "Black Sabbath"
    assert row.source_album == "Paranoid"

    store.delete_album_status(db_conn, file_id)
    assert store.get_album_status(db_conn, file_id) is None
    store.delete_album_status(db_conn, file_id)  # idempotent no-op


def test_derived_album_status_across_all_five_states(db_conn: sqlite3.Connection) -> None:
    pending = _insert(db_conn, filename="alp.mp3")
    assert store.derived_album_status(db_conn, pending) == "pending"

    no_match = _insert(db_conn, filename="alnm.mp3")
    store.set_album_status(
        db_conn,
        file_id=no_match,
        status="no_match",
        source_artist="A",
        source_album="B",
        now=_NOW,
    )
    assert store.derived_album_status(db_conn, no_match) == "no_match"

    manual = _insert(db_conn, filename="alm.mp3")
    store.set_album_status(
        db_conn,
        file_id=manual,
        status="manual",
        source_artist=None,
        source_album=None,
        now=_NOW,
    )
    assert store.derived_album_status(db_conn, manual) == "manual"

    staged = _insert(db_conn, filename="als.mp3")
    _stage_field(db_conn, staged, "originaldate")
    assert store.derived_album_status(db_conn, staged) == "staged"

    done = _insert(db_conn, filename="ald.mp3")
    _commit_auto_field(db_conn, done, "originaldate")
    assert store.derived_album_status(db_conn, done) == "done"


def test_album_status_counts_match_per_file_derivation(db_conn: sqlite3.Connection) -> None:
    pending = _insert(db_conn, filename="p.mp3")
    no_match = _insert(db_conn, filename="nm.mp3")
    store.set_album_status(
        db_conn,
        file_id=no_match,
        status="no_match",
        source_artist="A",
        source_album="B",
        now=_NOW,
    )
    done = _insert(db_conn, filename="d.mp3")
    _commit_auto_field(db_conn, done, "originaldate")

    expected = dict.fromkeys(store.ALBUM_WORKFLOW_STATUSES, 0)
    for file_id in (pending, no_match, done):
        expected[store.derived_album_status(db_conn, file_id)] += 1
    assert store.album_status_counts(db_conn) == expected


def test_compute_stats_includes_album_block(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _commit_auto_field(db_conn, file_id, "originaldate")
    stats = store.compute_stats(db_conn)
    assert "album" in stats
    assert stats["album"] == store.album_status_counts(db_conn)


# --- musicbrainz_cache --------------------------------------------------------------


def test_mb_cache_miss_returns_none(db_conn: sqlite3.Connection) -> None:
    assert store.get_cached_mb_album(db_conn, "missing") is None


def test_mb_cache_negative_round_trip(db_conn: sqlite3.Connection) -> None:
    store.put_cached_mb_album(
        db_conn,
        request_key="k",
        found=False,
        album_title=None,
        original_date=None,
        release_mbid=None,
        release_group_id=None,
        now=_NOW,
    )
    cached = store.get_cached_mb_album(db_conn, "k")
    assert cached is not None
    found, row = cached
    assert found is False
    assert row.original_date is None


def test_mb_cache_found_round_trip(db_conn: sqlite3.Connection) -> None:
    store.put_cached_mb_album(
        db_conn,
        request_key="k",
        found=True,
        album_title="Paranoid",
        original_date="1970",
        release_mbid="rel-1",
        release_group_id="rg-1",
        now=_NOW,
    )
    cached = store.get_cached_mb_album(db_conn, "k")
    assert cached is not None
    found, row = cached
    assert found is True
    assert row.album_title == "Paranoid"
    assert row.original_date == "1970"
    assert row.release_mbid == "rel-1"
    assert row.release_group_id == "rg-1"


# --- voided_auto watermark (the NN7 re-pend primitive) ------------------------------


def _commit_auto_field_at(
    conn: sqlite3.Connection,
    file_id: int,
    field_name: str,
    version: int,
) -> None:
    """Append a committed ``auto`` revision at *version* whose diff changed only *field_name*."""
    store.insert_revision(
        conn,
        file_id=file_id,
        version=version,
        origin="auto",
        managed_tags={field_name: ["X"]},
        diff={field_name: {"from": [], "to": ["X"]}},
        now=_NOW,
    )


def test_void_auto_changes_repends_the_voided_field(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _commit_auto_field(db_conn, file_id, "genre")  # version 0, auto
    assert store.has_auto_change_for(db_conn, file_id, ("genre",)) is True

    store.void_auto_changes(db_conn, file_id, ("genre",))
    # The stale auto genre no longer reads as done — it re-pends.
    assert store.has_auto_change_for(db_conn, file_id, ("genre",)) is False


def test_void_auto_changes_is_re_pend_safe_for_a_later_auto_commit(
    db_conn: sqlite3.Connection,
) -> None:
    file_id = _insert(db_conn)
    _commit_auto_field_at(db_conn, file_id, "genre", 0)
    store.void_auto_changes(db_conn, file_id, ("genre",))  # watermark = 0
    assert store.has_auto_change_for(db_conn, file_id, ("genre",)) is False

    # A fresh auto revision ABOVE the watermark counts as done again.
    _commit_auto_field_at(db_conn, file_id, "genre", 1)
    assert store.has_auto_change_for(db_conn, file_id, ("genre",)) is True


def test_void_auto_changes_isolates_other_files(db_conn: sqlite3.Connection) -> None:
    one = _insert(db_conn, filename="one.mp3")
    two = _insert(db_conn, filename="two.mp3")
    _commit_auto_field(db_conn, one, "genre")
    _commit_auto_field(db_conn, two, "genre")

    store.void_auto_changes(db_conn, one, ("genre",))
    assert store.has_auto_change_for(db_conn, one, ("genre",)) is False
    assert store.has_auto_change_for(db_conn, two, ("genre",)) is True  # untouched


def test_void_auto_changes_is_per_field_correlated(db_conn: sqlite3.Connection) -> None:
    # The artist axis passes two fields — voiding one must not void the other (a single
    # shared watermark subquery would be wrong).
    file_id = _insert(db_conn)
    _commit_auto_field_at(db_conn, file_id, "artist", 0)
    _commit_auto_field_at(db_conn, file_id, "albumartist", 1)
    assert store.has_auto_change_for(db_conn, file_id, ("artist", "albumartist")) is True

    store.void_auto_changes(db_conn, file_id, ("artist",))  # watermark(artist) = MAX = 1
    # albumartist (unvoided) still counts, so the two-field axis stays done...
    assert store.has_auto_change_for(db_conn, file_id, ("artist", "albumartist")) is True
    # ...but artist on its own re-pends.
    assert store.has_auto_change_for(db_conn, file_id, ("artist",)) is False


def test_void_auto_changes_re_void_moves_watermark_forward(
    db_conn: sqlite3.Connection,
) -> None:
    file_id = _insert(db_conn)
    _commit_auto_field_at(db_conn, file_id, "genre", 0)
    store.void_auto_changes(db_conn, file_id, ("genre",))  # watermark = 0

    _commit_auto_field_at(db_conn, file_id, "genre", 1)
    assert store.has_auto_change_for(db_conn, file_id, ("genre",)) is True  # 1 > 0

    # INSERT OR REPLACE: re-voiding advances the watermark to the new MAX, re-pending again.
    store.void_auto_changes(db_conn, file_id, ("genre",))  # watermark = 1
    assert store.has_auto_change_for(db_conn, file_id, ("genre",)) is False


def test_void_auto_changes_is_a_noop_without_revisions(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    store.void_auto_changes(db_conn, file_id, ("genre",))  # no versions yet -> no-op

    # No watermark was written, so a later auto commit still reads as done.
    _commit_auto_field(db_conn, file_id, "genre")
    assert store.has_auto_change_for(db_conn, file_id, ("genre",)) is True


def test_void_flips_derived_genre_and_album_status(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _commit_auto_field_at(db_conn, file_id, "genre", 0)  # genre done
    _commit_auto_field_at(db_conn, file_id, "originaldate", 1)  # album done
    assert store.derived_genre_status(db_conn, file_id) == "done"
    assert store.derived_album_status(db_conn, file_id) == "done"

    store.void_auto_changes(db_conn, file_id, ("genre", "originaldate"))
    assert store.derived_genre_status(db_conn, file_id) == "pending"
    assert store.derived_album_status(db_conn, file_id) == "pending"

    # A fresh auto commit past the watermark returns both axes to done.
    _commit_auto_field_at(db_conn, file_id, "genre", 2)
    _commit_auto_field_at(db_conn, file_id, "originaldate", 3)
    assert store.derived_genre_status(db_conn, file_id) == "done"
    assert store.derived_album_status(db_conn, file_id) == "done"
