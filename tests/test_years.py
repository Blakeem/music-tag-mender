"""Integration tests for the album-year fill (:mod:`tagmend.engine.years`).

These use real temp audio files (the silent templates) across all four formats and a real
temp ledger via the ``engine_settings`` fixture, so they exercise the full loop end to end
— scan → ``resolve_years`` → ``diff_tags`` → ``commit_tags`` → ``read_tags`` /
``revert_commit`` — with **no network**: a fake :class:`MBAlbumSource` is injected at the
``resolve_years(client=...)`` signature, mapping ``(artist, album)`` → ``MBAlbum`` (or
``None`` for "no usable release group").

``tagmend.engine.tags`` is imported FIRST so its module-load ``RegisterFreeformKey`` runs
before ``make_track`` writes any ``originaldate`` via raw mutagen easy mode (the M4A
freeform-atom path), mirroring ``test_artists.py``'s import chain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from conftest import make_track
from tagmend.engine import staging, store, versioning, years
from tagmend.engine.db import connect
from tagmend.engine.library import list_files as library_list
from tagmend.engine.library import scan_library
from tagmend.engine.musicbrainz import MBAlbum
from tagmend.engine.schema import apply_schema

# Import tags so its module-load RegisterFreeformKey runs before make_track writes an
# ``originaldate`` via raw mutagen easy mode (the M4A freeform atom must be registered).
from tagmend.engine.tags import read_tags

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

_FORMATS = [".mp3", ".flac", ".m4a", ".ogg"]


class FakeMBAlbumSource:
    """An in-memory :class:`tagmend.engine.musicbrainz.MBAlbumSource` for DI in tests.

    Maps ``(artist, album)`` → :class:`MBAlbum` (or ``None`` for "no usable release group").
    A pair absent from the map also yields ``None``. Records the lookups it received.
    """

    def __init__(self, table: dict[tuple[str, str], MBAlbum | None]) -> None:
        self._table = table
        self.lookups: list[tuple[str, str]] = []

    def album_first_release(self, artist: str, album: str) -> MBAlbum | None:
        self.lookups.append((artist, album))
        return self._table.get((artist, album))


def _mb(date: str, *, title: str = "Album", rgid: str = "rg-1") -> MBAlbum:
    return MBAlbum(album_title=title, original_date=date, release_group_id=rgid, release_mbid=None)


def _file_id(settings: Settings, folder: Path, filename: str) -> int:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        return row.id
    finally:
        conn.close()


# --- (a) blank-fill stages originaldate; date is never written -----------------------


def test_blank_fill_stages_originaldate_only(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "a.mp3",
        {"artist": ["Black Sabbath"], "album": ["Paranoid"], "date": ["2015"], "genre": ["Metal"]},
    )
    scan_library(engine_settings)

    fake = FakeMBAlbumSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    result = years.resolve_years(engine_settings, client=fake)

    assert result.staged_files == 1
    assert result.no_match == 0
    assert result.mappings == [
        {"artist": "Black Sabbath", "album": "Paranoid", "original_date": "1970"},
    ]

    views = staging.diff_tags(engine_settings)
    assert len(views) == 1
    diff = views[0].diff
    assert diff["originaldate"] == {"from": [], "to": ["1970"]}
    # date (the reissue year) is never touched.
    assert "date" not in diff


def test_commit_then_read_fills_originaldate_and_keeps_date(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(
        music_dir / "a.mp3",
        {"artist": ["Black Sabbath"], "album": ["Paranoid"], "date": ["2015"]},
    )
    scan_library(engine_settings)

    fake = FakeMBAlbumSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    years.resolve_years(engine_settings, client=fake)
    staging.commit_tags(engine_settings, origin="auto")

    on_disk = read_tags(track).tags
    assert on_disk["originaldate"] == ["1970"]
    assert on_disk["date"] == ["2015"]  # reissue year preserved on disk


# --- (b) skipped_present: already-tagged file is untouched ---------------------------


def test_existing_originaldate_is_skipped_present(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "a.mp3",
        {"artist": ["Black Sabbath"], "album": ["Paranoid"], "originaldate": ["1970"]},
    )
    scan_library(engine_settings)

    fake = FakeMBAlbumSource({("Black Sabbath", "Paranoid"): _mb("1971")})
    result = years.resolve_years(engine_settings, client=fake)

    assert result.skipped_present == 1
    assert result.staged_files == 0
    # The already-tagged file is never even looked up (no overwrite).
    assert fake.lookups == []
    assert len(staging.diff_tags(engine_settings)) == 0


# --- skip: no album / no artist ------------------------------------------------------


def test_file_without_album_is_skipped(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Solo"]})
    scan_library(engine_settings)

    fake = FakeMBAlbumSource({})
    result = years.resolve_years(engine_settings, client=fake)
    assert result.skipped_no_album == 1
    assert result.staged_files == 0


def test_file_without_artist_is_skipped(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"album": ["Orphan Album"]})
    scan_library(engine_settings)

    fake = FakeMBAlbumSource({})
    result = years.resolve_years(engine_settings, client=fake)
    assert result.skipped_no_artist == 1
    assert result.staged_files == 0


# --- (e) grouping: one mapping per album --------------------------------------------


def test_groups_by_album_identity_one_lookup_per_group(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t1.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    make_track(music_dir / "t2.flac", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    make_track(music_dir / "o.mp3", {"artist": ["Pink Floyd"], "album": ["Animals"]})
    scan_library(engine_settings)

    fake = FakeMBAlbumSource(
        {
            ("Black Sabbath", "Paranoid"): _mb("1970"),
            ("Pink Floyd", "Animals"): _mb("1977"),
        },
    )
    result = years.resolve_years(engine_settings, client=fake)

    assert result.staged_files == 3
    # One lookup per distinct album group (not per file).
    assert sorted(fake.lookups) == [("Black Sabbath", "Paranoid"), ("Pink Floyd", "Animals")]
    assert len(result.mappings) == 2


def test_albumartist_else_artist_identity(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # albumartist wins over artist for the lookup identity (the genre identity shape).
    make_track(
        music_dir / "comp.mp3",
        {"artist": ["Various"], "albumartist": ["Black Sabbath"], "album": ["Paranoid"]},
    )
    scan_library(engine_settings)

    fake = FakeMBAlbumSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    result = years.resolve_years(engine_settings, client=fake)
    assert result.staged_files == 1
    assert fake.lookups == [("Black Sabbath", "Paranoid")]


def test_album_scope_narrows_to_that_album(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # Two files in different albums; resolve_years(album="Paranoid") must touch only the one.
    make_track(music_dir / "p.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    make_track(music_dir / "a.mp3", {"artist": ["Pink Floyd"], "album": ["Animals"]})
    scan_library(engine_settings)
    paranoid_id = _file_id(engine_settings, music_dir, "p.mp3")

    fake = FakeMBAlbumSource(
        {
            ("Black Sabbath", "Paranoid"): _mb("1970"),
            ("Pink Floyd", "Animals"): _mb("1977"),
        },
    )
    result = years.resolve_years(engine_settings, album="Paranoid", client=fake)

    assert result.staged_files == 1
    # Only the requested album is even looked up (no library-wide fan-out).
    assert fake.lookups == [("Black Sabbath", "Paranoid")]
    staged_ids = {v.file_id for v in staging.diff_tags(engine_settings)}
    assert staged_ids == {paranoid_id}


# --- (d) no_match recorded + re-opened on identity change ----------------------------


def test_no_match_recorded_and_reported(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Obscure"], "album": ["Demos"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "a.mp3")

    fake = FakeMBAlbumSource({("Obscure", "Demos"): None})
    result = years.resolve_years(engine_settings, client=fake)

    assert result.no_match == 1
    assert result.staged_files == 0

    view = next(v for v in library_list(engine_settings) if v.file_id == file_id)
    assert view.year_status == "no_match"
    assert view.year_source_artist == "Obscure"
    assert view.year_source_album == "Demos"


def test_no_match_skipped_on_rerun_until_identity_changes(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Obscure"], "album": ["Demos"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "a.mp3")

    fake = FakeMBAlbumSource({("Obscure", "Demos"): None})
    years.resolve_years(engine_settings, client=fake)

    # Re-run: the non-stale no_match is held back (not re-looked-up).
    fake.lookups.clear()
    second = years.resolve_years(engine_settings, client=fake)
    assert second.no_match == 0
    assert fake.lookups == []

    # Change the album → the stored no_match goes stale → re-processable.
    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        store.replace_tags(conn, file_id, {"artist": ["Obscure"], "album": ["New Demos"]}, "now")
        conn.commit()
    finally:
        conn.close()

    fake2 = FakeMBAlbumSource({("Obscure", "New Demos"): _mb("1990")})
    third = years.resolve_years(engine_settings, client=fake2)
    assert third.staged_files == 1


def test_no_match_reopens_on_artist_fallback_change(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # The lookup identity falls back to artist (no albumartist); changing artist re-opens.
    make_track(music_dir / "a.mp3", {"artist": ["Wrong Name"], "album": ["Paranoid"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "a.mp3")

    fake = FakeMBAlbumSource({("Wrong Name", "Paranoid"): None})
    years.resolve_years(engine_settings, client=fake)

    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        store.replace_tags(
            conn,
            file_id,
            {"artist": ["Black Sabbath"], "album": ["Paranoid"]},
            "now",
        )
        conn.commit()
    finally:
        conn.close()

    fake2 = FakeMBAlbumSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    result = years.resolve_years(engine_settings, client=fake2)
    assert result.staged_files == 1


# --- manual exclusion ----------------------------------------------------------------


def test_manual_excluded_file_is_skipped(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "ex.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    make_track(music_dir / "keep.flac", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    scan_library(engine_settings)
    excluded_id = _file_id(engine_settings, music_dir, "ex.mp3")
    kept_id = _file_id(engine_settings, music_dir, "keep.flac")

    assert years.set_year_status(engine_settings, file_ids=[excluded_id], status="manual") == 1

    fake = FakeMBAlbumSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    result = years.resolve_years(engine_settings, client=fake)

    assert result.skipped_manual == 1
    assert result.staged_files == 1
    staged_ids = {v.file_id for v in staging.diff_tags(engine_settings)}
    assert staged_ids == {kept_id}


def test_set_year_status_by_value_scopes_on_album(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "a.mp3")

    assert years.set_year_status(engine_settings, value="Paranoid", status="manual") == 1
    view = next(v for v in library_list(engine_settings) if v.file_id == file_id)
    assert view.year_status == "manual"


def test_reset_year_status_requeues(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "a.mp3")
    years.set_year_status(engine_settings, file_ids=[file_id], status="manual")

    assert years.reset_year_status(engine_settings, file_ids=[file_id]) == 1
    fake = FakeMBAlbumSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    result = years.resolve_years(engine_settings, client=fake)
    assert result.skipped_manual == 0
    assert result.staged_files == 1


def test_set_year_status_rejects_unknown_status(engine_settings: Settings) -> None:
    with pytest.raises(ValueError, match="invalid status"):
        years.set_year_status(engine_settings, file_ids=[1], status="no_match")


# --- dry-run + precondition ----------------------------------------------------------


def test_dry_run_returns_mappings_but_stages_nothing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    make_track(music_dir / "b.flac", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    scan_library(engine_settings)

    fake = FakeMBAlbumSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    result = years.resolve_years(engine_settings, client=fake, dry_run=True)

    assert result.staged_files == 2  # would-stage count
    assert result.mappings == [
        {"artist": "Black Sabbath", "album": "Paranoid", "original_date": "1970"},
    ]
    assert len(staging.diff_tags(engine_settings)) == 0


def test_dry_run_ignores_empty_staging_precondition(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    other = make_track(music_dir / "other.mp3", {"artist": ["Someone"], "album": ["X"]})
    scan_library(engine_settings)

    other_id = _file_id(engine_settings, music_dir, other.name)
    staging.stage_tags(
        engine_settings,
        file_id=other_id,
        managed_tags={"genre": ["Rock"]},
        origin="manual",
    )

    fake = FakeMBAlbumSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    result = years.resolve_years(engine_settings, client=fake, dry_run=True)
    assert result.staged_files == 1


def test_non_dry_run_requires_empty_staging(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "a.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    staging.stage_tags(
        engine_settings,
        file_id=file_id,
        managed_tags={"genre": ["Rock"]},
        origin="manual",
    )

    fake = FakeMBAlbumSource({})
    with pytest.raises(ValueError, match="commit or unstage pending changes first"):
        years.resolve_years(engine_settings, client=fake)


# --- limit / more loop ---------------------------------------------------------------


def test_limit_caps_groups_and_reports_pending(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["A"], "album": ["One"]})
    make_track(music_dir / "b.mp3", {"artist": ["B"], "album": ["Two"]})
    make_track(music_dir / "c.mp3", {"artist": ["C"], "album": ["Three"]})
    scan_library(engine_settings)

    fake = FakeMBAlbumSource(
        {
            ("A", "One"): _mb("1991"),
            ("B", "Two"): _mb("1992"),
            ("C", "Three"): _mb("1993"),
        },
    )
    first = years.resolve_years(engine_settings, client=fake, limit=2, dry_run=True)
    assert first.processed == 2
    assert first.pending_remaining == 1
    assert first.more is True


def test_two_identical_dry_runs_reprocess_the_same_groups(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["A"], "album": ["One"]})
    make_track(music_dir / "b.mp3", {"artist": ["B"], "album": ["Two"]})
    scan_library(engine_settings)

    fake = FakeMBAlbumSource({("A", "One"): _mb("1991"), ("B", "Two"): _mb("1992")})
    first = years.resolve_years(engine_settings, client=fake, limit=1, dry_run=True)
    second = years.resolve_years(engine_settings, client=fake, limit=1, dry_run=True)

    # A dry run stages nothing and records no no_match, so the frontier is unchanged.
    assert first.to_dict() == second.to_dict()
    assert fake.lookups == [("A", "One"), ("A", "One")]
    assert "call again to continue" not in second.summary
    assert "Raise limit above 1" in second.summary
    assert "album= / file_ids=" in second.summary

    # The real path DOES advance (staged then committed), and keeps saying so.
    real = years.resolve_years(engine_settings, client=fake, limit=1)
    assert "call again to continue" in real.summary


def test_summary_labels_group_and_file_counts(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["A"], "album": ["One"]})
    make_track(music_dir / "b.flac", {"artist": ["A"], "album": ["One"]})
    make_track(music_dir / "c.mp3", {"artist": ["B"], "album": ["Two"]})
    make_track(music_dir / "d.mp3", {"artist": ["C"], "album": ["Three"], "originaldate": ["1999"]})
    scan_library(engine_settings)

    # ("B", "Two") is absent from the table → a no_match on that group's one file.
    fake = FakeMBAlbumSource({("A", "One"): _mb("1991")})
    result = years.resolve_years(engine_settings, client=fake)

    assert result.processed == 2  # album groups
    assert result.staged_files == 2  # files
    assert result.no_match == 1  # files
    assert result.skipped_present == 1  # files
    assert "Processed 2 album group(s):" in result.summary
    assert "staged 2 file(s)" in result.summary
    assert "no_match 1 file(s)" in result.summary
    assert "Skipped 1 file(s) present" in result.summary
    assert "0 file(s) no_album" in result.summary
    assert "0 file(s) no_artist" in result.summary
    assert "0 file(s) manual" in result.summary


# --- idempotent re-run after commit --------------------------------------------------


def test_rerun_after_commit_is_idempotent(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    scan_library(engine_settings)

    fake = FakeMBAlbumSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    years.resolve_years(engine_settings, client=fake)
    staging.commit_tags(engine_settings, origin="auto")
    scan_library(engine_settings)

    second = years.resolve_years(engine_settings, client=fake)
    assert second.staged_files == 0
    assert second.skipped_present == 1
    assert len(staging.diff_tags(engine_settings)) == 0


# --- revert round-trip across all four formats ---------------------------------------


@pytest.mark.parametrize("suffix", _FORMATS)
def test_commit_then_revert_commit_restores_blank_originaldate(
    engine_settings: Settings,
    music_dir: Path,
    suffix: str,
) -> None:
    track = make_track(
        music_dir / f"track{suffix}",
        {"artist": ["Black Sabbath"], "album": ["Paranoid"], "date": ["2015"]},
    )
    scan_library(engine_settings)

    fake = FakeMBAlbumSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    years.resolve_years(engine_settings, client=fake)
    commit_result = staging.commit_tags(engine_settings, origin="auto")
    assert commit_result.commit_id is not None

    on_disk = read_tags(track).tags
    assert on_disk["originaldate"] == ["1970"]
    assert on_disk["date"] == ["2015"]

    versioning.revert_commit(engine_settings, commit_result.commit_id)

    restored = read_tags(track).tags
    assert "originaldate" not in restored  # back to blank
    assert restored["date"] == ["2015"]  # reissue year never disturbed


# --- list_albums: blank_originaldate + limit + year_status/actionable filters -------


def test_list_albums_reports_blank_originaldate_count(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # Group with all files already carrying originaldate → 0 blanks.
    make_track(
        music_dir / "present.mp3",
        {"artist": ["Rush"], "album": ["Moving Pictures"], "originaldate": ["1981"]},
    )
    # Group with some blanks → the exact blank count.
    make_track(music_dir / "blank1.mp3", {"artist": ["Yes"], "album": ["Fragile"]})
    make_track(
        music_dir / "blank2.flac",
        {"artist": ["Yes"], "album": ["Fragile"], "originaldate": ["1971"]},
    )
    scan_library(engine_settings)

    rows = {row.album: row for row in years.list_albums(engine_settings)}
    assert rows["Moving Pictures"].blank_originaldate == 0
    assert rows["Fragile"].file_count == 2
    assert rows["Fragile"].blank_originaldate == 1
    # to_dict carries the new field.
    assert rows["Fragile"].to_dict()["blank_originaldate"] == 1


def test_list_albums_limit_caps_after_ordering(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Alpha"], "album": ["A1"]})
    make_track(music_dir / "b.mp3", {"artist": ["Bravo"], "album": ["B1"]})
    make_track(music_dir / "c.mp3", {"artist": ["Charlie"], "album": ["C1"]})
    scan_library(engine_settings)

    limited = years.list_albums(engine_settings, limit=2)
    assert [row.artist for row in limited] == ["Alpha", "Bravo"]


def test_list_albums_year_status_filter(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "a.mp3", {"artist": ["Alpha"], "album": ["A1"]})
    make_track(music_dir / "b.mp3", {"artist": ["Bravo"], "album": ["B1"]})
    scan_library(engine_settings)

    file_id = _file_id(engine_settings, music_dir, track.name)
    years.set_year_status(engine_settings, file_ids=[file_id], status="manual")

    manual = years.list_albums(engine_settings, year_status="manual")
    assert [row.album for row in manual] == ["A1"]

    pending = years.list_albums(engine_settings, year_status="pending")
    assert [row.album for row in pending] == ["B1"]


def test_list_albums_actionable_keeps_only_groups_with_blanks(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "full.mp3",
        {"artist": ["Rush"], "album": ["Moving Pictures"], "originaldate": ["1981"]},
    )
    make_track(music_dir / "blank.mp3", {"artist": ["Yes"], "album": ["Fragile"]})
    scan_library(engine_settings)

    rows = years.list_albums(engine_settings, actionable=True)
    assert [row.album for row in rows] == ["Fragile"]
    assert all(row.blank_originaldate > 0 for row in rows)
    # Unfiltered, the fully-tagged group is still listed.
    assert len(years.list_albums(engine_settings)) == 2


def test_list_albums_actionable_composes_with_year_status_and_precedes_limit(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # Alpha: blank but manual. Bravo: pending, no blanks. Charlie/Delta: pending + blank.
    manual_track = make_track(music_dir / "a.mp3", {"artist": ["Alpha"], "album": ["A1"]})
    make_track(
        music_dir / "b.mp3",
        {"artist": ["Bravo"], "album": ["B1"], "originaldate": ["1999"]},
    )
    make_track(music_dir / "c.mp3", {"artist": ["Charlie"], "album": ["C1"]})
    make_track(music_dir / "d.mp3", {"artist": ["Delta"], "album": ["D1"]})
    scan_library(engine_settings)

    manual_id = _file_id(engine_settings, music_dir, manual_track.name)
    years.set_year_status(engine_settings, file_ids=[manual_id], status="manual")

    both = years.list_albums(engine_settings, year_status="pending", actionable=True)
    assert [row.album for row in both] == ["C1", "D1"]

    # The filters run BEFORE limit: limit=1 keeps the first ACTIONABLE pending group,
    # not the first pending group (B1, which has no blanks).
    limited = years.list_albums(
        engine_settings,
        year_status="pending",
        actionable=True,
        limit=1,
    )
    assert [row.album for row in limited] == ["C1"]


def test_dry_run_counts_no_match_without_recording_it(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # A preview exists to show what the real run will do. The lookup happens either way, so
    # a group MusicBrainz cannot resolve must be COUNTED in the preview; only the sticky
    # status row is withheld until the real run.
    make_track(music_dir / "a.mp3", {"artist": ["Obscure"], "album": ["Demos"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "a.mp3")

    fake = FakeMBAlbumSource({("Obscure", "Demos"): None})
    result = years.resolve_years(engine_settings, client=fake, dry_run=True)

    assert result.no_match == 1
    assert result.staged_files == 0

    # ...but nothing is recorded, so the real run still has the group to do.
    view = next(v for v in library_list(engine_settings) if v.file_id == file_id)
    assert view.year_status == "pending"
