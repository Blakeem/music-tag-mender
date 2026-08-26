"""Unit tests for the staged-tag + commits data-access building blocks.

These exercise the staging-area SQL in :mod:`tagmend.engine.store` and the
``commits``-table ops in :mod:`tagmend.engine.commits` against an in-memory ledger
(``db_conn``) without touching real files. The full stage -> commit -> revert disk loop
and crash recovery are covered in ``test_staging_integration``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tagmend.engine import commits, db, schema, staging, store, versioning

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings

_NOW = "2026-06-02T00:00:00+00:00"
_LATER = "2026-06-02T01:00:00+00:00"


def _insert_file(
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
        size_bytes=1,
        mtime_ns=1,
        now=_NOW,
    )


# --- staged-tag round trips --------------------------------------------------------


def test_upsert_and_get_staged_tag(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)

    store.upsert_staged_tag(
        db_conn,
        file_id=file_id,
        managed_tags={"genre": ["Synthwave"]},
        origin="manual",
        now=_NOW,
        note="hi",
    )

    staged = store.get_staged_tag(db_conn, file_id)
    assert staged is not None
    assert staged.managed_tags == {"genre": ["Synthwave"]}
    assert staged.origin == "manual"
    assert staged.note == "hi"


def test_get_staged_tag_absent_returns_none(db_conn: sqlite3.Connection) -> None:
    assert store.get_staged_tag(db_conn, 123) is None


def test_restage_second_target_wins(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)
    store.upsert_staged_tag(
        db_conn, file_id=file_id, managed_tags={"genre": ["Old"]}, origin="auto", now=_NOW
    )

    store.upsert_staged_tag(
        db_conn, file_id=file_id, managed_tags={"genre": ["New"]}, origin="manual", now=_LATER
    )

    staged = store.get_staged_tag(db_conn, file_id)
    assert staged is not None
    assert staged.managed_tags == {"genre": ["New"]}  # second target wins
    assert staged.origin == "manual"
    assert store.list_staged_tags(db_conn) == [staged]  # exactly one row


def test_delete_staged_tag(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)
    store.upsert_staged_tag(
        db_conn, file_id=file_id, managed_tags={"genre": ["X"]}, origin="manual", now=_NOW
    )

    store.delete_staged_tag(db_conn, file_id)

    assert store.get_staged_tag(db_conn, file_id) is None


# --- listing -----------------------------------------------------------------------


def test_list_staged_tags_orders_by_file_id(db_conn: sqlite3.Connection) -> None:
    a = _insert_file(db_conn, filename="a.mp3")
    b = _insert_file(db_conn, filename="b.mp3")
    for file_id in (b, a):  # stage out of order
        store.upsert_staged_tag(
            db_conn, file_id=file_id, managed_tags={"genre": ["X"]}, origin="auto", now=_NOW
        )

    assert [s.file_id for s in store.list_staged_tags(db_conn)] == [a, b]  # id order


def test_list_staged_tags_under_filters_by_folder(db_conn: sqlite3.Connection) -> None:
    rock = _insert_file(db_conn, folder="/music/rock", filename="r.mp3")
    rock_sub = _insert_file(db_conn, folder="/music/rock/live", filename="s.mp3")
    jazz = _insert_file(db_conn, folder="/music/jazz", filename="j.mp3")
    for file_id in (rock, rock_sub, jazz):
        store.upsert_staged_tag(
            db_conn, file_id=file_id, managed_tags={"genre": ["X"]}, origin="auto", now=_NOW
        )

    under_rock = store.list_staged_tags_under(db_conn, Path("/music/rock"))

    assert {s.file_id for s in under_rock} == {rock, rock_sub}  # nested included, jazz out


# --- commits -----------------------------------------------------------------------


def test_commit_lifecycle(db_conn: sqlite3.Connection) -> None:
    commit_id = commits.create_commit(db_conn, origin="manual", message="msg", now=_NOW)

    commit = commits.get_commit(db_conn, commit_id)
    assert commit is not None
    assert commit.status == "applying"
    assert commit.message == "msg"
    assert [c.id for c in commits.get_applying_commits(db_conn)] == [commit_id]

    commits.set_commit_status(db_conn, commit_id, "applied")

    assert commits.get_commit(db_conn, commit_id).status == "applied"  # type: ignore[union-attr]
    assert commits.get_applying_commits(db_conn) == []  # no longer interrupted


def test_set_commit_status_rejects_unknown(db_conn: sqlite3.Connection) -> None:
    commit_id = commits.create_commit(db_conn, origin="manual", message=None, now=_NOW)

    with pytest.raises(ValueError, match="unknown commit status"):
        commits.set_commit_status(db_conn, commit_id, "done")


def test_mark_interrupted_flips_lingering_applying(db_conn: sqlite3.Connection) -> None:
    applied = commits.create_commit(db_conn, origin="manual", message=None, now=_NOW)
    commits.set_commit_status(db_conn, applied, "applied")
    lingering = commits.create_commit(db_conn, origin="manual", message=None, now=_LATER)

    flipped = commits.mark_interrupted(db_conn)

    assert flipped == 1  # only the lingering 'applying' row
    assert commits.get_commit(db_conn, lingering).status == "interrupted"  # type: ignore[union-attr]
    assert commits.get_commit(db_conn, applied).status == "applied"  # type: ignore[union-attr]
    assert commits.get_applying_commits(db_conn) == []


def test_list_commits_newest_first_with_limit(db_conn: sqlite3.Connection) -> None:
    first = commits.create_commit(db_conn, origin="manual", message="1", now=_NOW)
    second = commits.create_commit(db_conn, origin="manual", message="2", now=_LATER)
    third = commits.create_commit(db_conn, origin="manual", message="3", now=_LATER)

    assert [c.id for c in commits.list_commits(db_conn)] == [third, second, first]
    assert [c.id for c in commits.list_commits(db_conn, limit=2)] == [third, second]


# --- append_revision threads commit_id (versioning unchanged) ----------------------


def test_append_revision_threads_commit_id(db_conn: sqlite3.Connection) -> None:
    file_id = _insert_file(db_conn)
    versioning.ensure_baseline(db_conn, file_id, managed_tags={"genre": ["Electronic"]}, now=_NOW)
    commit_id = commits.create_commit(db_conn, origin="manual", message=None, now=_NOW)

    v1 = versioning.append_revision(
        db_conn,
        file_id,
        managed_tags={"genre": ["Synthwave"]},
        origin="manual",
        now=_LATER,
        commit_id=commit_id,
    )
    assert v1 == 1
    assert store.get_revision(db_conn, file_id, 1).commit_id == commit_id  # type: ignore[union-attr]

    # Default stays None when omitted (baseline and ungrouped edits).
    v2 = versioning.append_revision(
        db_conn, file_id, managed_tags={"genre": ["House"]}, origin="manual", now=_LATER
    )
    assert v2 == 2
    assert store.get_revision(db_conn, file_id, 2).commit_id is None  # type: ignore[union-attr]
    assert store.get_revision(db_conn, file_id, 0).commit_id is None  # type: ignore[union-attr]


# --- stage_tags_batch entry-shape validation ---------------------------------------


def _seed_file(settings: Settings, filename: str) -> int:
    """Insert one file row into the ledger *settings* points at; no audio on disk."""
    conn = db.connect(settings.db_path)
    try:
        schema.apply_schema(conn)
        file_id = store.insert_file(
            conn,
            folder=str(settings.music_path),
            filename=filename,
            ext=".mp3",
            size_bytes=1,
            mtime_ns=1,
            now=_NOW,
        )
        conn.commit()
    finally:
        conn.close()
    return file_id


def _staged_ids(settings: Settings) -> list[int]:
    conn = db.connect(settings.db_path)
    try:
        schema.apply_schema(conn)
        return [row.file_id for row in store.list_staged_tags(conn)]
    finally:
        conn.close()


def test_stage_tags_batch_rejects_mcp_shaped_dict_entry(engine_settings: Settings) -> None:
    file_id = _seed_file(engine_settings, "a.mp3")

    with pytest.raises(ValueError, match=r"entry 0: expected a \(file_id, tags\) tuple") as excinfo:
        staging.stage_tags_batch(
            engine_settings,
            entries=[{"file_id": file_id, "tags": {"genre": ["Rock"]}}],
        )
    # The old destructuring bug reported this shape error as a duplicate id.
    assert "duplicate" not in str(excinfo.value)
    assert _staged_ids(engine_settings) == []


def test_stage_tags_batch_rejects_non_integer_file_id(engine_settings: Settings) -> None:
    _seed_file(engine_settings, "a.mp3")

    with pytest.raises(ValueError, match="entry 0: file_id must be an integer"):
        staging.stage_tags_batch(engine_settings, entries=[("1", {"genre": ["Rock"]})])
    assert _staged_ids(engine_settings) == []


def test_stage_tags_batch_rejects_wrong_length_tuple(engine_settings: Settings) -> None:
    file_id = _seed_file(engine_settings, "a.mp3")

    with pytest.raises(ValueError, match=r"entry 1: expected a \(file_id, tags\) tuple of 2"):
        staging.stage_tags_batch(
            engine_settings,
            entries=[(file_id, {"genre": ["Rock"]}), (file_id, {"genre": ["Metal"]}, "extra")],
        )
    assert _staged_ids(engine_settings) == []


def test_stage_tags_batch_rejects_non_dict_tags(engine_settings: Settings) -> None:
    file_id = _seed_file(engine_settings, "a.mp3")

    with pytest.raises(ValueError, match=r"entry 0 .*: tags must be a dict"):
        staging.stage_tags_batch(engine_settings, entries=[(file_id, "Rock")])
    assert _staged_ids(engine_settings) == []


def test_stage_tags_batch_valid_entries_still_stage(engine_settings: Settings) -> None:
    a_id = _seed_file(engine_settings, "a.mp3")
    b_id = _seed_file(engine_settings, "b.mp3")

    staged = staging.stage_tags_batch(
        engine_settings,
        entries=[(a_id, {"genre": ["Rock"]}), (b_id, {"genre": ["Metal"]})],
    )

    assert staged == [a_id, b_id]
    assert _staged_ids(engine_settings) == [a_id, b_id]
