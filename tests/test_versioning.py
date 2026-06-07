"""Unit tests for the tag-revision engine (:mod:`tagmend.engine.versioning`).

These exercise the append/diff/baseline logic against an in-memory ledger (``db_conn``)
without touching real files. The disk-write + revert path is covered end-to-end in
``test_versioning_integration``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tagmend.engine import store, versioning

if TYPE_CHECKING:
    import sqlite3

_NOW = "2026-06-02T00:00:00+00:00"
_LATER = "2026-06-02T01:00:00+00:00"


def _insert_file(conn: sqlite3.Connection) -> int:
    return store.insert_file(
        conn,
        folder="/lib",
        filename="a.mp3",
        ext=".mp3",
        size_bytes=1,
        mtime_ns=1,
        now=_NOW,
    )


def test_ensure_baseline_creates_version_zero(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)

    created = versioning.ensure_baseline(
        db_conn,
        file_id,
        managed_tags={"genre": ["Electronic"]},
        now=_NOW,
    )

    assert created is True
    rev = store.get_revision(db_conn, file_id, 0)
    assert rev is not None
    assert rev.origin == "scan"
    assert rev.managed_tags == {"genre": ["Electronic"]}
    assert rev.diff == {}


def test_ensure_baseline_slices_to_managed_tags(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)

    versioning.ensure_baseline(
        db_conn,
        file_id,
        managed_tags={"genre": ["Electronic"], "title": ["Song"], "tracknumber": ["1"]},
        now=_NOW,
    )

    rev = store.get_revision(db_conn, file_id, 0)
    assert rev is not None
    # Unmanaged keys (title, tracknumber) are dropped from the snapshot.
    assert rev.managed_tags == {"genre": ["Electronic"]}


def test_ensure_baseline_is_idempotent(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)

    assert (
        versioning.ensure_baseline(db_conn, file_id, managed_tags={"genre": ["A"]}, now=_NOW)
        is True
    )
    # A second call is a no-op and must not overwrite the original baseline.
    assert (
        versioning.ensure_baseline(db_conn, file_id, managed_tags={"genre": ["B"]}, now=_LATER)
        is False
    )

    revisions = versioning.history(db_conn, file_id)
    assert len(revisions) == 1
    assert revisions[0].managed_tags == {"genre": ["A"]}


def test_append_revision_records_change(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)
    versioning.ensure_baseline(db_conn, file_id, managed_tags={"genre": ["Electronic"]}, now=_NOW)

    version = versioning.append_revision(
        db_conn,
        file_id,
        managed_tags={"genre": ["Synthwave"]},
        origin="manual",
        now=_LATER,
    )

    assert version == 1
    rev = store.get_revision(db_conn, file_id, 1)
    assert rev is not None
    assert rev.origin == "manual"
    assert rev.managed_tags == {"genre": ["Synthwave"]}
    assert rev.diff == {"genre": {"from": ["Electronic"], "to": ["Synthwave"]}}


def test_append_revision_no_managed_change_returns_none(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)
    versioning.ensure_baseline(db_conn, file_id, managed_tags={"genre": ["Electronic"]}, now=_NOW)

    # Identical managed tags, plus an unmanaged tag that differs -> still a no-op.
    result = versioning.append_revision(
        db_conn,
        file_id,
        managed_tags={"genre": ["Electronic"], "title": ["New Title"]},
        origin="manual",
        now=_LATER,
    )

    assert result is None
    assert len(versioning.history(db_conn, file_id)) == 1


def test_append_revision_without_baseline_raises(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)
    with pytest.raises(ValueError, match="no baseline"):
        versioning.append_revision(
            db_conn,
            file_id,
            managed_tags={"genre": ["X"]},
            origin="manual",
            now=_NOW,
        )


def test_append_revision_diff_shapes_added_and_removed(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)
    versioning.ensure_baseline(db_conn, file_id, managed_tags={"genre": ["Electronic"]}, now=_NOW)

    # Remove genre and add albumartist in a single change.
    versioning.append_revision(
        db_conn,
        file_id,
        managed_tags={"albumartist": ["AA"]},
        origin="manual",
        now=_LATER,
    )

    rev = store.get_revision(db_conn, file_id, 1)
    assert rev is not None
    assert rev.diff == {
        "genre": {"from": ["Electronic"], "to": []},
        "albumartist": {"from": [], "to": ["AA"]},
    }


def test_append_revision_preserves_multi_value_order(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)
    versioning.ensure_baseline(
        db_conn,
        file_id,
        managed_tags={"genre": ["Synthwave", "Darksynth"]},
        now=_NOW,
    )

    # Same values, different order -> a real change (order is meaningful).
    version = versioning.append_revision(
        db_conn,
        file_id,
        managed_tags={"genre": ["Darksynth", "Synthwave"]},
        origin="manual",
        now=_LATER,
    )
    assert version == 1


def test_history_is_ordered_oldest_first(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)
    versioning.ensure_baseline(db_conn, file_id, managed_tags={"genre": ["A"]}, now=_NOW)
    versioning.append_revision(
        db_conn,
        file_id,
        managed_tags={"genre": ["B"]},
        origin="manual",
        now=_LATER,
    )

    assert [r.version for r in versioning.history(db_conn, file_id)] == [0, 1]
