"""Integration tests for the versioning write/revert path.

These use real temp audio files (the silent templates) and a real temp ledger via the
``engine_settings`` fixture, so they prove the full loop: an edit rewrites a managed
tag on disk and logs a revision, and ``revert`` restores the bytes, refreshes the live
``file_tags`` snapshot, and appends an audited revert revision — across all four formats.

The ``_edit`` helper simulates what the future M3 commit path will do (disk write +
live-snapshot refresh + revision append); the versioning engine itself only owns the
log and the revert.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from conftest import make_track
from tagmend.engine import commits, store, versioning
from tagmend.engine.db import connect
from tagmend.engine.library import scan_library
from tagmend.engine.schema import apply_schema
from tagmend.engine.tags import read_tags, write_managed_tags

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

_FORMATS = [".mp3", ".flac", ".m4a", ".ogg"]
_NOW = "2026-06-02T00:00:00+00:00"
_LATER = "2026-06-02T01:00:00+00:00"


def _file_id(settings: Settings, folder: Path, filename: str) -> int:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        return row.id
    finally:
        conn.close()


def _baseline(settings: Settings, file_id: int, managed_tags: dict[str, list[str]]) -> None:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        versioning.ensure_baseline(conn, file_id, managed_tags=managed_tags, now=_NOW)
        conn.commit()
    finally:
        conn.close()


def _edit(
    settings: Settings,
    path: Path,
    file_id: int,
    managed_tags: dict[str, list[str]],
) -> int | None:
    """Simulate a managed-tag edit: write disk, refresh live snapshot, log a revision."""
    write_managed_tags(path, managed_tags)
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        store.replace_tags(conn, file_id, read_tags(path).tags, _LATER)
        version = versioning.append_revision(
            conn,
            file_id,
            managed_tags=managed_tags,
            origin="manual",
            now=_LATER,
        )
        conn.commit()
    finally:
        conn.close()
    return version


def _live_tags(settings: Settings, file_id: int) -> dict[str, list[str]]:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return store.get_tags(conn, file_id)
    finally:
        conn.close()


def _revisions(settings: Settings, file_id: int) -> list[store.Revision]:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return versioning.history(conn, file_id)
    finally:
        conn.close()


def _commit(settings: Settings, commit_id: int) -> commits.Commit | None:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return commits.get_commit(conn, commit_id)
    finally:
        conn.close()


@pytest.mark.parametrize("suffix", _FORMATS)
def test_revert_restores_file_and_live_snapshot(
    engine_settings: Settings,
    music_dir: Path,
    suffix: str,
) -> None:
    track = make_track(
        music_dir / f"track{suffix}",
        {"genre": ["Electronic"], "title": ["Song"]},
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    _baseline(engine_settings, file_id, {"genre": ["Electronic"]})
    assert _edit(engine_settings, track, file_id, {"genre": ["Synthwave"]}) == 1
    # The edit really changed the bytes on disk.
    assert read_tags(track).tags["genre"] == ["Synthwave"]

    result = versioning.revert(engine_settings, file_id, 0)
    assert result.new_version == 2

    reverted = read_tags(track).tags
    assert reverted["genre"] == ["Electronic"]
    assert reverted.get("title") == ["Song"]  # unmanaged tag left untouched

    assert _live_tags(engine_settings, file_id)["genre"] == ["Electronic"]

    revisions = _revisions(engine_settings, file_id)
    assert [r.version for r in revisions] == [0, 1, 2]
    assert revisions[-1].origin == "revert"
    assert revisions[-1].reverted_from == 0
    assert revisions[-1].managed_tags == {"genre": ["Electronic"]}

    # The revert is recorded under its own origin='revert' commit (not NULL).
    assert revisions[-1].commit_id == result.commit_id
    commit = _commit(engine_settings, result.commit_id)
    assert commit is not None
    assert commit.origin == "revert"
    assert commit.status == "applied"


def test_revert_a_revert_rolls_back_and_forward(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # The lifecycle the design centers on: revert never destroys history, and "rolling
    # forward" is just reverting to the version that holds the wanted state.
    track = make_track(music_dir / "t.flac", {"genre": ["Electronic"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    _baseline(engine_settings, file_id, {"genre": ["Electronic"]})
    _edit(engine_settings, track, file_id, {"genre": ["Synthwave"]})  # v1

    back = versioning.revert(engine_settings, file_id, 0)  # back to Electronic
    assert back.new_version == 2
    assert read_tags(track).tags["genre"] == ["Electronic"]

    forward = versioning.revert(engine_settings, file_id, 1)  # forward to Synthwave
    assert forward.new_version == 3
    assert read_tags(track).tags["genre"] == ["Synthwave"]

    revisions = _revisions(engine_settings, file_id)
    assert [r.version for r in revisions] == [0, 1, 2, 3]  # append-only; nothing lost
    assert revisions[-1].reverted_from == 1
    assert revisions[-1].managed_tags == {"genre": ["Synthwave"]}


def test_revert_with_no_change_still_appends(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Electronic"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    _baseline(engine_settings, file_id, {"genre": ["Electronic"]})

    # Reverting to v0 while already at v0 content still records an audited revert row.
    assert versioning.revert(engine_settings, file_id, 0).new_version == 1
    revisions = _revisions(engine_settings, file_id)
    assert revisions[-1].origin == "revert"
    assert revisions[-1].diff == {}


def test_revert_unknown_target_raises(engine_settings: Settings, music_dir: Path) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    _baseline(engine_settings, file_id, {"genre": ["Electronic"]})

    with pytest.raises(ValueError, match="no revision 5"):
        versioning.revert(engine_settings, file_id, 5)


def test_revert_missing_file_raises(engine_settings: Settings, music_dir: Path) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    _baseline(engine_settings, file_id, {"genre": ["Electronic"]})

    track.unlink()
    scan_library(engine_settings)  # flags the file missing

    with pytest.raises(ValueError, match="missing file"):
        versioning.revert(engine_settings, file_id, 0)


def test_revert_with_staged_change_raises(engine_settings: Settings, music_dir: Path) -> None:
    """A pending staged change blocks a single-file revert (commit or unstage first)."""
    from tagmend.engine import staging  # noqa: PLC0415 - local import keeps module imports lean

    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    _baseline(engine_settings, file_id, {"genre": ["Electronic"]})

    staging.stage_tags(engine_settings, file_id=file_id, managed_tags={"genre": ["Synthwave"]})

    with pytest.raises(ValueError, match="staged change"):
        versioning.revert(engine_settings, file_id, 0)


def test_write_managed_tags_preserves_unmanaged_and_deletes_omitted(tmp_path: Path) -> None:
    track = make_track(
        tmp_path / "t.flac",
        {"genre": ["Electronic"], "artist": ["A"], "title": ["Song"]},
    )

    write_managed_tags(track, {"genre": ["Synthwave"]})

    tags = read_tags(track).tags
    assert tags["genre"] == ["Synthwave"]
    assert "artist" not in tags  # managed but omitted -> deleted
    assert tags.get("title") == ["Song"]  # unmanaged -> preserved


def test_write_managed_tags_leaves_no_temp_file(tmp_path: Path) -> None:
    track = make_track(tmp_path / "t.mp3", {"genre": ["Electronic"]})

    write_managed_tags(track, {"genre": ["Synthwave"]})

    assert not (tmp_path / "t.mp3.tagmend.tmp").exists()
    assert list(tmp_path.iterdir()) == [track]


def test_write_managed_tags_rejects_unmanaged_key(tmp_path: Path) -> None:
    track = make_track(tmp_path / "t.mp3", {"genre": ["Electronic"]})

    with pytest.raises(ValueError, match="non-managed"):
        write_managed_tags(track, {"title": ["Nope"]})
