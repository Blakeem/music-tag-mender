"""Integration tests for commit-level revert (:func:`tagmend.engine.versioning.revert_commit`).

These use real temp audio files (the silent templates) and a real temp ledger via the
``engine_settings`` fixture, so they prove the full group-undo loop end to end: an entire
commit's files are restored to their pre-commit state on disk, each gets a new
``origin='revert'`` revision under ONE fresh revert commit, and the rollback is itself a
tracked, revertible commit. Skip-later-changes, missing files, per-file disk failures,
the dry run, and the guards are all exercised.

Helpers mirror :mod:`tests.test_staging_integration` (``_file_id``, the small conn-owning
wrappers); they open the isolated ledger the ``engine_settings`` fixture points at and
never touch the real ``music/`` folder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from conftest import make_track
from tagmend.engine import commits, staging, store, versioning
from tagmend.engine.db import connect
from tagmend.engine.library import scan_library
from tagmend.engine.schema import apply_schema
from tagmend.engine.tags import read_tags, write_managed_tags

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

_NOW = "2026-06-02T00:00:00+00:00"


# --- conn-owning read helpers (same shape as test_staging_integration) ---------------


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


def _live_tags(settings: Settings, file_id: int) -> dict[str, list[str]]:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return store.get_tags(conn, file_id)
    finally:
        conn.close()


def _commit(settings: Settings, commit_id: int) -> commits.Commit | None:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return commits.get_commit(conn, commit_id)
    finally:
        conn.close()


def _commit_count(settings: Settings) -> int:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return len(commits.list_commits(conn))
    finally:
        conn.close()


def _outcome(result: versioning.RevertCommitResult, file_id: int) -> versioning.FileRevertOutcome:
    """Pluck the per-file outcome for *file_id* out of a result (one per file)."""
    return next(o for o in result.outcomes if o.file_id == file_id)


def _stage_and_commit(
    settings: Settings,
    targets: dict[int, dict[str, list[str]]],
    *,
    message: str | None = None,
) -> int:
    """Stage each ``file_id -> managed_tags`` target and commit them into one commit."""
    for file_id, managed_tags in targets.items():
        staging.stage_tags(settings, file_id=file_id, managed_tags=managed_tags)
    result = staging.commit_tags(settings, message=message)
    assert result.commit_id is not None
    return result.commit_id


# --- scenario 1: happy path ----------------------------------------------------------


def test_revert_commit_restores_all_files(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    a = make_track(music_dir / "a.mp3", {"genre": ["Electronic"], "title": ["A"]})
    b = make_track(music_dir / "b.flac", {"genre": ["Rock"]})
    c = make_track(music_dir / "c.m4a", {"genre": ["Jazz"]})

    scan_library(engine_settings)
    a_id = _file_id(engine_settings, music_dir, a.name)
    b_id = _file_id(engine_settings, music_dir, b.name)
    c_id = _file_id(engine_settings, music_dir, c.name)

    target = _stage_and_commit(
        engine_settings,
        {
            a_id: {"genre": ["Synthwave"]},
            b_id: {"genre": ["Metal"]},
            c_id: {"genre": ["Fusion"]},
        },
        message="reclassify",
    )

    result = versioning.revert_commit(engine_settings, target)

    assert result.reverted == 3
    assert result.skipped == 0
    assert result.missing == 0
    assert result.errors == 0
    assert result.dry_run is False
    assert result.reverted_from == target
    assert result.commit_id is not None
    assert result.commit_id != target

    # Disk restored to the pre-commit genres; the unmanaged title is untouched.
    a_tags = read_tags(a).tags
    assert a_tags["genre"] == ["Electronic"]
    assert a_tags.get("title") == ["A"]
    assert read_tags(b).tags["genre"] == ["Rock"]
    assert read_tags(c).tags["genre"] == ["Jazz"]

    # Live file_tags snapshot refreshed to match disk.
    assert _live_tags(engine_settings, a_id)["genre"] == ["Electronic"]
    assert _live_tags(engine_settings, b_id)["genre"] == ["Rock"]
    assert _live_tags(engine_settings, c_id)["genre"] == ["Jazz"]

    # Each file gets a new origin='revert' revision under the NEW commit, reverting to
    # version 0 (the commit created version 1, so pre-commit version is 0).
    for file_id in (a_id, b_id, c_id):
        revisions = _revisions(engine_settings, file_id)
        assert [r.version for r in revisions] == [0, 1, 2]
        latest = revisions[-1]
        assert latest.origin == "revert"
        assert latest.reverted_from == 0
        assert latest.commit_id == result.commit_id

    # The new commit row: origin='revert', reverted_from=target, applied.
    commit = _commit(engine_settings, result.commit_id)
    assert commit is not None
    assert commit.origin == "revert"
    assert commit.reverted_from == target
    assert commit.status == "applied"


# --- scenario 2: skip later changes --------------------------------------------------


def test_revert_commit_skips_files_changed_later(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    a = make_track(music_dir / "a.mp3", {"genre": ["Electronic"]})
    b = make_track(music_dir / "b.flac", {"genre": ["Rock"]})

    scan_library(engine_settings)
    a_id = _file_id(engine_settings, music_dir, a.name)
    b_id = _file_id(engine_settings, music_dir, b.name)

    target = _stage_and_commit(
        engine_settings,
        {a_id: {"genre": ["Synthwave"]}, b_id: {"genre": ["Metal"]}},
    )

    # A second commit touches only file A, so A has a later revision than the target.
    _stage_and_commit(engine_settings, {a_id: {"genre": ["Darksynth"]}})

    result = versioning.revert_commit(engine_settings, target)

    assert result.reverted == 1
    assert result.skipped == 1
    assert _outcome(result, a_id).status == "skipped_later_changes"
    assert _outcome(result, b_id).status == "reverted"

    # A is left at its later state on disk; B is rolled back to its pre-commit genre.
    assert read_tags(a).tags["genre"] == ["Darksynth"]
    assert read_tags(b).tags["genre"] == ["Rock"]


# --- scenario 3: missing file --------------------------------------------------------


def test_revert_commit_reports_missing_file(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    a = make_track(music_dir / "a.mp3", {"genre": ["Electronic"]})
    b = make_track(music_dir / "b.flac", {"genre": ["Rock"]})

    scan_library(engine_settings)
    a_id = _file_id(engine_settings, music_dir, a.name)
    b_id = _file_id(engine_settings, music_dir, b.name)

    target = _stage_and_commit(
        engine_settings,
        {a_id: {"genre": ["Synthwave"]}, b_id: {"genre": ["Metal"]}},
    )

    a.unlink()
    scan_library(engine_settings)  # flags A missing

    result = versioning.revert_commit(engine_settings, target)

    assert result.missing == 1
    assert result.reverted == 1
    assert _outcome(result, a_id).status == "missing"
    assert _outcome(result, b_id).status == "reverted"
    assert read_tags(b).tags["genre"] == ["Rock"]


# --- scenario 4: revert a revert -----------------------------------------------------


def test_revert_of_a_revert_restores_post_commit_state(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Electronic"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    commit_c = _stage_and_commit(engine_settings, {file_id: {"genre": ["Synthwave"]}})

    revert_r = versioning.revert_commit(engine_settings, commit_c)
    assert revert_r.commit_id is not None
    assert read_tags(track).tags["genre"] == ["Electronic"]  # back to pre-C

    # Reverting the revert commit R rolls forward to the post-C state.
    second = versioning.revert_commit(engine_settings, revert_r.commit_id)
    assert second.reverted == 1
    assert second.commit_id is not None
    assert read_tags(track).tags["genre"] == ["Synthwave"]  # post-C restored

    # Commit chain: R.reverted_from == C, the second revert's reverted_from == R.
    r_commit = _commit(engine_settings, revert_r.commit_id)
    assert r_commit is not None
    assert r_commit.reverted_from == commit_c
    second_commit = _commit(engine_settings, second.commit_id)
    assert second_commit is not None
    assert second_commit.reverted_from == revert_r.commit_id


# --- scenario 5: reverting version-1 commit restores the v0 baseline -----------------


def test_revert_commit_that_created_version_one_restores_baseline(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"], "title": ["Orig"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    target = _stage_and_commit(engine_settings, {file_id: {"genre": ["Synthwave"]}})

    result = versioning.revert_commit(engine_settings, target)

    assert result.reverted == 1
    assert _outcome(result, file_id).target_version == 0  # the v0 baseline
    on_disk = read_tags(track).tags
    assert on_disk["genre"] == ["Electronic"]  # original baseline genre restored
    assert on_disk.get("title") == ["Orig"]


# --- scenario 6: zero revertable -> no new commit ------------------------------------


def test_revert_commit_zero_revertable_creates_no_commit(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    a = make_track(music_dir / "a.mp3", {"genre": ["Electronic"]})
    b = make_track(music_dir / "b.flac", {"genre": ["Rock"]})

    scan_library(engine_settings)
    a_id = _file_id(engine_settings, music_dir, a.name)
    b_id = _file_id(engine_settings, music_dir, b.name)

    target = _stage_and_commit(
        engine_settings,
        {a_id: {"genre": ["Synthwave"]}, b_id: {"genre": ["Metal"]}},
    )
    # Every file changes again after the target, so nothing in it is revertable.
    _stage_and_commit(
        engine_settings,
        {a_id: {"genre": ["Darksynth"]}, b_id: {"genre": ["Doom"]}},
    )

    before = _commit_count(engine_settings)
    result = versioning.revert_commit(engine_settings, target)
    after = _commit_count(engine_settings)

    assert result.reverted == 0
    assert result.skipped == 2
    assert result.commit_id is None  # nothing revertable -> no new commit row
    assert after == before  # no commits row created
    # Disk untouched (still at the later state).
    assert read_tags(a).tags["genre"] == ["Darksynth"]
    assert read_tags(b).tags["genre"] == ["Doom"]


# --- scenario 7: guards --------------------------------------------------------------


def test_revert_commit_unknown_id_raises(engine_settings: Settings) -> None:
    with pytest.raises(ValueError, match="unknown commit_id"):
        versioning.revert_commit(engine_settings, 9999)


def test_revert_commit_applying_target_raises(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    target = _stage_and_commit(engine_settings, {file_id: {"genre": ["Synthwave"]}})

    # Force the target back into the in-flight 'applying' state.
    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        commits.set_commit_status(conn, target, "applying")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="still applying"):
        versioning.revert_commit(engine_settings, target)


def test_revert_commit_interrupted_target_is_allowed(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    target = _stage_and_commit(engine_settings, {file_id: {"genre": ["Synthwave"]}})

    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        commits.set_commit_status(conn, target, "interrupted")
        conn.commit()
    finally:
        conn.close()

    # An interrupted target reverts whatever it durably committed.
    result = versioning.revert_commit(engine_settings, target)
    assert result.reverted == 1
    assert read_tags(track).tags["genre"] == ["Electronic"]


def test_revert_commit_with_dirty_staging_raises(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    a = make_track(music_dir / "a.mp3", {"genre": ["Electronic"]})
    b = make_track(music_dir / "b.flac", {"genre": ["Rock"]})

    scan_library(engine_settings)
    a_id = _file_id(engine_settings, music_dir, a.name)
    b_id = _file_id(engine_settings, music_dir, b.name)
    target = _stage_and_commit(engine_settings, {a_id: {"genre": ["Synthwave"]}})

    # Leave a pending staged change for a different file -> the staging area is dirty.
    staging.stage_tags(engine_settings, file_id=b_id, managed_tags={"genre": ["Metal"]})

    with pytest.raises(ValueError, match="staging area is not empty"):
        versioning.revert_commit(engine_settings, target)


# --- scenario 8: dry run -------------------------------------------------------------


def test_revert_commit_dry_run_changes_nothing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    a = make_track(music_dir / "a.mp3", {"genre": ["Electronic"]})
    b = make_track(music_dir / "b.flac", {"genre": ["Rock"]})

    scan_library(engine_settings)
    a_id = _file_id(engine_settings, music_dir, a.name)
    b_id = _file_id(engine_settings, music_dir, b.name)
    target = _stage_and_commit(
        engine_settings,
        {a_id: {"genre": ["Synthwave"]}, b_id: {"genre": ["Metal"]}},
    )
    # Change A again so the preview classifies a mix of statuses.
    _stage_and_commit(engine_settings, {a_id: {"genre": ["Darksynth"]}})

    before = _commit_count(engine_settings)
    result = versioning.revert_commit(engine_settings, target, dry_run=True)
    after = _commit_count(engine_settings)

    assert result.dry_run is True
    assert result.commit_id is None
    assert result.reverted == 1  # would-be reverted
    assert result.noop == 0
    assert result.skipped == 1
    assert _outcome(result, b_id).status == "reverted"
    assert _outcome(result, b_id).target_version == 0
    assert _outcome(result, b_id).new_version is None
    assert _outcome(result, a_id).status == "skipped_later_changes"

    assert after == before  # no new commit row
    # Disk untouched by the preview.
    assert read_tags(a).tags["genre"] == ["Darksynth"]
    assert read_tags(b).tags["genre"] == ["Metal"]
    # No revert revision was appended.
    assert [r.version for r in _revisions(engine_settings, b_id)] == [0, 1]


# --- scenario 8b: a revert that would change nothing is reported as noop -------------


def _stamp_managed_set(settings: Settings, file_id: int, version: int, managed_set: int) -> None:
    """Force one revision's managed-set marker, fabricating a capture under an older set."""
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        conn.execute(
            "UPDATE tag_revisions SET managed_set = ? WHERE file_id = ? AND version = ?",
            (managed_set, file_id, version),
        )
        conn.commit()
    finally:
        conn.close()


def _commit_adding_title_over_a_v1_baseline(settings: Settings, file_id: int) -> int:
    """Commit a widened-field change whose target baseline predates the widening.

    Reverting it can only preserve ``title`` (managed set 1 never governed it), so the
    revert moves nothing - the exact shape that used to report a false ``reverted``.
    """
    target = _stage_and_commit(settings, {file_id: {"genre": ["Electronic"], "title": ["Added"]}})
    _stamp_managed_set(settings, file_id, version=0, managed_set=1)
    return target


def test_revert_commit_dry_run_reports_noop(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    target = _commit_adding_title_over_a_v1_baseline(engine_settings, file_id)

    result = versioning.revert_commit(engine_settings, target, dry_run=True)

    assert _outcome(result, file_id).status == "noop"
    assert result.noop == 1
    assert result.reverted == 0  # a preview promising 1 revert would be the defect


def test_revert_commit_noop_still_appends_the_revision(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    target = _commit_adding_title_over_a_v1_baseline(engine_settings, file_id)

    result = versioning.revert_commit(engine_settings, target)

    assert result.noop == 1
    assert result.reverted == 0
    outcome = _outcome(result, file_id)
    assert outcome.status == "noop"
    assert outcome.new_version == 2  # revert is always audited, even when nothing moved
    assert [r.version for r in _revisions(engine_settings, file_id)] == [0, 1, 2]
    assert read_tags(track).tags.get("title") == ["Added"]  # untouched on disk


# --- scenario 9: crash sim (per-file disk failure, then resume-free re-run) -----------


def test_revert_commit_per_file_failure_then_rerun(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = make_track(music_dir / "a.mp3", {"genre": ["Electronic"]})
    b = make_track(music_dir / "b.flac", {"genre": ["Rock"]})

    scan_library(engine_settings)
    a_id = _file_id(engine_settings, music_dir, a.name)
    b_id = _file_id(engine_settings, music_dir, b.name)
    target = _stage_and_commit(
        engine_settings,
        {a_id: {"genre": ["Synthwave"]}, b_id: {"genre": ["Metal"]}},
    )

    def flaky_write(path: Path, managed_tags: dict[str, list[str]]) -> None:
        # Fail the write for the SECOND file (B) only; A reverts durably.
        if path.name == b.name:
            message = "simulated disk failure"
            raise OSError(message)
        write_managed_tags(path, managed_tags)

    monkeypatch.setattr(versioning, "write_managed_tags", flaky_write)
    result = versioning.revert_commit(engine_settings, target)

    assert result.reverted == 1
    assert result.errors == 1
    assert _outcome(result, a_id).status == "reverted"
    b_outcome = _outcome(result, b_id)
    assert b_outcome.status == "error"
    assert b_outcome.detail is not None
    assert "simulated disk failure" in b_outcome.detail

    # A is durably reverted on disk; B is untouched (still the committed value).
    assert read_tags(a).tags["genre"] == ["Electronic"]
    assert read_tags(b).tags["genre"] == ["Metal"]
    # B got no new revision from the failed run.
    assert [r.version for r in _revisions(engine_settings, b_id)] == [0, 1]

    # Resume-free: re-run without the patch. A now has a later revision (so it is
    # skipped), B is reverted this time.
    monkeypatch.undo()
    second = versioning.revert_commit(engine_settings, target)

    assert _outcome(second, a_id).status == "skipped_later_changes"
    assert _outcome(second, b_id).status == "reverted"
    assert read_tags(b).tags["genre"] == ["Rock"]
    assert read_tags(a).tags["genre"] == ["Electronic"]


# --- scenario 10: single-file revert guard against a staged change -------------------


def test_single_file_revert_blocked_by_staged_change(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    # Commit one change to give the file a real revision to revert to.
    _stage_and_commit(engine_settings, {file_id: {"genre": ["Synthwave"]}})

    # Now stage a fresh change and try a single-file revert -> refused.
    staging.stage_tags(engine_settings, file_id=file_id, managed_tags={"genre": ["Darksynth"]})

    with pytest.raises(ValueError, match="staged change"):
        versioning.revert(engine_settings, file_id, 0)
