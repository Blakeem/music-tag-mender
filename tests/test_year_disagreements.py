"""Unit tests for the year-vs-release-group detector (``engine/year_disagreements.py``).

The release-group lookup is injected as a fake :class:`MBReleaseGroupCacheSource`, so these
never touch the network. Tier cases drive the pure classifier with :class:`_FileInput` rows.
Scope, view and lookup-cap cases run through the real tool over ``make_track`` files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest

from conftest import make_track
from tagmend.engine import detector_core, year_disagreements
from tagmend.engine.detector_core import Tier
from tagmend.engine.library import scan_library
from tagmend.engine.musicbrainz import MBReleaseGroup, MusicBrainzError
from tagmend.engine.year_disagreements import _REASON_NON_ALBUM, _classify, _FileInput

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from tagmend.config import Settings

_SABBATH: Final = ("Black Sabbath", "Paranoid")
_FOLDER: Final = "/m/Black Sabbath/Paranoid"

type _Answer = MBReleaseGroup | MusicBrainzError | None


class FakeReleaseGroupSource:
    """An in-memory release-group source that caches every answer it gives, like the client."""

    def __init__(self, table: Mapping[tuple[str, str], _Answer]) -> None:
        self._table = table
        self.cached: set[tuple[str, str]] = set()
        self.lookups: list[tuple[str, str]] = []

    def has_cached_album(self, artist: str, album: str) -> bool:
        return (artist, album) in self.cached

    def album_first_release(self, artist: str, album: str) -> MBReleaseGroup | None:
        self.lookups.append((artist, album))
        answer = self._table.get((artist, album))
        if isinstance(answer, MusicBrainzError):
            raise answer
        self.cached.add((artist, album))
        return answer


def _rg(year: str, title: str = "Paranoid", rgid: str = "rg-1") -> MBReleaseGroup:
    return MBReleaseGroup(
        album_title=title,
        original_date=year,
        release_group_mbid=rgid,
        release_mbid=None,
    )


def _f(file_id: int = 1, **overrides: object) -> _FileInput:
    """Build one file input whose years agree with ``_rg("1970")``."""
    fields: dict[str, object] = {
        "file_id": file_id,
        "folder": _FOLDER,
        "filename": f"{file_id}.mp3",
        "artist": _SABBATH[0],
        "album": _SABBATH[1],
        "originaldate": "1970",
        "date": "1970",
    }
    fields.update(overrides)
    return _FileInput(**fields)  # type: ignore[arg-type]


def _run(
    files: list[_FileInput],
    table: Mapping[tuple[str, str], _Answer] | None = None,
) -> year_disagreements.YearDisagreementsReport:
    source = FakeReleaseGroupSource(table if table is not None else {_SABBATH: _rg("1970")})
    return _classify(files, source, release_limit=200)


def _view(
    report: year_disagreements.YearDisagreementsReport,
    *,
    tier: str | None,
    folder_key: str | None,
    limit: int | None,
    group: bool,
) -> year_disagreements.YearDisagreementsReport:
    return detector_core.narrow(
        report,
        rows=report.rows,
        groups=report.groups,
        secondary_field="folder_context_rows",
        secondary_rows=report.folder_context_rows,
        secondary_in_tier=False,
        refold=year_disagreements._refold_group,
        key=year_disagreements._group_key,
        tier=tier,
        folder_key=folder_key,
        limit=limit,
        group=group,
    )


# --- tiers ---------------------------------------------------------------------------


def test_years_agreeing_with_the_first_release_flag_nothing() -> None:
    # A full originaldate in the right year agrees, and a later date is a reissue.
    report = _run([_f(originaldate="1970-09-18", date="2009-04-01")])

    assert report.flagged == 0
    assert report.rows == []
    assert report.release_groups_checked == 1


def test_an_originaldate_in_another_year_is_high() -> None:
    report = _run([_f(originaldate="1999-01-01")])

    assert report.high == 1
    assert report.flagged == 1
    row = report.rows[0]
    assert row.field == "originaldate"
    assert row.have == "1999-01-01"
    assert row.first_release_year == "1970"
    assert row.release_group_mbid == "rg-1"
    assert row.release_group_title == "Paranoid"
    assert row.tier == Tier.HIGH.value


def test_a_full_first_release_date_compares_by_its_year() -> None:
    # resolve_years writes the release group's full date, so the lookup carries one too.
    table = {_SABBATH: _rg("1970-09-18")}

    agreeing = _run([_f(originaldate="1970", date="1970-09-18")], table)
    differing = _run([_f(originaldate="1999-01-01")], table)

    assert agreeing.flagged == 0
    [row] = differing.rows
    assert row.first_release_year == "1970"


def test_an_originaldate_carrying_no_year_is_high() -> None:
    report = _run([_f(originaldate="unknown")])

    assert [(r.field, r.tier) for r in report.rows] == [("originaldate", Tier.HIGH.value)]


def test_a_date_before_the_first_release_is_medium() -> None:
    report = _run([_f(date="1969-12")])

    assert report.medium == 1
    assert [(r.field, r.have, r.tier) for r in report.rows] == [
        ("date", "1969-12", Tier.MEDIUM.value),
    ]


def test_no_row_is_ever_low() -> None:
    report = _run([_f(originaldate="1999", date="1960")])

    assert report.low == 0
    assert {r.tier for r in report.rows} == {Tier.HIGH.value, Tier.MEDIUM.value}


def test_a_file_with_both_years_wrong_counts_once_in_its_worst_tier() -> None:
    report = _run([_f(originaldate="1999", date="1960")])

    assert report.flagged == 1
    assert report.flagged_fields == 2
    assert (report.high, report.medium) == (1, 0)


# --- blanks --------------------------------------------------------------------------


def test_a_blank_originaldate_is_not_a_row_but_its_date_still_compares() -> None:
    report = _run([_f(originaldate=None, date="1960")])

    assert [r.field for r in report.rows] == ["date"]


def test_a_file_with_no_year_at_all_costs_no_lookup() -> None:
    source = FakeReleaseGroupSource({_SABBATH: _rg("1970")})

    report = _classify([_f(originaldate=None, date=None)], source, release_limit=200)

    assert source.lookups == []
    assert report.release_groups_checked == 0


def test_a_file_with_no_album_is_skipped_without_a_lookup() -> None:
    source = FakeReleaseGroupSource({})

    report = _classify([_f(album=None)], source, release_limit=200)

    assert source.lookups == []
    assert report.skipped_no_identity == 1


# --- lookup outcomes -----------------------------------------------------------------


def test_an_album_with_no_usable_release_group_flags_nothing() -> None:
    report = _run([_f(originaldate="1999")], {_SABBATH: None})

    assert report.flagged == 0
    assert report.unknown_release_groups == 1


def test_a_lookup_error_is_reported_and_flags_nothing() -> None:
    report = _run([_f(originaldate="1999")], {_SABBATH: MusicBrainzError("HTTP 500")})

    assert report.flagged == 0
    assert report.errors == 1
    assert report.error_items == [{"key": "Black Sabbath - Paranoid", "message": "HTTP 500"}]
    assert "errored" in report.summary


# --- folders -------------------------------------------------------------------------


def test_a_non_album_folder_reports_folder_context_outside_flagged() -> None:
    report = _run([_f(folder="/m/Black Sabbath/Singles", originaldate="1999")])

    assert report.rows == []
    assert report.flagged == 0
    assert report.high == 0
    assert report.folder_context == 1
    assert [r.reason for r in report.folder_context_rows] == [_REASON_NON_ALBUM]


def test_a_folder_holding_two_albums_is_grouped_once_per_album() -> None:
    files = [
        _f(1, originaldate="1999"),
        _f(2, album="Master of Reality", originaldate="1999"),
    ]
    table = {
        _SABBATH: _rg("1970"),
        ("Black Sabbath", "Master of Reality"): _rg("1971", "Master of Reality", "rg-2"),
    }

    view = _view(_run(files, table), tier=None, folder_key=None, limit=None, group=True)

    assert [(g.album, g.first_release_year, g.file_ids) for g in view.groups] == [
        ("Master of Reality", "1971", [2]),
        ("Paranoid", "1970", [1]),
    ]
    assert view.rows == []


def test_a_tier_filter_refolds_each_group_over_its_rows() -> None:
    files = [
        _f(1, originaldate="1999"),
        _f(2, date="1960"),
        _f(3, folder="/m/Black Sabbath/Singles", originaldate="1999"),
    ]
    report = _run(files)

    view = _view(report, tier="medium", folder_key=None, limit=None, group=True)

    assert [(g.flagged, g.file_ids, g.tiers) for g in view.groups] == [
        (1, [2], {Tier.MEDIUM.value: 1}),
    ]
    assert view.flagged == report.flagged == 2


def test_limit_caps_rows_and_groups_without_changing_counts() -> None:
    files = [_f(1, originaldate="1999"), _f(2, originaldate="1998", folder="/m/Other")]
    report = _run(files)

    flat = _view(report, tier=None, folder_key=None, limit=1, group=False)
    grouped = _view(report, tier=None, folder_key=None, limit=1, group=True)

    assert len(flat.rows) == 1
    assert len(grouped.groups) == 1
    assert flat.flagged == grouped.flagged == 2


# --- through the real tool -----------------------------------------------------------


def _scan_two_albums(music_dir: Path, settings: Settings) -> tuple[Path, Path]:
    paranoid = music_dir / "Black Sabbath" / "Paranoid"
    reality = music_dir / "Black Sabbath" / "Master of Reality"
    make_track(
        paranoid / "a.mp3",
        {
            "albumartist": ["Black Sabbath"],
            "artist": ["Ozzy"],
            "album": ["Paranoid"],
            "originaldate": ["1999"],
        },
    )
    make_track(
        reality / "b.mp3",
        {"artist": ["Black Sabbath"], "album": ["Master of Reality"], "date": ["1960"]},
    )
    scan_library(settings)
    return paranoid, reality


def _two_album_source() -> FakeReleaseGroupSource:
    return FakeReleaseGroupSource(
        {
            _SABBATH: _rg("1970"),
            ("Black Sabbath", "Master of Reality"): _rg("1971", "Master of Reality", "rg-2"),
        },
    )


def test_detect_year_disagreements_end_to_end(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    paranoid, reality = _scan_two_albums(music_dir, engine_settings)
    source = _two_album_source()

    report = year_disagreements.detect_year_disagreements(engine_settings, client=source)

    # The album artist wins over the track artist, as in resolve_years.
    assert sorted(source.lookups) == [("Black Sabbath", "Master of Reality"), _SABBATH]
    assert [(r.folder, r.field, r.tier) for r in report.rows] == [
        (str(paranoid), "originaldate", Tier.HIGH.value),
        (str(reality), "date", Tier.MEDIUM.value),
    ]
    assert report.to_dict()["flagged"] == 2


def test_release_limit_spends_only_on_uncached_groups(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _scan_two_albums(music_dir, engine_settings)
    source = _two_album_source()

    first = year_disagreements.detect_year_disagreements(
        engine_settings,
        release_limit=1,
        client=source,
    )
    reached_first = set(source.lookups)
    second = year_disagreements.detect_year_disagreements(
        engine_settings,
        release_limit=1,
        client=source,
    )

    assert len(reached_first) == 1
    assert (first.release_groups_checked, first.release_groups_remaining) == (1, 1)
    assert first.more is True
    # The first group is cached now, so it is free and the cap reaches the second.
    assert set(source.lookups) == {_SABBATH, ("Black Sabbath", "Master of Reality")}
    assert (second.release_groups_checked, second.release_groups_remaining) == (2, 0)
    assert second.more is False
    assert second.flagged == 2


def test_folder_narrows_the_view_and_wins_over_group(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    paranoid, _ = _scan_two_albums(music_dir, engine_settings)

    view = year_disagreements.detect_year_disagreements(
        engine_settings,
        folder=str(paranoid),
        group=True,
        client=_two_album_source(),
    )

    assert {r.folder for r in view.rows} == {str(paranoid)}
    assert view.groups == []
    assert view.flagged == 2


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"release_limit": -1}, "release_limit must be >= 0"),
        ({"limit": -1}, "limit must be >= 0"),
        ({"tier": "severe"}, "tier"),
    ],
)
def test_bad_arguments_are_refused_before_any_lookup(
    engine_settings: Settings,
    kwargs: dict[str, object],
    message: str,
) -> None:
    source = FakeReleaseGroupSource({})

    with pytest.raises(ValueError, match=message):
        year_disagreements.detect_year_disagreements(engine_settings, client=source, **kwargs)  # type: ignore[arg-type]

    assert source.lookups == []
