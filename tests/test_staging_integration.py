"""Integration tests for the staging + commit path (:mod:`tagmend.engine.staging`).

These use real temp audio files (the silent templates) and a real temp ledger via the
``engine_settings`` fixture, so they prove the full loop end to end: a staged change is
written to disk on commit, a revision is appended under a shared commit id, the staged
row is cleared, and an interrupted commit is recovered by simply committing again (the
resume-free model — there is no ``resume`` call).
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path
from typing import TYPE_CHECKING

import mutagen
import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TYER, MakeID3v1  # type: ignore[attr-defined]

from conftest import make_track
from tagmend import mcp_server
from tagmend.config import load_settings
from tagmend.engine import artists, axis, commits, staging, store, tags, versioning
from tagmend.engine.db import connect
from tagmend.engine.library import ScanMode, scan_library
from tagmend.engine.schema import apply_schema
from tagmend.engine.tags import read_tags, write_managed_tags

if TYPE_CHECKING:
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
        commit = commits.get_commit_in(conn, commit_id)
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


def _stored_tags(settings: Settings, file_id: int) -> dict[str, list[str]]:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return store.get_tags(conn, file_id)
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
        {"genre": ["Electronic"], "grouping": ["Song"]},
    )

    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Synthwave"]})
    assert len(staging.diff_tags(engine_settings)) == 1

    result = staging.commit_tags(engine_settings, message="reclassify")

    assert result.commit_id is not None
    assert result.committed == 1
    assert result.noop == 0
    assert result.missing == 0

    # The edit really changed the bytes on disk; the unmanaged `grouping` tag is untouched.
    on_disk = read_tags(track).tags
    assert on_disk["genre"] == ["Synthwave"]
    assert on_disk.get("grouping") == ["Song"]

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

    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Electronic"]})
    before_bytes = track.read_bytes()
    before_mtime = track.stat().st_mtime_ns
    result = staging.commit_tags(engine_settings)

    assert result.noop == 1
    assert result.committed == 0
    # Only the baseline is recorded — an unchanged commit adds no new revision.
    assert [r.version for r in _revisions(engine_settings, file_id)] == [0]
    assert _staged(engine_settings, file_id) is None
    # Nothing changed, so the file is never rewritten.
    assert track.read_bytes() == before_bytes
    assert track.stat().st_mtime_ns == before_mtime


def test_commit_missing_file_is_flagged_and_dropped(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})

    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Synthwave"]})
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

    staging.stage_tags(engine_settings, file_id=a_id, tags={"genre": ["Synthwave"]})
    staging.stage_tags(engine_settings, file_id=b_id, tags={"genre": ["Metal"]})

    result = staging.commit_tags(engine_settings)

    assert result.committed == 2
    assert _revisions(engine_settings, a_id)[-1].commit_id == result.commit_id
    assert _revisions(engine_settings, b_id)[-1].commit_id == result.commit_id


def test_commit_refreshes_signature_so_next_scan_is_unchanged(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    """A commit rewrites the file (new mtime/size) and must refresh the files-row
    signature, so the next incremental scan reports it ``unchanged`` — not spuriously
    ``updated`` — while the snapshot still matches disk."""
    track = make_track(music_dir / "sig.mp3", {"genre": ["Electronic"]})

    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Synthwave"]})
    staging.commit_tags(engine_settings)

    result = scan_library(engine_settings)

    assert result.updated == 0
    assert result.unchanged == 1
    assert result.tags_read == 0
    assert _stored_tags(engine_settings, file_id)["genre"] == ["Synthwave"]
    assert read_tags(track).tags["genre"] == ["Synthwave"]


def test_revert_refreshes_signature_so_next_scan_is_unchanged(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    """A revert rewrites the file too, so it must likewise refresh the signature."""
    track = make_track(music_dir / "sigrev.mp3", {"genre": ["Electronic"]})

    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Synthwave"]})
    staging.commit_tags(engine_settings)
    versioning.revert_tags(engine_settings, file_id, 0)

    result = scan_library(engine_settings)

    assert result.updated == 0
    assert result.unchanged == 1
    assert result.tags_read == 0
    assert _stored_tags(engine_settings, file_id)["genre"] == ["Electronic"]
    assert read_tags(track).tags["genre"] == ["Electronic"]


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
    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Synthwave"]})

    # Simulate an interrupted commit: disk advances to the target, but nothing is
    # committed. A FULL rescan then advances the live snapshot to the half-written state.
    write_managed_tags(track, {"genre": ["Synthwave"]})
    scan_library(engine_settings, mode=ScanMode.FULL)

    # Re-stage a different target; v0 already exists so no new baseline is captured.
    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Darksynth"]})

    staging.commit_tags(engine_settings)

    baseline = _revision(engine_settings, file_id, 0)
    assert baseline is not None
    assert baseline.managed_tags == {"genre": ["Electronic"]}  # original preserved


def test_commit_continues_past_an_unwritable_file(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracks = [make_track(music_dir / f"t{index}.mp3", {"genre": ["Rock"]}) for index in range(3)]
    scan_library(engine_settings)
    file_ids = [_file_id(engine_settings, music_dir, track.name) for track in tracks]
    for file_id in file_ids:
        staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Jazz"]})
    locked = tracks[1]
    real_write = write_managed_tags

    def write_unless_locked(path: Path, managed: dict[str, list[str]]) -> bool:
        if path.name == locked.name:
            message = f"file in use by another process: {path}"
            raise OSError(message)
        return real_write(path, managed)

    monkeypatch.setattr(staging, "write_managed_tags", write_unless_locked)
    result = staging.commit_tags(engine_settings)

    assert result.committed == 2
    assert result.errors == 1
    failed = next(o for o in result.outcomes if o.file_id == file_ids[1])
    assert failed.status == "error"
    assert failed.version is None
    assert failed.detail is not None
    assert "file in use by another process" in failed.detail
    assert _staged(engine_settings, file_ids[1]) is not None  # kept for a retry
    assert result.commit_id is not None
    assert _commit_status(engine_settings, result.commit_id) == "applied"
    assert read_tags(locked).tags["genre"] == ["Rock"]
    assert result.to_dict()["errors"] == 1

    monkeypatch.setattr(staging, "write_managed_tags", real_write)
    retry = staging.commit_tags(engine_settings)

    assert retry.committed == 1
    assert retry.commit_id is not None
    assert retry.commit_id != result.commit_id
    assert _revisions(engine_settings, file_ids[1])[-1].commit_id == retry.commit_id
    assert read_tags(locked).tags["genre"] == ["Jazz"]


def test_commit_error_envelope_via_mcp(music_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    track = make_track(music_dir / "locked.mp3", {"genre": ["Rock"]})
    mcp_server.scan_library(path=str(music_dir))
    file_id = _file_id(load_settings(), music_dir, track.name)
    assert mcp_server.stage_tags(file_id, {"genre": ["Jazz"]}) == {"ok": True}

    def always_locked(path: Path, managed: dict[str, list[str]]) -> bool:
        message = f"file in use by another process: {path} ({len(managed)} tags)"
        raise OSError(message)

    monkeypatch.setattr(staging, "write_managed_tags", always_locked)
    payload = mcp_server.commit_tags()

    assert payload["ok"] is True
    assert payload["errors"] == 1
    assert payload["committed"] == 0
    outcomes = payload["outcomes"]
    assert isinstance(outcomes, list)
    assert outcomes[0]["status"] == "error"
    assert "file in use" in outcomes[0]["detail"]


def _edit_on_disk(path: Path, field: str, value: str) -> None:
    """Change one tag the way an external tagger would, with no rescan afterwards."""
    audio = mutagen.File(path, easy=True)  # type: ignore[attr-defined]
    audio[field] = [value]
    audio.save()


def test_commit_refuses_a_file_edited_after_staging(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Rock"], "title": ["Song"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Jazz"]})
    edited_title = "Song (Picard Edit With A Much Longer Title)"
    _edit_on_disk(track, "title", edited_title)

    result = staging.commit_tags(engine_settings)

    assert result.changed_since_stage == 1
    assert result.committed == 0
    assert result.outcomes[0].status == "changed_since_stage"
    assert result.outcomes[0].detail is not None
    assert "Re-stage" in result.outcomes[0].detail
    on_disk = read_tags(track).tags
    assert on_disk["title"] == [edited_title]
    assert on_disk["genre"] == ["Rock"]
    assert _staged(engine_settings, file_id) is not None

    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Jazz"]})
    retry = staging.commit_tags(engine_settings)

    assert retry.committed == 1
    on_disk = read_tags(track).tags
    assert on_disk["title"] == [edited_title]
    assert on_disk["genre"] == ["Jazz"]


def test_commit_completes_a_write_that_landed_before_a_crash(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Rock"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Jazz"]})
    staged = _staged(engine_settings, file_id)
    assert staged is not None
    # The crash window: the disk write landed and the DB commit did not.
    write_managed_tags(track, staged.managed_tags)

    result = staging.commit_tags(engine_settings)

    assert result.committed == 1
    assert result.changed_since_stage == 0
    assert [r.version for r in _revisions(engine_settings, file_id)] == [0, 1]
    assert read_tags(track).tags["genre"] == ["Jazz"]
    assert _staged(engine_settings, file_id) is None


def test_commit_skips_the_check_for_a_legacy_staged_row(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Rock"], "title": ["Song"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    current = read_tags(track).tags
    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        versioning.ensure_baseline(conn, file_id, managed_tags=current, now=_NOW)
        store.upsert_staged_tag(
            conn,
            file_id=file_id,
            managed_tags=versioning.managed_subset(current) | {"genre": ["Jazz"]},
            origin="manual",
            now=_NOW,
            base_size_bytes=None,
            base_mtime_ns=None,
        )
        conn.commit()
    finally:
        conn.close()
    _edit_on_disk(track, "title", "Song (Edited Outside TagMend)")

    result = staging.commit_tags(engine_settings)

    assert result.committed == 1
    assert result.changed_since_stage == 0
    assert read_tags(track).tags["genre"] == ["Jazz"]


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

    staging.stage_tags(engine_settings, file_id=a_id, tags={"genre": ["Synthwave"]})
    staging.stage_tags(engine_settings, file_id=b_id, tags={"genre": ["Metal"]})

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

    staging.stage_tags(engine_settings, file_id=rock_id, tags={"genre": ["Synthwave"]})
    staging.stage_tags(engine_settings, file_id=jazz_id, tags={"genre": ["Metal"]})

    result = staging.commit_tags(engine_settings, path=music_dir / "rock")

    assert result.committed == 1
    assert read_tags(rock).tags["genre"] == ["Synthwave"]  # in scope, committed
    assert _staged(engine_settings, rock_id) is None
    assert _staged(engine_settings, jazz_id) is not None  # out of scope, still staged
    assert read_tags(jazz).tags["genre"] == ["Rock"]  # untouched on disk


def test_commit_path_scope_includes_nested_folders(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # The detector workflows expand one exact folder, yet a folder-scoped commit also sweeps
    # every nested folder. The MCP docs state this rule, and this pins it.
    album = make_track(music_dir / "album" / "a.mp3", {"genre": ["Rock"]})
    disc_two = make_track(music_dir / "album" / "CD2" / "b.mp3", {"genre": ["Rock"]})
    scan_library(engine_settings)
    album_id = _file_id(engine_settings, music_dir / "album", album.name)
    disc_two_id = _file_id(engine_settings, music_dir / "album" / "CD2", disc_two.name)
    staging.stage_tags(engine_settings, file_id=album_id, tags={"genre": ["Metal"]})
    staging.stage_tags(engine_settings, file_id=disc_two_id, tags={"genre": ["Metal"]})

    result = staging.commit_tags(engine_settings, path=music_dir / "album")

    assert result.committed == 2
    assert _staged(engine_settings, album_id) is None
    assert _staged(engine_settings, disc_two_id) is None


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS is case-insensitive")
def test_commit_path_scope_ignores_case(engine_settings: Settings, music_dir: Path) -> None:
    rock = make_track(music_dir / "Rock" / "a.mp3", {"genre": ["Electronic"]})
    jazz = make_track(music_dir / "Jazz" / "b.mp3", {"genre": ["Rock"]})
    scan_library(engine_settings)
    rock_id = _file_id(engine_settings, music_dir / "Rock", rock.name)
    jazz_id = _file_id(engine_settings, music_dir / "Jazz", jazz.name)
    staging.stage_tags(engine_settings, file_id=rock_id, tags={"genre": ["Synthwave"]})
    staging.stage_tags(engine_settings, file_id=jazz_id, tags={"genre": ["Metal"]})
    shouted = Path(str(music_dir / "Rock").upper())

    diffs = staging.diff_tags(engine_settings, path=shouted)
    result = staging.commit_tags(engine_settings, path=shouted)

    assert [d.file_id for d in diffs] == [rock_id]
    assert result.committed == 1
    assert read_tags(rock).tags["genre"] == ["Synthwave"]
    assert _staged(engine_settings, jazz_id) is not None


def test_stage_strips_and_nfc_normalises_values(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "a.flac", {"album": ["Old"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    staging.stage_tags(
        engine_settings,
        file_id=file_id,
        tags={"album": ["  ", "Cafe\u0301 "]},
    )
    staging.commit_tags(engine_settings)

    assert read_tags(track).tags["album"] == ["Caf\u00e9"]


def test_stage_rejects_control_characters(engine_settings: Settings, music_dir: Path) -> None:
    good = make_track(music_dir / "a.mp3", {"title": ["Fine"]})
    bad = make_track(music_dir / "b.mp3", {"title": ["Fine"]})
    scan_library(engine_settings)
    good_id = _file_id(engine_settings, music_dir, good.name)
    bad_id = _file_id(engine_settings, music_dir, bad.name)

    with pytest.raises(ValueError, match="contains a NUL, CR or LF"):
        staging.stage_tags(engine_settings, file_id=bad_id, tags={"title": ["a\nb"]})
    with pytest.raises(ValueError, match="contains a NUL, CR or LF"):
        staging.stage_tags_batch(
            engine_settings,
            entries=[(good_id, {"title": ["New"]}), (bad_id, {"title": ["a\x00b"]})],
        )

    assert _staged(engine_settings, good_id) is None
    assert _staged(engine_settings, bad_id) is None


def test_stage_refuses_a_container_the_writer_cannot_verify(
    engine_settings: Settings, music_dir: Path
) -> None:
    wav = music_dir / "clip.wav"
    with wave.open(str(wav), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(bytes(1600))
    scan_library(engine_settings)
    wav_id = _file_id(engine_settings, music_dir, wav.name)

    with pytest.raises(ValueError, match="no layout for the WAVE container"):
        staging.stage_tags(engine_settings, file_id=wav_id, tags={"title": ["Clip"]})

    assert _staged(engine_settings, wav_id) is None


def test_stage_refuses_a_file_whose_audio_payload_cannot_be_located(
    engine_settings: Settings, music_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = make_track(music_dir / "lost.flac", {"title": ["Lost"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    monkeypatch.setattr(tags, "_audio_ranges", lambda *_args: None)

    with pytest.raises(ValueError, match="audio payload could not be located"):
        staging.stage_tags(engine_settings, file_id=file_id, tags={"title": ["Found"]})

    assert _staged(engine_settings, file_id) is None


def test_diff_tags_enrichment(engine_settings: Settings, music_dir: Path) -> None:
    changed = make_track(music_dir / "c.mp3", {"genre": ["Electronic"]})
    noop = make_track(music_dir / "n.flac", {"genre": ["Rock"]})

    scan_library(engine_settings)
    changed_id = _file_id(engine_settings, music_dir, changed.name)
    noop_id = _file_id(engine_settings, music_dir, noop.name)

    staging.stage_tags(engine_settings, file_id=changed_id, tags={"genre": ["Synthwave"]})
    staging.stage_tags(engine_settings, file_id=noop_id, tags={"genre": ["Rock"]})

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
        staging.stage_tags(engine_settings, file_id=file_id, tags={"composer": ["Nope"]})


def test_stage_tags_rejects_unknown_file_id(engine_settings: Settings) -> None:
    with pytest.raises(ValueError, match="unknown file_id"):
        staging.stage_tags(engine_settings, file_id=999, tags={"genre": ["X"]})


def test_stage_tags_rejects_bad_origin(engine_settings: Settings, music_dir: Path) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Electronic"]})

    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    with pytest.raises(ValueError, match="invalid staged origin"):
        staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["X"]}, origin="bogus")


# The full wrong-release "stamp" the mismatch-fix flow repairs, rich in the widened fields.
# Valid year values so EasyID3 keeps them (it silently drops an unparseable date/TDRC).
_RICH_STAMP: dict[str, list[str]] = {
    "genre": ["Rock"],
    "title": ["Right Title"],
    "album": ["Right Album"],
    "date": ["2001"],
    "tracknumber": ["3/12"],
    "discnumber": ["1/2"],
    "artistsort": ["Osbourne, Ozzy"],
    "albumartistsort": ["Osbourne, Ozzy"],
    "musicbrainz_albumartistid": ["mb-aa"],
    "musicbrainz_albumid": ["mb-al"],
    "musicbrainz_releasegroupid": ["mb-rg"],
    "musicbrainz_releasetrackid": ["mb-rt"],
    "musicbrainz_trackid": ["mb-tr"],
}


@pytest.mark.parametrize("suffix", _FORMATS)
def test_widened_fields_ride_through_commit_and_revert(
    engine_settings: Settings,
    music_dir: Path,
    suffix: str,
) -> None:
    # A genre-only auto stage, built the way the resolve flows build it (the full managed
    # subset of current tags | the changed field — genres.py:384 / years.py:377), must
    # carry every OTHER widened field through the commit un-deleted, record a diff touching
    # ONLY $.genre (no spurious axis flips), and stay revertible via the v0 baseline the
    # stage auto-captured for the new fields.
    track = make_track(music_dir / f"track{suffix}", dict(_RICH_STAMP))
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    current = versioning.managed_subset(read_tags(track).tags)
    target = {**current, "genre": ["Synthwave"]}
    staging.stage_tags(engine_settings, file_id=file_id, tags=target, origin="auto")
    result = staging.commit_tags(engine_settings)
    assert result.committed == 1

    # Every widened field survived the commit un-deleted; only genre changed on disk.
    on_disk = read_tags(track).tags
    for field, value in _RICH_STAMP.items():
        expected = ["Synthwave"] if field == "genre" else value
        assert on_disk.get(field) == expected, field

    # The auto revision's diff is genre-only (unchanged fields ride along with zero diff).
    committed = _revision(engine_settings, file_id, 1)
    assert committed is not None
    assert committed.diff == {"genre": {"from": ["Rock"], "to": ["Synthwave"]}}

    # Revert to the v0 baseline restores genre; the widened fields (in the baseline) survive.
    versioning.revert_tags(engine_settings, file_id, 0)
    reverted = read_tags(track).tags
    for field, value in _RICH_STAMP.items():
        assert reverted.get(field) == value, field


@pytest.mark.parametrize("suffix", _FORMATS)
def test_partial_stage_preserves_other_managed_fields(
    engine_settings: Settings,
    music_dir: Path,
    suffix: str,
) -> None:
    # Staging ONLY genre through the RAW stage path (no resolve-flow subset build — the
    # externally-reachable stage_tags/commit_tags surface) must merge onto the file's
    # current managed tags, so commit does NOT delete the rest of the widened identity
    # stamp (title/album/date/track/disc/sort/MB ids). Regression for the delete-on-absent
    # footgun the widened MANAGED_TAGS would otherwise expose.
    track = make_track(music_dir / f"track{suffix}", dict(_RICH_STAMP))
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Synthwave"]})

    # The merged target carries the whole subset; the diff still touches only genre.
    view = next(v for v in staging.diff_tags(engine_settings) if v.file_id == file_id)
    assert view.diff == {"genre": {"from": ["Rock"], "to": ["Synthwave"]}}

    result = staging.commit_tags(engine_settings)
    assert result.committed == 1

    on_disk = read_tags(track).tags
    for field, value in _RICH_STAMP.items():
        expected = ["Synthwave"] if field == "genre" else value
        assert on_disk.get(field) == expected, field


def test_explicit_empty_list_still_deletes_a_managed_field(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # The merge preserves OMITTED keys, but an explicit empty list is still an intentional
    # delete (so revert-to-a-baseline-that-lacked-a-tag keeps working).
    track = make_track(music_dir / "t.flac", {"genre": ["Rock"], "album": ["An Album"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    staging.stage_tags(engine_settings, file_id=file_id, tags={"album": []})
    staging.commit_tags(engine_settings)

    on_disk = read_tags(track).tags
    assert on_disk.get("album") is None  # explicitly cleared
    assert on_disk.get("genre") == ["Rock"]  # untouched managed field preserved


@pytest.mark.parametrize("with_id3v1", [False, True])
def test_explicit_empty_date_deletes_a_v23_iso_tyer(
    engine_settings: Settings,
    music_dir: Path,
    *,
    with_id3v1: bool,
) -> None:
    track = make_track(music_dir / "iso.mp3")
    frames = ID3()  # type: ignore[no-untyped-call]
    frames.add(TYER(encoding=0, text=["2013-10-04T07:00:00Z"]))  # type: ignore[no-untyped-call]
    frames.save(track, v2_version=3, v1=0)
    if with_id3v1:
        year = TYER(encoding=0, text=["2013"])  # type: ignore[no-untyped-call]
        with track.open("ab") as handle:
            handle.write(MakeID3v1({"TYER": year}))  # type: ignore[no-untyped-call]
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    assert _stored_tags(engine_settings, file_id)["date"] == ["2013-10-04"]

    staging.stage_tags(engine_settings, file_id=file_id, tags={"date": []})
    result = staging.commit_tags(engine_settings)

    assert (result.committed, result.errors) == (1, 0)
    assert "date" not in read_tags(track).tags
    assert "date" not in _stored_tags(engine_settings, file_id)


# --- stage_tags_batch (atomic multi-file staging) -----------------------------------


def test_stage_tags_batch_stages_all_and_commits_as_one(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    a = make_track(music_dir / "a.mp3", {"genre": ["Pop"], "title": ["A"]})
    b = make_track(music_dir / "b.flac", {"genre": ["Pop"], "title": ["B"]})
    scan_library(engine_settings)
    a_id = _file_id(engine_settings, music_dir, a.name)
    b_id = _file_id(engine_settings, music_dir, b.name)

    staged = staging.stage_tags_batch(
        engine_settings,
        entries=[(a_id, {"albumartist": ["Ozzy"]}), (b_id, {"albumartist": ["Ozzy"]})],
    )
    assert staged == [a_id, b_id]
    assert _staged(engine_settings, a_id) is not None
    assert _staged(engine_settings, b_id) is not None

    result = staging.commit_tags(engine_settings, path=music_dir)
    assert result.committed == 2
    # Both files landed under ONE commit; the merge preserved each file's title.
    assert _revisions(engine_settings, a_id)[-1].commit_id == result.commit_id
    assert _revisions(engine_settings, b_id)[-1].commit_id == result.commit_id
    assert read_tags(a).tags["albumartist"] == ["Ozzy"]
    assert read_tags(a).tags["title"] == ["A"]  # omitted managed key preserved


def test_stage_tags_batch_rolls_back_on_any_invalid_entry(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    a = make_track(music_dir / "a.mp3", {"genre": ["Pop"]})
    scan_library(engine_settings)
    a_id = _file_id(engine_settings, music_dir, a.name)

    # The second entry names an unknown file id -> the whole batch rolls back.
    with pytest.raises(ValueError, match="unknown file_id=9999"):
        staging.stage_tags_batch(
            engine_settings,
            entries=[(a_id, {"albumartist": ["Ozzy"]}), (9999, {"albumartist": ["Ozzy"]})],
        )
    # NOTHING staged: the valid first entry was rolled back too.
    assert _staged(engine_settings, a_id) is None


def test_stage_tags_batch_rejects_unmanaged_key_atomically(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    a = make_track(music_dir / "a.mp3", {"genre": ["Pop"]})
    b = make_track(music_dir / "b.flac", {"genre": ["Pop"]})
    scan_library(engine_settings)
    a_id = _file_id(engine_settings, music_dir, a.name)
    b_id = _file_id(engine_settings, music_dir, b.name)

    with pytest.raises(ValueError, match="non-managed"):
        staging.stage_tags_batch(
            engine_settings,
            entries=[(a_id, {"genre": ["Rock"]}), (b_id, {"composer": ["Nope"]})],
        )
    assert _staged(engine_settings, a_id) is None
    assert _staged(engine_settings, b_id) is None


def test_stage_tags_batch_rejects_duplicate_file_id(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    a = make_track(music_dir / "a.mp3", {"genre": ["Pop"]})
    scan_library(engine_settings)
    a_id = _file_id(engine_settings, music_dir, a.name)

    with pytest.raises(ValueError, match="duplicate file_id"):
        staging.stage_tags_batch(
            engine_settings,
            entries=[(a_id, {"genre": ["Rock"]}), (a_id, {"genre": ["Metal"]})],
        )
    assert _staged(engine_settings, a_id) is None


# --- reopen_axes (NN7: re-open derived axes after a manual identity fix) -------------


def _auto_genre_year(
    engine_settings: Settings,
    file_id: int,
    *,
    genre: str = "Metal",
    year: str = "2001",
) -> None:
    """Stage, record ``done`` and commit an auto genre+year change, as the resolvers do."""
    staging.stage_tags(
        engine_settings,
        file_id=file_id,
        tags={"genre": [genre], "originaldate": [year]},
        origin="auto",
    )
    conn = connect(engine_settings.db_path)
    try:
        for tag_axis in (axis.GENRE_AXIS, axis.YEAR_AXIS):
            store.record_outcome(
                conn,
                tag_axis,
                file_id=file_id,
                status="done",
                now="2026-09-30T00:00:00+00:00",
            )
        conn.commit()
    finally:
        conn.close()
    staging.commit_tags(engine_settings)


def _derived(engine_settings: Settings, file_id: int) -> tuple[str, str]:
    conn = connect(engine_settings.db_path)
    try:
        return (
            store.derived_status(conn, axis.GENRE_AXIS, file_id),
            store.derived_status(conn, axis.YEAR_AXIS, file_id),
        )
    finally:
        conn.close()


_IDENTIFIED = {"genre": ["Pop"], "albumartist": ["Jem"], "album": ["LP"]}


def test_reopen_axes_flips_done_to_pending_and_stays_reopen_safe(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", _IDENTIFIED)
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    _auto_genre_year(engine_settings, file_id)
    assert _derived(engine_settings, file_id) == ("done", "done")

    # A manual fix outside every axis's identity leaves both outcomes matching the file.
    staging.stage_tags(engine_settings, file_id=file_id, tags={"title": ["Fixed Title"]})
    fix = staging.commit_tags(engine_settings)
    assert fix.commit_id is not None
    assert _derived(engine_settings, file_id) == ("done", "done")

    reopen = staging.reopen_axes(engine_settings, commit_id=fix.commit_id)
    assert reopen.files == 1
    assert reopen.to_dict()["genre"] == {"outcomes_reopened": 1, "manual_kept": 0}
    assert reopen.to_dict()["year"] == {"outcomes_reopened": 1, "manual_kept": 0}
    assert _derived(engine_settings, file_id) == ("pending", "pending")

    # Reopen-safe: a LATER fresh resolver outcome reads done again.
    _auto_genre_year(engine_settings, file_id, genre="Ambient", year="2002")
    assert _derived(engine_settings, file_id) == ("done", "done")


def test_reopen_axes_keeps_manual_rows(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", _IDENTIFIED)
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    artists.set_artist_status(engine_settings, file_ids=[file_id], status="manual")
    staging.stage_tags(engine_settings, file_id=file_id, tags={"albumartist": ["Ozzy"]})
    fix = staging.commit_tags(engine_settings)
    assert fix.commit_id is not None

    reopen = staging.reopen_axes(engine_settings, commit_id=fix.commit_id)
    assert reopen.to_dict()["artist"] == {"outcomes_reopened": 0, "manual_kept": 1}
    conn = connect(engine_settings.db_path)
    try:
        assert store.derived_status(conn, axis.ARTIST_AXIS, file_id) == "manual"
    finally:
        conn.close()


def test_reopen_axes_rejects_auto_and_unknown_commit(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"genre": ["Pop"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    staging.stage_tags(
        engine_settings,
        file_id=file_id,
        tags={"genre": ["Rock"]},
        origin="auto",
    )
    auto = staging.commit_tags(engine_settings)
    assert auto.commit_id is not None

    with pytest.raises(ValueError, match="auto-resolved"):
        staging.reopen_axes(engine_settings, commit_id=auto.commit_id)
    with pytest.raises(ValueError, match="unknown commit_id"):
        staging.reopen_axes(engine_settings, commit_id=9999)


def _stage_mixed_sweep(engine_settings: Settings, music_dir: Path) -> None:
    """Stage one auto change and one manual change on two fresh files."""
    auto_track = make_track(music_dir / "auto.mp3", {"genre": ["Pop"]})
    manual_track = make_track(music_dir / "manual.mp3", {"genre": ["Pop"]})
    scan_library(engine_settings)
    auto_id = _file_id(engine_settings, music_dir, auto_track.name)
    manual_id = _file_id(engine_settings, music_dir, manual_track.name)
    staging.stage_tags(
        engine_settings,
        file_id=auto_id,
        tags={"genre": ["Rock"]},
        origin="auto",
    )
    staging.stage_tags(engine_settings, file_id=manual_id, tags={"genre": ["Jazz"]})


def _commit_origin(engine_settings: Settings, commit_id: int) -> str:
    conn = connect(engine_settings.db_path)
    try:
        commit = commits.get_commit_in(conn, commit_id)
        assert commit is not None
        return commit.origin
    finally:
        conn.close()


def test_commit_origin_is_auto_when_every_staged_row_is_auto(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    first = make_track(music_dir / "a.mp3", {"genre": ["Pop"]})
    second = make_track(music_dir / "b.mp3", {"genre": ["Pop"]})
    scan_library(engine_settings)
    for track in (first, second):
        staging.stage_tags(
            engine_settings,
            file_id=_file_id(engine_settings, music_dir, track.name),
            tags={"genre": ["Rock"]},
            origin="auto",
        )

    result = staging.commit_tags(engine_settings)

    assert result.commit_id is not None
    assert _commit_origin(engine_settings, result.commit_id) == "auto"


def test_commit_origin_is_manual_for_a_mixed_sweep(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _stage_mixed_sweep(engine_settings, music_dir)

    result = staging.commit_tags(engine_settings)

    assert result.commit_id is not None
    assert result.committed == 2
    assert _commit_origin(engine_settings, result.commit_id) == "manual"


def test_reopen_axes_refuses_a_manual_commit_holding_an_auto_revision(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _stage_mixed_sweep(engine_settings, music_dir)
    mixed = staging.commit_tags(engine_settings)
    assert mixed.commit_id is not None

    with pytest.raises(ValueError, match="auto-resolved"):
        staging.reopen_axes(engine_settings, commit_id=mixed.commit_id)


def test_reopen_axes_refuses_a_commit_with_no_tag_revisions(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # A no-op commit (target == current) leaves no revision row, so there is nothing to reopen.
    track = make_track(music_dir / "t.mp3", _IDENTIFIED)
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    _auto_genre_year(engine_settings, file_id)  # genre done at Metal

    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Metal"]})
    noop = staging.commit_tags(engine_settings)
    assert noop.commit_id is not None
    assert noop.noop == 1

    with pytest.raises(ValueError, match="changed no tags"):
        staging.reopen_axes(engine_settings, commit_id=noop.commit_id)
    # Both stay done: nothing was deleted, and a no-op manual commit records no manual row.
    assert _derived(engine_settings, file_id) == ("done", "done")


def test_stale_snapshot_does_not_delete_a_managed_tag_from_disk(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # The v0 baseline and the staged target were both built from the snapshot mirror, which
    # can lag the file (an older tag reader wrote the row, or nothing rescanned after an
    # upgrade). Since the commit deletes every managed key absent from the staged target,
    # a stale row silently destroyed a tag the caller never mentioned — and the baseline,
    # taken from the same stale source, could not bring it back. Disk is the only truth
    # at stage time.
    track = make_track(
        music_dir / "t.mp3",
        {"artist": ["A"], "album": ["Alb"], "musicbrainz_albumtype": ["album"]},
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "t.mp3")

    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        conn.execute(
            "DELETE FROM file_tags WHERE file_id=? AND name='musicbrainz_albumtype'",
            (file_id,),
        )
        conn.commit()
        assert "musicbrainz_albumtype" not in store.get_tags(conn, file_id)
    finally:
        conn.close()

    staging.stage_tags(
        engine_settings,
        file_id=file_id,
        tags={"genre": ["Industrial"]},
        origin="manual",
    )
    staging.commit_tags(engine_settings, message="unrelated field")

    assert read_tags(track).tags["musicbrainz_albumtype"] == ["album"]

    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        baseline = store.get_revision(conn, file_id, 0)
        assert baseline is not None
        assert baseline.managed_tags["musicbrainz_albumtype"] == ["album"]
    finally:
        conn.close()


def test_stage_rejects_a_file_that_vanished_from_disk(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # Staging reads the file to build its target and baseline, so a row whose file is gone
    # has to be rejected by name rather than surfacing a raw mutagen traceback.
    track = make_track(music_dir / "gone.mp3", {"artist": ["A"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "gone.mp3")
    track.unlink()

    with pytest.raises(ValueError, match=f"cannot read tags from disk for file_id={file_id}"):
        staging.stage_tags(
            engine_settings,
            file_id=file_id,
            tags={"genre": ["Rock"]},
            origin="manual",
        )


def test_diff_reads_current_from_disk_not_the_stale_snapshot(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # The staged target is built from disk, so reading `current` from the snapshot mirror
    # would show a field the mirror happens to lack as an ADDITION when nothing changes on
    # disk. A review surface that invents changes is worse than no review surface.
    track = make_track(
        music_dir / "t.mp3",
        {"artist": ["A"], "musicbrainz_albumtype": ["album"]},
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "t.mp3")

    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        conn.execute(
            "DELETE FROM file_tags WHERE file_id=? AND name='musicbrainz_albumtype'",
            (file_id,),
        )
        conn.commit()
    finally:
        conn.close()

    staging.stage_tags(
        engine_settings,
        file_id=file_id,
        tags={"genre": ["Industrial"]},
        origin="manual",
    )

    view = staging.diff_tags(engine_settings)[0]
    assert view.current["musicbrainz_albumtype"] == ["album"]
    assert set(view.diff) == {"genre"}
    assert read_tags(track).tags["artist"] == ["A"]


def test_diff_flags_a_name_changed_without_its_id(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # The merge-onto-current trap: rewriting artist while omitting musicbrainz_artistid
    # leaves the OLD artist's id in place, so the file names one artist and points at
    # another. Reported for review, never blocked and never auto-changed.
    make_track(
        music_dir / "t.mp3",
        {"artist": ["Linkin Park"], "musicbrainz_artistid": ["lp-id"]},
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "t.mp3")

    staging.stage_tags(
        engine_settings,
        file_id=file_id,
        tags={"artist": ["Alice in Chains"]},
        origin="manual",
    )

    view = staging.diff_tags(engine_settings)[0]
    assert view.stale_identity == [
        {"changed": "artist", "stale_field": "musicbrainz_artistid", "stale_value": ["lp-id"]},
    ]


def test_diff_flags_a_name_changed_without_its_sort_name(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "t.mp3",
        {"artist": ["The Doors"], "artistsort": ["Doors, The"]},
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "t.mp3")

    staging.stage_tags(engine_settings, file_id=file_id, tags={"artist": ["Skrillex"]})

    view = staging.diff_tags(engine_settings)[0]
    assert view.stale_identity == [
        {"changed": "artist", "stale_field": "artistsort", "stale_value": ["Doors, The"]},
    ]


def test_diff_flags_an_artists_list_changed_without_its_ids(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # Picard aligns the two lists by position, so a renamed element keeps the old id beside it.
    make_track(
        music_dir / "t.flac",
        {"artists": ["A", "B"], "musicbrainz_artistid": ["id-a", "id-b"]},
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "t.flac")

    staging.stage_tags(engine_settings, file_id=file_id, tags={"artists": ["A", "C"]})

    assert staging.diff_tags(engine_settings)[0].stale_identity == [
        {
            "changed": "artists",
            "stale_field": "musicbrainz_artistid",
            "stale_value": ["id-a", "id-b"],
        },
    ]


@pytest.mark.parametrize("suffix", _FORMATS)
def test_commit_then_revert_restores_the_artists_list(
    engine_settings: Settings,
    music_dir: Path,
    suffix: str,
) -> None:
    track = make_track(music_dir / f"t{suffix}", {"artists": ["Bryan El", "Guest"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    staging.stage_tags(engine_settings, file_id=file_id, tags={"artists": ["Bryan EL", "Guest"]})
    result = staging.commit_tags(engine_settings)
    assert result.commit_id is not None
    assert read_tags(track).tags["artists"] == ["Bryan EL", "Guest"]

    versioning.revert_commit(engine_settings, result.commit_id)

    assert read_tags(track).tags["artists"] == ["Bryan El", "Guest"]


def test_diff_does_not_flag_a_sort_only_change(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "t.mp3",
        {"artist": ["The Doors"], "artistsort": ["The Doors"]},
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "t.mp3")

    staging.stage_tags(
        engine_settings,
        file_id=file_id,
        tags={"artistsort": ["Doors, The"]},
    )

    assert staging.diff_tags(engine_settings)[0].stale_identity == []


def test_diff_flags_the_release_stamp_an_album_change_leaves_behind(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "t.mp3",
        {"album": ["Greatest Hits"], "releasecountry": ["RU"], "media": ["CD"]},
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "t.mp3")

    staging.stage_tags(engine_settings, file_id=file_id, tags={"album": ["Dirt"]})

    stale = staging.diff_tags(engine_settings)[0].stale_identity
    assert {entry["stale_field"] for entry in stale} == {"releasecountry", "media"}


def test_diff_does_not_flag_a_name_changed_with_its_id(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "t.mp3",
        {"artist": ["Linkin Park"], "musicbrainz_artistid": ["lp-id"]},
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "t.mp3")

    staging.stage_tags(
        engine_settings,
        file_id=file_id,
        tags={"artist": ["Alice in Chains"], "musicbrainz_artistid": ["aic-id"]},
        origin="manual",
    )

    assert staging.diff_tags(engine_settings)[0].stale_identity == []


def test_diff_does_not_flag_when_the_coupled_field_is_empty(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # Nothing stale about an id that was never there.
    make_track(music_dir / "t.mp3", {"artist": ["Linkin Park"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "t.mp3")

    staging.stage_tags(
        engine_settings,
        file_id=file_id,
        tags={"artist": ["Alice in Chains"]},
        origin="manual",
    )

    assert staging.diff_tags(engine_settings)[0].stale_identity == []


@pytest.mark.parametrize("lookalike", ["BAND", "ALBUM ARTIST"])
def test_albumartist_lookalike_survives_commit_and_revert(
    engine_settings: Settings,
    music_dir: Path,
    lookalike: str,
) -> None:
    # A blank look-alike beside a real ALBUMARTIST is a live-library shape. An artist-only change
    # and its revert must leave both raw fields exactly as found.
    track = make_track(music_dir / "track.flac")
    raw = FLAC(track)
    raw[lookalike] = [""]
    raw["ALBUMARTIST"] = ["Smashing Pumpkins"]
    raw["ARTIST"] = ["Smashing Pumpkins"]
    raw.save()
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    staging.stage_tags(engine_settings, file_id=file_id, tags={"artist": ["The Smashing Pumpkins"]})
    assert staging.commit_tags(engine_settings).committed == 1
    committed = FLAC(track)
    assert committed["ARTIST"] == ["The Smashing Pumpkins"]
    assert committed["ALBUMARTIST"] == ["Smashing Pumpkins"]
    assert committed[lookalike] == [""]

    versioning.revert_tags(engine_settings, file_id, 0)
    reverted = FLAC(track)
    assert reverted["ALBUMARTIST"] == ["Smashing Pumpkins"]
    assert reverted["ARTIST"] == ["Smashing Pumpkins"]
    assert reverted[lookalike] == [""]


@pytest.mark.parametrize(
    ("supplied", "stale"),
    [
        pytest.param({"album": ["LP"]}, [], id="album-supplied-unchanged"),
        pytest.param(
            {},
            [{"changed": "musicbrainz_albumid", "stale_field": "album", "stale_value": ["LP"]}],
            id="album-left-out",
        ),
    ],
)
def test_diff_skips_a_group_member_the_caller_supplied(
    engine_settings: Settings,
    music_dir: Path,
    supplied: dict[str, list[str]],
    stale: list[dict[str, object]],
) -> None:
    # A value the caller wrote, even an unchanged one, confirms it rather than leaving it stale.
    make_track(music_dir / "t.mp3", {"album": ["LP"], "musicbrainz_albumid": ["old-release"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "t.mp3")

    staging.stage_tags_batch(
        engine_settings,
        entries=[(file_id, {"musicbrainz_albumid": ["new-release"], **supplied})],
    )

    staged = _staged(engine_settings, file_id)
    assert staged is not None
    assert staged.supplied_keys == frozenset({"musicbrainz_albumid", *supplied})
    assert staging.diff_tags(engine_settings)[0].stale_identity == stale
