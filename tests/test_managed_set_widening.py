"""Stage, commit and revert across a managed-set widening (set 3 ledgers meeting set 4).

A revision snapshot covers only the fields its own managed set governed. These tests build a
file whose revisions a set-3 build wrote, then prove that every write path first observes the
newer ``artists`` field, so a commit diff is exact and a revert restores the field. Real audio
files from ``make_track`` and a temp ledger from ``engine_settings``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from conftest import make_track
from tagmend.engine import commits, staging, store, versioning
from tagmend.engine.db import connect
from tagmend.engine.library import scan_library
from tagmend.engine.schema import apply_schema
from tagmend.engine.tags import MANAGED_SETS, read_tags, write_managed_tags

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from tagmend.config import Settings

_FORMATS = [".mp3", ".flac", ".m4a", ".ogg"]
_NOW = "2026-06-02T00:00:00+00:00"
_LEGACY_SET = 3


class _Crash(BaseException):
    """A process death: no ``except`` clause in the engine catches it."""


def _crash_after_write(monkeypatch: pytest.MonkeyPatch) -> None:
    def write_then_die(
        path: Path, tags: dict[str, list[str]], *, droppable_frames: frozenset[str] = frozenset()
    ) -> None:
        write_managed_tags(path, tags, droppable_frames=droppable_frames)
        raise _Crash

    monkeypatch.setattr(versioning, "write_managed_tags", write_then_die)


def _file_id(settings: Settings, track: Path) -> int:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file(conn, str(track.parent), track.name)
        assert row is not None
        return row.id
    finally:
        conn.close()


def _history(settings: Settings, file_id: int) -> list[tuple[int, str, int, object]]:
    """Each revision as (version, origin, managed set, diff)."""
    return [
        (r.version, r.origin, r.managed_set, r.diff)
        for r in versioning.history_tags(settings, file_id)
    ]


def _legacy_subset(
    tags: dict[str, list[str]],
    managed_set: int = _LEGACY_SET,
) -> dict[str, list[str]]:
    return {key: values for key, values in tags.items() if key in MANAGED_SETS[managed_set]}


def _insert_legacy(  # noqa: PLR0913 - one revision row, every column spelled out
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    file_id: int,
    version: int,
    origin: str,
    managed_tags: dict[str, list[str]],
    diff: dict[str, dict[str, list[str]]],
    commit_id: int | None = None,
    managed_set: int = _LEGACY_SET,
) -> None:
    """Append a revision as an older build wrote it: its stamp, a snapshot of its fields."""
    with monkeypatch.context() as patch:
        patch.setattr(store, "MANAGED_SET_VERSION", managed_set)
        store.insert_revision(
            conn,
            file_id=file_id,
            version=version,
            origin=origin,
            managed_tags=_legacy_subset(managed_tags, managed_set),
            diff=diff,
            now=_NOW,
            commit_id=commit_id,
        )


def _legacy_baseline(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    track: Path,
    managed_set: int = _LEGACY_SET,
) -> int:
    """Scan *track*, give it a version 0 stamped *managed_set* and return its file id."""
    scan_library(settings)
    file_id = _file_id(settings, track)
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        _insert_legacy(
            conn,
            monkeypatch,
            file_id=file_id,
            version=0,
            origin="scan",
            managed_tags=read_tags(track).tags,
            diff={},
            managed_set=managed_set,
        )
        conn.commit()
    finally:
        conn.close()
    return file_id


def _legacy_commit(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    track: Path,
    changes: dict[str, list[str]],
) -> int:
    """Write *changes* to disk and record them as version 1 of a set-3 commit."""
    file_id = _file_id(settings, track)
    before = versioning.managed_subset(read_tags(track).tags)
    after = {**before, **changes}
    write_managed_tags(track, after)
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        commit_id = commits.create_commit(conn, origin="manual", message=None, now=_NOW)
        _insert_legacy(
            conn,
            monkeypatch,
            file_id=file_id,
            version=1,
            origin="manual",
            managed_tags=after,
            diff=versioning.compute_diff(_legacy_subset(before), _legacy_subset(after)),
            commit_id=commit_id,
        )
        commits.set_commit_status(conn, commit_id, "applied")
        conn.commit()
    finally:
        conn.close()
    return commit_id


def _staged_target(settings: Settings, file_id: int) -> dict[str, list[str]]:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        staged = store.get_staged_tag(conn, file_id)
        assert staged is not None
        return staged.managed_tags
    finally:
        conn.close()


def _outcome_status(result: commits.RevertCommitResult, file_id: int) -> str:
    return next(o.status for o in result.outcomes if o.file_id == file_id)


@pytest.mark.parametrize("suffix", _FORMATS)
def test_commit_over_an_older_set_records_the_exact_diff_and_reverts(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    track = make_track(music_dir / f"t{suffix}", {"genre": ["Rock"], "artists": ["Bryan El", "B"]})
    file_id = _legacy_baseline(engine_settings, monkeypatch, track)

    staging.stage_tags(engine_settings, file_id=file_id, tags={"artists": ["Bryan EL", "B"]})
    result = staging.commit_tags(engine_settings)

    assert result.commit_id is not None
    assert _history(engine_settings, file_id) == [
        (0, "scan", 3, {}),
        (1, "scan", 4, {}),
        (2, "manual", 4, {"artists": {"from": ["Bryan El", "B"], "to": ["Bryan EL", "B"]}}),
    ]
    reverted = versioning.revert_commit(engine_settings, result.commit_id)
    assert reverted.reverted == 1
    assert read_tags(track).tags["artists"] == ["Bryan El", "B"]


def test_rebaseline_diff_holds_only_drift_on_the_older_set_fields(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Rock"], "artists": ["A", "B"]})
    file_id = _legacy_baseline(engine_settings, monkeypatch, track)
    write_managed_tags(track, {"genre": ["Jazz"], "artists": ["A", "B"]})  # an external edit

    staging.stage_tags(engine_settings, file_id=file_id, tags={"artists": ["A", "C"]})

    assert _history(engine_settings, file_id) == [
        (0, "scan", 3, {}),
        (1, "scan", 4, {"genre": {"from": ["Rock"], "to": ["Jazz"]}}),
    ]


def test_batch_stage_rebaselines_an_older_set_file(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Rock"], "artists": ["A", "B"]})
    file_id = _legacy_baseline(engine_settings, monkeypatch, track)

    staging.stage_tags_batch(engine_settings, entries=[(file_id, {"artists": ["A", "C"]})])

    assert _history(engine_settings, file_id) == [(0, "scan", 3, {}), (1, "scan", 4, {})]


@pytest.mark.parametrize("suffix", [".mp3", ".flac"])
def test_crash_reapply_over_an_older_set_keeps_the_change_in_the_commit(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    track = make_track(music_dir / f"t{suffix}", {"genre": ["Rock"], "artists": ["A", "B"]})
    file_id = _legacy_baseline(engine_settings, monkeypatch, track)
    staging.stage_tags(engine_settings, file_id=file_id, tags={"artists": ["A", "C"]})
    # The crashed run wrote the target to disk and died before its DB commit.
    write_managed_tags(track, _staged_target(engine_settings, file_id))

    result = staging.commit_tags(engine_settings)

    assert result.committed == 1
    history = _history(engine_settings, file_id)
    assert history[-1] == (2, "manual", 4, {"artists": {"from": ["A", "B"], "to": ["A", "C"]}})
    assert [diff for _, origin, _, diff in history if origin == "scan"] == [{}, {}]


def test_commit_refuses_a_row_staged_before_the_current_set(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Rock"], "artists": ["A", "B"]})
    file_id = _legacy_baseline(engine_settings, monkeypatch, track)
    # A set-3 build staged this row, so its target lacks the artists list.
    base = track.stat()
    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        store.upsert_staged_tag(
            conn,
            file_id=file_id,
            managed_tags={"genre": ["Jazz"]},
            origin="manual",
            now=_NOW,
            base_size_bytes=base.st_size,
            base_mtime_ns=base.st_mtime_ns,
            changed_fields=["genre"],
        )
        conn.commit()
    finally:
        conn.close()

    result = staging.commit_tags(engine_settings)

    assert result.errors == 1
    detail = result.outcomes[0].detail
    assert detail is not None
    assert "staged before managed set 4, unstage and stage it again" in detail
    assert read_tags(track).tags["artists"] == ["A", "B"]
    assert read_tags(track).tags["genre"] == ["Rock"]
    assert _staged_target(engine_settings, file_id) == {"genre": ["Jazz"]}


def test_noop_revert_blocks_revert_commit_of_the_earlier_commit(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Rock"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, track)
    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Jazz"]})
    commit_id = staging.commit_tags(engine_settings).commit_id
    assert commit_id is not None
    assert versioning.revert_tags(engine_settings, file_id, 1).status == "noop"

    result = versioning.revert_commit(engine_settings, commit_id)

    assert _outcome_status(result, file_id) == "skipped_later_changes"
    assert read_tags(track).tags["genre"] == ["Jazz"]


def test_drift_free_rebaseline_does_not_block_revert_commit(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Rock"], "artists": ["A", "B"]})
    file_id = _legacy_baseline(engine_settings, monkeypatch, track)
    commit_id = _legacy_commit(engine_settings, monkeypatch, track, {"genre": ["Jazz"]})
    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Blues"]})
    staging.unstage_tags(engine_settings, file_id=file_id)
    assert _history(engine_settings, file_id)[-1] == (2, "scan", 4, {})

    result = versioning.revert_commit(engine_settings, commit_id)

    assert _outcome_status(result, file_id) == "reverted"
    assert read_tags(track).tags["genre"] == ["Rock"]
    assert read_tags(track).tags["artists"] == ["A", "B"]


def test_revert_to_an_older_set_version_restores_the_newer_field_from_the_rebaseline(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Rock"], "artists": ["A", "B"]})
    file_id = _legacy_baseline(engine_settings, monkeypatch, track)
    staging.stage_tags(
        engine_settings,
        file_id=file_id,
        tags={"genre": ["Jazz"], "artists": ["A", "C"]},
    )
    staging.commit_tags(engine_settings)

    result = versioning.revert_tags(engine_settings, file_id, 0)

    assert result.status == "reverted"
    assert read_tags(track).tags["genre"] == ["Rock"]
    assert read_tags(track).tags["artists"] == ["A", "B"]


def test_revert_to_an_older_set_version_deletes_a_newer_field_the_rebaseline_saw_absent(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Rock"]})
    file_id = _legacy_baseline(engine_settings, monkeypatch, track)
    staging.stage_tags(engine_settings, file_id=file_id, tags={"artists": ["A"]})
    staging.commit_tags(engine_settings)
    assert read_tags(track).tags["artists"] == ["A"]

    result = versioning.revert_tags(engine_settings, file_id, 0)

    assert result.status == "reverted"
    assert "artists" not in read_tags(track).tags
    assert read_tags(track).tags["genre"] == ["Rock"]


def test_revert_keeps_the_current_value_when_a_commit_first_governs_the_field(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Rock"], "artists": ["A", "B"]})
    file_id = _legacy_baseline(engine_settings, monkeypatch, track)
    # A set-4 build before this fix committed straight over the set-3 baseline, so nothing
    # observed the artists list before the commit wrote it.
    write_managed_tags(track, {"genre": ["Jazz"], "artists": ["A", "C"]})
    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        versioning.append_revision(
            conn,
            file_id,
            managed_tags=read_tags(track).tags,
            origin="manual",
            now=_NOW,
        )
        conn.commit()
    finally:
        conn.close()

    versioning.revert_tags(engine_settings, file_id, 0)

    assert read_tags(track).tags["genre"] == ["Rock"]
    assert read_tags(track).tags["artists"] == ["A", "C"]


def test_revert_to_the_latest_stale_set_version_is_a_noop(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Rock"], "artists": ["A", "B"]})
    file_id = _legacy_baseline(engine_settings, monkeypatch, track)
    _legacy_commit(engine_settings, monkeypatch, track, {"genre": ["Jazz"]})

    preview = versioning.revert_tags(engine_settings, file_id, 1, dry_run=True)
    result = versioning.revert_tags(engine_settings, file_id, 1)

    assert preview.status == "noop"
    assert result.status == "noop"
    assert _history(engine_settings, file_id)[2:] == [(2, "scan", 4, {}), (3, "revert", 4, {})]
    assert read_tags(track).tags["artists"] == ["A", "B"]


@pytest.mark.parametrize("entry_point", ["revert_commit", "revert_tags"])
def test_revert_crashed_after_its_write_still_reverts_on_rerun(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_point: str,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Rock"], "artists": ["A", "B"]})
    file_id = _legacy_baseline(engine_settings, monkeypatch, track)
    commit_id = _legacy_commit(engine_settings, monkeypatch, track, {"genre": ["Jazz"]})

    def revert() -> str:
        if entry_point == "revert_commit":
            return _outcome_status(versioning.revert_commit(engine_settings, commit_id), file_id)
        return versioning.revert_tags(engine_settings, file_id, 0).status

    with monkeypatch.context() as patch:
        _crash_after_write(patch)
        with pytest.raises(_Crash):
            revert()
    assert read_tags(track).tags["genre"] == ["Rock"]  # the write landed, the revert row did not

    assert revert() == "reverted"
    assert _history(engine_settings, file_id)[2:] == [
        (2, "scan", 4, {}),
        (3, "revert", 4, {"genre": {"from": ["Jazz"], "to": ["Rock"]}}),
    ]


def test_revert_keeps_the_current_value_when_a_commit_governs_the_field_before_an_observation(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Set 2 never governed releasecountry. A set-3 commit wrote XW, a set-4 re-baseline then
    # observed that output, and a set-4 commit wrote GB. XW was never the value before v0.
    track = make_track(music_dir / "t.flac", {"genre": ["Rock"], "releasecountry": ["US"]})
    file_id = _legacy_baseline(engine_settings, monkeypatch, track, managed_set=2)
    _legacy_commit(engine_settings, monkeypatch, track, {"releasecountry": ["XW"]})
    staging.stage_tags(engine_settings, file_id=file_id, tags={"releasecountry": ["GB"]})
    staging.commit_tags(engine_settings)

    versioning.revert_tags(engine_settings, file_id, 0)

    assert read_tags(track).tags["releasecountry"] == ["GB"]


def test_commit_completes_a_reapply_staged_by_an_older_build(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Rock"], "artists": ["A", "B"]})
    file_id = _legacy_baseline(engine_settings, monkeypatch, track)
    # A set-3 build staged a genre change, wrote it and died before its DB commit. Its write
    # never touched the artists list, which it did not manage.
    base = track.stat()
    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        store.upsert_staged_tag(
            conn,
            file_id=file_id,
            managed_tags={"genre": ["Jazz"]},
            origin="manual",
            now=_NOW,
            base_size_bytes=base.st_size,
            base_mtime_ns=base.st_mtime_ns,
            changed_fields=["genre"],
        )
        conn.commit()
    finally:
        conn.close()
    write_managed_tags(track, {"genre": ["Jazz"], "artists": ["A", "B"]})

    result = staging.commit_tags(engine_settings)

    assert result.committed == 1
    assert read_tags(track).tags["artists"] == ["A", "B"]
    version, origin, managed_set, diff = _history(engine_settings, file_id)[-1]
    assert (version, origin, managed_set) == (1, "manual", 4)
    assert isinstance(diff, dict)
    assert diff["genre"] == {"from": ["Rock"], "to": ["Jazz"]}
