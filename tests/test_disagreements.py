"""Unit tests for the tag-vs-MusicBrainz-release detector (``engine/disagreements.py``).

The release lookup is injected as a fake :class:`MBReleaseSource`, so these never touch the
network. Most cases drive the pure classifier with :class:`_FileInput` rows; the end-to-end
path through the real tool is covered once at the bottom via ``make_track``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from conftest import make_track
from tagmend.engine import disagreements
from tagmend.engine.disagreements import Tier, _classify, _FileInput
from tagmend.engine.library import scan_library
from tagmend.engine.musicbrainz import MBMedium, MBRelease, MBTrack

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

_RELEASE_ID = "rel-1"


class FakeReleaseSource:
    """An in-memory :class:`tagmend.engine.musicbrainz.MBReleaseSource` for DI in tests."""

    def __init__(self, table: dict[str, MBRelease | None]) -> None:
        self._table = table
        self.lookups: list[str] = []

    def release_by_mbid(self, mbid: str) -> MBRelease | None:
        self.lookups.append(mbid)
        return self._table.get(mbid)


def _track(
    number: str, title: str, *, rt: str = "", rec: str = "", credit: str = "Band"
) -> MBTrack:
    return MBTrack(
        position=int(number) if number.isdigit() else 0,
        number=number,
        title=title,
        release_track_mbid=rt or f"rt-{number}",
        recording_mbid=rec or f"rec-{number}",
        artist_credit=credit,
        artist_mbids=("artist-1",),
    )


def _release(*tracks: MBTrack, **overrides: object) -> MBRelease:
    fields: dict[str, object] = {
        "mbid": _RELEASE_ID,
        "title": "Real Album",
        "artist_credit": "Band",
        "artist_mbids": ("artist-1",),
        "date": "1997",
        "country": "US",
        "status": "Official",
        "barcode": "12345",
        "media": (
            MBMedium(
                position=1,
                title="",
                format="CD",
                track_count=len(tracks),
                tracks=tracks,
            ),
        ),
    }
    fields.update(overrides)
    return MBRelease(**fields)  # type: ignore[arg-type]


def _f(file_id: int = 1, **overrides: object) -> _FileInput:
    """Build one file input; defaults agree with ``_release(_track("1", "Song One"))``."""
    fields: dict[str, object] = {
        "file_id": file_id,
        "folder": r"C:\m\Band\Album",
        "filename": f"{file_id}.mp3",
        "release_id": _RELEASE_ID,
        "release_track_id": "rt-1",
        "recording_id": "rec-1",
        "album": "Real Album",
        "albumartist": "Band",
        "artist": "Band",
        "title": "Song One",
        "tracknumber": "1",
        "discnumber": "1",
        "date": "1997",
        "releasecountry": "US",
        "musicbrainz_albumstatus": "Official",
    }
    fields.update(overrides)
    return _FileInput(**fields)  # type: ignore[arg-type]


def _run(
    files: list[_FileInput],
    release: MBRelease | None = None,
) -> disagreements.DisagreementsReport:
    source = FakeReleaseSource({_RELEASE_ID: release or _release(_track("1", "Song One"))})
    return _classify(files, source, limit=None)


# --- an agreeing file is silent ------------------------------------------------------


def test_a_file_matching_its_release_flags_nothing() -> None:
    report = _run([_f()])

    assert report.flagged == 0
    assert report.rows == []
    assert report.releases_checked == 1


def test_a_file_with_no_release_id_is_never_looked_up() -> None:
    source = FakeReleaseSource({})
    report = _classify([_f(release_id=None)], source, limit=None)

    assert report.flagged == 0
    assert report.skipped_no_release_id == 1
    assert source.lookups == []


def test_each_release_is_fetched_once_for_all_its_files() -> None:
    source = FakeReleaseSource(
        {_RELEASE_ID: _release(_track("1", "Song One"), _track("2", "Song Two"))},
    )
    files = [
        _f(1),
        _f(2, release_track_id="rt-2", recording_id="rec-2", title="Song Two", tracknumber="2"),
    ]
    report = _classify(files, source, limit=None)

    assert source.lookups == [_RELEASE_ID]
    assert report.flagged == 0


# --- high: the file is not on the release it names -----------------------------------


def test_a_file_whose_track_id_is_not_on_the_release_is_high() -> None:
    report = _run([_f(release_track_id="rt-99", recording_id="rec-99")])

    assert report.high == 1
    assert report.rows[0].field == "musicbrainz_releasetrackid"
    assert "not on" in report.rows[0].reason


def test_a_release_musicbrainz_does_not_know_is_reported_not_an_error() -> None:
    source = FakeReleaseSource({_RELEASE_ID: None})
    report = _classify([_f()], source, limit=None)

    assert report.flagged == 0
    assert report.unknown_releases == 1


# --- medium: a grouping or ordering field disagrees ----------------------------------


def test_a_wrong_album_title_is_medium() -> None:
    report = _run([_f(album="Wrong Album")])

    assert report.medium == 1
    row = report.rows[0]
    assert row.field == "album"
    assert row.have == "Wrong Album"
    assert row.want == "Real Album"


def test_a_wrong_albumartist_is_medium() -> None:
    report = _run([_f(albumartist="Wrong Band")])

    assert report.medium == 1
    assert report.rows[0].field == "albumartist"


def test_a_wrong_track_number_is_medium() -> None:
    report = _run([_f(tracknumber="7")])

    assert report.medium == 1
    assert report.rows[0].field == "tracknumber"
    assert report.rows[0].want == "1"


def test_a_wrong_title_is_medium() -> None:
    report = _run([_f(title="Some Other Song")])

    assert report.medium == 1
    assert report.rows[0].field == "title"


def test_a_blank_field_is_a_fill_not_a_disagreement() -> None:
    # The glossary separates the two: a gap is tag against absent, a disagreement is tag
    # against an external source. Counting blanks as disagreements would bury the real
    # contradictions under thousands of them.
    report = _run([_f(tracknumber=None)])

    assert report.flagged == 0
    assert report.fills == 1
    assert report.fill_rows[0].field == "tracknumber"
    assert report.fill_rows[0].have == ""
    assert report.fill_rows[0].want == "1"


def test_a_track_number_with_a_total_compares_on_the_position() -> None:
    # Tags carry "1/12" where MusicBrainz carries "1". Same position, so no disagreement.
    report = _run([_f(tracknumber="1/12")])

    assert report.flagged == 0


def test_a_leading_zero_track_number_still_agrees() -> None:
    report = _run([_f(tracknumber="01")])

    assert report.flagged == 0


def test_casing_and_spacing_alone_do_not_disagree() -> None:
    report = _run([_f(album="  real   ALBUM ")])

    assert report.flagged == 0


def test_punctuation_does_disagree() -> None:
    # The same reason detect_album_conflicts keeps punctuation significant: it changes the
    # grouping key downstream.
    report = _run([_f(album="Real-Album")])

    assert report.medium == 1


# --- low: a provenance field disagrees -----------------------------------------------


def test_a_wrong_release_country_is_low() -> None:
    report = _run([_f(releasecountry="RU")])

    assert report.low == 1
    assert report.rows[0].field == "releasecountry"


def test_a_wrong_album_status_is_low() -> None:
    report = _run([_f(musicbrainz_albumstatus="Bootleg")])

    assert report.low == 1


def test_a_wrong_date_is_low() -> None:
    report = _run([_f(date="2019")])

    assert report.low == 1
    assert report.rows[0].field == "date"


def test_a_date_agreeing_on_the_year_alone_is_not_a_disagreement() -> None:
    # MusicBrainz carries 1997 while the tag carries the full release date it came from.
    report = _run([_f(date="1997-09-18")], _release(_track("1", "Song One"), date="1997"))

    assert report.flagged == 0


# --- release-level fields work without a track match ---------------------------------


def test_release_level_fields_are_checked_even_with_no_track_ids() -> None:
    report = _run([_f(release_track_id=None, recording_id=None, album="Wrong Album")])

    fields = {r.field for r in report.rows}
    assert "album" in fields
    # No track ids means no track to match, which is a gap rather than a wrong id.
    assert "musicbrainz_releasetrackid" not in fields
    assert report.unmatched_tracks == 1


def test_track_level_fields_are_skipped_with_no_track_ids() -> None:
    report = _run([_f(release_track_id=None, recording_id=None, title="Anything At All")])

    assert {r.field for r in report.rows} == set()


def test_the_recording_id_matches_when_the_release_track_id_is_missing() -> None:
    report = _run([_f(release_track_id=None, title="Wrong Title")])

    assert report.medium == 1
    assert report.rows[0].field == "title"


# --- counts and narrowing ------------------------------------------------------------


def test_tier_counts_sum_to_flagged() -> None:
    report = _run([_f(1, album="Wrong", releasecountry="RU"), _f(2, release_track_id="rt-9")])

    assert report.high + report.medium + report.low == report.flagged


def test_limit_caps_the_releases_fetched_and_reports_the_remainder() -> None:
    source = FakeReleaseSource(
        {
            "rel-a": _release(_track("1", "A"), mbid="rel-a", title="A Album"),
            "rel-b": _release(_track("1", "B"), mbid="rel-b", title="B Album"),
        },
    )
    files = [
        _f(1, release_id="rel-a", album="Wrong A"),
        _f(2, release_id="rel-b", album="Wrong B"),
    ]
    report = _classify(files, source, limit=1)

    assert len(source.lookups) == 1
    assert report.releases_checked == 1
    assert report.releases_remaining == 1
    assert report.more is True


def test_groups_summarize_one_folder_each() -> None:
    report = _run([_f(1, album="Wrong Album"), _f(2, title="Wrong Title")])

    assert len(report.groups) == 1
    group = report.groups[0]
    assert group.folder == r"C:\m\Band\Album"
    assert group.file_count == 2
    assert group.flagged == 2
    assert group.fields == {"album": 1, "title": 1}
    assert group.release_title == "Real Album"


def test_the_recording_id_still_matches_when_the_release_track_id_is_wrong() -> None:
    # A file carrying a stale release-track id but the right recording id is still placed,
    # so its track-level fields are checked rather than skipped.
    report = _run([_f(release_track_id="rt-99", title="Wrong Title")])

    fields = {r.field for r in report.rows}
    assert fields == {"title"}


# --- end to end through the real tool ------------------------------------------------


def test_detect_disagreements_end_to_end(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "a.mp3",
        {
            "album": ["Wrong Album"],
            "albumartist": ["Band"],
            "title": ["Song One"],
            "tracknumber": ["1"],
            "musicbrainz_albumid": [_RELEASE_ID],
            "musicbrainz_releasetrackid": ["rt-1"],
        },
    )
    scan_library(engine_settings)

    source = FakeReleaseSource({_RELEASE_ID: _release(_track("1", "Song One"))})
    report = disagreements.detect_disagreements(engine_settings, client=source)

    # One real contradiction. The fields the generated file simply lacks are fills.
    assert report.flagged == 1
    assert report.rows[0].field == "album"
    assert report.rows[0].have == "Wrong Album"
    assert report.rows[0].want == "Real Album"
    assert report.rows[0].tier == Tier.MEDIUM.value
    assert {r.field for r in report.fill_rows} >= {
        "releasecountry",
        "musicbrainz_albumstatus",
        "discnumber",
    }
    assert all(r.have == "" for r in report.fill_rows)
