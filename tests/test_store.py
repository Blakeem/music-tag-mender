"""Unit tests for the snapshot data-access layer (:mod:`tagmend.engine.store`)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tagmend.engine import store

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

_NOW = "2026-06-02T00:00:00+00:00"
_LATER = "2026-06-02T01:00:00+00:00"


def _insert(  # noqa: PLR0913 - thin keyword-only wrapper mirroring insert_file
    conn: sqlite3.Connection,
    *,
    folder: str,
    filename: str,
    ext: str = ".mp3",
    size_bytes: int | None = 100,
    mtime_ns: int | None = 1_000,
) -> int:
    return store.insert_file(
        conn,
        folder=folder,
        filename=filename,
        ext=ext,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        now=_NOW,
    )


def test_insert_get_round_trip(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn, folder="/lib/music", filename="a.mp3")

    row = store.get_file(db_conn, "/lib/music", "a.mp3")
    assert row is not None
    assert row.id == file_id
    assert row.folder == "/lib/music"
    assert row.filename == "a.mp3"
    assert row.ext == ".mp3"
    assert row.size_bytes == 100
    assert row.mtime_ns == 1_000
    assert row.is_missing is False
    assert row.tags_updated_at is None


def test_get_file_absent_returns_none(db_conn: sqlite3.Connection) -> None:
    assert store.get_file(db_conn, "/lib/music", "missing.mp3") is None


def test_update_signature_changes_size_and_mtime(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn, folder="/lib", filename="a.mp3")

    store.update_signature(db_conn, file_id, size_bytes=200, mtime_ns=2_000, now=_LATER)

    row = store.get_file(db_conn, "/lib", "a.mp3")
    assert row is not None
    assert row.size_bytes == 200
    assert row.mtime_ns == 2_000


def test_flag_and_clear_missing_toggle(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn, folder="/lib", filename="a.mp3")

    store.flag_missing(db_conn, file_id, _LATER)
    flagged = store.get_file(db_conn, "/lib", "a.mp3")
    assert flagged is not None
    assert flagged.is_missing is True

    store.clear_missing(db_conn, file_id, _LATER)
    cleared = store.get_file(db_conn, "/lib", "a.mp3")
    assert cleared is not None
    assert cleared.is_missing is False


def test_replace_tags_preserves_order_and_replaces(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn, folder="/lib", filename="a.mp3")

    store.replace_tags(
        db_conn,
        file_id,
        {"genre": ["Synthwave", "Darksynth"], "artist": ["A"]},
        _NOW,
    )
    first = store.get_tags(db_conn, file_id)
    # Multi-value preserved and ordered by ordinal.
    assert first["genre"] == ["Synthwave", "Darksynth"]
    assert first["artist"] == ["A"]

    # tags_updated_at gets stamped on replace.
    row = store.get_file(db_conn, "/lib", "a.mp3")
    assert row is not None
    assert row.tags_updated_at == _NOW

    # A second replace fully overwrites rather than appending.
    store.replace_tags(db_conn, file_id, {"genre": ["Ambient"]}, _LATER)
    second = store.get_tags(db_conn, file_id)
    assert second == {"genre": ["Ambient"]}


def test_replace_tags_empty_clears(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn, folder="/lib", filename="a.mp3")
    store.replace_tags(db_conn, file_id, {"genre": ["X"]}, _NOW)

    store.replace_tags(db_conn, file_id, {}, _LATER)
    assert store.get_tags(db_conn, file_id) == {}


def test_tracked_files_under_excludes_prefix_sibling(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    root = tmp_path / "lib" / "music"
    sub = root / "sub"
    sibling = tmp_path / "lib" / "music_extra"  # shares the string prefix, NOT a child
    unrelated = tmp_path / "other"
    for folder in (root, sub, sibling, unrelated):
        folder.mkdir(parents=True)

    root_id = _insert(db_conn, folder=str(root), filename="root.mp3")
    sub_id = _insert(db_conn, folder=str(sub), filename="sub.mp3")
    _insert(db_conn, folder=str(sibling), filename="sibling.mp3")
    _insert(db_conn, folder=str(unrelated), filename="other.mp3")

    found = {row.id for row in store.tracked_files_under(db_conn, root)}

    # Only the real descendants of root are returned; the prefix sibling is excluded
    # (guards is_relative_to vs a naive LIKE 'root%' prefix match).
    assert found == {root_id, sub_id}


def test_compute_stats_full_shape(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    present = _insert(db_conn, folder=str(tmp_path), filename="present.mp3", ext=".mp3")
    store.replace_tags(db_conn, present, {"genre": ["A", "B"]}, _NOW)  # 2 tag values

    missing = _insert(db_conn, folder=str(tmp_path), filename="missing.flac", ext=".flac")
    store.replace_tags(db_conn, missing, {"genre": ["C"]}, _NOW)
    store.flag_missing(db_conn, missing, _NOW)

    # unprocessed: present on disk but tags never read (tags_updated_at IS NULL).
    _insert(db_conn, folder=str(tmp_path), filename="raw.flac", ext=".flac")

    stats = store.compute_stats(db_conn)

    assert stats["total_files"] == 3
    assert stats["missing"] == 1
    assert stats["present"] == 2
    assert stats["unprocessed"] == 1
    assert stats["total_tag_values"] == 3
    assert stats["by_ext"] == {".flac": 2, ".mp3": 1}


def test_delete_file_cascades_to_tags(db_conn: sqlite3.Connection) -> None:
    file_id = _insert(db_conn, folder="/lib", filename="a.mp3")
    store.replace_tags(db_conn, file_id, {"genre": ["X", "Y"]}, _NOW)
    assert store.get_tags(db_conn, file_id) == {"genre": ["X", "Y"]}

    db_conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    # ON DELETE CASCADE removes the file_tags rows (db_conn sets foreign_keys=ON).
    remaining = db_conn.execute(
        "SELECT COUNT(*) FROM file_tags WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    assert remaining[0] == 0
