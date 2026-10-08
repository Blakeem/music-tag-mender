"""Tests for the picture staging area: ``stage_pictures``, ``unstage_pictures`` and
``diff_pictures`` (:mod:`tagmend.engine.pictures`), on generated tracks with embedded pictures."""

from __future__ import annotations

import dataclasses
import hashlib
from typing import TYPE_CHECKING

import pytest
from mutagen.id3 import ID3, RVAD  # type: ignore[attr-defined]

from conftest import PictureImage, embed_pictures, make_track
from tagmend import config, mcp_server
from tagmend.engine import db, genres, pictures, schema, store, versioning
from tagmend.engine.library import scan_library
from tagmend.engine.tags import PictureData, read_picture_data

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


def _staged_rows(settings: Settings) -> list[tuple[object, ...]]:
    """Return every staged row as ``(file_id, sha256, ordinal, note, attributes, content)``."""
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        cursor = connection.execute(
            "SELECT file_id, sha256, ordinal, note, attributes, content "
            "FROM picture_writes_staged ORDER BY file_id, sha256"
        )
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _file_id(settings: Settings, track: Path) -> int:
    connection = db.connect(settings.db_path)
    try:
        row = store.get_file(connection, str(track.parent), track.name)
    finally:
        connection.close()
    assert row is not None
    return row.id


# --- stage_pictures --------------------------------------------------------------------


def test_stage_pictures_stages_every_picture_of_the_folder_and_writes_no_file(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = music_dir / "Band" / "One"
    first = _track(folder, "01", [_FRONT, _BACK])
    second = _track(folder, "02", [_FRONT])
    _track(music_dir / "Band" / "Two", "01", [_FRONT])
    scan_library(engine_settings)
    before = (first.read_bytes(), second.read_bytes())

    result = pictures.stage_pictures(engine_settings, path="Band/One", note="wrong art")

    first_id, second_id = _file_id(engine_settings, first), _file_id(engine_settings, second)
    assert result.dry_run is False
    assert [(row.file_id, row.sha256, row.mime, row.size_bytes) for row in result.staged] == [
        (first_id, _sha(_FRONT), "image/jpeg", len(_FRONT[3])),
        (first_id, _sha(_BACK), "image/png", len(_BACK[3])),
        (second_id, _sha(_FRONT), "image/jpeg", len(_FRONT[3])),
    ]
    assert result.staged[0].path == str(first)
    assert result.skipped == []
    assert result.summary == "Staged the removal of 3 picture(s) from 2 file(s). Skipped 0 file(s)."
    rows = _staged_rows(engine_settings)
    assert len(rows) == 3
    back = next(row for row in rows if row[1] == _sha(_BACK))
    assert (back[0], back[2], back[3]) == (first_id, 1, "wrong art")
    assert isinstance(back[5], bytes)
    assert PictureData.from_json(str(back[4]), back[5]) == read_picture_data(first)[1]
    assert (first.read_bytes(), second.read_bytes()) == before


def test_stage_pictures_stages_one_digest_across_two_folders(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    one = _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK])
    two = _track(music_dir / "Other" / "Two", "01", [_OTHER, _FRONT])
    _track(music_dir / "Other" / "Three", "01", [_OTHER])
    scan_library(engine_settings)

    result = pictures.stage_pictures(engine_settings, path=music_dir, sha256=_sha(_FRONT).upper())

    assert [(row.path, row.sha256) for row in result.staged] == [
        (str(one), _sha(_FRONT)),
        (str(two), _sha(_FRONT)),
    ]
    assert [(row[1], row[2]) for row in _staged_rows(engine_settings)] == [
        (_sha(_FRONT), 0),
        (_sha(_FRONT), 1),
    ]


def test_two_copies_of_one_digest_in_a_file_make_one_row(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK, _FRONT])
    scan_library(engine_settings)

    result = pictures.stage_pictures(engine_settings, path="Band", sha256=_sha(_FRONT))

    assert len(result.staged) == 1
    assert [row[2] for row in _staged_rows(engine_settings)] == [0]


def test_a_dry_run_reads_the_same_and_writes_nothing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK])
    scan_library(engine_settings)

    preview = pictures.stage_pictures(engine_settings, path="Band", dry_run=True)

    assert preview.dry_run is True
    assert len(preview.staged) == 2
    assert preview.summary.startswith("Would stage the removal of 2 picture(s) from 1 file(s).")
    assert _staged_rows(engine_settings) == []
    assert pictures.diff_pictures(engine_settings) == []


@pytest.mark.parametrize("sha256", ["abc", "g" * 64, "a" * 65, "a" * 63, ""])
def test_a_bad_sha256_is_refused(engine_settings: Settings, sha256: str) -> None:
    with pytest.raises(ValueError, match="sha256 must be 64 hex characters"):
        pictures.stage_pictures(engine_settings, path="Band", sha256=sha256)
    with pytest.raises(ValueError, match="sha256 must be 64 hex characters"):
        pictures.unstage_pictures(engine_settings, sha256=sha256)


def test_staging_again_replaces_the_row_of_the_same_file_and_digest(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK])
    scan_library(engine_settings)

    pictures.stage_pictures(engine_settings, path="Band", note="first")
    pictures.stage_pictures(engine_settings, path="Band", sha256=_sha(_FRONT), note="second")

    assert [(row[1], row[3]) for row in _staged_rows(engine_settings)] == sorted(
        [(_sha(_FRONT), "second"), (_sha(_BACK), "first")]
    )


def test_a_file_the_picture_writer_refuses_is_skipped_as_unwritable(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    loud = make_track(music_dir / "Band" / "One" / "01.mp3", {"title": ["Loud"]})
    embed_pictures(loud, [_FRONT])
    frames = ID3(loud)  # type: ignore[no-untyped-call]
    frames.add(RVAD(adjustments=[1, 1], peaks=[1, 1]))  # type: ignore[no-untyped-call]
    frames.save(loud, v2_version=3)
    plain = _track(music_dir / "Band" / "One", "02", [_FRONT])
    scan_library(engine_settings)

    result = pictures.stage_pictures(engine_settings, path="Band")
    droppable = dataclasses.replace(engine_settings, id3_droppable_frames=("RVAD",))
    again = pictures.stage_pictures(droppable, path="Band")

    [skipped] = result.skipped
    assert (skipped.path, skipped.reason) == (str(loud), pictures.SKIP_UNWRITABLE)
    assert skipped.detail is not None
    assert "dropped RVAD" in skipped.detail
    assert [row.path for row in result.staged] == [str(plain)]
    assert result.summary.endswith("Skipped 1 file(s): 1 unwritable.")
    assert again.skipped == []
    assert len(again.staged) == 2


def test_a_file_that_cannot_be_read_is_skipped_as_unreadable(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    broken = _track(music_dir / "Band" / "One", "01", [_FRONT])
    scan_library(engine_settings)
    broken.write_bytes(b"not audio at all")

    result = pictures.stage_pictures(engine_settings, path="Band")

    [skipped] = result.skipped
    assert (skipped.path, skipped.reason) == (str(broken), pictures.SKIP_UNREADABLE)
    assert result.staged == []


def test_a_file_holding_no_selected_picture_is_neither_staged_nor_skipped(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _track(music_dir / "Band" / "One", "01", [_BACK])
    make_track(music_dir / "Band" / "One" / "02.flac", {"title": ["Bare"]})
    scan_library(engine_settings)

    result = pictures.stage_pictures(engine_settings, path="Band", sha256=_sha(_FRONT))

    assert (result.staged, result.skipped) == ([], [])


# --- diff_pictures and unstage_pictures ------------------------------------------------


def test_diff_pictures_states_ready_landed_and_missing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    ready = _track(music_dir / "Band" / "One", "01", [_FRONT])
    landed = _track(music_dir / "Band" / "Two", "01", [_FRONT, _BACK])
    gone = _track(music_dir / "Band" / "Three", "01", [_FRONT])
    scan_library(engine_settings)
    pictures.stage_pictures(engine_settings, path="Band", sha256=_sha(_FRONT), note="n")
    embed_pictures(landed, [_BACK])
    gone.unlink()

    views = pictures.diff_pictures(engine_settings)
    scan_library(engine_settings)
    flagged = pictures.diff_pictures(engine_settings, path="Band/Three")
    capped = pictures.diff_pictures(engine_settings, limit=1)

    assert {view.path: view.state for view in views} == {
        str(ready): pictures.STATE_READY,
        str(landed): pictures.STATE_LANDED,
        str(gone): pictures.STATE_MISSING,
    }
    assert [view.file_id for view in views] == sorted(view.file_id for view in views)
    assert (views[0].sha256, views[0].origin, views[0].note) == (_sha(_FRONT), "manual", "n")
    assert [view.state for view in flagged] == [pictures.STATE_MISSING]
    assert [view.path for view in capped] == [str(ready)]


def test_unstage_pictures_by_path_and_by_digest(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    one = _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK])
    _track(music_dir / "Band" / "Two", "01", [_FRONT, _BACK])
    scan_library(engine_settings)
    pictures.stage_pictures(engine_settings, path="Band")

    by_digest = pictures.unstage_pictures(engine_settings, path="Band/One", sha256=_sha(_BACK))
    by_path = pictures.unstage_pictures(engine_settings, path="Band/Two")
    left = [(view.path, view.sha256) for view in pictures.diff_pictures(engine_settings)]
    everything = pictures.unstage_pictures(engine_settings)

    assert (by_digest, by_path, everything) == (1, 2, 1)
    assert left == [(str(one), _sha(_FRONT))]
    assert _staged_rows(engine_settings) == []


def test_unstage_pictures_refuses_a_landed_removal_and_drops_nothing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    landed = _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK])
    _track(music_dir / "Band" / "Two", "01", [_FRONT])
    scan_library(engine_settings)
    pictures.stage_pictures(engine_settings, path="Band", sha256=_sha(_FRONT))
    embed_pictures(landed, [_BACK])

    with pytest.raises(ValueError, match="Run commit_pictures") as refused:
        pictures.unstage_pictures(engine_settings)
    removed = pictures.unstage_pictures(engine_settings, path="Band/Two")

    assert str(landed) in str(refused.value)
    assert removed == 1
    assert [row[1] for row in _staged_rows(engine_settings)] == [_sha(_FRONT)]


def test_a_staged_picture_blocks_the_resolvers_and_revert_commit(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _track(music_dir / "Band" / "One", "01", [_FRONT])
    scan_library(engine_settings)
    pictures.stage_pictures(engine_settings, path="Band")
    connection = db.connect(engine_settings.db_path)
    try:
        connection.execute(
            "INSERT INTO commits (created_at, origin, status) "
            "VALUES ('2026-10-01T00:00:00+00:00', 'manual', 'applied')"
        )
        connection.commit()
        assert store.any_staged(connection) is True
    finally:
        connection.close()

    with pytest.raises(ValueError, match="commit or unstage pending changes first"):
        genres.resolve_genres(engine_settings)
    with pytest.raises(ValueError, match="staging area is not empty"):
        versioning.revert_commit(engine_settings, 1)


def test_the_mcp_tools_stage_diff_and_unstage(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    _track(music_dir / "Band" / "One", "01", [_FRONT, _BACK])
    mcp_server.scan_library()

    preview = mcp_server.stage_pictures(path="Band", dry_run=True)
    staged = mcp_server.stage_pictures(path="Band/One", sha256=_sha(_FRONT))
    diff = mcp_server.diff_pictures()
    removed = mcp_server.unstage_pictures(path="Band", sha256=_sha(_FRONT))
    refused = mcp_server.stage_pictures(path="Band", sha256="nope")

    assert preview["ok"] is True
    assert preview["dry_run"] is True
    assert staged["ok"] is True
    assert isinstance(staged["staged"], list)
    assert len(staged["staged"]) == 1
    changes = diff["changes"]
    assert isinstance(changes, list)
    assert changes[0]["state"] == pictures.STATE_READY
    assert "content" not in changes[0]
    assert removed == {"ok": True, "removed": 1}
    assert refused["ok"] is False
