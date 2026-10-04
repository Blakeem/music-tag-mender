"""Tests for the paths domain (:mod:`tagmend.engine.paths`).

Real generated audio in ``tmp_path`` and real ledgers. One test per cell of the module
docstring's state table (six disk states by six events), a crash simulated by an exception that
escapes the commit loop after the disk move and before the ledger commit, the batch holds, the
c1 commit check, the pruner, the volume check, revert by commit and by file, and the scan guard.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import mutagen
import pytest

from conftest import make_track
from tagmend import config
from tagmend.engine import (
    commits,
    health,
    mismatch,
    naming,
    path_keys,
    paths,
    staging,
    store,
    versioning,
    years,
)
from tagmend.engine.db import connect
from tagmend.engine.library import scan_library
from tagmend.engine.schema import apply_schema

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tagmend.config import Settings

_SOURCE = Path("Artist") / "Album" / "01.mp3"
_TARGET = Path("Artist") / "Moved" / "01.mp3"
_OTHER_TARGET = Path("Artist") / "Elsewhere" / "01.mp3"
_STATES = (
    paths.AT_SOURCE,
    paths.LANDED,
    paths.LANDED_CHANGED,
    paths.HALF_LINK,
    paths.TARGET_TAKEN,
    paths.GONE,
)


class _CrashError(BaseException):
    """A process death: escapes every handler, so the ledger rolls back on close."""


@dataclass(frozen=True)
class _Lib:
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


def _library(settings: Settings, music: Path, *extra: Path) -> _Lib:
    """Clean folders that keep the mismatch gate open, plus two Artist tracks and *extra*."""
    for name in ("CleanA", "CleanB", "CleanC", "CleanD"):
        make_track(music / name / "Album" / "01.mp3", {"albumartist": [name], "artist": [name]})
    for relative in (_SOURCE, _SOURCE.with_name("02.mp3"), *extra):
        top = relative.parts[0]
        make_track(music / relative, {"albumartist": [top], "artist": [top]})
    scan_library(settings)
    assert mismatch.gate_state(settings).open
    return _Lib(
        settings=settings,
        music=music,
        x=_id_at(settings, music / _SOURCE),
        y=_id_at(settings, music / _SOURCE.with_name("02.mp3")),
    )


@pytest.fixture
def lib(engine_settings: Settings, music_dir: Path) -> _Lib:
    return _library(engine_settings, music_dir)


def _stage(lib: _Lib, *moves: tuple[int, Path | str]) -> list[int]:
    result = paths.stage_paths_batch(
        lib.settings,
        entries=[(file_id, str(target)) for file_id, target in moves],
    )
    return list(result.file_ids)


def _row(lib: _Lib, file_id: int) -> store.FileRow:
    with _ledger(lib.settings) as conn:
        row = store.get_file_by_id(conn, file_id)
        assert row is not None
        return row


def _location(lib: _Lib, file_id: int) -> Path:
    row = _row(lib, file_id)
    return Path(row.folder) / row.filename


def _staged(lib: _Lib, file_id: int) -> store.StagedPath | None:
    with _ledger(lib.settings) as conn:
        return store.get_staged_path(conn, file_id)


def _state(lib: _Lib, file_id: int) -> str:
    view = next(v for v in paths.diff_paths(lib.settings) if v.file_id == file_id)
    return view.state


def _file_count(lib: _Lib) -> int:
    with _ledger(lib.settings) as conn:
        return len(store.list_files(conn))


def _external_write(path: Path, field: str = "title", value: str = "Edited elsewhere") -> None:
    """A tagger edit: new tag bytes and a later mtime."""
    audio = mutagen.File(path, easy=True)  # type: ignore[attr-defined]
    audio[field] = [value]
    audio.save()
    stat_result = path.stat()
    os.utime(path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 5_000_000_000))


def _enter(lib: _Lib, state: str) -> None:
    """Stage x to the target, then shape the disk into *state*."""
    _stage(lib, (lib.x, _TARGET))
    source = lib.music / _SOURCE
    target = lib.music / _TARGET
    if state == paths.GONE:
        source.unlink()
    elif state == paths.TARGET_TAKEN:
        make_track(target, {"title": ["Intruder"]})
    elif state == paths.HALF_LINK:
        target.parent.mkdir(parents=True)
        os.link(source, target)
    elif state in {paths.LANDED, paths.LANDED_CHANGED}:
        target.parent.mkdir(parents=True)
        source.rename(target)
        if state == paths.LANDED_CHANGED:
            _external_write(target)
    assert _state(lib, lib.x) == state


# --- the state table: commit_paths ---------------------------------------------------

_COMMIT_CELLS = {
    paths.AT_SOURCE: "committed",
    paths.LANDED: "committed",
    paths.LANDED_CHANGED: "changed_since_stage",
    paths.HALF_LINK: "committed",
    paths.TARGET_TAKEN: "errors",
    paths.GONE: "missing",
}


@pytest.mark.parametrize("state", _STATES)
def test_commit_paths_cell(lib: _Lib, state: str) -> None:
    _enter(lib, state)

    result = paths.commit_paths(lib.settings).to_dict()

    expected = _COMMIT_CELLS[state]
    assert result[expected] == 1
    if expected == "committed":
        assert _location(lib, lib.x) == lib.music / _TARGET
        assert _staged(lib, lib.x) is None
        assert not (lib.music / _SOURCE).exists()
        assert (lib.music / _TARGET).exists()
        history = paths.history_paths(lib.settings, lib.x)
        assert [(r.version, r.from_path, r.to_path) for r in history] == [
            (0, str(_SOURCE), str(_SOURCE)),
            (1, str(_SOURCE), str(_TARGET)),
        ]
        return
    problems = result["problems"]
    assert isinstance(problems, list)
    assert problems[0]["status"] == ("error" if expected == "errors" else expected)
    assert _location(lib, lib.x) == lib.music / _SOURCE
    if expected == "missing":
        assert _staged(lib, lib.x) is None
        assert _row(lib, lib.x).is_missing
    else:
        assert _staged(lib, lib.x) is not None


# --- the state table: unstage_paths ----------------------------------------------------


@pytest.mark.parametrize("state", _STATES)
def test_unstage_paths_cell(lib: _Lib, state: str) -> None:
    _enter(lib, state)

    if state in {paths.LANDED, paths.LANDED_CHANGED, paths.HALF_LINK}:
        with pytest.raises(ValueError, match=rf"\[{lib.x}\].*commit_paths"):
            paths.unstage_paths(lib.settings, file_id=lib.x)
        assert _staged(lib, lib.x) is not None
        return
    assert paths.unstage_paths(lib.settings, file_id=lib.x).removed == 1
    assert _staged(lib, lib.x) is None


def test_unstage_paths_by_folder_refuses_the_whole_call_for_a_landed_move(lib: _Lib) -> None:
    _stage(lib, (lib.y, Path("Artist") / "Second" / "02.mp3"))
    _enter(lib, paths.LANDED)

    with pytest.raises(ValueError, match="Nothing was unstaged"):
        paths.unstage_paths(lib.settings, path=str(lib.music / "Artist"))

    assert _staged(lib, lib.y) is not None
    assert paths.unstage_paths(lib.settings, file_id=lib.y).removed == 1


def test_unstage_paths_needs_exactly_one_argument(lib: _Lib) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        paths.unstage_paths(lib.settings)
    with pytest.raises(ValueError, match="exactly one"):
        paths.unstage_paths(lib.settings, file_id=lib.x, path="Artist")
    with pytest.raises(ValueError, match="unknown file_id"):
        paths.unstage_paths(lib.settings, file_id=99999)


# --- the state table: stage_paths_batch re-stage ---------------------------------------

_RESTAGE_CELLS = [
    (paths.AT_SOURCE, _TARGET, None),
    (paths.AT_SOURCE, _OTHER_TARGET, None),
    (paths.LANDED, _TARGET, None),
    (paths.LANDED, _OTHER_TARGET, paths.LANDED_MOVE),
    (paths.LANDED_CHANGED, _TARGET, None),
    (paths.LANDED_CHANGED, _OTHER_TARGET, paths.LANDED_MOVE),
    (paths.HALF_LINK, _TARGET, None),
    (paths.HALF_LINK, _OTHER_TARGET, paths.LANDED_MOVE),
    (paths.TARGET_TAKEN, _TARGET, paths.OCCUPIED),
    (paths.TARGET_TAKEN, _OTHER_TARGET, None),
    (paths.GONE, _TARGET, paths.MISSING),
    (paths.GONE, _OTHER_TARGET, paths.MISSING),
]


@pytest.mark.parametrize(("state", "target", "held"), _RESTAGE_CELLS)
def test_restage_cell(lib: _Lib, state: str, target: Path, held: str | None) -> None:
    _enter(lib, state)
    before = _staged(lib, lib.x)

    if held is not None:
        with pytest.raises(ValueError, match=rf"file_id={lib.x}\): {held}"):
            _stage(lib, (lib.x, target))
        assert _staged(lib, lib.x) == before
        return
    _stage(lib, (lib.x, target))

    staged = _staged(lib, lib.x)
    assert staged is not None
    assert staged.to_path == str(target)
    if state in {paths.LANDED, paths.LANDED_CHANGED}:
        # The re-stage confirms the file found at the target.
        assert _state(lib, lib.x) == paths.LANDED
        assert paths.commit_paths(lib.settings).committed == 1
        assert _location(lib, lib.x) == lib.music / _TARGET


# --- the state table: scan_library ----------------------------------------------------

_SCAN_PENDING = {
    paths.AT_SOURCE: 0,
    paths.LANDED: 1,
    paths.LANDED_CHANGED: 1,
    paths.HALF_LINK: 1,
    paths.TARGET_TAKEN: 1,
    paths.GONE: 0,
}


@pytest.mark.parametrize("state", _STATES)
def test_scan_library_cell(lib: _Lib, state: str) -> None:
    _enter(lib, state)
    files_before = _file_count(lib)

    result = scan_library(lib.settings)

    assert result.pending_commit == _SCAN_PENDING[state]
    assert result.missing_flagged == 0
    assert _file_count(lib) == files_before
    assert not _row(lib, lib.x).is_missing
    assert _staged(lib, lib.x) is not None


# --- the state table: revert_commit ----------------------------------------------------


@pytest.mark.parametrize("state", _STATES)
def test_revert_commit_cell(lib: _Lib, state: str) -> None:
    _stage(lib, (lib.y, Path("Artist") / "Second" / "02.mp3"))
    earlier = paths.commit_paths(lib.settings).commit_id
    assert earlier is not None
    _enter(lib, state)

    with pytest.raises(ValueError, match=r"staging area is not empty.*commit_paths"):
        versioning.revert_commit(lib.settings, earlier)

    assert _location(lib, lib.y) == lib.music / "Artist" / "Second" / "02.mp3"


# --- the state table: a crash inside the commit loop -----------------------------------

_CRASH_CELLS = {
    paths.AT_SOURCE: paths.LANDED,
    paths.LANDED: paths.LANDED,
    paths.LANDED_CHANGED: paths.LANDED_CHANGED,
    paths.HALF_LINK: paths.LANDED,
    paths.TARGET_TAKEN: paths.TARGET_TAKEN,
    paths.GONE: paths.GONE,
}


def _crash_after_the_disk_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill the commit at the log append, or at the drop of a missing file's row."""

    def crash(*_args: object, **_kwargs: object) -> None:
        raise _CrashError

    monkeypatch.setattr(store, "insert_path_revision", crash)
    monkeypatch.setattr(store, "delete_staged_path", crash)


@pytest.mark.parametrize("state", _STATES)
def test_crash_in_the_commit_loop_cell(
    lib: _Lib,
    state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enter(lib, state)

    with monkeypatch.context() as patch:
        _crash_after_the_disk_action(patch)
        with contextlib.suppress(_CrashError):
            paths.commit_paths(lib.settings)

    assert _state(lib, lib.x) == _CRASH_CELLS[state]
    assert _location(lib, lib.x) == lib.music / _SOURCE
    assert not _row(lib, lib.x).is_missing
    assert [r.version for r in paths.history_paths(lib.settings, lib.x)] == [0]


def test_a_refused_log_append_moves_the_file_back(
    lib: _Lib,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stage(lib, (lib.x, _TARGET))

    def refuse(*_args: object, **_kwargs: object) -> None:
        message = "refused"
        raise sqlite3.IntegrityError(message)

    with monkeypatch.context() as patch:
        patch.setattr(store, "insert_path_revision", refuse)
        result = paths.commit_paths(lib.settings)

    assert result.errors == 1
    assert (lib.music / _SOURCE).exists()
    assert not (lib.music / _TARGET).exists()
    assert _state(lib, lib.x) == paths.AT_SOURCE
    assert _location(lib, lib.x) == lib.music / _SOURCE
    assert _staged(lib, lib.x) is not None
    assert [r.version for r in paths.history_paths(lib.settings, lib.x)] == [0]
    assert paths.commit_paths(lib.settings).committed == 1
    assert _location(lib, lib.x) == lib.music / _TARGET


def test_a_crashed_move_survives_a_scan_and_the_next_commit_finishes_it(
    lib: _Lib,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stage(lib, (lib.x, _TARGET))
    with monkeypatch.context() as patch:
        _crash_after_the_disk_action(patch)
        with pytest.raises(_CrashError):
            paths.commit_paths(lib.settings)
    assert _state(lib, lib.x) == paths.LANDED

    scanned = scan_library(lib.settings)

    assert (scanned.pending_commit, scanned.added, scanned.missing_flagged) == (1, 0, 0)
    result = paths.commit_paths(lib.settings)
    assert result.committed == 1
    assert _location(lib, lib.x) == lib.music / _TARGET
    assert scan_library(lib.settings).added == 0
    with _ledger(lib.settings) as conn:
        assert [c.status for c in commits.list_commits_in(conn)] == ["applied", "interrupted"]


def test_an_edit_after_a_crashed_move_keeps_the_row_until_it_is_confirmed(
    lib: _Lib,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stage(lib, (lib.x, _TARGET))
    with monkeypatch.context() as patch:
        _crash_after_the_disk_action(patch)
        with pytest.raises(_CrashError):
            paths.commit_paths(lib.settings)
    _external_write(lib.music / _TARGET)

    edited = paths.commit_paths(lib.settings)
    assert edited.changed_since_stage == 1
    assert _staged(lib, lib.x) is not None
    with pytest.raises(ValueError, match="commit_paths"):
        paths.unstage_paths(lib.settings, file_id=lib.x)

    _stage(lib, (lib.x, _TARGET))
    assert paths.commit_paths(lib.settings).committed == 1
    assert _location(lib, lib.x) == lib.music / _TARGET


def test_only_the_exact_landed_spelling_confirms_a_landed_move(
    lib: _Lib,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    landed = _TARGET.with_name("One.mp3")
    _stage(lib, (lib.x, landed))
    with monkeypatch.context() as patch:
        _crash_after_the_disk_action(patch)
        with pytest.raises(_CrashError):
            paths.commit_paths(lib.settings)
    assert _state(lib, lib.x) == paths.LANDED

    with pytest.raises(ValueError, match=rf"file_id={lib.x}\): {paths.LANDED_MOVE}"):
        _stage(lib, (lib.x, landed.with_name("one.mp3")))

    _stage(lib, (lib.x, landed))
    assert paths.commit_paths(lib.settings).committed == 1
    assert _location(lib, lib.x) == lib.music / landed
    assert scan_library(lib.settings).added == 0


# --- stage_paths_batch holds -------------------------------------------------------------


def _entry_for(lib: _Lib, hold: str) -> tuple[int, str]:
    """Shape the library so the returned entry is held for *hold*."""
    file_id, target = lib.x, str(_TARGET)
    if hold == paths.OCCUPIED:
        target = str(Path("CleanA") / "Album" / "01.mp3")
    elif hold == "occupied_on_disk":
        (lib.music / "Artist" / "Album" / "untracked.mp3").write_bytes(bytes(1))
        target = str(Path("Artist") / "Album" / "untracked.mp3")
    elif hold == paths.SHARED_TARGET:
        _stage(lib, (lib.y, _TARGET))
    elif hold == paths.TOO_LONG:
        target = str(Path("Artist") / ("x" * 256) / "01.mp3")
    elif hold == paths.CUE_REFERENCE:
        sheet = lib.music / "Artist" / "Album" / "album.cue"
        sheet.write_text('FILE "01.mp3" MP3\n  TRACK 01 AUDIO\n', encoding="utf-8")
    elif hold == paths.STAGED_TAG:
        staging.stage_tags(lib.settings, file_id=lib.x, tags={"genre": ["Rock"]})
    elif hold == paths.UNKNOWN_FILE:
        file_id = 99999
    return file_id, target


@pytest.mark.parametrize(
    ("hold", "reason"),
    [
        (paths.OCCUPIED, paths.OCCUPIED),
        ("occupied_on_disk", paths.OCCUPIED),
        (paths.SHARED_TARGET, paths.SHARED_TARGET),
        (paths.TOO_LONG, paths.TOO_LONG),
        (paths.CUE_REFERENCE, paths.CUE_REFERENCE),
        (paths.STAGED_TAG, paths.STAGED_TAG),
        (paths.UNKNOWN_FILE, paths.UNKNOWN_FILE),
    ],
)
def test_batch_holds_the_entry_and_stages_nothing(lib: _Lib, hold: str, reason: str) -> None:
    held_entry = _entry_for(lib, hold)
    good_entry = (lib.y, str(Path("Artist") / "Fine" / "02.mp3"))
    if hold == paths.SHARED_TARGET:
        good_entry = (_id_at(lib.settings, lib.music / "CleanB" / "Album" / "01.mp3"), "B.mp3")

    with pytest.raises(ValueError, match=rf"entry 0 \(file_id={held_entry[0]}\): {reason}"):
        paths.stage_paths_batch(lib.settings, entries=[held_entry, good_entry])

    assert _staged(lib, good_entry[0]) is None
    assert _staged(lib, lib.x) is None


@pytest.mark.parametrize(
    "to_path",
    [
        str(Path("Artist") / "Bad:Name" / "01.mp3"),
        str(Path("Artist") / "Dot." / "01.mp3"),
        str(Path("Artist") / " Edge" / "01.mp3"),
        str(Path("Artist") / "CON" / "01.mp3"),
        str(Path("Artist") / "Album" / "01.flac"),
        str(Path("..") / "Escape" / "01.mp3"),
        str(_SOURCE),
        "",
    ],
)
def test_batch_holds_an_invalid_path(lib: _Lib, to_path: str) -> None:
    with pytest.raises(ValueError, match=rf"file_id={lib.x}\): invalid_path"):
        _stage(lib, (lib.x, to_path))
    assert _staged(lib, lib.x) is None


@pytest.mark.parametrize(
    ("sheet", "content", "track"),
    [
        ("album.m3u8", b"#EXTM3U\n#EXTINF:1,One\n01.mp3\n", "01.mp3"),
        ("album.cue", 'FILE "Café.mp3" MP3\n'.encode("cp1252"), "Café.mp3"),
    ],
    ids=["utf8_playlist", "ansi_cue_sheet"],
)
def test_batch_holds_a_file_a_playlist_or_an_ansi_cue_sheet_names(
    engine_settings: Settings,
    music_dir: Path,
    sheet: str,
    content: bytes,
    track: str,
) -> None:
    source = _SOURCE.with_name(track)
    extra = () if source == _SOURCE else (source,)
    _library(engine_settings, music_dir, *extra)
    (music_dir / _SOURCE.parent / sheet).write_bytes(content)
    file_id = _id_at(engine_settings, music_dir / source)

    with pytest.raises(ValueError, match=rf"file_id={file_id}\): {paths.CUE_REFERENCE}"):
        paths.stage_paths_batch(engine_settings, entries=[(file_id, str(_TARGET))])


def test_batch_ignores_a_playlist_comment_naming_the_file(lib: _Lib) -> None:
    (lib.music / _SOURCE.parent / "album.m3u").write_bytes(b"#01.mp3\n")

    assert _stage(lib, (lib.x, _TARGET)) == [lib.x]


def test_batch_holds_an_absolute_path_outside_music_path(lib: _Lib, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"invalid_path: .* is outside music_path"):
        _stage(lib, (lib.x, tmp_path / "elsewhere" / "01.mp3"))


def test_batch_lists_every_held_entry(lib: _Lib) -> None:
    entries = [
        (lib.x, str(_TARGET)),
        (lib.y, str(_TARGET)),
        (99999, "z.mp3"),
    ]

    with pytest.raises(ValueError, match="staged nothing") as caught:
        paths.stage_paths_batch(lib.settings, entries=entries)

    message = str(caught.value)
    assert f"entry 0 (file_id={lib.x}): shared_target" in message
    assert f"entry 1 (file_id={lib.y}): shared_target" in message
    assert "entry 2 (file_id=99999): unknown_file" in message


def test_batch_refuses_malformed_entries(lib: _Lib) -> None:
    with pytest.raises(ValueError, match="duplicate file_id"):
        _stage(lib, (lib.x, _TARGET), (lib.x, _OTHER_TARGET))
    with pytest.raises(ValueError, match="pair"):
        paths.stage_paths_batch(lib.settings, entries=[{"file_id": lib.x}])
    with pytest.raises(ValueError, match="to_path must be a string"):
        paths.stage_paths_batch(lib.settings, entries=[(lib.x, None)])


def test_batch_accepts_an_absolute_path_under_music_path(lib: _Lib) -> None:
    _stage(lib, (lib.x, lib.music / _TARGET))
    staged = _staged(lib, lib.x)
    assert staged is not None
    assert staged.to_path == str(_TARGET)
    assert staged.to_key == path_keys.path_key(_TARGET)


def test_batch_refuses_the_whole_call_while_the_gate_is_closed(lib: _Lib) -> None:
    audio = mutagen.File(lib.music / _SOURCE.with_name("02.mp3"), easy=True)  # type: ignore[attr-defined]
    audio["albumartist"] = ["Somebody Else"]
    audio.save()
    scan_library(lib.settings)

    with pytest.raises(ValueError, match="path gate is closed"):
        _stage(lib, (lib.x, _TARGET))
    assert _staged(lib, lib.x) is None


def test_a_valid_batch_commits_as_one_revertible_commit(lib: _Lib) -> None:
    _stage(
        lib, (lib.x, Path("Artist") / "New" / "01.mp3"), (lib.y, Path("Artist") / "New" / "02.mp3")
    )

    result = paths.commit_paths(lib.settings, message="regroup")

    assert (result.committed, result.problems) == (2, ())
    assert result.commit_id is not None
    assert versioning.commit_logs(lib.settings, result.commit_id) == {"path_revisions": 2}
    commit = commits.get_commit(lib.settings, result.commit_id)
    assert commit is not None
    assert (commit.origin, commit.message, commit.status) == ("manual", "regroup", "applied")


# --- the c1 commit check ---------------------------------------------------------------


def test_commit_paths_refuses_a_staged_file_that_flags_at_its_recorded_path(lib: _Lib) -> None:
    _stage(lib, (lib.x, _TARGET))
    audio = mutagen.File(lib.music / _SOURCE, easy=True)  # type: ignore[attr-defined]
    audio["albumartist"] = ["Somebody Else"]
    audio.save()
    scan_library(lib.settings)

    with pytest.raises(ValueError, match=rf"\({lib.x}, 'top_folder_artist'\).*unstage_paths"):
        paths.commit_paths(lib.settings)

    assert (lib.music / _SOURCE).exists()
    assert _staged(lib, lib.x) is not None


def _land_the_second_of_a_straight_through_two_disc_album(
    lib: _Lib,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash at the second log append: y lands and flags alone at its recorded folder."""
    for name, disc in (("01.mp3", "1"), ("02.mp3", "2")):
        audio = mutagen.File(lib.music / _SOURCE.with_name(name), easy=True)  # type: ignore[attr-defined]
        audio["discnumber"] = [disc]
        audio["tracknumber"] = ["1"]
        audio.save()
    scan_library(lib.settings)
    assert mismatch.gate_state(lib.settings).open
    _stage(lib, (lib.x, _TARGET), (lib.y, _TARGET.with_name("02.mp3")))
    appends: list[int] = []
    append = store.insert_path_revision

    def crash_on_the_second(*args: object, **kwargs: object) -> None:
        appends.append(1)
        if len(appends) == 2:  # the second file's append
            raise _CrashError
        append(*args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as patch:
        patch.setattr(store, "insert_path_revision", crash_on_the_second)
        with pytest.raises(_CrashError):
            paths.commit_paths(lib.settings)
    assert _state(lib, lib.y) == paths.LANDED
    with _ledger(lib.settings) as conn:
        assert mismatch.check_files(conn, lib.settings, [lib.y])


def test_a_landed_move_that_flags_at_its_recorded_path_still_commits(
    lib: _Lib,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _land_the_second_of_a_straight_through_two_disc_album(lib, monkeypatch)

    result = paths.commit_paths(lib.settings)

    assert result.committed == 1
    assert _location(lib, lib.y) == lib.music / _TARGET.with_name("02.mp3")


def test_an_edited_landed_move_that_flags_is_confirmed_while_the_gate_is_closed(
    lib: _Lib,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _land_the_second_of_a_straight_through_two_disc_album(lib, monkeypatch)
    landed = lib.music / _TARGET.with_name("02.mp3")
    _external_write(landed)
    assert paths.commit_paths(lib.settings).changed_since_stage == 1
    assert not mismatch.gate_state(lib.settings).open
    with pytest.raises(ValueError, match="path gate is closed"):
        _stage(lib, (lib.y, _TARGET.with_name("02.mp3")), (lib.x, _OTHER_TARGET))

    _stage(lib, (lib.y, _TARGET.with_name("02.mp3")))

    assert paths.commit_paths(lib.settings).committed == 1
    assert _location(lib, lib.y) == landed


# --- folder case, filename case and the pruner ------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS ignores case")
def test_a_case_only_filename_rename_commits_and_keeps_the_id(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    lib = _library(engine_settings, music_dir, Path("Artist") / "Album" / "track.mp3")
    track = _id_at(lib.settings, lib.music / "Artist" / "Album" / "track.mp3")
    renamed = Path("Artist") / "Album" / "Track.mp3"

    _stage(lib, (track, renamed))
    result = paths.commit_paths(lib.settings)

    assert result.committed == 1
    assert "Track.mp3" in {p.name for p in (lib.music / "Artist" / "Album").iterdir()}
    assert _row(lib, track).filename == "Track.mp3"
    assert [r.to_path for r in paths.history_paths(lib.settings, track)] == [
        str(Path("Artist") / "Album" / "track.mp3"),
        str(renamed),
    ]
    assert scan_library(lib.settings).added == 0
    assert _id_at(lib.settings, lib.music / renamed) == track


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS ignores case")
def test_a_folder_part_reuses_the_on_disk_spelling(lib: _Lib) -> None:
    _stage(lib, (lib.x, Path("ARTIST") / "moved" / "01.mp3"))

    staged = _staged(lib, lib.x)
    assert staged is not None
    assert staged.to_path == str(Path("Artist") / "moved" / "01.mp3")


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS ignores case")
def test_a_target_naming_the_files_own_entry_in_another_folder_spelling(lib: _Lib) -> None:
    new = Path("Artist") / "New"
    # One call spells a new folder once, so only two calls record the second spelling.
    _stage(lib, (lib.x, new / "01.mp3"))
    _stage(lib, (lib.y, Path("Artist") / "NEW" / "02.mp3"))
    assert paths.commit_paths(lib.settings).committed == 2
    on_disk = lib.music / new / "02.mp3"
    assert Path(_row(lib, lib.y).folder).name == "NEW"

    with pytest.raises(ValueError, match=rf"file_id={lib.y}\): invalid_path: .*already sits"):
        _stage(lib, (lib.y, new / "02.mp3"))

    # The probe and the mover must survive a row the batch refuses, so it is written directly.
    with _ledger(lib.settings) as conn:
        size, mtime_ns = on_disk.stat().st_size, on_disk.stat().st_mtime_ns
        store.upsert_staged_path(
            conn,
            store.StagedPath(
                file_id=lib.y,
                to_path=str(new / "02.mp3"),
                to_key=path_keys.path_key(new / "02.mp3"),
                origin="manual",
                note=None,
                staged_at="2026-10-01",
                base_size_bytes=size,
                base_mtime_ns=mtime_ns,
                reverted_to_version=None,
            ),
        )
        conn.commit()
    assert _state(lib, lib.y) == paths.AT_SOURCE

    assert paths.commit_paths(lib.settings).committed == 1
    assert on_disk.exists()
    assert _location(lib, lib.y) == on_disk
    assert Path(_row(lib, lib.y).folder).name == "New"


def test_respell_folders_keeps_an_absent_folder_and_the_filename(lib: _Lib) -> None:
    respelled = paths.respell_folders(lib.music, str(Path("Artist") / "New" / "ONE.mp3"))
    assert respelled == str(Path("Artist") / "New" / "ONE.mp3")


@pytest.mark.skipif(path_keys.path_key("A") == "A", reason="only a case-folding key respells")
def test_a_batch_stages_two_casings_of_one_new_folder_under_the_first_spelling(lib: _Lib) -> None:
    first = Path("Artist") / "New Album" / "01.mp3"

    _stage(lib, (lib.x, first), (lib.y, Path("Artist") / "new album" / "02.mp3"))

    staged = [_staged(lib, file_id) for file_id in (lib.x, lib.y)]
    assert [row.to_path if row else None for row in staged] == [
        str(first),
        str(first.with_name("02.mp3")),
    ]


def test_the_pruner_removes_emptied_folders_up_to_music_path(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    lib = _library(engine_settings, music_dir, Path("Solo") / "Album" / "01.mp3")
    solo = _id_at(lib.settings, music_dir / "Solo" / "Album" / "01.mp3")

    _stage(lib, (solo, Path("Solo2") / "Album" / "01.mp3"))
    paths.commit_paths(lib.settings)

    assert not (music_dir / "Solo").exists()
    assert music_dir.exists()
    assert (music_dir / "Solo2" / "Album" / "01.mp3").exists()


def test_the_pruner_keeps_a_folder_holding_a_hidden_file(lib: _Lib) -> None:
    hidden = lib.music / "Artist" / "Album" / ".hidden"
    hidden.write_bytes(b"\x00")
    new = Path("Artist") / "New"
    # The same name at the destination holds the sidecar, so it stays in the vacated folder.
    (lib.music / new).mkdir()
    (lib.music / new / ".hidden").write_bytes(b"\x01")
    _stage(lib, (lib.x, new / "01.mp3"), (lib.y, new / "02.mp3"))

    result = paths.commit_paths(lib.settings)

    assert result.sidecars_held == (str(Path("Artist") / "Album" / ".hidden"),)
    assert hidden.exists()
    assert (lib.music / new / "01.mp3").exists()


def test_the_pruner_removes_the_vacated_album_folder_only(lib: _Lib) -> None:
    new = Path("Artist") / "New"
    _stage(lib, (lib.x, new / "01.mp3"), (lib.y, new / "02.mp3"))

    paths.commit_paths(lib.settings)

    assert not (lib.music / "Artist" / "Album").exists()
    assert (lib.music / "Artist").exists()


# --- the volume check ------------------------------------------------------------------


def test_the_volume_check_passes_on_the_test_filesystem(lib: _Lib) -> None:
    assert paths.volume_refusal(lib.music) is None


def test_the_volume_check_refuses_a_case_blind_posix_volume(
    lib: _Lib,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stage(lib, (lib.y, Path("Artist") / "Second" / "02.mp3"))
    monkeypatch.setattr(paths, "_case_blind_volume", lambda _music_path: True)

    with pytest.raises(ValueError, match="ignores case"):
        _stage(lib, (lib.x, _TARGET))
    with pytest.raises(ValueError, match="ignores case"):
        paths.commit_paths(lib.settings)
    check = health._check_path_staging(lib.settings)
    assert check.ok
    assert "ignores case" in check.detail
    assert (lib.music / _SOURCE.with_name("02.mp3")).exists()


def _probe_as_posix(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Make path keys keep case, as on POSIX, and record each pair of names the probe compares."""
    compared: list[tuple[str, str]] = []
    samefile = Path.samefile

    def spy(self: Path, other: str | os.PathLike[str]) -> bool:
        compared.append((self.name, Path(other).name))
        return samefile(self, other)

    monkeypatch.setattr(path_keys, "path_key", lambda p: os.path.normpath(os.fspath(p)))
    monkeypatch.setattr(Path, "samefile", spy)
    return compared


def test_the_volume_probe_swaps_a_name_inside_music_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A mount point's own name is resolved by its parent volume, so only a name inside it tells.
    music = tmp_path / "music"
    (music / "Zebra").mkdir(parents=True)
    (music / "Artist").mkdir()
    (music / "2001").mkdir()
    case_blind = (music / "aRTIST").exists()
    compared = _probe_as_posix(monkeypatch)

    assert paths._case_blind_volume(music) is case_blind
    assert compared == ([("Artist", "aRTIST")] if case_blind else [])


def test_the_volume_probe_falls_back_to_music_path_itself_when_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    music = tmp_path / "music"
    music.mkdir()
    case_blind = (tmp_path / "MUSIC").exists()
    compared = _probe_as_posix(monkeypatch)

    assert paths._case_blind_volume(music) is case_blind
    assert compared == ([("music", "MUSIC")] if case_blind else [])


# --- the pruner and links --------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are a Windows feature")
def test_the_pruner_stops_at_a_junction_and_never_removes_it(tmp_path: Path) -> None:
    import _winapi  # noqa: PLC0415 - Windows-only module

    music = tmp_path / "music"
    music.mkdir()
    target = tmp_path / "target"
    (target / "Empty").mkdir(parents=True)
    (target / "Other").mkdir()
    (target / "Other" / "b.mp3").write_bytes(bytes(1))
    _winapi.CreateJunction(str(target), str(music / "Vinyl"))

    removed = paths._prune(music, music / "Vinyl" / "Empty")

    assert removed == [music / "Vinyl" / "Empty"]
    assert (music / "Vinyl").is_junction()
    assert (target / "Other" / "b.mp3").exists()
    assert (music / "Vinyl" / "Other" / "b.mp3").exists()


# --- revert ------------------------------------------------------------------------------


def _move(lib: _Lib, *moves: tuple[int, Path]) -> int:
    _stage(lib, *moves)
    commit_id = paths.commit_paths(lib.settings).commit_id
    assert commit_id is not None
    return commit_id


def test_revert_commit_moves_every_file_back_and_a_second_revert_moves_them_forward(
    lib: _Lib,
) -> None:
    new = Path("Artist") / "New"
    forward = _move(lib, (lib.x, new / "01.mp3"), (lib.y, new / "02.mp3"))

    back = versioning.revert_commit(lib.settings, forward, note="undo")

    assert (back.reverted, back.skipped, back.errors) == (2, 0, 0)
    assert back.commit_id is not None
    assert _location(lib, lib.x) == lib.music / _SOURCE
    assert _location(lib, lib.y) == lib.music / _SOURCE.with_name("02.mp3")
    assert not (lib.music / new).exists()
    reverted = paths.history_paths(lib.settings, lib.x)[-1]
    assert (reverted.origin, reverted.reverted_to_version, reverted.commit_id) == (
        "revert",
        0,
        back.commit_id,
    )
    commit = commits.get_commit(lib.settings, back.commit_id)
    assert commit is not None
    assert (commit.origin, commit.reverted_from) == ("revert", forward)

    again = versioning.revert_commit(lib.settings, back.commit_id)

    assert again.reverted == 2
    assert _location(lib, lib.x) == lib.music / new / "01.mp3"
    assert [r.version for r in paths.history_paths(lib.settings, lib.x)] == [0, 1, 2, 3]


def test_revert_commit_dry_run_changes_nothing(lib: _Lib) -> None:
    forward = _move(lib, (lib.x, _TARGET))

    preview = versioning.revert_commit(lib.settings, forward, dry_run=True)

    assert (preview.commit_id, preview.reverted, preview.dry_run) == (None, 1, True)
    assert _location(lib, lib.x) == lib.music / _TARGET


def test_revert_commit_skips_a_file_a_later_path_commit_moved(lib: _Lib) -> None:
    first = _move(lib, (lib.x, _TARGET))
    _move(lib, (lib.x, _OTHER_TARGET))

    result = versioning.revert_commit(lib.settings, first)

    assert result.commit_id is None
    assert [o.status for o in result.outcomes] == ["skipped_later_changes"]
    assert _location(lib, lib.x) == lib.music / _OTHER_TARGET


def test_revert_commit_reports_a_source_taken_since(lib: _Lib) -> None:
    forward = _move(lib, (lib.x, _TARGET))
    make_track(lib.music / _SOURCE, {"title": ["Newcomer"]})

    result = versioning.revert_commit(lib.settings, forward)

    assert [o.status for o in result.outcomes] == ["error"]
    assert "already on disk" in str(result.outcomes[0].detail)
    assert _location(lib, lib.x) == lib.music / _TARGET


def test_revert_commit_refuses_a_commit_in_no_log(lib: _Lib) -> None:
    with _ledger(lib.settings) as conn:
        empty = commits.create_commit(conn, origin="manual", message=None, now="2026-10-01")
        commits.set_commit_status(conn, empty, "applied")
        conn.commit()

    with pytest.raises(ValueError, match="holds no change"):
        versioning.revert_commit(lib.settings, empty)


def test_revert_paths_moves_one_file_to_a_prior_version(lib: _Lib) -> None:
    _move(lib, (lib.x, _TARGET))
    _move(lib, (lib.x, _OTHER_TARGET))

    preview = paths.revert_paths(lib.settings, lib.x, 0, dry_run=True)
    assert (preview.status, preview.commit_id) == ("reverted", None)
    assert _location(lib, lib.x) == lib.music / _OTHER_TARGET

    result = paths.revert_paths(lib.settings, lib.x, 0, note="home")

    assert (result.status, result.new_version, result.to_path) == ("reverted", 3, str(_SOURCE))
    assert _location(lib, lib.x) == lib.music / _SOURCE
    assert result.commit_id is not None
    assert versioning.commit_logs(lib.settings, result.commit_id) == {"path_revisions": 1}


def test_revert_paths_refusals(lib: _Lib) -> None:
    _move(lib, (lib.x, _TARGET))
    with pytest.raises(ValueError, match="no path version 7"):
        paths.revert_paths(lib.settings, lib.x, 7)
    with pytest.raises(ValueError, match="already on disk"):
        paths.revert_paths(lib.settings, lib.x, 1)
    with pytest.raises(ValueError, match="no path history"):
        paths.revert_paths(lib.settings, lib.y, 0)
    with pytest.raises(ValueError, match="unknown file_id"):
        paths.revert_paths(lib.settings, 99999, 0)


def test_both_reverts_refuse_while_a_row_is_staged_and_name_commit_paths(
    lib: _Lib,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forward = _move(lib, (lib.x, _TARGET))
    with monkeypatch.context() as patch:
        _crash_after_the_disk_action(patch)
        with pytest.raises(_CrashError):
            versioning.revert_commit(lib.settings, forward)
    leftover = _staged(lib, lib.x)
    assert leftover is not None
    assert (leftover.origin, leftover.to_path) == ("revert", str(_SOURCE))

    with pytest.raises(ValueError, match="commit_paths"):
        versioning.revert_commit(lib.settings, forward)
    with pytest.raises(ValueError, match="commit_paths"):
        paths.revert_paths(lib.settings, lib.x, 0)

    swept = paths.commit_paths(lib.settings)

    assert swept.committed == 1
    assert _location(lib, lib.x) == lib.music / _SOURCE
    assert swept.commit_id is not None
    commit = commits.get_commit(lib.settings, swept.commit_id)
    assert commit is not None
    assert commit.origin == "revert"


def test_history_paths_lists_every_location(lib: _Lib) -> None:
    first = _move(lib, (lib.x, _TARGET))

    history = [r.to_dict() for r in paths.history_paths(lib.settings, lib.x)]

    assert [(h["version"], h["origin"], h["commit_id"], h["to_path"]) for h in history] == [
        (0, "scan", None, str(_SOURCE)),
        (1, "manual", first, str(_TARGET)),
    ]
    assert paths.history_paths(lib.settings, lib.y) == []
    with pytest.raises(ValueError, match="unknown file_id"):
        paths.history_paths(lib.settings, 99999)


# --- mutual exclusion, health and the tag-side guards -----------------------------------


def test_tag_staging_refuses_a_file_with_a_staged_move(lib: _Lib) -> None:
    _stage(lib, (lib.x, _TARGET))

    with pytest.raises(ValueError, match=r"staged path change.*commit_paths"):
        staging.stage_tags(lib.settings, file_id=lib.x, tags={"genre": ["Rock"]})
    with pytest.raises(ValueError, match="staged path change"):
        staging.stage_tags_batch(lib.settings, entries=[(lib.x, {"genre": ["Rock"]})])
    with pytest.raises(ValueError, match="commit or unstage pending changes first"):
        years.resolve_years(lib.settings)


def test_revert_tags_refuses_a_file_with_a_staged_move(lib: _Lib) -> None:
    staging.stage_tags(lib.settings, file_id=lib.x, tags={"genre": ["Rock"]})
    staging.commit_tags(lib.settings)
    _stage(lib, (lib.x, _TARGET))

    with pytest.raises(ValueError, match=r"staged path change.*unstage_paths"):
        versioning.revert_tags(lib.settings, lib.x, 0)


def test_check_health_reports_staged_and_landed_moves(lib: _Lib) -> None:
    _stage(lib, (lib.y, Path("Artist") / "Second" / "02.mp3"))
    _enter(lib, paths.LANDED)

    check = health._check_path_staging(lib.settings)

    assert check.ok
    assert "2 staged move(s)" in check.detail
    assert f"[{lib.x}] already sit at their target. Run commit_paths" in check.detail
    assert "the volume check passes" in check.detail


# --- the component rules -------------------------------------------------------------------


def test_check_components_and_check_length() -> None:
    assert paths.check_components(str(Path("A") / "M.I.A. Song.mp3")) == []
    assert len(paths.check_components(str(Path("lpt1.txt") / "x?.mp3"))) == 2
    assert paths.check_length(Path("/m"), "a.mp3") == []
    assert paths.check_length(Path("/m"), str(Path("b" * 100) / ("c" * 200)))


def test_check_length_counts_the_full_path_in_utf16_units() -> None:
    # An astral character is one code point but two UTF-16 units, and MAX_PATH counts units.
    music = Path("/m")
    folder = "x" * 120
    astral = "\U0001f3b5" * 2
    padding = 258 - len(str(music / folder / f"{astral}.mp3"))
    relative = str(Path(folder) / f"{astral}{'x' * padding}.mp3")

    assert len(str(music / relative)) == 258
    assert paths.check_length(music, relative) == ["the full path is 260 UTF-16 units, over 259"]


# --- forbidden calls -----------------------------------------------------------------------

_PATH_MODULES = (Path(paths.__file__),)
_FORBIDDEN_CALLS = frozenset(
    {"replace", "move", "rmtree", "remove", "removedirs", "unlink", "rmdir"}
)
_ALLOWED = {"unlink": {"move_no_clobber", "_finish_half_link"}, "rmdir": {"_prune"}}


def _calls_by_function(module: Path) -> list[tuple[str, str]]:
    """Return ``(enclosing function, called attribute)`` for every forbidden-named call."""
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, ast.FunctionDef) else function
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in _FORBIDDEN_CALLS
            ):
                found.append((function, child.func.attr))
            visit(child, inner)

    visit(ast.parse(module.read_text(encoding="utf-8")), "<module>")
    return found


def test_the_path_modules_never_delete_or_overwrite() -> None:
    for module in _PATH_MODULES:
        calls = _calls_by_function(module)
        assert {name for _, name in calls} == {"unlink", "rmdir"}
        for function, name in calls:
            assert function in _ALLOWED.get(name, set()), f"{module.name}: {name} in {function}"


# --- stage_paths and the naming settings ------------------------------------------------

_ONE = Path("Artist") / "Album" / "01 One.flac"
_TWO = Path("Artist") / "Album" / "02 Two.flac"
_TUNE = Path("Other") / "Record" / "01 Tune.flac"
_RENDERED_ALBUM = Path("Artist") / "(2001) Artist - Album"
_ONE_TARGET = _RENDERED_ALBUM / "Artist - Album - 01 - One.flac"


def _full_tags(artist: str, album: str, track: int, title: str) -> dict[str, list[str]]:
    return {
        "albumartist": [artist],
        "artist": [artist],
        "album": [album],
        "date": ["2001"],
        "tracknumber": [str(track)],
        "title": [title],
    }


@pytest.fixture
def loose(engine_settings: Settings, music_dir: Path) -> dict[Path, int]:
    """A library laid out loosely: the gate is open and every file deviates from the render."""
    make_track(music_dir / _ONE, _full_tags("Artist", "Album", 1, "One"))
    make_track(music_dir / _TWO, _full_tags("Artist", "Album", 2, "Two"))
    make_track(music_dir / _TUNE, _full_tags("Other", "Record", 1, "Tune"))
    scan_library(engine_settings)
    assert mismatch.gate_state(engine_settings).open
    return {
        relative: _id_at(engine_settings, music_dir / relative) for relative in (_ONE, _TWO, _TUNE)
    }


def _at(settings: Settings, file_id: int) -> Path:
    with _ledger(settings) as conn:
        row = store.get_file_by_id(conn, file_id)
        assert row is not None
        return Path(row.folder) / row.filename


def _tag_history(settings: Settings, file_ids: list[int]) -> dict[int, object]:
    with _ledger(settings) as conn:
        return {
            file_id: (store.get_tags(conn, file_id), store.get_revisions(conn, file_id))
            for file_id in file_ids
        }


def test_a_full_stage_paths_cycle_moves_every_file_and_revert_restores_every_path(
    engine_settings: Settings, music_dir: Path, loose: dict[Path, int]
) -> None:
    ids = list(loose.values())
    tags_before = _tag_history(engine_settings, ids)

    preview = paths.stage_paths(engine_settings, dry_run=True)
    assert (preview.dry_run, preview.staged, paths.diff_paths(engine_settings)) == (True, 3, [])

    staged = paths.stage_paths(engine_settings, note="tidy")
    assert (staged.staged, staged.folders, staged.kinds) == (3, 2, {"rename": 0, "move": 3})
    views = paths.diff_paths(engine_settings)
    assert {(view.origin, view.state, view.stale) for view in views} == {
        ("auto", paths.AT_SOURCE, False),
    }

    result = paths.commit_paths(engine_settings)
    assert (result.committed, result.problems) == (3, ())
    assert result.commit_id is not None
    commit = commits.get_commit(engine_settings, result.commit_id)
    assert commit is not None
    assert commit.message == f"naming pattern {naming.DEFAULT_PATTERN}"
    assert _at(engine_settings, loose[_ONE]) == music_dir / _ONE_TARGET
    assert paths.stage_paths(engine_settings, dry_run=True).at_target == 3

    back = versioning.revert_commit(engine_settings, result.commit_id)

    assert (back.reverted, back.errors) == (3, 0)
    for relative, file_id in loose.items():
        assert _at(engine_settings, file_id) == music_dir / relative
        assert (music_dir / relative).exists()
    assert not (music_dir / _RENDERED_ALBUM).exists()
    assert _tag_history(engine_settings, ids) == tags_before


def test_stage_paths_refuses_while_the_gate_is_closed_except_a_dry_run(
    engine_settings: Settings, music_dir: Path, loose: dict[Path, int]
) -> None:
    make_track(
        music_dir / "Wrong" / "Album" / "01 One.flac", _full_tags("Right", "Album", 1, "One")
    )
    scan_library(engine_settings)

    with pytest.raises(ValueError, match="gate is closed"):
        paths.stage_paths(engine_settings)

    assert paths.stage_paths(engine_settings, dry_run=True).staged == 4
    assert paths.diff_paths(engine_settings) == []


def test_stage_paths_replaces_its_own_auto_rows_and_never_touches_a_manual_row(
    engine_settings: Settings, music_dir: Path, loose: dict[Path, int]
) -> None:
    paths.stage_paths(engine_settings)
    kept = str(Path("Other") / "Kept" / "01 Tune.flac")
    paths.stage_paths_batch(engine_settings, entries=[(loose[_TUNE], kept)])
    in_place = dataclasses.replace(
        engine_settings, naming_pattern="{albumartist}/{album}/{tracknumber:02} {title}"
    )

    again = paths.stage_paths(in_place)

    assert (again.staged, again.unstaged, again.at_target, again.kept_staged) == (0, 2, 2, 1)
    rows = {view.file_id: (view.origin, view.to_path) for view in paths.diff_paths(in_place)}
    assert rows == {loose[_TUNE]: ("manual", kept)}


def test_a_folder_with_a_manual_row_holds_its_other_files(
    engine_settings: Settings, loose: dict[Path, int]
) -> None:
    paths.stage_paths_batch(engine_settings, entries=[(loose[_ONE], str(Path("Away") / "1.flac"))])

    result = paths.stage_paths(engine_settings, path="Artist")

    assert (result.matched, result.staged, result.kept_staged) == (2, 0, 1)
    held = result.held_files[0]
    assert (held.file_id, held.reasons[0][0]) == (loose[_TWO], paths.UNIT_MEMBER)
    assert str(loose[_ONE]) in held.reasons[0][1]


def test_stage_paths_never_replaces_a_landed_move_and_commit_paths_finishes_it(
    engine_settings: Settings, music_dir: Path, loose: dict[Path, int]
) -> None:
    paths.stage_paths(engine_settings, path="Artist")
    (music_dir / _ONE_TARGET).parent.mkdir(parents=True)
    (music_dir / _ONE).rename(music_dir / _ONE_TARGET)
    before = _staged_rows(engine_settings)

    again = paths.stage_paths(engine_settings, path="Artist")

    assert again.held[paths.LANDED_MOVE] == 1
    assert "commit_paths" in again.held_files[0].reasons[0][1]
    assert _staged_rows(engine_settings)[loose[_ONE]] == before[loose[_ONE]]
    assert loose[_TWO] not in _staged_rows(engine_settings)
    assert paths.commit_paths(engine_settings).committed == 1
    assert _at(engine_settings, loose[_ONE]) == music_dir / _ONE_TARGET


def _staged_rows(settings: Settings) -> dict[int, store.StagedPath]:
    with _ledger(settings) as conn:
        return {row.file_id: row for row in store.list_staged_paths(conn)}


def test_stage_paths_restages_two_auto_rows_that_swap_targets(
    engine_settings: Settings, music_dir: Path
) -> None:
    first = music_dir / "Artist" / "Album" / "01 Song.flac"
    second = music_dir / "Artist" / "Album" / "CD1" / "01 Song.flac"
    for path, year in ((first, "2001"), (second, "2003")):
        tags = _full_tags("Artist", "Album", 1, "Song")
        make_track(path, {**tags, "date": [year], "discnumber": ["1"]})
    scan_library(engine_settings)
    ids = (_id_at(engine_settings, first), _id_at(engine_settings, second))
    assert paths.stage_paths(engine_settings).staged == 2
    before = {file_id: row.to_path for file_id, row in _staged_rows(engine_settings).items()}
    _external_write(first, "date", "2003")
    _external_write(second, "date", "2001")
    scan_library(engine_settings)
    assert all(view.stale for view in paths.diff_paths(engine_settings))

    again = paths.stage_paths(engine_settings)

    after = {file_id: row.to_path for file_id, row in _staged_rows(engine_settings).items()}
    assert (again.staged, again.held, again.unstaged) == (2, {}, 0)
    assert after == {ids[0]: before[ids[1]], ids[1]: before[ids[0]]}
    assert not any(view.stale for view in paths.diff_paths(engine_settings))


def test_stage_paths_scopes_to_a_folder_and_counts_staged_targets_under_an_empty_one(
    engine_settings: Settings, music_dir: Path, loose: dict[Path, int]
) -> None:
    scoped = paths.stage_paths(engine_settings, path=music_dir / "Other")
    assert (scoped.matched, scoped.staged) == (1, 1)
    paths.stage_paths(engine_settings, path="Artist")

    empty = paths.stage_paths(engine_settings, path=str(_RENDERED_ALBUM), dry_run=True)

    assert (empty.matched, empty.staged_targets_under_path) == (0, 2)


def test_diff_paths_flags_an_auto_row_the_current_render_no_longer_targets(
    engine_settings: Settings, loose: dict[Path, int]
) -> None:
    paths.stage_paths(engine_settings, path="Artist")
    paths.stage_paths_batch(engine_settings, entries=[(loose[_TUNE], "Tune.flac")])
    changed = dataclasses.replace(engine_settings, naming_pattern="{albumartist}/{title}")

    stale = {view.file_id: view.stale for view in paths.diff_paths(changed)}

    assert stale == {loose[_ONE]: True, loose[_TWO]: True, loose[_TUNE]: False}
    assert not any(view.stale for view in paths.diff_paths(engine_settings))


def test_commit_paths_keeps_an_explicit_message(
    engine_settings: Settings, loose: dict[Path, int]
) -> None:
    paths.stage_paths(engine_settings)

    commit_id = paths.commit_paths(engine_settings, message="mine").commit_id

    assert commit_id is not None
    commit = commits.get_commit(engine_settings, commit_id)
    assert commit is not None
    assert commit.message == "mine"


def test_set_naming_pattern_saves_each_setting_it_is_given(engine_settings: Settings) -> None:
    saved = paths.set_naming_pattern(
        engine_settings, pattern=" {albumartist}/{title} ", container_folders=["Soundtracks"]
    )

    loaded = config.load_settings()
    assert (loaded.naming_pattern, loaded.container_folders) == (
        "{albumartist}/{title}",
        ("Soundtracks",),
    )
    assert saved.to_dict()["pattern"] == "{albumartist}/{title}"

    cleared = paths.set_naming_pattern(loaded, container_folders=[])

    assert (cleared.pattern, cleared.container_folders) == ("{albumartist}/{title}", ())
    assert config.load_settings().container_folders == ()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "pass pattern, container_folders or both"),
        ({"pattern": ""}, "empty"),
        ({"pattern": "{artist}/{bogus}"}, "unknown field name"),
        ({"pattern": "{artist}/{title"}, "unbalanced"),
        ({"pattern": "{artist}/[{album}/x]{title}"}, "inside a group"),
        ({"container_folders": ["a;b"]}, "invalid container_folders"),
    ],
)
def test_set_naming_pattern_refuses_invalid_input(
    engine_settings: Settings, kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        paths.set_naming_pattern(engine_settings, **kwargs)  # type: ignore[arg-type]

    assert config.load_settings().naming_pattern == ""


def test_set_naming_pattern_refuses_while_a_move_is_staged(
    engine_settings: Settings, loose: dict[Path, int]
) -> None:
    paths.stage_paths(engine_settings)

    with pytest.raises(ValueError, match="commit_paths"):
        paths.set_naming_pattern(engine_settings, pattern="{albumartist}/{title}")

    assert config.load_settings().naming_pattern == ""


def test_an_invalid_saved_pattern_is_named_on_use(
    engine_settings: Settings, loose: dict[Path, int]
) -> None:
    broken = dataclasses.replace(engine_settings, naming_pattern="{nope}")

    with pytest.raises(ValueError, match="naming_pattern setting is invalid"):
        paths.stage_paths(broken, dry_run=True)
