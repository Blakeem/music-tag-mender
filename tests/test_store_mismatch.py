"""Unit tests for the mismatch-axis data-access layer in :mod:`tagmend.engine.store`.

Covers the ``file_mismatch_status`` CRUD wrappers, the bulk ``load_mismatch_statuses``
reader, the stored-or-pending ``derived_mismatch_status`` (which must NOT route through the
field-aware ``derived_status`` used by the other axes), and the SQL-aggregate
``mismatch_status_counts`` + its ``compute_stats`` block.
"""

from __future__ import annotations

import sqlite3

from tagmend.engine import store

_NOW = "2026-07-04T00:00:00+00:00"
_LATER = "2026-07-04T01:00:00+00:00"


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


# --- CRUD ----------------------------------------------------------------------------


def test_mismatch_status_absent_returns_none(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    assert store.get_mismatch_status(db_conn, file_id) is None


def test_mismatch_status_set_get_delete_round_trip(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    store.set_mismatch_status(
        db_conn,
        file_id=file_id,
        status="legit_ignore",
        source_field="albumartist",
        source_value="Jem",
        now=_NOW,
    )
    row = store.get_mismatch_status(db_conn, file_id)
    assert row is not None
    assert row.status == "legit_ignore"
    assert row.source_field == "albumartist"
    assert row.source_value == "Jem"

    store.delete_mismatch_status(db_conn, file_id)
    assert store.get_mismatch_status(db_conn, file_id) is None
    store.delete_mismatch_status(db_conn, file_id)  # idempotent no-op


def test_mismatch_status_set_replaces_and_allows_null_sources(
    db_conn: sqlite3.Connection,
) -> None:
    file_id = _insert(db_conn)
    store.set_mismatch_status(
        db_conn,
        file_id=file_id,
        status="legit_ignore",
        source_field="artist",
        source_value="X",
        now=_NOW,
    )
    store.set_mismatch_status(
        db_conn,
        file_id=file_id,
        status="misfiled_deferred",
        source_field=None,
        source_value=None,
        now=_LATER,
    )
    row = store.get_mismatch_status(db_conn, file_id)
    assert row is not None
    assert row.status == "misfiled_deferred"
    assert row.source_field is None
    assert row.source_value is None


# --- load_mismatch_statuses (bulk read for the skip-filter) --------------------------


def test_load_mismatch_statuses_empty(db_conn: sqlite3.Connection) -> None:
    assert store.load_mismatch_statuses(db_conn) == {}


def test_load_mismatch_statuses_returns_all_rows(db_conn: sqlite3.Connection) -> None:
    one = _insert(db_conn, filename="one.mp3")
    two = _insert(db_conn, filename="two.mp3")
    _insert(db_conn, filename="none.mp3")  # no disposition -> absent from the map
    store.set_mismatch_status(
        db_conn,
        file_id=one,
        status="legit_ignore",
        source_field="albumartist",
        source_value="Jem",
        now=_NOW,
    )
    store.set_mismatch_status(
        db_conn,
        file_id=two,
        status="misfiled_deferred",
        source_field="artist",
        source_value="Q",
        now=_NOW,
    )

    loaded = store.load_mismatch_statuses(db_conn)
    assert set(loaded) == {one, two}
    assert loaded[one].status == "legit_ignore"
    assert loaded[one].source_field == "albumartist"
    assert loaded[two].source_value == "Q"


# --- derived_mismatch_status (stored-or-pending, never staged/done) ------------------


def test_derived_mismatch_status_pending_when_no_row(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    assert store.derived_mismatch_status(db_conn, file_id) == "pending"


def test_derived_mismatch_status_returns_stored(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    store.set_mismatch_status(
        db_conn,
        file_id=file_id,
        status="legit_ignore",
        source_field="albumartist",
        source_value="Jem",
        now=_NOW,
    )
    assert store.derived_mismatch_status(db_conn, file_id) == "legit_ignore"


def test_derived_mismatch_status_ignores_staged_and_auto_changes(
    db_conn: sqlite3.Connection,
) -> None:
    # A staged/committed artist change must NOT read as a mismatch state: the mismatch axis
    # is stored-or-pending, so a file with only an artist auto revision stays 'pending'.
    file_id = _insert(db_conn)
    store.insert_revision(
        db_conn,
        file_id=file_id,
        version=0,
        origin="auto",
        managed_tags={"artist": ["X"]},
        diff={"artist": {"from": [], "to": ["X"]}},
        now=_NOW,
    )
    store.upsert_staged_tag(
        db_conn,
        file_id=file_id,
        managed_tags={"albumartist": ["Y"]},
        origin="manual",
        now=_NOW,
    )
    # Artist axis sees staged/done; mismatch axis stays pending (no disposition row).
    assert store.derived_artist_status(db_conn, file_id) == "staged"
    assert store.derived_mismatch_status(db_conn, file_id) == "pending"


# --- mismatch_status_counts (SQL aggregate; pending = files - rows) ------------------


def test_mismatch_status_counts_all_keys_present_when_empty(
    db_conn: sqlite3.Connection,
) -> None:
    counts = store.mismatch_status_counts(db_conn)
    assert set(counts) == store.MISMATCH_WORKFLOW_STATUSES
    assert all(value == 0 for value in counts.values())


def test_mismatch_status_counts_matrix(db_conn: sqlite3.Connection) -> None:
    pending = _insert(db_conn, filename="p.mp3")  # no row
    ignore = _insert(db_conn, filename="i.mp3")
    deferred = _insert(db_conn, filename="d.mp3")
    assert pending  # bound for clarity
    store.set_mismatch_status(
        db_conn,
        file_id=ignore,
        status="legit_ignore",
        source_field="albumartist",
        source_value="Jem",
        now=_NOW,
    )
    store.set_mismatch_status(
        db_conn,
        file_id=deferred,
        status="misfiled_deferred",
        source_field="artist",
        source_value="Q",
        now=_NOW,
    )

    counts = store.mismatch_status_counts(db_conn)
    assert counts == {"pending": 1, "legit_ignore": 1, "misfiled_deferred": 1}


def test_mismatch_status_counts_pending_is_files_minus_rows(
    db_conn: sqlite3.Connection,
) -> None:
    for index in range(5):
        _insert(db_conn, filename=f"f{index}.mp3")
    ignore = _insert(db_conn, filename="i.mp3")
    store.set_mismatch_status(
        db_conn,
        file_id=ignore,
        status="legit_ignore",
        source_field="albumartist",
        source_value="Jem",
        now=_NOW,
    )
    counts = store.mismatch_status_counts(db_conn)
    assert counts["legit_ignore"] == 1
    assert counts["misfiled_deferred"] == 0
    assert counts["pending"] == 5  # 6 files - 1 disposition row


def test_compute_stats_includes_mismatch_block(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn)
    store.set_mismatch_status(
        db_conn,
        file_id=file_id,
        status="legit_ignore",
        source_field="albumartist",
        source_value="Jem",
        now=_NOW,
    )
    stats = store.compute_stats(db_conn)
    assert "mismatch" in stats
    assert stats["mismatch"] == store.mismatch_status_counts(db_conn)
