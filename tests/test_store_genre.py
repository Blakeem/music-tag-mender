"""Unit tests for the M2 genre data-access layer in :mod:`tagmend.engine.store`.

Covers the persistent Last.fm tag cache, the tag-axis outcome rows, the classifier that
derives each axis status from them, and the scope selection helpers.
"""

from __future__ import annotations

import sqlite3

import pytest

from tagmend.engine import axis, store

_NOW = "2026-06-08T00:00:00+00:00"
_LATER = "2026-06-08T01:00:00+00:00"


def _insert(
    conn: sqlite3.Connection,
    *,
    folder: str = "/lib",
    filename: str = "a.mp3",
    artist: str | None = "A",
) -> int:
    """Insert a file, giving it a source identity (``artist``) unless *artist* is ``None``.

    An identity keeps the file in ``pending``/``staged``/``done``/stored-status territory;
    passing ``artist=None`` (no ``artist`` and no ``albumartist``) makes it derive the new
    ``no_identity`` worklist state.
    """
    file_id = store.insert_file(
        conn,
        folder=folder,
        filename=filename,
        ext=".mp3",
        size_bytes=100,
        mtime_ns=1_000,
        now=_NOW,
    )
    if artist is not None:
        store.replace_tags(conn, file_id, {"artist": [artist]}, _NOW)
    return file_id


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


# --- tag-axis outcome rows ----------------------------------------------------------


def _record(
    conn: sqlite3.Connection,
    file_id: int,
    status: str,
    tag_axis: axis.Axis = axis.GENRE_AXIS,
) -> None:
    """Write *status* on *tag_axis*, snapshotting the file's current tags."""
    axis.put_outcome(
        conn,
        tag_axis,
        file_id=file_id,
        status=status,
        tags=store.get_tags(conn, file_id),
        now=_NOW,
    )


def _legacy_row(conn: sqlite3.Connection, file_id: int, status: str, artist: str | None) -> None:
    """Write a pre-v21 genre row: an identity snapshot and a NULL value snapshot."""
    conn.execute(
        """
        INSERT OR REPLACE INTO file_genre_status
          (file_id, status, source_artist, source_album, source_value, updated_at)
        VALUES (?, ?, ?, NULL, NULL, ?)
        """,
        (file_id, status, artist, _NOW),
    )


def test_outcome_absent_returns_none(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    assert axis.get_outcome(db_conn, axis.GENRE_AXIS, file_id) is None


def test_outcome_round_trip_snapshots_identity_and_values(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    store.replace_tags(
        db_conn,
        file_id,
        {"albumartist": ["BoC"], "album": ["Geogaddi"], "genre": ["idm", "ambient"]},
        _NOW,
    )
    _record(db_conn, file_id, "done")

    row = axis.get_outcome(db_conn, axis.GENRE_AXIS, file_id)
    assert row == axis.OutcomeRow(
        status="done",
        identity=axis.Identity(primary="BoC", secondary="Geogaddi"),
        values={"genre": ["idm", "ambient"]},
    )


def test_outcome_replace_and_delete(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _record(db_conn, file_id, "no_match")
    _record(db_conn, file_id, "manual")
    row = axis.get_outcome(db_conn, axis.GENRE_AXIS, file_id)
    assert row is not None
    assert row.status == "manual"

    axis.delete_status(db_conn, axis.GENRE_AXIS, file_id)
    assert axis.get_outcome(db_conn, axis.GENRE_AXIS, file_id) is None
    axis.delete_status(db_conn, axis.GENRE_AXIS, file_id)  # idempotent no-op


def test_outcome_without_identity_stores_null_identity(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn, artist=None)
    _record(db_conn, file_id, "manual")
    row = axis.get_outcome(db_conn, axis.GENRE_AXIS, file_id)
    assert row is not None
    assert row.identity == axis.Identity(primary=None, secondary=None)


def test_legacy_row_reads_null_values(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _legacy_row(db_conn, file_id, "no_match", "A")
    row = axis.get_outcome(db_conn, axis.GENRE_AXIS, file_id)
    assert row is not None
    assert row.values is None


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


# --- distinct_artists + files_in_scope ----------------------------------------------


def _seed_library(conn: sqlite3.Connection) -> dict[str, int]:
    """Seed three files: two by 'A' (one in album 'X'), one by 'B' with albumartist 'A'."""
    a1 = _insert(conn, filename="a1.mp3")
    a2 = _insert(conn, filename="a2.mp3")
    b1 = _insert(conn, filename="b1.mp3")
    store.replace_tags(conn, a1, {"artist": ["A"], "album": ["X"]}, _NOW)
    store.replace_tags(conn, a2, {"artist": ["A"], "album": ["Y"]}, _NOW)
    store.replace_tags(conn, b1, {"artist": ["B"], "albumartist": ["A"], "album": ["X"]}, _NOW)
    return {"a1": a1, "a2": a2, "b1": b1}


_NAME_FIELDS = ("artist", "albumartist")


def test_distinct_artists_counts_files(db_conn: sqlite3.Connection) -> None:
    _seed_library(db_conn)
    assert store.distinct_artists(db_conn) == [("A", 2), ("B", 1)]


def test_files_in_scope_all(db_conn: sqlite3.Connection) -> None:
    ids = _seed_library(db_conn)
    assert store.files_in_scope(db_conn) == sorted(ids.values())


def test_files_in_scope_by_value_matches_every_named_field(db_conn: sqlite3.Connection) -> None:
    ids = _seed_library(db_conn)
    assert store.files_in_scope(db_conn, value_fields=("artist",), value="A") == [
        ids["a1"],
        ids["a2"],
    ]
    assert store.files_in_scope(db_conn, value_fields=_NAME_FIELDS, value="A") == [
        ids["a1"],
        ids["a2"],
        ids["b1"],
    ]


def test_files_in_scope_by_value_and_album(db_conn: sqlite3.Connection) -> None:
    ids = _seed_library(db_conn)
    scoped = store.files_in_scope(db_conn, value_fields=_NAME_FIELDS, value="A", album="X")
    assert scoped == [ids["a1"], ids["b1"]]


def test_files_in_scope_value_needs_fields(db_conn: sqlite3.Connection) -> None:
    _seed_library(db_conn)
    with pytest.raises(ValueError, match="at least one tag"):
        store.files_in_scope(db_conn, value="A")


def test_files_in_scope_by_file_ids_keeps_existing_in_order(
    db_conn: sqlite3.Connection,
) -> None:
    ids = _seed_library(db_conn)
    # Passed out of order, returned in ascending order.
    requested = [ids["b1"], ids["a1"]]
    assert store.files_in_scope(db_conn, file_ids=requested) == [ids["a1"], ids["b1"]]


def test_files_in_scope_rejects_unknown_file_ids(db_conn: sqlite3.Connection) -> None:
    ids = _seed_library(db_conn)

    with pytest.raises(ValueError, match="9999"):
        store.files_in_scope(db_conn, file_ids=[ids["a1"], 9999])


def test_files_in_scope_empty_file_ids_returns_empty(db_conn: sqlite3.Connection) -> None:
    _seed_library(db_conn)
    assert store.files_in_scope(db_conn, file_ids=[]) == []


# --- the classifier: derived_status precedence --------------------------------------


def _stage(conn: sqlite3.Connection, file_id: int) -> None:
    """Stage a genre change for *file_id*, merged onto its current tags."""
    _stage_field(conn, file_id, "genre")


def _stage_field(conn: sqlite3.Connection, file_id: int, field_name: str) -> None:
    """Stage a change to *field_name* for *file_id*, merged onto its current tags.

    Only *field_name* differs from the file's current tags, so the file reads staged on that
    field ALONE, matching production ``staging.stage_tags`` (which merges onto current).
    """
    managed = dict(store.get_tags(conn, file_id))
    managed[field_name] = ["X"]
    store.upsert_staged_tag(
        conn,
        file_id=file_id,
        managed_tags=managed,
        origin="auto",
        now=_NOW,
    )


def _status(conn: sqlite3.Connection, file_id: int, tag_axis: axis.Axis = axis.GENRE_AXIS) -> str:
    return store.derived_status(conn, tag_axis, file_id)


def test_derived_status_pending_when_nothing(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    assert _status(db_conn, file_id) == "pending"


@pytest.mark.parametrize("outcome", ["done", "no_match"])
def test_resolver_outcome_counts_while_snapshots_match(
    db_conn: sqlite3.Connection,
    outcome: str,
) -> None:
    file_id = _insert(db_conn)
    _record(db_conn, file_id, outcome)
    assert _status(db_conn, file_id) == outcome


@pytest.mark.parametrize("outcome", ["done", "no_match"])
def test_resolver_outcome_goes_stale_on_identity_change(
    db_conn: sqlite3.Connection,
    outcome: str,
) -> None:
    file_id = _insert(db_conn)
    _record(db_conn, file_id, outcome)
    store.replace_tags(db_conn, file_id, {"artist": ["Renamed"]}, _LATER)
    assert _status(db_conn, file_id) == "pending"


@pytest.mark.parametrize("outcome", ["done", "no_match"])
def test_resolver_outcome_goes_stale_on_value_change(
    db_conn: sqlite3.Connection,
    outcome: str,
) -> None:
    file_id = _insert(db_conn)
    _record(db_conn, file_id, outcome)
    store.replace_tags(db_conn, file_id, {"artist": ["A"], "genre": ["edited"]}, _LATER)
    assert _status(db_conn, file_id) == "pending"


def test_legacy_row_matches_any_value_but_not_a_new_identity(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _legacy_row(db_conn, file_id, "no_match", "A")
    store.replace_tags(db_conn, file_id, {"artist": ["A"], "genre": ["any"]}, _LATER)
    assert _status(db_conn, file_id) == "no_match"

    store.replace_tags(db_conn, file_id, {"artist": ["Renamed"]}, _LATER)
    assert _status(db_conn, file_id) == "pending"


def test_manual_is_sticky_across_identity_and_value_changes(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _record(db_conn, file_id, "manual")
    store.replace_tags(db_conn, file_id, {"artist": ["Renamed"], "genre": ["edited"]}, _LATER)
    assert _status(db_conn, file_id) == "manual"


def test_staged_beats_manual_and_done(db_conn: sqlite3.Connection) -> None:
    manual = _insert(db_conn, filename="m.mp3")
    _record(db_conn, manual, "manual")
    _stage(db_conn, manual)
    assert _status(db_conn, manual) == "staged"

    done = _insert(db_conn, filename="d.mp3")
    _record(db_conn, done, "done")
    _stage(db_conn, done)
    assert _status(db_conn, done) == "staged"


def test_no_identity_file_derives_no_identity_on_all_axes(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn, artist=None)
    assert _status(db_conn, file_id, axis.GENRE_AXIS) == "no_identity"
    assert _status(db_conn, file_id, axis.ARTIST_AXIS) == "no_identity"
    assert _status(db_conn, file_id, axis.YEAR_AXIS) == "no_identity"


@pytest.mark.parametrize("blank", [" ", "\t", "\n", "   ", "\t\n "])
def test_whitespace_only_identity_derives_no_identity(
    db_conn: sqlite3.Connection,
    blank: str,
) -> None:
    file_id = _insert(db_conn, artist=None)
    store.replace_tags(db_conn, file_id, {"artist": [blank], "albumartist": [blank]}, _NOW)
    assert _status(db_conn, file_id, axis.GENRE_AXIS) == "no_identity"
    assert _status(db_conn, file_id, axis.ARTIST_AXIS) == "no_identity"


def test_identity_at_a_later_ordinal_is_an_identity(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn, artist=None)
    store.replace_tags(
        db_conn,
        file_id,
        {"albumartist": ["  ", "Real Artist"], "album": ["LP"]},
        _NOW,
    )
    assert _status(db_conn, file_id, axis.GENRE_AXIS) == "pending"
    assert _status(db_conn, file_id, axis.ARTIST_AXIS) == "pending"
    assert _status(db_conn, file_id, axis.YEAR_AXIS) == "pending"


def test_year_without_album_derives_no_identity(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    assert _status(db_conn, file_id, axis.GENRE_AXIS) == "pending"
    assert _status(db_conn, file_id, axis.YEAR_AXIS) == "no_identity"


def test_manual_and_staged_beat_no_identity(db_conn: sqlite3.Connection) -> None:
    manual = _insert(db_conn, filename="m.mp3", artist=None)
    _record(db_conn, manual, "manual")
    assert _status(db_conn, manual) == "manual"

    staged = _insert(db_conn, filename="s.mp3", artist=None)
    _stage(db_conn, staged)
    assert _status(db_conn, staged) == "staged"


def test_has_staged_change_for_is_field_specific(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _stage_field(db_conn, file_id, "artist")
    assert store.has_staged_change_for(db_conn, file_id, ("artist", "albumartist")) is True
    assert store.has_staged_change_for(db_conn, file_id, ("genre",)) is False


def test_field_aware_split_staged(db_conn: sqlite3.Connection) -> None:
    """A genre-only staged change reads as genre-staged but artist-pending, and vice versa."""
    genre_file = _insert(db_conn, filename="g.mp3")
    _stage_field(db_conn, genre_file, "genre")
    assert _status(db_conn, genre_file, axis.GENRE_AXIS) == "staged"
    assert _status(db_conn, genre_file, axis.ARTIST_AXIS) == "pending"

    artist_file = _insert(db_conn, filename="a.mp3")
    _stage_field(db_conn, artist_file, "artistsort")
    assert _status(db_conn, artist_file, axis.ARTIST_AXIS) == "staged"
    assert _status(db_conn, artist_file, axis.GENRE_AXIS) == "pending"


def test_axes_keep_independent_rows(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _record(db_conn, file_id, "done", axis.GENRE_AXIS)
    assert _status(db_conn, file_id, axis.GENRE_AXIS) == "done"
    assert _status(db_conn, file_id, axis.ARTIST_AXIS) == "pending"


# --- record_outcome + pending_file_ids ----------------------------------------------


def test_record_outcome_snapshots_the_staged_target(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    _stage(db_conn, file_id)
    store.record_outcome(db_conn, axis.GENRE_AXIS, file_id=file_id, status="done", now=_NOW)

    row = axis.get_outcome(db_conn, axis.GENRE_AXIS, file_id)
    assert row is not None
    assert row.values == {"genre": ["X"]}
    # The mirror catches up when the stage commits, and the row then counts.
    store.delete_staged_tag(db_conn, file_id)
    store.replace_tags(db_conn, file_id, {"artist": ["A"], "genre": ["X"]}, _LATER)
    assert _status(db_conn, file_id) == "done"


def test_pending_file_ids_skips_missing_and_settled_files(db_conn: sqlite3.Connection) -> None:
    pending = _insert(db_conn, filename="p.mp3")
    settled = _insert(db_conn, filename="s.mp3")
    _record(db_conn, settled, "done")
    missing = _insert(db_conn, filename="m.mp3")
    store.flag_missing(db_conn, missing, _NOW)
    no_identity = _insert(db_conn, filename="n.mp3", artist=None)

    scoped = [no_identity, missing, settled, pending]
    assert store.pending_file_ids(db_conn, axis.GENRE_AXIS, scoped) == [pending]


# --- status_counts + compute_stats --------------------------------------------------


def _seed_status_matrix(conn: sqlite3.Connection, tag_axis: axis.Axis) -> dict[str, int]:
    """Seed one present file in each workflow state on *tag_axis*. Returns name -> file id.

    A state the axis never derives (the song axis has no ``no_match`` and no ``no_identity``)
    is not seeded.
    """
    ids: dict[str, int] = {}
    for status in ("pending", "no_match", "manual", "done", "staged"):
        if status not in tag_axis.workflow_statuses:
            continue
        file_id = _insert(conn, filename=f"{status}.mp3")
        store.replace_tags(conn, file_id, {"artist": ["A"], "album": ["LP"]}, _NOW)
        ids[status] = file_id
        if status == "staged":
            _stage_field(conn, file_id, tag_axis.fields[0])
        elif status != "pending":
            _record(conn, file_id, status, tag_axis)
    if "no_identity" in tag_axis.workflow_statuses:
        ids["no_identity"] = _insert(conn, filename="none.mp3", artist=None)
    return ids


@pytest.mark.parametrize("tag_axis", axis.TAG_AXES, ids=lambda a: a.name)
def test_counts_all_keys_present_when_empty(
    db_conn: sqlite3.Connection,
    tag_axis: axis.Axis,
) -> None:
    counts = store.status_counts(db_conn, tag_axis)
    assert set(counts) == tag_axis.workflow_statuses
    assert all(value == 0 for value in counts.values())


@pytest.mark.parametrize("tag_axis", axis.TAG_AXES, ids=lambda a: a.name)
def test_counts_matrix(db_conn: sqlite3.Connection, tag_axis: axis.Axis) -> None:
    _seed_status_matrix(db_conn, tag_axis)
    assert store.status_counts(db_conn, tag_axis) == dict.fromkeys(
        tag_axis.workflow_statuses,
        1,
    )


@pytest.mark.parametrize("tag_axis", axis.TAG_AXES, ids=lambda a: a.name)
def test_counts_cover_present_files_only(db_conn: sqlite3.Connection, tag_axis: axis.Axis) -> None:
    ids = _seed_status_matrix(db_conn, tag_axis)
    store.flag_missing(db_conn, ids["done"], _NOW)

    counts = store.status_counts(db_conn, tag_axis)
    assert counts["done"] == 0
    stats = store.compute_stats(db_conn)
    assert sum(counts.values()) == stats["present"]
    assert stats[tag_axis.name] == counts


@pytest.mark.parametrize("tag_axis", axis.TAG_AXES, ids=lambda a: a.name)
def test_counts_match_per_file_derivation(
    db_conn: sqlite3.Connection,
    tag_axis: axis.Axis,
) -> None:
    """Drift guard: the counts must bucket exactly as the per-file derivation."""
    ids = _seed_status_matrix(db_conn, tag_axis)
    extra_done = _insert(db_conn, filename="done2.mp3")
    store.replace_tags(db_conn, extra_done, {"artist": ["A"], "album": ["LP"]}, _NOW)
    _record(db_conn, extra_done, "done", tag_axis)

    expected = dict.fromkeys(tag_axis.workflow_statuses, 0)
    for file_id in [*ids.values(), extra_done]:
        expected[store.derived_status(db_conn, tag_axis, file_id)] += 1
    assert store.status_counts(db_conn, tag_axis) == expected


# --- musicbrainz_release_group_cache ------------------------------------------------


def test_mb_cache_miss_returns_none(db_conn: sqlite3.Connection) -> None:
    assert store.get_cached_mb_release_group(db_conn, "missing") is None


def test_mb_cache_negative_round_trip(db_conn: sqlite3.Connection) -> None:
    store.put_cached_mb_release_group(
        db_conn,
        request_key="k",
        found=False,
        album_title=None,
        original_date=None,
        release_mbid=None,
        release_group_mbid=None,
        now=_NOW,
    )
    cached = store.get_cached_mb_release_group(db_conn, "k")
    assert cached is not None
    found, row = cached
    assert found is False
    assert row.original_date is None


def test_mb_cache_found_round_trip(db_conn: sqlite3.Connection) -> None:
    store.put_cached_mb_release_group(
        db_conn,
        request_key="k",
        found=True,
        album_title="Paranoid",
        original_date="1970",
        release_mbid="rel-1",
        release_group_mbid="rg-1",
        now=_NOW,
    )
    cached = store.get_cached_mb_release_group(db_conn, "k")
    assert cached is not None
    found, row = cached
    assert found is True
    assert row.album_title == "Paranoid"
    assert row.original_date == "1970"
    assert row.release_mbid == "rel-1"
    assert row.release_group_mbid == "rg-1"
