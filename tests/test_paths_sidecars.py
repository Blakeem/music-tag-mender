"""Tests for the sidecars of the paths domain (:mod:`tagmend.engine.paths`).

Generated album folders in ``tmp_path`` holding cover art, a hidden and system-attributed
thumbnail (set through ``ctypes`` on Windows), ``Thumbs.db``, a cue sheet, a rip log and a
``Scans`` folder. Real commits, reverts and a crash simulated by an exception that escapes the
commit after a sidecar's disk move and before its log row commits.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from conftest import make_track
from tagmend import config, mcp_server
from tagmend.engine import commits, mismatch, path_keys, paths, staging, store, versioning
from tagmend.engine.db import connect
from tagmend.engine.library import scan_library
from tagmend.engine.schema import apply_schema

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tagmend.config import Settings

_ALBUM = Path("Artist") / "Album"
_NEW = Path("Artist") / "New"
_HIDDEN = (Path("AlbumArtSmall.jpg"), Path("Thumbs.db"))
_SIDECARS = (
    Path("Album.cue"),
    Path("Album.log"),
    Path("AlbumArtSmall.jpg"),
    Path("Folder.jpg"),
    Path("Scans") / "Back.jpg",
    Path("Scans") / "Front.jpg",
    Path("Thumbs.db"),
)
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4


class _CrashError(BaseException):
    """A process death: escapes every handler, so the ledger rolls back on close."""


@dataclass(frozen=True)
class _Album:
    settings: Settings
    music: Path
    x: int
    y: int


@contextlib.contextmanager
def _ledger(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        yield conn
    finally:
        conn.close()


def _id_at(settings: Settings, path: Path) -> int:
    with _ledger(settings) as conn:
        row = store.get_file(conn, str(path.parent), path.name)
        assert row is not None
        return row.id


def _hide(path: Path) -> None:
    """Mark *path* Hidden and System, as Windows Media Player leaves its thumbnails."""
    if sys.platform != "win32":
        return
    import ctypes  # noqa: PLC0415 - Windows only

    flags = _FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM
    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), flags)


def _is_hidden_system(path: Path) -> bool:
    flags = _FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM
    return path.stat().st_file_attributes & flags == flags  # type: ignore[attr-defined,unused-ignore]


def _write_sidecars(folder: Path, marker: bytes = b"") -> None:
    for relative in _SIDECARS:
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(relative.name.encode() + marker)
    # The sheet names a disc image no ledger row tracks, so no cue guard holds the folder.
    (folder / "Album.cue").write_text('FILE "Album.wav" WAVE\n', encoding="utf-8")
    for relative in _HIDDEN:
        _hide(folder / relative)


def _library(settings: Settings, music: Path, *extra: Path) -> None:
    """Clean folders that keep the mismatch gate open, plus a track per *extra* path."""
    for name in ("CleanA", "CleanB", "CleanC", "CleanD"):
        make_track(music / name / "Album" / "01.mp3", {"albumartist": [name], "artist": [name]})
    for relative in extra:
        top = relative.parts[0]
        make_track(music / relative, {"albumartist": [top], "artist": [top]})
    scan_library(settings)
    assert mismatch.gate_state(settings).open


@pytest.fixture
def album(engine_settings: Settings, music_dir: Path) -> _Album:
    _library(engine_settings, music_dir, _ALBUM / "01.mp3", _ALBUM / "02.mp3")
    _write_sidecars(music_dir / _ALBUM)
    return _Album(
        settings=engine_settings,
        music=music_dir,
        x=_id_at(engine_settings, music_dir / _ALBUM / "01.mp3"),
        y=_id_at(engine_settings, music_dir / _ALBUM / "02.mp3"),
    )


def _stage(settings: Settings, *moves: tuple[int, Path]) -> list[int]:
    return paths.stage_paths_batch(
        settings, entries=[(file_id, str(target)) for file_id, target in moves]
    )


def _stage_album(album: _Album, folder: Path = _NEW) -> None:
    _stage(album.settings, (album.x, folder / "01.mp3"), (album.y, folder / "02.mp3"))


def _staged_sidecars(settings: Settings) -> list[store.StagedSidecar]:
    with _ledger(settings) as conn:
        return store.list_staged_sidecars(conn)


def _logged(settings: Settings, commit_id: int) -> list[store.SidecarMove]:
    with _ledger(settings) as conn:
        return store.sidecar_moves_for_commit(conn, commit_id)


def _at(settings: Settings, file_id: int) -> Path:
    with _ledger(settings) as conn:
        row = store.get_file_by_id(conn, file_id)
        assert row is not None
        return Path(row.folder) / row.filename


# --- staging ---------------------------------------------------------------------------


def test_a_moving_folder_carries_every_sidecar_and_is_pruned(album: _Album) -> None:
    _stage_album(album)
    staged = _staged_sidecars(album.settings)
    assert sorted(Path(row.from_path) for row in staged) == sorted(_ALBUM / r for r in _SIDECARS)
    assert {row.origin for row in staged} == {"manual"}

    result = paths.commit_paths(album.settings)

    assert (result.committed, result.sidecars_moved, result.sidecar_problems) == (2, 7, ())
    assert (result.sidecars_waiting, result.sidecars_held) == (0, ())
    for relative in _SIDECARS:
        assert (album.music / _NEW / relative).read_bytes()
    assert (album.music / _NEW / "Album.cue").read_text(encoding="utf-8").startswith("FILE")
    assert not (album.music / _ALBUM).exists()
    assert (album.music / "Artist").exists()
    assert result.folders_pruned == 2
    assert result.commit_id is not None
    logged = _logged(album.settings, result.commit_id)
    assert {(Path(m.from_path), Path(m.to_path)) for m in logged} == {
        (_ALBUM / relative, _NEW / relative) for relative in _SIDECARS
    }
    assert versioning.commit_logs(album.settings, result.commit_id) == {
        "path_revisions": 2,
        "sidecar_moves": 7,
    }
    assert _staged_sidecars(album.settings) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Hidden and System are Windows attributes")
def test_a_hidden_system_thumbnail_moves_and_keeps_its_attributes(album: _Album) -> None:
    _stage_album(album)

    paths.commit_paths(album.settings)

    for relative in _HIDDEN:
        assert _is_hidden_system(album.music / _NEW / relative)


def test_stage_paths_carries_the_sidecars_of_a_rendered_folder(
    engine_settings: Settings, music_dir: Path
) -> None:
    tags = {"albumartist": ["Artist"], "artist": ["Artist"], "album": ["Album"]}
    tags |= {"date": ["2001"], "title": ["One"], "tracknumber": ["1"]}
    make_track(music_dir / _ALBUM / "01 One.flac", tags)
    (music_dir / _ALBUM / "Folder.jpg").write_bytes(b"cover")
    scan_library(engine_settings)

    preview = paths.stage_paths(engine_settings, path="Artist", dry_run=True)
    assert (preview.staged, preview.sidecars_staged) == (1, 1)
    assert _staged_sidecars(engine_settings) == []

    staged = paths.stage_paths(engine_settings, path="Artist")

    assert (staged.sidecars_staged, staged.sidecars_held) == (1, ())
    (row,) = _staged_sidecars(engine_settings)
    assert (row.origin, Path(row.to_path).name) == ("auto", "Folder.jpg")
    paths.commit_paths(engine_settings)
    assert (music_dir / row.to_path).read_bytes() == b"cover"
    assert not (music_dir / _ALBUM).exists()


def test_a_folder_that_does_not_move_whole_stages_no_sidecar(album: _Album) -> None:
    _stage(album.settings, (album.x, _NEW / "01.mp3"))
    assert _staged_sidecars(album.settings) == []
    _stage(album.settings, (album.y, Path("Artist") / "Elsewhere" / "02.mp3"))
    assert _staged_sidecars(album.settings) == []

    result = paths.commit_paths(album.settings)

    assert (result.committed, result.sidecars_moved) == (2, 0)
    assert (album.music / _ALBUM / "Folder.jpg").exists()
    assert sorted(Path(p) for p in result.sidecars_held) == sorted(_ALBUM / r for r in _SIDECARS)


def test_a_held_member_leaves_its_sidecars_in_place(
    engine_settings: Settings, music_dir: Path
) -> None:
    tags = {"albumartist": ["Artist"], "artist": ["Artist"], "album": ["Album"]}
    tags |= {"date": ["2001"], "tracknumber": ["1"]}
    make_track(music_dir / _ALBUM / "01 One.flac", {**tags, "title": ["One"]})
    make_track(music_dir / _ALBUM / "02 Two.flac", {**tags, "tracknumber": ["2"]})
    (music_dir / _ALBUM / "Folder.jpg").write_bytes(b"cover")
    scan_library(engine_settings)

    # The pattern needs the blank title of 02, so it holds and its folder moves as no unit.
    result = paths.stage_paths(engine_settings, path="Artist")

    assert (result.staged, result.sidecars_staged) == (0, 0)
    assert result.held == {"missing_title": 1, "unit_member": 1}
    assert _staged_sidecars(engine_settings) == []


def test_unstaging_one_file_drops_its_folders_sidecar_rows(album: _Album) -> None:
    _stage_album(album)

    result = paths.unstage_paths(album.settings, file_id=album.x)

    assert (result.removed, result.sidecars_removed) == (1, 7)
    assert _staged_sidecars(album.settings) == []


def test_unstaging_the_path_of_one_sidecar_drops_that_row_alone(album: _Album) -> None:
    _stage_album(album)

    result = paths.unstage_paths(album.settings, path=str(_ALBUM / "Folder.jpg"))

    assert (result.removed, result.sidecars_removed) == (0, 1)
    left = {Path(row.from_path) for row in _staged_sidecars(album.settings)}
    assert left == {_ALBUM / r for r in _SIDECARS} - {_ALBUM / "Folder.jpg"}
    paths.commit_paths(album.settings)
    assert (album.music / _ALBUM / "Folder.jpg").exists()


# --- holds -----------------------------------------------------------------------------

_CD1 = _ALBUM / "CD1"
_CD2 = _ALBUM / "CD2"
_MERGED = Path("Artist") / "Merged"


@pytest.mark.parametrize("order", ["lower_first", "higher_first"])
def test_merging_disc_folders_keep_the_lower_file_ids_cover(
    engine_settings: Settings, music_dir: Path, order: str
) -> None:
    _library(engine_settings, music_dir, _CD1 / "01.mp3", _CD2 / "01.mp3")
    (music_dir / _CD1 / "Folder.jpg").write_bytes(b"one")
    (music_dir / _CD2 / "Folder.jpg").write_bytes(b"two")
    disc1 = _id_at(engine_settings, music_dir / _CD1 / "01.mp3")
    disc2 = _id_at(engine_settings, music_dir / _CD2 / "01.mp3")
    assert disc1 < disc2
    moves = [(disc1, _MERGED / "101.mp3"), (disc2, _MERGED / "201.mp3")]
    for move in moves if order == "lower_first" else moves[::-1]:
        _stage(engine_settings, move)

    result = paths.commit_paths(engine_settings)

    assert (result.committed, result.sidecars_moved) == (2, 1)
    assert (music_dir / _MERGED / "Folder.jpg").read_bytes() == b"one"
    assert result.sidecars_held == (str(_CD2 / "Folder.jpg"),)
    assert (music_dir / _CD2 / "Folder.jpg").read_bytes() == b"two"
    assert not (music_dir / _CD1).exists()


@pytest.mark.skipif(path_keys.path_key("A") == "A", reason="keys fold case only on Windows")
def test_a_target_taken_under_another_casing_holds_the_sidecar(album: _Album) -> None:
    (album.music / _NEW).mkdir(parents=True)
    (album.music / _NEW / "folder.JPG").write_bytes(b"theirs")

    _stage_album(album)

    staged = {Path(row.from_path) for row in _staged_sidecars(album.settings)}
    assert _ALBUM / "Folder.jpg" not in staged
    result = paths.commit_paths(album.settings)
    assert result.sidecars_held == (str(_ALBUM / "Folder.jpg"),)
    assert (album.music / _NEW / "folder.JPG").read_bytes() == b"theirs"


@pytest.mark.skipif(path_keys.path_key("A") == "A", reason="keys fold case only on Windows")
def test_a_sidecar_folder_keeps_the_destinations_on_disk_spelling(album: _Album) -> None:
    (album.music / _NEW / "scans").mkdir(parents=True)

    _stage_album(album)

    to_paths = {Path(row.to_path) for row in _staged_sidecars(album.settings)}
    assert {_NEW / "scans" / "Back.jpg", _NEW / "scans" / "Front.jpg"} <= to_paths


# --- the sidecar step --------------------------------------------------------------------


def test_a_sidecar_waits_for_its_folders_audio_and_moves_on_the_next_commit(
    album: _Album,
) -> None:
    _stage_album(album)

    first = paths.commit_paths(album.settings, path=str(_ALBUM / "Folder.jpg"))

    assert (first.committed, first.sidecars_moved, first.sidecars_waiting) == (0, 0, 1)
    assert (album.music / _ALBUM / "Folder.jpg").exists()
    assert first.commit_id is not None
    with pytest.raises(ValueError, match="staging area is not empty"):
        versioning.revert_commit(album.settings, first.commit_id)

    second = paths.commit_paths(album.settings)

    assert (second.committed, second.sidecars_moved, second.sidecars_waiting) == (2, 7, 0)
    assert (album.music / _NEW / "Folder.jpg").exists()
    assert not (album.music / _ALBUM).exists()


def test_a_sidecar_whose_target_is_taken_at_commit_keeps_its_row(album: _Album) -> None:
    _stage_album(album)
    (album.music / _NEW).mkdir(parents=True)
    (album.music / _NEW / "Folder.jpg").write_bytes(b"raced")

    result = paths.commit_paths(album.settings)

    (problem,) = result.sidecar_problems
    assert (problem.status, Path(problem.from_path)) == ("error", _ALBUM / "Folder.jpg")
    assert problem.detail is not None
    assert "unstage_paths" in problem.detail
    assert [Path(row.from_path) for row in _staged_sidecars(album.settings)] == [
        _ALBUM / "Folder.jpg"
    ]
    assert (album.music / _ALBUM / "Folder.jpg").exists()


def test_a_gone_sidecar_drops_its_row_and_logs_nothing(album: _Album) -> None:
    _stage_album(album)
    (album.music / _ALBUM / "Album.log").unlink()

    result = paths.commit_paths(album.settings)

    (problem,) = result.sidecar_problems
    assert (problem.status, Path(problem.from_path)) == ("missing", _ALBUM / "Album.log")
    assert result.commit_id is not None
    logged = {Path(m.from_path) for m in _logged(album.settings, result.commit_id)}
    assert _ALBUM / "Album.log" not in logged
    assert _staged_sidecars(album.settings) == []
    assert not (album.music / _ALBUM).exists()


def _crash_after_moving(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill the commit right after the disk move of the sidecar called *name*."""
    real = paths.move_no_clobber

    def move_then_crash(source: Path, target: Path) -> None:
        real(source, target)
        if source.name == name:
            raise _CrashError

    monkeypatch.setattr(paths, "move_no_clobber", move_then_crash)


def test_a_crash_after_a_sidecar_move_is_logged_by_the_next_commit(
    album: _Album, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_album(album)
    with monkeypatch.context() as patch:
        _crash_after_moving("Folder.jpg", patch)
        with pytest.raises(_CrashError):
            paths.commit_paths(album.settings)

    assert (album.music / _NEW / "Folder.jpg").exists()
    assert not (album.music / _ALBUM / "Folder.jpg").exists()
    staged = {Path(row.from_path) for row in _staged_sidecars(album.settings)}
    assert _ALBUM / "Folder.jpg" in staged
    with pytest.raises(ValueError, match="commit_paths"):
        paths.unstage_paths(album.settings, path=str(_ALBUM))

    result = paths.commit_paths(album.settings)

    assert result.sidecar_problems == ()
    assert result.commit_id is not None
    logged = {Path(m.from_path) for m in _logged(album.settings, result.commit_id)}
    assert _ALBUM / "Folder.jpg" in logged
    assert _staged_sidecars(album.settings) == []
    assert not (album.music / _ALBUM).exists()
    with _ledger(album.settings) as conn:
        assert [c.status for c in commits.list_commits_in(conn)] == ["applied", "interrupted"]


def test_a_sidecar_changed_at_its_landed_target_waits_for_a_confirming_stage(
    album: _Album, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_album(album)
    with monkeypatch.context() as patch:
        _crash_after_moving("Folder.jpg", patch)
        with pytest.raises(_CrashError):
            paths.commit_paths(album.settings)
    landed = album.music / _NEW / "Folder.jpg"
    landed.write_bytes(b"edited after the crash")
    stat_result = landed.stat()
    os.utime(landed, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 5_000_000_000))

    held = paths.commit_paths(album.settings)

    (problem,) = held.sidecar_problems
    assert problem.status == "changed_since_stage"
    assert problem.detail is not None
    assert "stage_paths" in problem.detail
    assert len(_staged_sidecars(album.settings)) == 1

    paths.stage_paths(album.settings, path=str(_ALBUM))
    confirmed = paths.commit_paths(album.settings)

    assert (confirmed.sidecars_moved, confirmed.sidecar_problems) == (1, ())
    assert _staged_sidecars(album.settings) == []
    assert landed.read_bytes() == b"edited after the crash"
    assert not (album.music / _ALBUM).exists()


def test_a_reverted_sidecar_changed_at_its_landed_target_is_confirmed_by_its_hint(
    album: _Album, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage_album(album)
    forward = paths.commit_paths(album.settings)
    assert forward.commit_id is not None
    with monkeypatch.context() as patch:
        _crash_after_moving("Folder.jpg", patch)
        with pytest.raises(_CrashError):
            versioning.revert_commit(album.settings, forward.commit_id)
    landed = album.music / _ALBUM / "Folder.jpg"
    landed.write_bytes(b"edited after the crash")
    stat_result = landed.stat()
    os.utime(landed, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 5_000_000_000))

    held = paths.commit_paths(album.settings)

    (problem,) = held.sidecar_problems
    assert problem.status == "changed_since_stage"
    assert problem.detail is not None
    assert f"stage_paths(path={str(_NEW)!r})" in problem.detail

    paths.stage_paths(album.settings, path=str(_NEW))
    confirmed = paths.commit_paths(album.settings)

    assert (confirmed.sidecars_moved, confirmed.sidecar_problems) == (1, ())
    assert confirmed.commit_id is not None
    logged = {Path(m.to_path) for m in _logged(album.settings, confirmed.commit_id)}
    assert logged == {_ALBUM / "Folder.jpg"}
    with _ledger(album.settings) as conn:
        assert not store.any_staged(conn)
    assert landed.read_bytes() == b"edited after the crash"


# --- revert ----------------------------------------------------------------------------


def test_revert_commit_moves_the_sidecars_back(album: _Album) -> None:
    _stage_album(album)
    forward = paths.commit_paths(album.settings)
    assert forward.commit_id is not None

    back = versioning.revert_commit(album.settings, forward.commit_id)

    assert isinstance(back, paths.PathRevertCommitResult)
    assert (back.reverted, back.errors) == (2, 0)
    assert [o.status for o in back.sidecars] == ["reverted"] * 7
    for relative in _SIDECARS:
        assert (album.music / _ALBUM / relative).exists()
    assert _at(album.settings, album.x) == album.music / _ALBUM / "01.mp3"
    assert not (album.music / _NEW).exists()
    payload = back.to_dict()
    assert payload["sidecars_reverted"] == 7
    assert back.commit_id is not None
    assert len(_logged(album.settings, back.commit_id)) == 7


def test_revert_commit_of_one_sidecar_reverts_it_alone(album: _Album) -> None:
    _stage_album(album)
    forward = paths.commit_paths(album.settings)
    assert forward.commit_id is not None

    one = versioning.revert_commit(
        album.settings, forward.commit_id, path=str(album.music / _NEW / "Folder.jpg")
    )

    assert isinstance(one, paths.PathRevertCommitResult)
    assert (one.reverted, one.outcomes) == (0, ())
    assert [(Path(o.to_path), o.status) for o in one.sidecars] == [
        (_ALBUM / "Folder.jpg", "reverted")
    ]
    assert (album.music / _ALBUM / "Folder.jpg").exists()
    assert not (album.music / _NEW / "Folder.jpg").exists()
    assert [r.version for r in paths.history_paths(album.settings, album.x)] == [0, 1]

    whole = versioning.revert_commit(album.settings, forward.commit_id)

    assert isinstance(whole, paths.PathRevertCommitResult)
    statuses = {Path(o.from_path).name: o.status for o in whole.sidecars}
    assert statuses.pop("Folder.jpg") == "skipped_later_changes"
    assert set(statuses.values()) == {"reverted"}
    assert whole.reverted == 2
    assert not (album.music / _NEW).exists()


def test_revert_commit_refuses_a_path_scope_on_a_tag_commit(album: _Album) -> None:
    staging.stage_tags(album.settings, file_id=album.x, tags={"title": ["Renamed"]})
    tagged = staging.commit_tags(album.settings)
    assert tagged.commit_id is not None

    with pytest.raises(ValueError, match="path= selects the files of a path commit"):
        versioning.revert_commit(album.settings, tagged.commit_id, path=str(_ALBUM))


# --- the MCP surface -------------------------------------------------------------------


def test_the_mcp_path_tools_report_sidecars(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    for name in ("CleanA", "CleanB", "CleanC", "CleanD"):
        make_track(music_dir / name / "Album" / "01.mp3", {"albumartist": [name], "artist": [name]})
    make_track(music_dir / _ALBUM / "01.mp3", {"albumartist": ["Artist"], "artist": ["Artist"]})
    (music_dir / _ALBUM / "Folder.jpg").write_bytes(b"cover")
    mcp_server.scan_library()
    settings = config.load_settings()
    file_id = _id_at(settings, music_dir / _ALBUM / "01.mp3")
    mcp_server.stage_paths_batch([{"file_id": file_id, "to_path": str(_NEW / "01.mp3")}])

    committed = mcp_server.commit_paths()

    assert (committed["ok"], committed["sidecars_moved"], committed["sidecars_held"]) == (
        True,
        1,
        [],
    )
    commit_id = committed["commit_id"]
    assert isinstance(commit_id, int)
    assert mcp_server.get_commit(commit_id)["logs"] == {"path_revisions": 1, "sidecar_moves": 1}
    reverted = mcp_server.revert_commit(commit_id, path=str(_NEW / "Folder.jpg"))
    assert (reverted["ok"], reverted["reverted"], reverted["sidecars_reverted"]) == (True, 0, 1)
    assert (music_dir / _ALBUM / "Folder.jpg").exists()
