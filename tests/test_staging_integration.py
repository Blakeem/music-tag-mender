"""Integration tests for the staging + commit path (:mod:`tagmend.engine.staging`).

These use real temp audio files (the silent templates) and a real temp ledger via the
``engine_settings`` fixture, so they prove the full loop end to end: a staged change is
written to disk on commit, a revision is appended under a shared commit id, the staged
row is cleared, and an interrupted commit is recovered by simply committing again (the
resume-free model — there is no ``resume`` call).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from conftest import make_track
from tagmend.engine import commits, staging, store, versioning
from tagmend.engine.db import connect
from tagmend.engine.library import ScanMode, scan_library
from tagmend.engine.schema import apply_schema
from tagmend.engine.tags import read_tags, write_managed_tags

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

_FORMATS = [".mp3", ".flac", ".m4a", ".ogg"]
_NOW = "2026-06-02T00:00:00+00:00"


def _file_id(settings: Settings, folder: Path, filename: str) -> int:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        return row.id
    finally:
        conn.close()


def _revisions(settings: Settings, file_id: int) -> list[store.Revision]:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return store.get_revisions(conn, file_id)
    finally:
        conn.close()


def _revision(settings: Settings, file_id: int, version: int) -> store.Revision | None:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return store.get_revision(conn, file_id, version)
    finally:
        conn.close()


def _staged(settings: Settings, file_id: int) -> store.StagedTag | None:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return store.get_staged_tag(conn, file_id)
    finally:
        conn.close()


def _commit_status(settings: Settings, commit_id: int) -> str | None:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        commit = commits.get_commit(conn, commit_id)
        return None if commit is None else commit.status
    finally:
        conn.close()


def _is_missing(settings: Settings, file_id: int) -> bool:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file_by_id(conn, file_id)
        assert row is not None
        return row.is_missing
    finally:
        conn.close()


@pytest.mark.parametrize("suffix", _FORMATS)
def test_commit_applies_and_records(
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

    staging.stage_tags(engine_settings, file_id=file_id, managed_tags={"genre": ["Synthwave"]})
    assert len(staging.diff_tags(engine_settings)) == 1

    result = staging.commit_tags(engine_settings, message="reclassify")

    assert result.commit_id is not None
    assert result.committed == 1
    assert result.noop == 0
    assert result.missing == 0

    # The edit really changed the bytes on disk; the unmanaged tag is untouched.
    on_disk = read_tags(track).tags
    assert on_disk["genre"] == ["Synthwave"]
    assert on_disk.get("title") == ["Song"]

    revisions = _revisions(engine_settings, file_id)
    assert [r.version for r in revisions] == [0, 1]
    assert revisions[0].commit_id is None  # baseline precedes any commit
    assert revisions[1].commit_id == result.commit_id
    assert revisions[1].origin == "manual"

    assert _staged(engine_settings, file_id) is None  # staged row cleared
    assert _commit_status(engine_settings, result.commit_id) == "applied"


def test_commit_noop_when_target_equals_current(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Electronic"]})

    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    staging.stage_tags(engine_settings, file_id=file_id, managed_tags={"genre": ["Electronic"]})
    result = staging.commit_tags(engine_settings)

    assert result.noop == 1
    assert result.committed == 0
    # Only the baseline is recorded — an unchanged commit adds no new revision.
    assert [r.version for r in _revisions(engine_settings, file_id)] == [0]
    assert _staged(engine_settings, file_id) is None


def test_commit_missing_file_is_flagged_and_dropped(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})

    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    staging.stage_tags(engine_settings, file_id=file_id, managed_tags={"genre": ["Synthwave"]})
    track.unlink()  # disappears after staging, before commit

    result = staging.commit_tags(engine_settings)

    assert result.missing == 1
    assert result.committed == 0
    assert result.missing_files[0].file_id == file_id
    assert result.to_dict()["missing_files"] == [{"file_id": file_id, "path": str(track)}]
    # Only the stage-time baseline exists; nothing committed for a missing file.
    assert [r.version for r in _revisions(engine_settings, file_id)] == [0]
    assert _is_missing(engine_settings, file_id) is True
    assert _staged(engine_settings, file_id) is None  # dropped so the commit finalizes


def test_commit_groups_multiple_files_under_one_commit_id(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    a = make_track(music_dir / "a.mp3", {"genre": ["Electronic"]})
    b = make_track(music_dir / "b.flac", {"genre": ["Rock"]})

    scan_library(engine_settings)
    a_id = _file_id(engine_settings, music_dir, a.name)
    b_id = _file_id(engine_settings, music_dir, b.name)

    staging.stage_tags(engine_settings, file_id=a_id, managed_tags={"genre": ["Synthwave"]})
    staging.stage_tags(engine_settings, file_id=b_id, managed_tags={"genre": ["Metal"]})

    result = staging.commit_tags(engine_settings)

    assert result.committed == 2
    assert _revisions(engine_settings, a_id)[-1].commit_id == result.commit_id
    assert _revisions(engine_settings, b_id)[-1].commit_id == result.commit_id


def test_baseline_captured_at_stage_survives_rescan(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    """v0 is frozen at stage time, so a crash-then-rescan cannot corrupt the original.

    Regression for opus-dev C1: if the baseline were captured at *commit* time, the
    rescan below (which advances the snapshot to the half-written target) would record
    the wrong v0. Capturing at stage time keeps the true original.
    """
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})

    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    # Stage -> v0 = Electronic captured now.
    staging.stage_tags(engine_settings, file_id=file_id, managed_tags={"genre": ["Synthwave"]})

    # Simulate an interrupted commit: disk advances to the target, but nothing is
    # committed. A FULL rescan then advances the live snapshot to the half-written state.
    write_managed_tags(track, {"genre": ["Synthwave"]})
    scan_library(engine_settings, mode=ScanMode.FULL)

    # Re-stage a different target; v0 already exists so no new baseline is captured.
    staging.stage_tags(engine_settings, file_id=file_id, managed_tags={"genre": ["Darksynth"]})

    staging.commit_tags(engine_settings)

    baseline = _revision(engine_settings, file_id, 0)
    assert baseline is not None
    assert baseline.managed_tags == {"genre": ["Electronic"]}  # original preserved


def test_split_batch_recovery_under_new_commit(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    """A crash mid-commit leaves one file committed under an 'applying' commit and the
    rest staged; the next commit marks that one interrupted and sweeps the remainder into
    a brand-new commit. Built faithfully via the engine functions.
    """
    a = make_track(music_dir / "a.mp3", {"genre": ["Electronic"]})
    b = make_track(music_dir / "b.flac", {"genre": ["Rock"]})

    scan_library(engine_settings)
    a_id = _file_id(engine_settings, music_dir, a.name)
    b_id = _file_id(engine_settings, music_dir, b.name)

    staging.stage_tags(engine_settings, file_id=a_id, managed_tags={"genre": ["Synthwave"]})
    staging.stage_tags(engine_settings, file_id=b_id, managed_tags={"genre": ["Metal"]})

    # Simulate a crash: A is committed under C0 (still 'applying'); B stays staged.
    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        c0 = commits.create_commit(conn, origin="manual", message="x", now=_NOW)
        conn.commit()
        domain = staging.TagDomain()
        path_a = domain.resolve_path(conn, a_id)
        assert path_a is not None
        domain.apply_to_disk(conn, a_id, path_a, commit_id=c0, now=_NOW)
        conn.commit()  # A's revision durable under C0; A's staged row gone
    finally:
        conn.close()

    assert _commit_status(engine_settings, c0) == "applying"
    assert _staged(engine_settings, a_id) is None
    assert _staged(engine_settings, b_id) is not None

    result = staging.commit_tags(engine_settings)

    assert result.commit_id is not None
    assert result.commit_id != c0
    assert _commit_status(engine_settings, c0) == "interrupted"
    assert _commit_status(engine_settings, result.commit_id) == "applied"

    # B's latest revision belongs to the NEW commit; both files end committed on disk.
    assert _revisions(engine_settings, b_id)[-1].commit_id == result.commit_id
    assert read_tags(a).tags["genre"] == ["Synthwave"]
    assert read_tags(b).tags["genre"] == ["Metal"]
    assert _staged(engine_settings, a_id) is None
    assert _staged(engine_settings, b_id) is None


def test_commit_root_scope_limits_to_subtree(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    rock = make_track(music_dir / "rock" / "a.mp3", {"genre": ["Electronic"]})
    jazz = make_track(music_dir / "jazz" / "b.flac", {"genre": ["Rock"]})

    scan_library(engine_settings)
    rock_id = _file_id(engine_settings, music_dir / "rock", rock.name)
    jazz_id = _file_id(engine_settings, music_dir / "jazz", jazz.name)

    staging.stage_tags(engine_settings, file_id=rock_id, managed_tags={"genre": ["Synthwave"]})
    staging.stage_tags(engine_settings, file_id=jazz_id, managed_tags={"genre": ["Metal"]})

    result = staging.commit_tags(engine_settings, root=music_dir / "rock")

    assert result.committed == 1
    assert read_tags(rock).tags["genre"] == ["Synthwave"]  # in scope, committed
    assert _staged(engine_settings, rock_id) is None
    assert _staged(engine_settings, jazz_id) is not None  # out of scope, still staged
    assert read_tags(jazz).tags["genre"] == ["Rock"]  # untouched on disk


def test_diff_tags_enrichment(engine_settings: Settings, music_dir: Path) -> None:
    changed = make_track(music_dir / "c.mp3", {"genre": ["Electronic"]})
    noop = make_track(music_dir / "n.flac", {"genre": ["Rock"]})

    scan_library(engine_settings)
    changed_id = _file_id(engine_settings, music_dir, changed.name)
    noop_id = _file_id(engine_settings, music_dir, noop.name)

    staging.stage_tags(engine_settings, file_id=changed_id, managed_tags={"genre": ["Synthwave"]})
    staging.stage_tags(engine_settings, file_id=noop_id, managed_tags={"genre": ["Rock"]})

    views = {v.file_id: v for v in staging.diff_tags(engine_settings)}
    assert set(views) == {changed_id, noop_id}

    changed_view = views[changed_id]
    assert changed_view.current == {"genre": ["Electronic"]}
    assert changed_view.target == {"genre": ["Synthwave"]}
    assert changed_view.diff == {"genre": {"from": ["Electronic"], "to": ["Synthwave"]}}

    # A no-op stage still shows up, with an empty diff.
    assert views[noop_id].diff == {}
    assert views[noop_id].current == versioning.managed_subset({"genre": ["Rock"]})


def test_stage_tags_rejects_unmanaged_key(engine_settings: Settings, music_dir: Path) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})

    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    with pytest.raises(ValueError, match="non-managed"):
        staging.stage_tags(engine_settings, file_id=file_id, managed_tags={"title": ["Nope"]})


def test_stage_tags_rejects_unknown_file_id(engine_settings: Settings) -> None:
    with pytest.raises(ValueError, match="unknown file_id"):
        staging.stage_tags(engine_settings, file_id=999, managed_tags={"genre": ["X"]})


def test_stage_tags_rejects_bad_origin(engine_settings: Settings, music_dir: Path) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})

    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    with pytest.raises(ValueError, match="invalid staged origin"):
        staging.stage_tags(
            engine_settings, file_id=file_id, managed_tags={"genre": ["X"]}, origin="bogus"
        )
