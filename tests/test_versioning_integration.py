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
from tagmend.engine.tags import MANAGED_TAGS, read_tags, write_managed_tags

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

_FORMATS = [".mp3", ".flac", ".m4a", ".ogg"]
_NOW = "2026-06-02T00:00:00+00:00"
_LATER = "2026-06-02T01:00:00+00:00"

# A value for every tag in the closed managed set, so the "revert restores emptiness" test
# covers all 18 rather than a sample.
_ALL_MANAGED_TAGS = {
    "genre": ["Electronic"],
    "artist": ["Artist"],
    "albumartist": ["Album Artist"],
    "originaldate": ["1985"],
    "musicbrainz_artistid": ["mb-artist"],
    "title": ["Title"],
    "album": ["Album"],
    "date": ["1999"],
    "tracknumber": ["3/12"],
    "discnumber": ["1/1"],
    "artistsort": ["Artist, The"],
    "albumartistsort": ["Album Artist, The"],
    "musicbrainz_albumtype": ["album"],
    "musicbrainz_albumartistid": ["mb-albumartist"],
    "musicbrainz_albumid": ["mb-album"],
    "musicbrainz_releasegroupid": ["mb-releasegroup"],
    "musicbrainz_releasetrackid": ["mb-releasetrack"],
    "musicbrainz_trackid": ["mb-track"],
}


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
        {"genre": ["Electronic"], "grouping": ["Song"]},
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
    # `grouping` is unmanaged (writable on all four formats via easy mode) -> left untouched.
    assert reverted.get("grouping") == ["Song"]

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


@pytest.mark.parametrize("suffix", _FORMATS)
def test_revert_to_pre_widening_baseline_preserves_new_fields(
    engine_settings: Settings,
    music_dir: Path,
    suffix: str,
) -> None:
    # A revision stamped managed-set version 1 holds only the original 5-field subset (here
    # genre+artist). The widened identity fields (title/album/track/MB id) live on disk but
    # that snapshot never governed them, so reverting to it must NOT delete them (their
    # absence is "not tracked then", not "delete") while the original fields still restore to
    # the baseline values. Guards the migration-compat path: every pre-v13 revision captured
    # before the widening carries this marker.
    track = make_track(
        music_dir / f"track{suffix}",
        {
            "genre": ["Wrong Genre"],
            "artist": ["Wrong Artist"],
            "title": ["Keep Title"],
            "album": ["Keep Album"],
            "tracknumber": ["3/12"],
            "musicbrainz_albumid": ["keep-mb-al"],
        },
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    # Simulate the pre-widening v0 baseline: only the original managed subset was captured.
    _baseline(
        engine_settings,
        file_id,
        {"genre": ["Original Genre"], "artist": ["Original Artist"]},
    )
    # ensure_baseline stamps TODAY's managed set, so force version 1: the row must really be
    # what it simulates, a snapshot taken when only the original 5 tags were managed.
    _stamp_managed_set(engine_settings, file_id, version=0, managed_set=1)

    versioning.revert(engine_settings, file_id, 0)

    reverted = read_tags(track).tags
    # Original 5-field values restored from the baseline (delete-on-revert intact).
    assert reverted["genre"] == ["Original Genre"]
    assert reverted["artist"] == ["Original Artist"]
    # Widened fields the baseline never governed survive untouched on disk.
    assert reverted.get("title") == ["Keep Title"]
    assert reverted.get("album") == ["Keep Album"]
    assert reverted.get("tracknumber") == ["3/12"]
    assert reverted.get("musicbrainz_albumid") == ["keep-mb-al"]


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

    # Reverting to v0 while already at v0 content still records an audited revert row, but
    # reports 'noop': nothing on disk moved, so the caller is never told a change happened.
    result = versioning.revert(engine_settings, file_id, 0)
    assert result.new_version == 1
    assert result.status == "noop"
    revisions = _revisions(engine_settings, file_id)
    assert revisions[-1].origin == "revert"
    assert revisions[-1].diff == {}


def test_revert_to_empty_baseline_clears_every_managed_tag(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # The revert-fidelity defect in full: a v0 baseline captured with NO managed tags means
    # all 18 were empty, so reverting to it must delete all 18. Before the managed-set stamp
    # the 13 widened fields were preserved instead and the revert reported success while the
    # file kept them.
    assert set(_ALL_MANAGED_TAGS) == MANAGED_TAGS
    track = make_track(music_dir / "t.flac")
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    _baseline(engine_settings, file_id, {})
    assert _edit(engine_settings, track, file_id, _ALL_MANAGED_TAGS) == 1
    assert set(read_tags(track).tags) >= MANAGED_TAGS  # all 18 really landed on disk

    result = versioning.revert(engine_settings, file_id, 0)

    assert result.status == "reverted"
    assert set(read_tags(track).tags) & MANAGED_TAGS == set()
    revisions = _revisions(engine_settings, file_id)
    assert revisions[-1].managed_tags == {}


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
        {"genre": ["Electronic"], "artist": ["A"], "composer": ["Song"]},
    )

    write_managed_tags(track, {"genre": ["Synthwave"]})

    tags = read_tags(track).tags
    assert tags["genre"] == ["Synthwave"]
    assert "artist" not in tags  # managed but omitted -> deleted
    assert tags.get("composer") == ["Song"]  # unmanaged -> preserved


def test_write_managed_tags_leaves_no_temp_file(tmp_path: Path) -> None:
    track = make_track(tmp_path / "t.mp3", {"genre": ["Electronic"]})

    write_managed_tags(track, {"genre": ["Synthwave"]})

    assert not (tmp_path / "t.mp3.tagmend.tmp").exists()
    assert list(tmp_path.iterdir()) == [track]


def test_write_managed_tags_rejects_unmanaged_key(tmp_path: Path) -> None:
    track = make_track(tmp_path / "t.mp3", {"genre": ["Electronic"]})

    with pytest.raises(ValueError, match="non-managed"):
        write_managed_tags(track, {"composer": ["Nope"]})
