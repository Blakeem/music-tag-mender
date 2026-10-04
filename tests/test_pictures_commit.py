"""Tests for ``commit_pictures`` and the revert of a picture commit
(:mod:`tagmend.engine.pictures`), on generated tracks with embedded pictures."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from conftest import PictureImage, embed_pictures, make_track
from tagmend import config, mcp_server
from tagmend.engine import commits, db, pictures, schema, staging, store, tags, versioning
from tagmend.engine.library import scan_library
from tagmend.engine.tags import PictureData, read_picture_data, read_pictures, read_tags

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

_FRONT: PictureImage = (3, "image/jpeg", "front", b"\xff\xd8\xff\xe0 the front")
_BACK: PictureImage = (4, "image/png", "back", b"\x89PNG\r\n\x1a\n the back")
_OTHER: PictureImage = (3, "image/jpeg", "", b"\xff\xd8\xff\xe0 another front")


def _sha(image: PictureImage) -> str:
    return hashlib.sha256(image[3]).hexdigest()


def _track(folder: Path, name: str, images: list[PictureImage]) -> Path:
    track = make_track(folder / f"{name}.flac", {"title": [name], "album": [folder.name]})
    embed_pictures(track, images)
    return track


def _file_id(settings: Settings, track: Path) -> int:
    connection = db.connect(settings.db_path)
    try:
        row = store.get_file(connection, str(track.parent), track.name)
    finally:
        connection.close()
    assert row is not None
    return row.id


def _log_rows(settings: Settings) -> list[tuple[object, ...]]:
    """Return every ``picture_writes`` row as ``(commit_id, action, file_id, version, sha256,
    ordinal, origin, reverted_from, attributes, content)``."""
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        cursor = connection.execute(
            "SELECT commit_id, action, file_id, version, sha256, ordinal, origin, reverted_from, "
            "attributes, content FROM picture_writes ORDER BY id"
        )
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _logged_picture(log: tuple[object, ...]) -> PictureData:
    attributes, content = log[8], log[9]
    assert isinstance(attributes, str)
    assert isinstance(content, bytes)
    return PictureData.from_json(attributes, content)


def _staged_digests(settings: Settings) -> list[str]:
    connection = db.connect(settings.db_path)
    try:
        return [row.sha256 for row in store.list_staged_pictures(connection)]
    finally:
        connection.close()


def _commit_status(settings: Settings, commit_id: int) -> str:
    commit = commits.get_commit(settings, commit_id)
    assert commit is not None
    return commit.status


def _assert_snapshot_matches(settings: Settings, track: Path) -> None:
    connection = db.connect(settings.db_path)
    try:
        row = store.get_file(connection, str(track.parent), track.name)
        assert row is not None
        stat = track.stat()
        assert (row.size_bytes, row.mtime_ns) == (stat.st_size, stat.st_mtime_ns)
        assert store.get_pictures(connection, row.id) == store.picture_rows(read_pictures(track))
        assert store.get_tags(connection, row.id) == read_tags(track).tags
    finally:
        connection.close()


def _stage_and_commit(settings: Settings, path: str, image: PictureImage) -> commits.CommitResult:
    pictures.stage_pictures(settings, path=path, sha256=_sha(image))
    return pictures.commit_pictures(settings)


# --- commit_pictures -------------------------------------------------------------------


def test_commit_pictures_removes_the_picture_and_logs_it_with_its_bytes(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK, _OTHER])
    scan_library(engine_settings)
    before = read_picture_data(track)
    pictures.stage_pictures(engine_settings, path="Band", sha256=_sha(_BACK), note="wrong art")

    result = pictures.commit_pictures(engine_settings)

    file_id = _file_id(engine_settings, track)
    assert result.commit_id is not None
    assert (result.committed, result.errors, result.missing) == (1, 0, 0)
    assert result.outcomes[0].version == 1
    assert read_picture_data(track) == [before[0], before[2]]
    assert read_tags(track).tags["title"] == ["01"]
    assert read_tags(track).tags["album"] == ["One"]
    [log] = _log_rows(engine_settings)
    assert log[:8] == (result.commit_id, "remove", file_id, 1, _sha(_BACK), 1, "manual", None)
    assert _logged_picture(log) == before[1]
    assert _staged_digests(engine_settings) == []
    assert _commit_status(engine_settings, result.commit_id) == "applied"
    assert versioning.commit_logs(engine_settings, result.commit_id) == {"picture_writes": 1}
    commit = commits.get_commit(engine_settings, result.commit_id)
    assert commit is not None
    assert (commit.origin, commit.message) == (
        "manual",
        "remove 1 staged picture(s) from 1 file(s)",
    )
    _assert_snapshot_matches(engine_settings, track)


def test_two_copies_of_one_digest_log_two_rows(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK, _FRONT])
    scan_library(engine_settings)

    result = _stage_and_commit(engine_settings, "Band", _FRONT)

    assert result.outcomes[0].version == 2
    assert [picture.data for picture in read_picture_data(track)] == [_BACK[3]]
    assert [(row[1], row[3], row[4], row[5]) for row in _log_rows(engine_settings)] == [
        ("remove", 1, _sha(_FRONT), 0),
        ("remove", 2, _sha(_FRONT), 2),
    ]
    _assert_snapshot_matches(engine_settings, track)


def test_a_staged_digest_already_gone_is_logged_as_landed_with_no_write(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK])
    scan_library(engine_settings)
    pictures.stage_pictures(engine_settings, path="Band", sha256=_sha(_FRONT))
    staged = read_picture_data(track)[0]
    embed_pictures(track, [_BACK])
    landed_bytes = track.read_bytes()

    result = pictures.commit_pictures(engine_settings)

    assert result.committed == 1
    assert track.read_bytes() == landed_bytes
    [log] = _log_rows(engine_settings)
    assert (log[1], log[4], log[5]) == ("remove", _sha(_FRONT), 0)
    assert _logged_picture(log) == staged
    assert _staged_digests(engine_settings) == []
    _assert_snapshot_matches(engine_settings, track)


def test_a_staged_tag_change_on_the_file_refuses_the_commit(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK])
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, track)
    pictures.stage_pictures(engine_settings, path="Band", sha256=_sha(_FRONT))
    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Rock"]})
    before = track.read_bytes()

    with pytest.raises(ValueError, match="Run commit_tags or unstage_tags first") as refused:
        pictures.commit_pictures(engine_settings)
    refused_left = (commits.list_commits(engine_settings), track.read_bytes())
    staging.commit_tags(engine_settings)
    after_tags = pictures.commit_pictures(engine_settings)

    assert f"File(s) {file_id} also have a staged tag change" in str(refused.value)
    assert refused_left == ([], before)
    assert after_tags.committed == 1
    assert [picture.data for picture in read_picture_data(track)] == [_BACK[3]]
    assert read_tags(track).tags["genre"] == ["Rock"]
    _assert_snapshot_matches(engine_settings, track)


def test_a_crash_after_the_write_leaves_the_row_and_the_next_commit_logs_it_as_landed(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK])
    scan_library(engine_settings)
    pictures.stage_pictures(engine_settings, path="Band", sha256=_sha(_FRONT))

    def write_then_crash(*args: object, **kwargs: object) -> tags.TagWriteResult:
        tags.write_pictures(*args, **kwargs)  # type: ignore[arg-type]
        message = "crash after the write"
        raise RuntimeError(message)

    monkeypatch.setattr(pictures, "write_pictures", write_then_crash)
    with pytest.raises(RuntimeError, match="crash after the write"):
        pictures.commit_pictures(engine_settings)
    monkeypatch.undo()
    crashed = commits.list_commits(engine_settings)[0]
    left = (_staged_digests(engine_settings), _log_rows(engine_settings))

    result = pictures.commit_pictures(engine_settings)

    assert left == ([_sha(_FRONT)], [])
    assert [picture.data for picture in read_picture_data(track)] == [_BACK[3]]
    assert _commit_status(engine_settings, crashed.id) == "interrupted"
    assert result.committed == 1
    assert [(row[0], row[1], row[4]) for row in _log_rows(engine_settings)] == [
        (result.commit_id, "remove", _sha(_FRONT)),
    ]
    assert _staged_digests(engine_settings) == []
    _assert_snapshot_matches(engine_settings, track)


def test_a_file_gone_from_disk_is_flagged_missing_and_its_rows_dropped(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = _track(music_dir / "Band" / "One", "01", [_FRONT])
    scan_library(engine_settings)
    pictures.stage_pictures(engine_settings, path="Band")
    track.unlink()

    result = pictures.commit_pictures(engine_settings)

    assert (result.committed, result.missing) == (0, 1)
    assert result.missing_files[0].path == str(track)
    assert _staged_digests(engine_settings) == []
    assert _log_rows(engine_settings) == []


def test_nothing_staged_creates_no_commit(engine_settings: Settings) -> None:
    result = pictures.commit_pictures(engine_settings)

    assert result.commit_id is None
    assert commits.list_commits(engine_settings) == []


# --- revert_commit of a picture commit -------------------------------------------------


def test_revert_commit_restores_the_picture_and_a_revert_of_the_revert_removes_it(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK, _OTHER])
    scan_library(engine_settings)
    before = read_picture_data(track)
    committed = _stage_and_commit(engine_settings, "Band", _BACK)
    assert committed.commit_id is not None

    preview = versioning.revert_commit(engine_settings, committed.commit_id, dry_run=True)
    reverted = versioning.revert_commit(engine_settings, committed.commit_id, note="undo")

    file_id = _file_id(engine_settings, track)
    assert (preview.commit_id, preview.reverted, preview.outcomes[0].status) == (
        None,
        1,
        "reverted",
    )
    assert reverted.commit_id is not None
    assert reverted.reverted == 1
    assert reverted.outcomes[0] == commits.FileRevertOutcome(
        file_id=file_id, target_version=0, new_version=2, status="reverted"
    )
    assert read_picture_data(track) == before
    first, restore = _log_rows(engine_settings)
    assert restore[:8] == (reverted.commit_id, "restore", file_id, 2, _sha(_BACK), 1, "revert", 1)
    assert (restore[8], restore[9]) == (first[8], first[9])
    revert_commit = commits.get_commit(engine_settings, reverted.commit_id)
    assert revert_commit is not None
    assert (revert_commit.reverted_from, revert_commit.status) == (committed.commit_id, "applied")
    _assert_snapshot_matches(engine_settings, track)

    again = versioning.revert_commit(engine_settings, reverted.commit_id)

    assert again.reverted == 1
    assert read_picture_data(track) == [before[0], before[2]]
    assert [row[1] for row in _log_rows(engine_settings)] == ["remove", "restore", "remove"]
    _assert_snapshot_matches(engine_settings, track)


def test_revert_commit_writes_nothing_for_a_picture_already_back(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK])
    scan_library(engine_settings)
    committed = _stage_and_commit(engine_settings, "Band", _BACK)
    assert committed.commit_id is not None
    embed_pictures(track, [_FRONT, _BACK])
    back_on_disk = track.read_bytes()

    result = versioning.revert_commit(engine_settings, committed.commit_id)

    assert (result.reverted, result.noop) == (0, 1)
    assert result.commit_id is not None
    assert track.read_bytes() == back_on_disk
    assert [row[1] for row in _log_rows(engine_settings)] == ["remove", "restore"]
    _assert_snapshot_matches(engine_settings, track)


def test_revert_commit_skips_a_file_a_later_picture_commit_changed(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK])
    scan_library(engine_settings)
    first = _stage_and_commit(engine_settings, "Band", _FRONT)
    _stage_and_commit(engine_settings, "Band", _BACK)
    assert first.commit_id is not None

    result = versioning.revert_commit(engine_settings, first.commit_id)

    assert result.commit_id is None
    assert (result.skipped, result.outcomes[0].status) == (1, "skipped_later_changes")
    assert read_picture_data(track) == []


def test_revert_commit_reports_a_missing_file(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = _track(music_dir / "Band" / "One", "01", [_FRONT])
    scan_library(engine_settings)
    committed = _stage_and_commit(engine_settings, "Band", _FRONT)
    assert committed.commit_id is not None
    track.unlink()

    result = versioning.revert_commit(engine_settings, committed.commit_id)

    assert result.commit_id is None
    assert (result.missing, result.outcomes[0].status) == (1, "missing")


def test_revert_commit_refuses_a_path_on_a_picture_commit(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _track(music_dir / "Band" / "One", "01", [_FRONT])
    scan_library(engine_settings)
    committed = _stage_and_commit(engine_settings, "Band", _FRONT)
    assert committed.commit_id is not None

    with pytest.raises(ValueError, match="changed embedded pictures, and path= selects"):
        versioning.revert_commit(engine_settings, committed.commit_id, path="Band")


def test_the_mcp_tools_commit_and_name_the_picture_log(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK])
    mcp_server.scan_library()
    mcp_server.stage_pictures(path="Band", sha256=_sha(_FRONT))

    committed = mcp_server.commit_pictures()
    commit_id = committed["commit_id"]
    assert isinstance(commit_id, int)
    logged = mcp_server.get_commit(commit_id)
    reverted = mcp_server.revert_commit(commit_id)
    empty = mcp_server.commit_pictures()

    assert (committed["ok"], committed["committed"]) == (True, 1)
    assert logged["logs"] == {"picture_writes": 1}
    assert (reverted["ok"], reverted["reverted"]) == (True, 1)
    assert (empty["ok"], empty["commit_id"]) == (True, None)
