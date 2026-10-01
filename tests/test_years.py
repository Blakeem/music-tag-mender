"""Integration tests for the year fill (:mod:`tagmend.engine.years`).

These use real temp audio files (the silent templates) across all four formats and a real
temp ledger via the ``engine_settings`` fixture, so they exercise the full loop end to end
(scan → ``resolve_years`` → ``diff_tags`` → ``commit_tags`` → ``read_tags`` /
``revert_commit``) with **no network**. A fake :class:`MBReleaseGroupSource` is injected at
the ``resolve_years(client=...)`` signature, mapping ``(artist, album)`` → ``MBReleaseGroup``
(or ``None`` for "no usable release group").

``tagmend.engine.tags`` is imported FIRST so its module-load ``RegisterFreeformKey`` runs
before ``make_track`` writes any ``originaldate`` via raw mutagen easy mode (the M4A
freeform-atom path), mirroring ``test_artists.py``'s import chain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import mutagen
import pytest

from conftest import make_track
from tagmend.engine import axis, staging, store, versioning, years
from tagmend.engine.db import connect
from tagmend.engine.library import list_files as library_list
from tagmend.engine.library import scan_library
from tagmend.engine.musicbrainz import MBReleaseGroup, MusicBrainzClient, MusicBrainzError
from tagmend.engine.schema import apply_schema

# Import tags so its module-load RegisterFreeformKey runs before make_track writes an
# ``originaldate`` via raw mutagen easy mode (the M4A freeform atom must be registered).
from tagmend.engine.tags import read_tags

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

_FORMATS = [".mp3", ".flac", ".m4a", ".ogg"]


class FakeMBReleaseGroupSource:
    """An in-memory :class:`tagmend.engine.musicbrainz.MBReleaseGroupSource` for DI in tests.

    Maps ``(artist, album)`` → :class:`MBReleaseGroup` (or ``None`` for "no usable release group").
    A pair absent from the map also yields ``None``. Records the lookups it received.
    """

    def __init__(self, table: dict[tuple[str, str], MBReleaseGroup | None]) -> None:
        self._table = table
        self.lookups: list[tuple[str, str]] = []

    def album_first_release(self, artist: str, album: str) -> MBReleaseGroup | None:
        self.lookups.append((artist, album))
        return self._table.get((artist, album))


def _mb(date: str, *, title: str = "Album", rgid: str = "rg-1") -> MBReleaseGroup:
    return MBReleaseGroup(
        album_title=title, original_date=date, release_group_mbid=rgid, release_mbid=None
    )


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

    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
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

    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    years.resolve_years(engine_settings, client=fake)
    staging.commit_tags(engine_settings)

    on_disk = read_tags(track).tags
    assert on_disk["originaldate"] == ["1970"]
    assert on_disk["date"] == ["2015"]  # reissue year preserved on disk


def test_blank_fill_stages_the_full_first_release_date_from_a_search_answer(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    scan_library(engine_settings)
    group = {
        "id": "rg-1",
        "title": "Paranoid",
        "primary-type": "Album",
        "first-release-date": "1970-09-18",
        "score": 100,
    }
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"release-groups": [group]}),
    )
    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        with MusicBrainzClient("TagMend/test", conn, rate_per_sec=0.0, transport=transport) as mb:
            years.resolve_years(engine_settings, client=mb)
    finally:
        conn.close()

    [view] = staging.diff_tags(engine_settings)
    assert view.diff["originaldate"] == {"from": [], "to": ["1970-09-18"]}


# --- (b) present value: already-tagged file is settled, never overwritten ----------


def test_existing_originaldate_records_done_without_a_lookup(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "a.mp3",
        {"artist": ["Black Sabbath"], "album": ["Paranoid"], "originaldate": ["1970"]},
    )
    scan_library(engine_settings)

    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1971")})
    result = years.resolve_years(engine_settings, client=fake)

    assert result.settled == 1
    assert result.staged_files == 0
    # The already-tagged file is never even looked up (no overwrite).
    assert fake.lookups == []
    assert len(staging.diff_tags(engine_settings)) == 0
    assert [v.year_status for v in library_list(engine_settings)] == ["done"]


def test_resolve_years_never_overwrites_a_disk_value_the_mirror_lacks(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "a.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    scan_library(engine_settings)
    audio = mutagen.File(track, easy=True)  # type: ignore[attr-defined]
    audio["originaldate"] = ["1969"]
    audio.save()  # on disk only: no rescan, so the mirror still reads it as blank

    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    result = years.resolve_years(engine_settings, client=fake)

    assert result.staged_files == 0
    assert result.settled == 1
    assert staging.diff_tags(engine_settings) == []
    assert read_tags(track).tags["originaldate"] == ["1969"]


# --- skip: no album / no artist ------------------------------------------------------


def test_file_without_album_is_skipped(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Solo"]})
    scan_library(engine_settings)

    fake = FakeMBReleaseGroupSource({})
    result = years.resolve_years(engine_settings, client=fake)
    # No album means no year identity, so the file is never selected.
    assert result.settled == 0
    assert result.pending_remaining == 0
    assert result.staged_files == 0
    assert [v.year_status for v in library_list(engine_settings)] == ["no_identity"]


def test_file_without_artist_is_skipped(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"album": ["Orphan Album"]})
    scan_library(engine_settings)

    fake = FakeMBReleaseGroupSource({})
    result = years.resolve_years(engine_settings, client=fake)
    assert result.settled == 0
    assert result.pending_remaining == 0
    assert result.staged_files == 0
    assert [v.year_status for v in library_list(engine_settings)] == ["no_identity"]


# --- (e) grouping: one mapping per album --------------------------------------------


def test_groups_by_album_identity_one_lookup_per_group(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t1.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    make_track(music_dir / "t2.flac", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    make_track(music_dir / "o.mp3", {"artist": ["Pink Floyd"], "album": ["Animals"]})
    scan_library(engine_settings)

    fake = FakeMBReleaseGroupSource(
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

    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    result = years.resolve_years(engine_settings, client=fake)
    assert result.staged_files == 1
    assert fake.lookups == [("Black Sabbath", "Paranoid")]


def test_album_scope_narrows_to_that_album(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # Two files in different albums; resolve_years(value="Paranoid") must touch only the one.
    make_track(music_dir / "p.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    make_track(music_dir / "a.mp3", {"artist": ["Pink Floyd"], "album": ["Animals"]})
    scan_library(engine_settings)
    paranoid_id = _file_id(engine_settings, music_dir, "p.mp3")

    fake = FakeMBReleaseGroupSource(
        {
            ("Black Sabbath", "Paranoid"): _mb("1970"),
            ("Pink Floyd", "Animals"): _mb("1977"),
        },
    )
    result = years.resolve_years(engine_settings, value="Paranoid", client=fake)

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

    fake = FakeMBReleaseGroupSource({("Obscure", "Demos"): None})
    result = years.resolve_years(engine_settings, client=fake)

    assert result.no_match == 1
    assert result.staged_files == 0

    view = next(v for v in library_list(engine_settings) if v.file_id == file_id)
    assert view.year_status == "no_match"
    assert view.year_source_artist == "Obscure"
    assert view.year_source_album == "Demos"


class _RaisingAlbumSource(FakeMBReleaseGroupSource):
    """A fake whose lookup fails for one pair, as during a MusicBrainz outage."""

    def album_first_release(self, artist: str, album: str) -> MBReleaseGroup | None:
        if (artist, album) == ("Band", "Album"):
            self.lookups.append((artist, album))
            message = "503 Service Unavailable"
            raise MusicBrainzError(message)
        return super().album_first_release(artist, album)


def test_a_musicbrainz_error_is_counted_and_itemized(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Band"], "album": ["Album"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "a.mp3")

    result = years.resolve_years(engine_settings, client=_RaisingAlbumSource({}))

    assert result.errors == 1
    assert result.error_items == [{"key": "Band - Album", "message": "503 Service Unavailable"}]
    assert result.staged_files == 0
    assert "errored" in result.summary
    assert staging.diff_tags(engine_settings) == []
    # No status row: the group stays pending so the next run retries it.
    view = next(v for v in library_list(engine_settings) if v.file_id == file_id)
    assert view.year_status == "pending"


def test_held_no_match_is_not_reselected(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Obscure"], "album": ["Demos"]})
    make_track(music_dir / "b.mp3", {"artist": ["Obscure"], "album": ["Demos"]})
    scan_library(engine_settings)
    fake = FakeMBReleaseGroupSource({("Obscure", "Demos"): None})
    first = years.resolve_years(engine_settings, client=fake)
    assert first.no_match == 2

    fake.lookups.clear()
    second = years.resolve_years(engine_settings, client=fake)

    assert second.settled == 0
    assert second.pending_remaining == 0
    assert fake.lookups == []


def test_result_follows_the_resolver_contract(engine_settings: Settings) -> None:
    result = years.resolve_years(engine_settings, client=FakeMBReleaseGroupSource({}))

    assert set(result.to_dict()) == {
        "settled",
        "staged_files",
        "no_match",
        "pending_remaining",
        "more",
        "mappings",
        "errors",
        "error_items",
        "summary",
    }


def test_no_match_skipped_on_rerun_until_identity_changes(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Obscure"], "album": ["Demos"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "a.mp3")

    fake = FakeMBReleaseGroupSource({("Obscure", "Demos"): None})
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

    fake2 = FakeMBReleaseGroupSource({("Obscure", "New Demos"): _mb("1990")})
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

    fake = FakeMBReleaseGroupSource({("Wrong Name", "Paranoid"): None})
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

    fake2 = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
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

    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    result = years.resolve_years(engine_settings, client=fake)

    assert result.settled == 1
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
    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    result = years.resolve_years(engine_settings, client=fake)
    assert result.settled == 1
    assert result.staged_files == 1


@pytest.mark.parametrize("status", ["no_match", "pending"])
def test_set_year_status_accepts_manual_only(engine_settings: Settings, status: str) -> None:
    with pytest.raises(ValueError, match="unknown status"):
        years.set_year_status(engine_settings, file_ids=[1], status=status)


# --- dry-run + precondition ----------------------------------------------------------


def test_dry_run_returns_mappings_but_stages_nothing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    make_track(music_dir / "b.flac", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    scan_library(engine_settings)

    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
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
        tags={"genre": ["Rock"]},
        origin="manual",
    )

    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
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
        tags={"genre": ["Rock"]},
        origin="manual",
    )

    fake = FakeMBReleaseGroupSource({})
    with pytest.raises(ValueError, match="commit or unstage pending changes first"):
        years.resolve_years(engine_settings, client=fake)


# --- limit / more loop ---------------------------------------------------------------


def test_limit_caps_files_and_reports_pending(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["A"], "album": ["One"]})
    make_track(music_dir / "b.mp3", {"artist": ["B"], "album": ["Two"]})
    make_track(music_dir / "c.mp3", {"artist": ["C"], "album": ["Three"]})
    scan_library(engine_settings)

    fake = FakeMBReleaseGroupSource(
        {
            ("A", "One"): _mb("1991"),
            ("B", "Two"): _mb("1992"),
            ("C", "Three"): _mb("1993"),
        },
    )
    first = years.resolve_years(engine_settings, client=fake, limit=2)
    assert first.settled == 2
    assert first.staged_files == 2
    assert first.pending_remaining == 1
    assert first.more is True


def test_two_identical_dry_runs_reprocess_the_same_groups(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["A"], "album": ["One"]})
    make_track(music_dir / "b.mp3", {"artist": ["B"], "album": ["Two"]})
    scan_library(engine_settings)

    fake = FakeMBReleaseGroupSource({("A", "One"): _mb("1991"), ("B", "Two"): _mb("1992")})
    first = years.resolve_years(engine_settings, client=fake, limit=1, dry_run=True)
    second = years.resolve_years(engine_settings, client=fake, limit=1, dry_run=True)

    # A dry run stages nothing and records no no_match, so the frontier is unchanged.
    assert first.to_dict() == second.to_dict()
    assert fake.lookups == [("A", "One"), ("A", "One")]
    assert second.more is False
    assert "Call again to continue" not in second.summary
    assert "A dry run records nothing" in second.summary

    # The real path DOES advance, and keeps saying so.
    real = years.resolve_years(engine_settings, client=fake, limit=1)
    assert real.more is True
    assert "Call again to continue" in real.summary


def test_summary_counts_files(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["A"], "album": ["One"]})
    make_track(music_dir / "b.flac", {"artist": ["A"], "album": ["One"]})
    make_track(music_dir / "c.mp3", {"artist": ["B"], "album": ["Two"]})
    make_track(music_dir / "d.mp3", {"artist": ["C"], "album": ["Three"], "originaldate": ["1999"]})
    scan_library(engine_settings)

    # ("B", "Two") is absent from the table → a no_match on that group's one file.
    fake = FakeMBReleaseGroupSource({("A", "One"): _mb("1991")})
    result = years.resolve_years(engine_settings, client=fake)

    # Every count is files: two filled, one no_match, one already carrying a year.
    assert result.settled == 4
    assert result.staged_files == 2
    assert result.no_match == 1
    assert "Settled 4 file(s): staged 2, no_match 1." in result.summary


# --- idempotent re-run after commit --------------------------------------------------


def test_rerun_after_commit_is_idempotent(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    scan_library(engine_settings)

    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    years.resolve_years(engine_settings, client=fake)
    staging.commit_tags(engine_settings)
    scan_library(engine_settings)

    second = years.resolve_years(engine_settings, client=fake)
    assert second.staged_files == 0
    assert second.settled == 0
    assert second.pending_remaining == 0
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

    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    years.resolve_years(engine_settings, client=fake)
    commit_result = staging.commit_tags(engine_settings)
    assert commit_result.commit_id is not None

    on_disk = read_tags(track).tags
    assert on_disk["originaldate"] == ["1970"]
    assert on_disk["date"] == ["2015"]

    versioning.revert_commit(engine_settings, commit_result.commit_id)

    restored = read_tags(track).tags
    assert "originaldate" not in restored  # back to blank
    assert restored["date"] == ["2015"]  # reissue year never disturbed


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

    fake = FakeMBReleaseGroupSource({("Obscure", "Demos"): None})
    result = years.resolve_years(engine_settings, client=fake, dry_run=True)

    assert result.no_match == 1
    assert result.staged_files == 0

    # ...but nothing is recorded, so the real run still has the group to do.
    view = next(v for v in library_list(engine_settings) if v.file_id == file_id)
    assert view.year_status == "pending"


def _edit_title_on_disk(path: Path, title: str) -> None:
    """Change ``title`` the way an external tagger would, with no rescan afterwards."""
    audio = mutagen.File(path, easy=True)  # type: ignore[attr-defined]
    audio["title"] = [title]
    audio.save()


def _committed_diff_fields(settings: Settings, commit_id: int | None) -> list[set[str]]:
    assert commit_id is not None
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return [set(revision.diff) for revision in store.revisions_for_commit(conn, commit_id)]
    finally:
        conn.close()


def test_resolve_years_keeps_disk_values_the_mirror_lacks(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(
        music_dir / "a.mp3",
        {"artist": ["Black Sabbath"], "album": ["Paranoid"], "title": ["Old Title"]},
    )
    scan_library(engine_settings)
    _edit_title_on_disk(track, "Title Edited In Picard")

    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    years.resolve_years(engine_settings, client=fake)
    result = staging.commit_tags(engine_settings)

    assert read_tags(track).tags["title"] == ["Title Edited In Picard"]
    assert read_tags(track).tags["originaldate"] == ["1970"]
    assert _committed_diff_fields(engine_settings, result.commit_id) == [{"originaldate"}]


def test_resolve_years_skips_missing_files(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    kept = make_track(music_dir / "kept.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    gone = make_track(music_dir / "gone.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    scan_library(engine_settings)
    gone.unlink()
    scan_library(engine_settings)  # flags the deleted file missing

    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    result = years.resolve_years(engine_settings, client=fake)

    # A missing file is never selected and never counted as pending.
    assert result.settled == 1
    assert result.pending_remaining == 0
    assert result.staged_files == 1
    assert [view.filename for view in staging.diff_tags(engine_settings)] == [kept.name]


def test_a_file_staging_refuses_is_itemized_and_its_sibling_still_settles(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "kept.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    gone = make_track(music_dir / "gone.mp3", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    scan_library(engine_settings)
    kept_id = _file_id(engine_settings, music_dir, "kept.mp3")
    gone_id = _file_id(engine_settings, music_dir, "gone.mp3")
    gone.unlink()  # after the scan, so the file is still selected

    fake = FakeMBReleaseGroupSource({("Black Sabbath", "Paranoid"): _mb("1970")})
    result = years.resolve_years(engine_settings, client=fake)

    assert result.staged_files == 1
    assert result.settled == 1
    assert [item["key"] for item in result.error_items] == [f"file_id={gone_id}"]
    assert "errored" in result.summary
    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        kept_row = axis.get_outcome(conn, axis.YEAR_AXIS, kept_id)
        gone_row = axis.get_outcome(conn, axis.YEAR_AXIS, gone_id)
    finally:
        conn.close()
    assert kept_row is not None
    assert kept_row.status == "done"
    assert gone_row is None
    view = next(v for v in library_list(engine_settings) if v.file_id == gone_id)
    assert view.year_status == "pending"
