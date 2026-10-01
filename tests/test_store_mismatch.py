"""Unit tests for the mismatch-axis data access in :mod:`tagmend.engine.store`.

Covers the ``file_mismatch_status`` row and its JSON snapshot, the bulk
``load_mismatch_statuses`` reader, and the ``path_revisions`` helpers the mismatch reading and
the path tools share. The reading itself (:func:`tagmend.engine.mismatch.file_states`) is
covered in ``test_mismatch.py``.
"""

from __future__ import annotations

import sqlite3

from tagmend.engine import path_keys, store

_NOW = "2026-07-04T00:00:00+00:00"
_LATER = "2026-07-04T01:00:00+00:00"

_KEEP = store.MismatchStatusRow(
    status="legit_ignore",
    covers=("top_folder_artist",),
    tags={"albumartist": "Jem", "artist": None},
    path_version=0,
    folder_key="/lib",
)
_DEFER = store.MismatchStatusRow(
    status="misfiled_deferred",
    covers=("filename_title", "curated"),
    tags={"title": "Song"},
    path_version=2,
    folder_key=None,
)


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


# --- the decision row ----------------------------------------------------------------


def test_mismatch_status_absent_returns_none(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    assert store.get_mismatch_status(db_conn, file_id) is None


def test_mismatch_status_set_get_delete_round_trip(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)

    store.set_mismatch_status(db_conn, file_id=file_id, row=_KEEP, now=_NOW)

    assert store.get_mismatch_status(db_conn, file_id) == _KEEP
    store.delete_mismatch_status(db_conn, file_id)
    assert store.get_mismatch_status(db_conn, file_id) is None
    store.delete_mismatch_status(db_conn, file_id)  # idempotent no-op


def test_mismatch_status_snapshot_omits_folder_key_on_a_deferral(
    db_conn: sqlite3.Connection,
) -> None:
    file_id = _insert(db_conn)

    store.set_mismatch_status(db_conn, file_id=file_id, row=_DEFER, now=_NOW)

    raw = db_conn.execute(
        "SELECT source_value FROM file_mismatch_status WHERE file_id = ?",
        (file_id,),
    ).fetchone()[0]
    assert raw == (
        '{"covers":["filename_title","curated"],"path_version":2,"tags":{"title":"Song"}}'
    )


def test_mismatch_status_set_replaces(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    store.set_mismatch_status(db_conn, file_id=file_id, row=_KEEP, now=_NOW)

    store.set_mismatch_status(db_conn, file_id=file_id, row=_DEFER, now=_LATER)

    assert store.get_mismatch_status(db_conn, file_id) == _DEFER


def test_a_row_with_no_snapshot_binds_nothing(db_conn: sqlite3.Connection) -> None:
    # The column stays nullable after the v24 upgrade, so a hand-written row may hold NULL.
    file_id = _insert(db_conn)
    db_conn.execute(
        "INSERT INTO file_mismatch_status (file_id, status, source_value, updated_at) "
        "VALUES (?, 'misfiled_deferred', NULL, ?)",
        (file_id, _NOW),
    )

    row = store.get_mismatch_status(db_conn, file_id)

    assert row == store.MismatchStatusRow(
        status="misfiled_deferred",
        covers=(),
        tags={},
        path_version=None,
        folder_key=None,
    )


def test_load_mismatch_statuses_empty(db_conn: sqlite3.Connection) -> None:
    assert store.load_mismatch_statuses(db_conn) == {}


def test_load_mismatch_statuses_returns_all_rows(db_conn: sqlite3.Connection) -> None:
    one = _insert(db_conn, filename="one.mp3")
    two = _insert(db_conn, filename="two.mp3")
    _insert(db_conn, filename="none.mp3")  # no decision -> absent from the map
    store.set_mismatch_status(db_conn, file_id=one, row=_KEEP, now=_NOW)
    store.set_mismatch_status(db_conn, file_id=two, row=_DEFER, now=_NOW)

    assert store.load_mismatch_statuses(db_conn) == {one: _KEEP, two: _DEFER}


def test_compute_stats_leaves_the_mismatch_block_to_the_library(
    db_conn: sqlite3.Connection,
) -> None:
    # The mismatch reading needs the comparator and the settings, which store has neither of.
    _insert(db_conn)
    assert "mismatch" not in store.compute_stats(db_conn)


# --- path_revisions helpers ------------------------------------------------------------


def test_path_versions_hold_each_file_s_highest_version(db_conn: sqlite3.Connection) -> None:
    moved = _insert(db_conn, filename="moved.mp3")
    _insert(db_conn, filename="still.mp3")
    for version in (1, 2):
        store.insert_path_revision(
            db_conn,
            file_id=moved,
            version=version,
            commit_id=None,
            origin="manual",
            from_path="/lib/a.mp3",
            to_path="/lib/b.mp3",
            now=_NOW,
        )

    assert store.path_versions(db_conn) == {moved: 2}


def test_relocate_file_moves_the_row_and_its_path_key(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn, folder="/lib/Old", filename="a.mp3")

    store.relocate_file(db_conn, file_id, folder="/lib/New", filename="b.mp3", now=_LATER)

    row = store.get_file_by_id(db_conn, file_id)
    assert row is not None
    assert (row.folder, row.filename) == ("/lib/New", "b.mp3")
    assert store.get_file(db_conn, "/lib/New", "b.mp3") == row
    assert store.get_file(db_conn, "/lib/Old", "a.mp3") is None
    key = db_conn.execute("SELECT path_key FROM files WHERE id = ?", (file_id,)).fetchone()[0]
    assert key == path_keys.file_path_key("/lib/New", "b.mp3")


def test_staged_path_file_ids_names_only_staged_files(db_conn: sqlite3.Connection) -> None:
    staged = _insert(db_conn, filename="staged.mp3")
    clean = _insert(db_conn, filename="clean.mp3")
    db_conn.execute(
        "INSERT INTO path_revisions_staged (file_id, to_path, origin, staged_at) "
        "VALUES (?, '/lib/x.mp3', 'manual', ?)",
        (staged, _NOW),
    )

    assert store.staged_path_file_ids(db_conn, [clean, staged]) == [staged]
    assert store.staged_path_file_ids(db_conn, []) == []
