"""Unit tests for the tag-vs-MusicBrainz-release detector (``engine/disagreements.py``).

The release lookup is injected as a fake :class:`MBReleaseSource`, so these never touch the
network. Most cases drive the pure classifier with :class:`_FileInput` rows; the end-to-end
path through the real tool is covered once at the bottom via ``make_track``.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

import pytest

from conftest import FOLDER_SPELLINGS, make_track, spell_folder
from tagmend.engine import disagreements
from tagmend.engine.detector_core import Tier
from tagmend.engine.disagreements import _classify, _FileInput
from tagmend.engine.library import scan_library
from tagmend.engine.musicbrainz import MBMedium, MBRelease, MBTrack, MusicBrainzError

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


def _track(  # noqa: PLR0913 - one keyword per track field, cohesive by design
    number: str,
    title: str,
    *,
    position: int | None = None,
    rt: str = "",
    rec: str = "",
    credit: str = "Band",
) -> MBTrack:
    """Build one track. *position* defaults to *number* when that is a plain integer.

    A vinyl medium numbers by side (``A1``), so those cases pass *position* explicitly.
    """
    resolved = position if position is not None else (int(number) if number.isdigit() else 0)
    return MBTrack(
        position=resolved,
        number=number,
        title=title,
        release_track_mbid=rt or f"rt-{resolved}",
        recording_mbid=rec or f"rec-{resolved}",
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
        "release_mbid": _RELEASE_ID,
        "release_track_mbid": "rt-1",
        "recording_mbid": "rec-1",
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
    return _classify(files, source, release_limit=None)


# --- an agreeing file is silent ------------------------------------------------------


def test_a_file_matching_its_release_flags_nothing() -> None:
    report = _run([_f()])

    assert report.flagged == 0
    assert report.rows == []
    assert report.releases_checked == 1


def test_a_file_with_no_release_mbid_is_never_looked_up() -> None:
    source = FakeReleaseSource({})
    report = _classify([_f(release_mbid=None)], source, release_limit=None)

    assert report.flagged == 0
    assert report.skipped_no_release_mbid == 1
    assert source.lookups == []


def test_each_release_is_fetched_once_for_all_its_files() -> None:
    source = FakeReleaseSource(
        {_RELEASE_ID: _release(_track("1", "Song One"), _track("2", "Song Two"))},
    )
    files = [
        _f(1),
        _f(2, release_track_mbid="rt-2", recording_mbid="rec-2", title="Song Two", tracknumber="2"),
    ]
    report = _classify(files, source, release_limit=None)

    assert source.lookups == [_RELEASE_ID]
    assert report.flagged == 0


# --- high: the file is not on the release it names -----------------------------------


def test_a_file_whose_track_id_is_not_on_the_release_is_high() -> None:
    report = _run([_f(release_track_mbid="rt-99", recording_mbid="rec-99")])

    assert report.high == 1
    assert report.rows[0].field == "musicbrainz_releasetrackid"
    assert "not on" in report.rows[0].reason


def test_a_release_musicbrainz_does_not_know_is_reported_not_an_error() -> None:
    source = FakeReleaseSource({_RELEASE_ID: None})
    report = _classify([_f()], source, release_limit=None)

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
    report = _run([_f(release_track_mbid=None, recording_mbid=None, album="Wrong Album")])

    fields = {r.field for r in report.rows}
    assert "album" in fields
    # No track ids means no track to match, which is a gap rather than a wrong id.
    assert "musicbrainz_releasetrackid" not in fields
    assert report.unmatched_tracks == 1


def test_track_level_fields_are_skipped_with_no_track_ids() -> None:
    report = _run([_f(release_track_mbid=None, recording_mbid=None, title="Anything At All")])

    assert {r.field for r in report.rows} == set()


def test_the_recording_mbid_matches_when_the_release_track_mbid_is_missing() -> None:
    report = _run([_f(release_track_mbid=None, title="Wrong Title")])

    assert report.medium == 1
    assert report.rows[0].field == "title"


# --- counts and narrowing ------------------------------------------------------------


def test_tier_counts_sum_to_flagged() -> None:
    report = _run([_f(1, album="Wrong", releasecountry="RU"), _f(2, release_track_mbid="rt-9")])

    assert report.high + report.medium + report.low == report.flagged


def test_flagged_counts_files_and_flagged_fields_counts_rows() -> None:
    report = _run([_f(album="Wrong Album", title="Wrong Title")])

    assert report.flagged == 1
    assert report.flagged_fields == 2
    assert report.medium == 1


def test_a_negative_release_limit_is_refused(engine_settings: Settings) -> None:
    with pytest.raises(ValueError, match="release_limit must be >= 0"):
        disagreements.detect_disagreements(
            engine_settings,
            release_limit=-1,
            client=FakeReleaseSource({}),
        )


def test_limit_caps_the_releases_fetched_and_reports_the_remainder() -> None:
    source = FakeReleaseSource(
        {
            "rel-a": _release(_track("1", "A"), mbid="rel-a", title="A Album"),
            "rel-b": _release(_track("1", "B"), mbid="rel-b", title="B Album"),
        },
    )
    files = [
        _f(1, release_mbid="rel-a", album="Wrong A"),
        _f(2, release_mbid="rel-b", album="Wrong B"),
    ]
    report = _classify(files, source, release_limit=1)

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
    assert group.flagged_fields == 2
    assert group.folder_context == 0
    assert group.tiers == {"medium": 2}
    assert group.file_ids == [1, 2]
    assert group.fields == {"album": 1, "title": 1}
    assert group.releases == [
        {"release_mbid": _RELEASE_ID, "release_title": "Real Album", "file_count": 2},
    ]


def test_one_group_per_folder_even_with_two_releases() -> None:
    source = FakeReleaseSource(
        {
            "rel-a": _release(_track("1", "Song One"), mbid="rel-a", title="A Album"),
            "rel-b": _release(_track("1", "Song One"), mbid="rel-b", title="B Album"),
        },
    )
    files = [
        _f(1, release_mbid="rel-a", album="Wrong A"),
        _f(2, release_mbid="rel-b", album="Wrong B"),
    ]

    report = _classify(files, source, release_limit=None)

    assert len(report.groups) == 1
    group = report.groups[0]
    assert group.flagged == 2
    assert [r["release_mbid"] for r in group.releases] == ["rel-a", "rel-b"]


def test_group_file_ids_exclude_fill_only_files() -> None:
    report = _run([_f(1, album="Wrong Album"), _f(2, releasecountry=None)])

    group = report.groups[0]
    assert group.file_ids == [1]
    assert group.fills == 1
    assert group.file_count == 2


def test_groups_ship_only_in_the_grouped_view() -> None:
    report = _run([_f(1, album="Wrong Album")])

    flat = disagreements._narrow(report, tier=None, folder_key=None, limit=None, group=False)
    grouped = disagreements._narrow(report, tier=None, folder_key=None, limit=None, group=True)

    assert flat.groups == []
    assert len(grouped.groups) == 1
    assert grouped.rows == []


def test_grouped_view_respects_tier() -> None:
    report = _run(
        [
            _f(1, folder=r"C:\m\Band\High", release_track_mbid="rt-99", recording_mbid="rec-99"),
            _f(2, folder=r"C:\m\Band\Low", releasecountry="GB"),
        ],
    )

    view = disagreements._narrow(report, tier="high", folder_key=None, limit=None, group=True)

    assert [g.folder for g in view.groups] == [r"C:\m\Band\High"]
    assert view.groups[0].flagged == 1
    assert view.groups[0].file_ids == [1]
    assert view.groups[0].tiers == {"high": 1}
    assert (view.flagged, view.high, view.low) == (2, 1, 1)  # run counts unchanged


def test_the_recording_mbid_still_matches_when_the_release_track_mbid_is_wrong() -> None:
    # A file carrying a stale release-track id but the right recording id is still placed,
    # so its track-level fields are checked rather than skipped.
    report = _run([_f(release_track_mbid="rt-99", title="Wrong Title")])

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
    # No discnumber: the release has one medium, so there is nothing to say about the disc.
    assert {r.field for r in report.fill_rows} == {
        "artist",
        "date",
        "releasecountry",
        "musicbrainz_albumstatus",
    }
    assert all(r.have == "" for r in report.fill_rows)


@pytest.mark.parametrize("spelling", FOLDER_SPELLINGS)
def test_folder_argument_variants_match_the_same_rows(
    engine_settings: Settings,
    music_dir: Path,
    spelling: str,
) -> None:
    album = music_dir / "Band" / "Album"
    for folder in (album, music_dir / "Band" / "Other"):
        make_track(
            folder / "a.mp3",
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

    exact = disagreements.detect_disagreements(
        engine_settings,
        path=music_dir / "Band",
        folder=str(album),
        client=source,
    )
    variant = disagreements.detect_disagreements(
        engine_settings,
        path=music_dir / "Band",
        folder=spell_folder(album, spelling),
        client=source,
    )

    assert exact.total_files == 2
    assert [r.folder for r in exact.rows] == [str(album)]
    assert [r.file_id for r in exact.rows] == [r.file_id for r in variant.rows]
    assert [r.file_id for r in exact.fill_rows] == [r.file_id for r in variant.fill_rows]
    assert variant.total_files == exact.total_files


def _scan_two_releases(music_dir: Path, settings: Settings) -> tuple[Path, Path]:
    """Scan one wrong-album file on ``rel-a`` under ``A/X`` and one on ``rel-b`` under ``B/Y``."""
    folder_a = music_dir / "A" / "X"
    folder_b = music_dir / "B" / "Y"
    for folder, release_mbid in ((folder_a, "rel-a"), (folder_b, "rel-b")):
        make_track(
            folder / "a.mp3",
            {
                "album": ["Wrong Album"],
                "title": ["Song One"],
                "musicbrainz_albumid": [release_mbid],
                "musicbrainz_releasetrackid": ["rt-1"],
            },
        )
    scan_library(settings)
    return folder_a, folder_b


def _two_release_source() -> FakeReleaseSource:
    return FakeReleaseSource(
        {
            "rel-a": _release(_track("1", "Song One"), mbid="rel-a"),
            "rel-b": _release(_track("1", "Song One"), mbid="rel-b"),
        },
    )


def test_a_bare_folder_without_a_run_scope_is_refused(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder_a, _ = _scan_two_releases(music_dir, engine_settings)
    source = _two_release_source()

    with pytest.raises(ValueError, match="path="):
        disagreements.detect_disagreements(engine_settings, folder=str(folder_a), client=source)

    assert source.lookups == []


def test_path_scopes_the_run_and_fetches_nothing_outside_it(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _scan_two_releases(music_dir, engine_settings)
    source = _two_release_source()

    report = disagreements.detect_disagreements(
        engine_settings,
        path=music_dir / "A",
        client=source,
    )

    assert source.lookups == ["rel-a"]
    assert report.total_files == 1


def test_folder_narrows_a_scoped_run_and_wins_over_group(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder_a, _ = _scan_two_releases(music_dir, engine_settings)
    make_track(
        folder_a / "Disc 2" / "b.mp3",
        {
            "album": ["Wrong Album"],
            "title": ["Song One"],
            "musicbrainz_albumid": ["rel-a"],
            "musicbrainz_releasetrackid": ["rt-1"],
        },
    )
    scan_library(engine_settings)

    whole = disagreements.detect_disagreements(
        engine_settings,
        path=music_dir / "A",
        client=_two_release_source(),
    )
    view = disagreements.detect_disagreements(
        engine_settings,
        path=music_dir / "A",
        folder=str(folder_a),
        group=True,
        client=_two_release_source(),
    )

    assert whole.flagged == 2
    assert {r.folder for r in view.rows} == {str(folder_a)}
    assert view.groups == []
    assert view.flagged == whole.flagged


# --- the track's own artist credit ---------------------------------------------------


def test_a_wrong_artist_is_medium_against_the_track_credit() -> None:
    report = _run([_f(artist="Somebody Else")])

    assert report.medium == 1
    assert report.rows[0].field == "artist"
    assert report.rows[0].want == "Band"


def test_the_track_credit_wins_over_the_release_credit() -> None:
    # A guest track carries its own credit. The file should name the track's, not the album's.
    guest = _track("2", "Song Two", credit="Band feat. Guest")
    release = _release(_track("1", "Song One"), guest)
    report = _run(
        [
            _f(
                release_track_mbid="rt-2",
                recording_mbid="rec-2",
                title="Song Two",
                tracknumber="2",
                artist="Band",
            )
        ],
        release,
    )

    assert report.medium == 1
    assert report.rows[0].field == "artist"
    assert report.rows[0].want == "Band feat. Guest"


def test_the_artist_is_not_checked_without_a_matched_track() -> None:
    # The credit is per track, so with no track matched there is nothing to compare against.
    report = _run([_f(release_track_mbid=None, recording_mbid=None, artist="Somebody Else")])

    assert "artist" not in {r.field for r in report.rows}


# --- vinyl numbering and typography are not disagreements ----------------------------


def test_a_vinyl_side_number_agrees_with_the_sequential_position() -> None:
    # 30 releases in the library number their tracks A1..B12 for a vinyl medium, and Picard
    # writes the sequential position, not the side designation. Comparing the two strings
    # flagged every file on every one of those releases.
    vinyl = _release(
        _track("A1", "Song One", position=1),
        _track("A2", "Song Two", position=2),
    )
    report = _run([_f(tracknumber="1")], vinyl)

    assert report.flagged == 0


def test_a_file_numbered_with_the_side_designation_also_agrees() -> None:
    # Either spelling is accepted, because either is a defensible reading of the release.
    vinyl = _release(_track("A1", "Song One", position=1))
    report = _run([_f(tracknumber="A1")], vinyl)

    assert report.flagged == 0


def test_a_genuinely_wrong_number_on_a_vinyl_release_still_disagrees() -> None:
    vinyl = _release(
        _track("A1", "Song One", position=1),
        _track("A2", "Song Two", position=2),
    )
    report = _run([_f(tracknumber="7")], vinyl)

    assert report.medium == 1
    assert report.rows[0].field == "tracknumber"


def test_a_curly_apostrophe_is_not_a_disagreement() -> None:
    # MusicBrainz writes typographic punctuation. No consumer distinguishes the two forms,
    # and folding them keeps the report about differences that matter.
    curly = _release(_track("1", "Someone\u2019s Standing on My Chest"))
    report = _run([_f(title="Someone's Standing on My Chest")], curly)

    assert report.flagged == 0


def test_a_dash_variant_is_not_a_disagreement() -> None:
    dashed = _release(_track("1", "Song One"), title="1994\u20132006 Chaos Years")
    report = _run([_f(album="1994-2006 Chaos Years")], dashed)

    assert report.flagged == 0


def test_other_punctuation_still_disagrees() -> None:
    # A colon against a hyphen is a real difference, not a typographic variant of one.
    report = _run([_f(album="Real: Album")])

    assert report.medium == 1


# --- findings from the adversarial review --------------------------------------------


def test_a_blank_date_is_a_fill_like_every_other_blank() -> None:
    # date is the only release-level field routed through its own comparison, and its blank
    # was being read as agreement, so the most useful fill of all was silently dropped.
    report = _run([_f(date=None, releasecountry=None, musicbrainz_albumstatus=None)])

    assert {r.field for r in report.fill_rows} == {
        "date",
        "releasecountry",
        "musicbrainz_albumstatus",
    }


def test_a_date_is_only_lenient_when_the_tag_is_the_more_precise_one() -> None:
    # MusicBrainz carrying a bare year under a full tag date is agreement. The reverse is a
    # real difference, and so is a shorter prefix that is not a whole date component.
    precise = _run([_f(date="1997-09-18")], _release(_track("1", "Song One"), date="1997"))
    assert precise.flagged == 0

    truncated = _run([_f(date="19")], _release(_track("1", "Song One"), date="1997"))
    assert truncated.flagged == 1

    wrong_month = _run(
        [_f(date="1997-1")],
        _release(_track("1", "Song One"), date="1997-10-05"),
    )
    assert wrong_month.flagged == 1


def test_group_flagged_counts_files_like_the_headline_does() -> None:
    # One file with two wrong fields is one file and two fields. The report and the group
    # must not disagree about what the word counts.
    report = _run([_f(album="Wrong Album", releasecountry="RU")])

    assert report.flagged == 1
    assert sum(g.flagged for g in report.groups) == report.flagged
    assert report.groups[0].flagged_fields == 2


def test_releases_checked_counts_what_was_actually_fetched() -> None:
    class Raiser:
        def __init__(self) -> None:
            self.lookups: list[str] = []

        def release_by_mbid(self, mbid: str) -> MBRelease | None:
            self.lookups.append(mbid)
            if mbid == "rel-a":
                message = "boom"
                raise MusicBrainzError(message)
            return _release(_track("1", "Song One"), mbid="rel-b")

    report = _classify(
        [_f(1, release_mbid="rel-a"), _f(2, release_mbid="rel-b", album="Wrong")],
        Raiser(),
        release_limit=None,
    )

    assert report.errors == 1
    assert report.error_items == [{"key": "rel-a", "message": "boom"}]
    assert report.releases_checked == 1
    assert report.releases_attempted == 2


def test_a_single_disc_release_does_not_propose_a_disc_number() -> None:
    # Picard routinely omits discnumber on a single-disc release, and proposing 1 on every
    # such file would bury the report in thousands of rows that mean nothing.
    report = _run([_f(discnumber=None)])

    assert "discnumber" not in {r.field for r in report.fill_rows}


def test_a_multi_disc_release_still_proposes_the_disc_number() -> None:
    two_discs = _release(
        _track("1", "Song One"),
        media=(
            MBMedium(1, "", "CD", 1, (_track("1", "Song One"),)),
            MBMedium(
                2, "", "CD", 1, (_track("1", "Song Two", position=1, rt="rt-9", rec="rec-9"),)
            ),
        ),
    )
    report = _run(
        [_f(release_track_mbid="rt-9", recording_mbid="rec-9", discnumber=None, title="Song Two")],
        two_discs,
    )

    assert ("discnumber", "", "2") in {(r.field, r.have, r.want) for r in report.fill_rows}


def test_a_limit_caps_both_row_lists() -> None:
    report = _run(
        [
            _f(1, album="Wrong", releasecountry="RU", date=None),
            _f(2, album="Wrong Too", releasecountry="XX", date=None),
        ]
    )
    view = disagreements._narrow(report, tier=None, folder_key=None, limit=1, group=False)

    assert len(view.rows) == 1
    assert len(view.fill_rows) == 1
    assert view.flagged == report.flagged


def test_a_lowercase_vinyl_side_still_agrees() -> None:
    vinyl = _release(_track("A1", "Song One", position=1))
    report = _run([_f(tracknumber="a1")], vinyl)

    assert report.flagged == 0


def test_the_same_accented_title_in_two_unicode_forms_agrees() -> None:
    nfd = unicodedata.normalize("NFD", "Café Song")
    nfc = unicodedata.normalize("NFC", "Café Song")
    assert nfc != nfd

    report = _run([_f(title=nfc)], _release(_track("1", nfd)))

    assert report.flagged == 0


def test_a_medium_position_of_zero_is_not_proposed() -> None:
    # A payload missing a medium position parses as 0, and disc zero is not a real answer.
    broken = _release(
        _track("1", "Song One"),
        media=(MBMedium(0, "", "CD", 1, (_track("1", "Song One"),)),),
    )
    report = _run([_f(discnumber="1")], broken)

    assert "discnumber" not in {r.field for r in report.rows}


def test_an_exotic_digit_does_not_abort_the_run() -> None:
    # str.isdigit accepts characters int() rejects, which crashed the whole detect run.
    report = _run([_f(2, tracknumber="⑧")])

    assert report.total_files == 1
