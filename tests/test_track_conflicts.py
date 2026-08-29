"""Unit tests for the intra-folder track-slot detector (:mod:`tagmend.engine.track_conflicts`).

The pure tests drive ``_classify`` directly with built ``_FileInput`` rows (no DB); the
integration tests build real audio with ``make_track``, scan, and call the public entry. The
four scenarios mirror the shapes measured on the real 11,196-file library: 89 same-title
duplicate stamps, 28 rows inside multi-album folders, 6 different-title collisions, and 3
same-title-different-container duplicate encodes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from conftest import make_track
from tagmend.engine import store, track_conflicts
from tagmend.engine.db import connect
from tagmend.engine.library import scan_library
from tagmend.engine.schema import apply_schema
from tagmend.engine.track_conflicts import detect_track_conflicts

if TYPE_CHECKING:
    from tagmend.config import Settings

_MUSIC = Path("/library/music")


def _mk(  # noqa: PLR0913 - one builder per detect field, cohesive and test-only
    file_id: int,
    folder: Path,
    filename: str,
    *,
    ext: str = ".mp3",
    tracknumber: str | None = None,
    discnumber: str | None = None,
    title: str | None = None,
    album: str | None = None,
) -> track_conflicts._FileInput:
    return track_conflicts._FileInput(
        file_id=file_id,
        folder=str(folder),
        filename=filename,
        ext=ext,
        tracknumber=tracknumber,
        discnumber=discnumber,
        title=title,
        album=album,
    )


def _find(
    report: track_conflicts.TrackConflictsReport,
    file_id: int,
) -> track_conflicts.TrackConflictRow | None:
    return next((r for r in report.rows if r.file_id == file_id), None)


# --- the decision table --------------------------------------------------------------


def test_same_title_same_container_is_medium() -> None:
    # The commonest real shape: one folder holding the same track twice under one number.
    folder = _MUSIC / "Alice in Chains" / "(1992) Dirt"
    files = [
        _mk(1, folder, "10 Angry Chair.mp3", tracknumber="10", title="Angry Chair", album="Dirt"),
        _mk(
            2, folder, "10 Angry Chair (1).mp3", tracknumber="10", title="Angry Chair", album="Dirt"
        ),
        _mk(3, folder, "11 Rooster.mp3", tracknumber="11", title="Rooster", album="Dirt"),
    ]

    report = track_conflicts._classify(files)

    assert report.flagged == 2
    assert report.medium == 2
    row = _find(report, 1)
    assert row is not None
    assert row.tier == "medium"
    assert row.reason == track_conflicts._REASON_MEDIUM
    assert row.track == 10
    assert row.disc == "1"
    assert row.peers == [2]
    # The uninvolved file is untouched.
    assert _find(report, 3) is None


def test_different_titles_sharing_a_slot_is_high() -> None:
    # Two distinct songs cannot both be track 2: the numbering itself is wrong.
    folder = _MUSIC / "Rank 1" / "(2009) Symfo"
    files = [
        _mk(1, folder, "a.mp3", tracknumber="1", title="Symfo (Original Mix)", album="Symfo"),
        _mk(
            2,
            folder,
            "b.mp3",
            tracknumber="1",
            title="Symfo (Marcus Schossow Remix)",
            album="Symfo",
        ),
    ]

    report = track_conflicts._classify(files)

    assert report.flagged == 2
    assert report.high == 2
    row = _find(report, 1)
    assert row is not None
    assert row.reason == track_conflicts._REASON_HIGH


def test_same_title_different_container_is_low() -> None:
    # An .mp3 and a .flac of one song is usually a deliberate duplicate encode.
    folder = _MUSIC / "Pendulum" / "(2008) In Silico"
    files = [
        _mk(
            1,
            folder,
            "03 Propane.mp3",
            ext=".mp3",
            tracknumber="3",
            title="Propane Nightmares",
            album="In Silico",
        ),
        _mk(
            2,
            folder,
            "03 Propane.flac",
            ext=".flac",
            tracknumber="3",
            title="Propane Nightmares",
            album="In Silico",
        ),
    ]

    report = track_conflicts._classify(files)

    assert report.low == 2
    assert report.high == 0
    row = _find(report, 1)
    assert row is not None
    assert row.reason == track_conflicts._REASON_LOW


def test_multi_album_folder_is_review_context_not_flagged() -> None:
    # A singles/compilation folder holds several releases, so two files at track 1 says
    # nothing. These stay visible but outside the headline count.
    folder = _MUSIC / "Blue Stahli" / "Singles"
    files = [
        _mk(
            1,
            folder,
            "a.mp3",
            tracknumber="1",
            title="Kill Me Every Time",
            album="Kill Me Every Time",
        ),
        _mk(2, folder, "b.mp3", tracknumber="1", title="Seasons", album="Seasons"),
    ]

    report = track_conflicts._classify(files)

    assert report.flagged == 0
    assert report.folder_context == 2
    assert report.folder_context_rows[0].reason == track_conflicts._REASON_MULTI_ALBUM


def test_non_album_folder_without_album_tags_is_review_context() -> None:
    # The leaf-name guard catches the same folders when their files carry no album tag, which
    # the album-count test alone cannot see.
    folder = _MUSIC / "Blue Stahli" / "Remixes"
    files = [
        _mk(1, folder, "a.mp3", tracknumber="2", title="Kill Me Every Time (Instrumental)"),
        _mk(2, folder, "b.mp3", tracknumber="2", title="Own Little World (Remorse Code remix)"),
    ]

    report = track_conflicts._classify(files)

    assert report.flagged == 0
    assert report.folder_context == 2
    assert report.folder_context_rows[0].reason == track_conflicts._REASON_NON_ALBUM


# --- slot parsing --------------------------------------------------------------------


def test_slash_form_and_bare_number_share_one_slot() -> None:
    # "7" and "7/12" are the same position; only the part before the slash counts.
    folder = _MUSIC / "A" / "B"
    files = [
        _mk(1, folder, "a.mp3", tracknumber="7", title="X", album="B"),
        _mk(2, folder, "b.mp3", tracknumber="7/12", title="X", album="B"),
    ]

    assert track_conflicts._classify(files).flagged == 2


def test_different_discs_do_not_collide() -> None:
    # A two-disc release legitimately has a track 1 on each disc.
    folder = _MUSIC / "A" / "B"
    files = [
        _mk(1, folder, "a.mp3", tracknumber="1", discnumber="1", title="X", album="B"),
        _mk(2, folder, "b.mp3", tracknumber="1", discnumber="2", title="Y", album="B"),
    ]

    assert track_conflicts._classify(files).flagged == 0


def test_missing_discnumber_defaults_to_disc_one() -> None:
    # Single-disc releases routinely omit discnumber; treating those files as disc-less
    # would make every one of them collide with a file that does carry "1".
    folder = _MUSIC / "A" / "B"
    files = [
        _mk(1, folder, "a.mp3", tracknumber="4", title="X", album="B"),
        _mk(2, folder, "b.mp3", tracknumber="4", discnumber="1", title="X", album="B"),
    ]

    assert track_conflicts._classify(files).flagged == 2


def test_files_without_a_track_number_are_ignored() -> None:
    folder = _MUSIC / "A" / "B"
    files = [
        _mk(1, folder, "a.mp3", title="X", album="B"),
        _mk(2, folder, "b.mp3", tracknumber="", title="Y", album="B"),
        _mk(3, folder, "c.mp3", tracknumber="not-a-number", title="Z", album="B"),
    ]

    report = track_conflicts._classify(files)
    assert report.flagged == 0
    assert report.total_files == 3


def test_separate_folders_never_collide() -> None:
    files = [
        _mk(1, _MUSIC / "A" / "One", "a.mp3", tracknumber="1", title="X", album="One"),
        _mk(2, _MUSIC / "A" / "Two", "b.mp3", tracknumber="1", title="X", album="Two"),
    ]

    assert track_conflicts._classify(files).flagged == 0


# --- counts, ordering, narrowing -----------------------------------------------------


def test_tier_counts_sum_to_flagged_and_context_is_outside() -> None:
    album = _MUSIC / "A" / "Album"
    singles = _MUSIC / "A" / "Singles"
    files = [
        _mk(1, album, "a.mp3", tracknumber="1", title="X", album="Album"),
        _mk(2, album, "b.mp3", tracknumber="1", title="Y", album="Album"),
        _mk(3, singles, "c.mp3", tracknumber="1", title="P", album="P"),
        _mk(4, singles, "d.mp3", tracknumber="1", title="Q", album="Q"),
    ]

    report = track_conflicts._classify(files)

    assert report.high + report.medium + report.low == report.flagged
    assert report.flagged == 2
    assert report.folder_context == 2


def test_rows_are_ordered_by_tier_then_file_id() -> None:
    dup = _MUSIC / "A" / "Dup"
    wrong = _MUSIC / "A" / "Wrong"
    files = [
        _mk(1, dup, "a.mp3", tracknumber="1", title="X", album="Dup"),
        _mk(2, dup, "b.mp3", tracknumber="1", title="X", album="Dup"),
        _mk(3, wrong, "c.mp3", tracknumber="1", title="P", album="Wrong"),
        _mk(4, wrong, "d.mp3", tracknumber="1", title="Q", album="Wrong"),
    ]

    rows = track_conflicts._classify(files).rows

    assert [r.file_id for r in rows] == [3, 4, 1, 2]  # high (3,4) before medium (1,2)


def test_narrowing_preserves_library_wide_counts() -> None:
    album = _MUSIC / "A" / "Album"
    files = [
        _mk(1, album, "a.mp3", tracknumber="1", title="X", album="Album"),
        _mk(2, album, "b.mp3", tracknumber="1", title="Y", album="Album"),
    ]
    report = track_conflicts._classify(files)

    narrowed = track_conflicts._narrow(report, {}, tier=None, limit=1, group=False, folder=None)

    assert len(narrowed.rows) == 1
    assert narrowed.flagged == 2  # unchanged by the view
    assert narrowed.high == 2


def test_tier_filter_returns_no_context_rows() -> None:
    singles = _MUSIC / "A" / "Singles"
    files = [
        _mk(1, singles, "a.mp3", tracknumber="1", title="P", album="P"),
        _mk(2, singles, "b.mp3", tracknumber="1", title="Q", album="Q"),
    ]
    report = track_conflicts._classify(files)

    narrowed = track_conflicts._narrow(report, {}, tier="low", limit=None, group=False, folder=None)

    assert narrowed.folder_context_rows == []


def test_group_view_counts_flagged_and_context_separately() -> None:
    album = _MUSIC / "A" / "Album"
    singles = _MUSIC / "A" / "Singles"
    files = [
        _mk(1, album, "a.mp3", tracknumber="1", title="X", album="Album"),
        _mk(2, album, "b.mp3", tracknumber="1", title="Y", album="Album"),
        _mk(3, singles, "c.mp3", tracknumber="1", title="P", album="P"),
        _mk(4, singles, "d.mp3", tracknumber="1", title="Q", album="Q"),
    ]
    report = track_conflicts._classify(files)
    counts = {str(album): 2, str(singles): 2}

    grouped = track_conflicts._narrow(
        report, counts, tier=None, limit=None, group=True, folder=None
    )

    by_folder = {g.folder: g for g in grouped.groups}
    assert by_folder[str(album)].flagged == 2
    assert by_folder[str(album)].folder_context == 0
    assert by_folder[str(singles)].flagged == 0
    assert by_folder[str(singles)].folder_context == 2
    # A group's file_ids only ever names flagged files, so a fix flow cannot pick up context.
    assert by_folder[str(singles)].file_ids == []


def test_folder_expansion_is_exact_not_a_prefix() -> None:
    outer = _MUSIC / "A" / "Album"
    inner = _MUSIC / "A" / "Album Deluxe"
    files = [
        _mk(1, outer, "a.mp3", tracknumber="1", title="X", album="Album"),
        _mk(2, outer, "b.mp3", tracknumber="1", title="Y", album="Album"),
        _mk(3, inner, "c.mp3", tracknumber="1", title="P", album="Deluxe"),
        _mk(4, inner, "d.mp3", tracknumber="1", title="Q", album="Deluxe"),
    ]
    report = track_conflicts._classify(files)

    narrowed = track_conflicts._narrow(
        report, {}, tier=None, limit=None, group=False, folder=str(outer)
    )

    assert {r.file_id for r in narrowed.rows} == {1, 2}


# --- integration: scan real audio, then detect ---------------------------------------


def _file_id(settings: Settings, folder: Path, filename: str) -> int:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        return row.id
    finally:
        conn.close()


def test_detect_integration_flags_and_is_read_only(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    album = music_dir / "Artist" / "Album"
    make_track(album / "01 One.mp3", {"tracknumber": ["1"], "title": ["One"], "album": ["Album"]})
    make_track(album / "02 Two.mp3", {"tracknumber": ["2"], "title": ["Two"], "album": ["Album"]})
    make_track(album / "02 Dup.mp3", {"tracknumber": ["2"], "title": ["Two"], "album": ["Album"]})
    scan_library(engine_settings)

    report = detect_track_conflicts(engine_settings)

    assert report.flagged == 2
    assert report.medium == 2
    dup_id = _file_id(engine_settings, album, "02 Dup.mp3")
    row = _find(report, dup_id)
    assert row is not None
    assert row.track == 2

    conn = connect(engine_settings.db_path)
    try:
        assert store.any_staged(conn) is False
    finally:
        conn.close()


def test_detect_rejects_an_unknown_tier(engine_settings: Settings, music_dir: Path) -> None:
    make_track(music_dir / "a.mp3", {"tracknumber": ["1"]})
    scan_library(engine_settings)

    with pytest.raises(ValueError, match="unknown tier"):
        detect_track_conflicts(engine_settings, tier="urgent")


def test_to_dict_shape(engine_settings: Settings, music_dir: Path) -> None:
    album = music_dir / "Artist" / "Album"
    make_track(album / "a.mp3", {"tracknumber": ["1"], "title": ["X"], "album": ["Album"]})
    make_track(album / "b.mp3", {"tracknumber": ["1"], "title": ["Y"], "album": ["Album"]})
    scan_library(engine_settings)

    payload = detect_track_conflicts(engine_settings).to_dict()

    assert set(payload) == {
        "rows",
        "total_files",
        "flagged",
        "high",
        "medium",
        "low",
        "folder_context",
        "folder_context_rows",
        "groups",
        "summary",
    }
    rows = payload["rows"]
    assert isinstance(rows, list)
    assert set(rows[0]) == {
        "file_id",
        "folder",
        "filename",
        "disc",
        "track",
        "title",
        "tier",
        "reason",
        "peers",
    }
