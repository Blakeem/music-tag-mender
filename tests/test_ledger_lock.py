"""The ledger-wide mutation lock: one mutating TagMend call at a time per ledger."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from conftest import make_track
from tagmend import config, mcp_server
from tagmend.config import load_settings
from tagmend.engine import (
    commits,
    covers,
    db,
    genres,
    health,
    ledger_lock,
    mismatch,
    paths,
    schema,
    staging,
    store,
)
from tagmend.engine.lastfm import Tag
from tagmend.engine.ledger_lock import LedgerBusyError
from tagmend.engine.library import scan_library
from tagmend.engine.tags import read_tags

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tagmend.config import Settings


class _OneArtistTags:
    """A Last.fm tag source that knows one artist and no album."""

    def artist_top_tags(self, name: str) -> list[Tag] | None:
        return [Tag("electronic", 100), Tag("house", 60)] if name == "Daft Punk" else None

    def album_top_tags(self, artist: str, album: str) -> list[Tag] | None:
        return None


@contextmanager
def _held_by_another_handle(db_path: Path) -> Iterator[None]:
    """Hold the ledger's OS lock through a second handle, as another TagMend process would."""
    fd = ledger_lock._os_lock(ledger_lock.lock_path(db_path))
    assert fd is not None
    try:
        yield
    finally:
        ledger_lock._os_unlock(fd)


def _track(settings: Settings, music_dir: Path) -> tuple[Path, int]:
    """Write and scan one Daft Punk track, returning its path and file id."""
    track = make_track(
        music_dir / "Daft Punk" / "Discovery" / "01 One More Time.mp3",
        {"artist": ["Daft Punk"], "album": ["Discovery"], "title": ["One More Time"]},
    )
    scan_library(settings)
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        row = store.get_file(connection, str(track.parent), track.name)
    finally:
        connection.close()
    assert row is not None
    return track, row.id


def test_mutating_calls_refuse_and_change_nothing_while_another_handle_holds_the_lock(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track, file_id = _track(engine_settings, music_dir)
    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Electronic"]})

    with _held_by_another_handle(engine_settings.db_path):
        for call in (
            lambda: staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["House"]}),
            lambda: staging.commit_tags(engine_settings),
            lambda: paths.stage_paths(engine_settings),
            lambda: covers.commit_covers(engine_settings),
            lambda: genres.resolve_genres(engine_settings, client=_OneArtistTags()),
        ):
            with pytest.raises(LedgerBusyError, match="busy: another TagMend process or call"):
                call()

    [change] = staging.diff_tags(engine_settings)
    assert change.target["genre"] == ["Electronic"]
    assert commits.list_commits(engine_settings) == []
    assert "genre" not in read_tags(track).tags


def test_read_only_calls_and_dry_runs_run_while_another_handle_holds_the_lock(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _track(engine_settings, music_dir)

    with _held_by_another_handle(engine_settings.db_path):
        changes = staging.diff_tags(engine_settings)
        report = mismatch.detect_mismatches(engine_settings)
        resolved = genres.resolve_genres(engine_settings, dry_run=True, client=_OneArtistTags())

    assert changes == []
    assert report is not None
    assert resolved.staged_files == 1
    assert staging.diff_tags(engine_settings) == []


def test_one_thread_re_enters_the_lock_and_another_thread_is_refused(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _, file_id = _track(engine_settings, music_dir)
    refusals: list[LedgerBusyError] = []
    seen_elsewhere: list[bool] = []

    def other_thread() -> None:
        seen_elsewhere.append(ledger_lock.held_elsewhere(engine_settings.db_path))
        try:
            with ledger_lock.mutation_lock(engine_settings):
                pass
        except LedgerBusyError as exc:
            refusals.append(exc)

    with ledger_lock.mutation_lock(engine_settings), ledger_lock.mutation_lock(engine_settings):
        staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["House"]})
        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join()
        held_here = ledger_lock.held_elsewhere(engine_settings.db_path)

    assert len(refusals) == 1
    assert (seen_elsewhere, held_here) == ([True], False)
    assert ledger_lock.held_elsewhere(engine_settings.db_path) is False
    assert [change.target["genre"] for change in staging.diff_tags(engine_settings)] == [["House"]]


def _fail_inside_the_lock(settings: Settings) -> None:
    with ledger_lock.mutation_lock(settings):
        message = "boom"
        raise RuntimeError(message)


def test_the_lock_is_released_after_an_exception(engine_settings: Settings) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        _fail_inside_the_lock(engine_settings)
    with pytest.raises(ValueError, match="unknown file_id=999"):
        staging.unstage_tags(engine_settings, file_id=999)

    assert ledger_lock._HOLDS == {}
    with _held_by_another_handle(engine_settings.db_path):
        assert ledger_lock.held_elsewhere(engine_settings.db_path) is True


def test_check_health_reports_an_applying_commit_as_running_while_the_lock_is_held(
    engine_settings: Settings,
) -> None:
    connection = db.connect(engine_settings.db_path)
    try:
        schema.apply_schema(connection)
        commit_id = commits.create_commit(
            connection, origin="manual", message=None, now="2026-10-03T00:00:00+00:00"
        )
        connection.commit()
    finally:
        connection.close()

    with _held_by_another_handle(engine_settings.db_path):
        running = health._check_interrupted_commits(engine_settings.db_path)
    interrupted = health._check_interrupted_commits(engine_settings.db_path)

    assert running.ok is True
    assert running.detail == (
        f"1 commit(s) ({commit_id}) running in another TagMend process or call"
    )
    assert "interrupted" in interrupted.detail
    assert "commit_covers" in interrupted.detail


def test_a_mutating_mcp_tool_returns_the_busy_envelope(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))

    with _held_by_another_handle(load_settings().db_path):
        refused = mcp_server.commit_tags()
        listed = mcp_server.diff_tags()

    assert (refused["ok"], refused["error_type"]) == (False, "LedgerBusyError")
    assert listed == {"ok": True, "changes": []}
