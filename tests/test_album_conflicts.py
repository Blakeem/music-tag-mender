"""Unit tests for the intra-folder album-identity detector (``engine/album_conflicts.py``).

These drive the pure classifier directly with :class:`_FileInput` rows, the same shape
``detect_album_conflicts`` loads from the ledger, so no database or audio file is needed. The
end-to-end path through the real tool is covered once at the bottom via ``make_track``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from conftest import make_track
from tagmend.engine import album_conflicts
from tagmend.engine.album_conflicts import _REASON_NO_ALBUMARTIST, _classify, _FileInput
from tagmend.engine.library import scan_library

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings


def _f(  # noqa: PLR0913 - one keyword per detected field, cohesive by design
    file_id: int,
    folder: str = r"C:\m\Band\Album",
    filename: str = "t.mp3",
    album: str | None = "Album",
    albumartist: str | None = "Band",
    artist: str | None = "Band",
    release_id: str | None = None,
    year: str | None = None,
    compilation: str | None = None,
) -> _FileInput:
    """Build one file input; defaults describe a clean single-album folder member."""
    return _FileInput(
        file_id=file_id,
        folder=folder,
        filename=filename,
        album=album,
        albumartist=albumartist,
        artist=artist,
        release_id=release_id,
        year=year,
        compilation=compilation,
    )


# --- a coherent folder is silent -----------------------------------------------------


def test_a_folder_whose_files_agree_flags_nothing() -> None:
    report = _classify([_f(1), _f(2, filename="b.mp3"), _f(3, filename="c.mp3")])

    assert report.flagged == 0
    assert report.rows == []
    assert report.total_files == 3


def test_a_folder_agreeing_only_on_the_release_id_flags_nothing() -> None:
    # The release id is the strongest claim, so it settles the folder on its own even when
    # the album strings differ in spacing or case.
    report = _classify(
        [
            _f(1, release_id="rel-1", album="The Album"),
            _f(2, filename="b.mp3", release_id="rel-1", album="the  album"),
        ],
    )

    assert report.flagged == 0


def test_a_single_file_folder_is_never_a_conflict() -> None:
    assert _classify([_f(1)]).flagged == 0


# --- high: the files claim different releases ----------------------------------------


def test_partial_release_id_coverage_is_high() -> None:
    # 21 real folders look like this. A server keyed on the id puts the tagged files in one
    # album and the untagged ones in another, however identical their album strings are.
    report = _classify(
        [
            _f(1, release_id="rel-1"),
            _f(2, filename="b.mp3", release_id="rel-1"),
            _f(3, filename="c.mp3", release_id=None),
        ],
    )

    assert report.flagged == 1
    assert report.high == 1
    assert [r.file_id for r in report.rows] == [3]
    assert "release id" in report.rows[0].reason


def test_two_different_release_ids_is_high() -> None:
    report = _classify(
        [
            _f(1, release_id="rel-1"),
            _f(2, filename="b.mp3", release_id="rel-1"),
            _f(3, filename="c.mp3", release_id="rel-2"),
        ],
    )

    assert report.high == 1
    assert [r.file_id for r in report.rows] == [3]


def test_the_minority_is_flagged_not_the_majority() -> None:
    # The fix direction is always "join the majority", so only the files that have to change
    # are rows. A caller can hand exactly those ids to stage_tags_batch.
    report = _classify(
        [_f(i, filename=f"{i}.mp3", release_id="rel-1") for i in range(1, 9)]
        + [_f(9, filename="9.mp3", release_id="rel-2")],
    )

    assert report.flagged == 1
    assert report.rows[0].file_id == 9
    assert report.groups[0].majority_files == 8


def test_a_folder_with_no_majority_flags_all_but_one() -> None:
    # The Hackers-soundtrack shape: 14 files, 14 different identities, no album artist. Every
    # file but the largest identity has to move.
    report = _classify(
        [
            _f(i, filename=f"{i}.mp3", album=f"Album {i}", albumartist=None, artist=f"Artist {i}")
            for i in range(1, 15)
        ],
    )

    assert report.flagged == 13
    assert report.groups[0].identities == 14


# --- medium: the names disagree ------------------------------------------------------


def test_album_name_disagreement_without_release_ids_is_medium() -> None:
    report = _classify(
        [
            _f(1, album="The Crow: City of Angels"),
            _f(2, filename="b.mp3", album="The Crow: City of Angels"),
            _f(3, filename="c.mp3", album="The Crow- City Of Angels"),
        ],
    )

    assert report.medium == 1
    assert [r.file_id for r in report.rows] == [3]
    assert "album" in report.rows[0].reason


def test_albumartist_disagreement_without_release_ids_is_medium() -> None:
    report = _classify(
        [
            _f(1, albumartist="Band"),
            _f(2, filename="b.mp3", albumartist="Band"),
            _f(3, filename="c.mp3", albumartist="Other Band"),
        ],
    )

    assert report.medium == 1
    assert [r.file_id for r in report.rows] == [3]


def test_a_missing_albumartist_falls_back_to_the_track_artist() -> None:
    # A server with no album artist groups by the track artist, so a folder of one artist
    # stays one album while a mixed folder shatters.
    coherent = _classify(
        [
            _f(1, albumartist=None, artist="Band"),
            _f(2, filename="b.mp3", albumartist=None, artist="Band"),
        ],
    )
    assert coherent.flagged == 0

    # Band holds two of the three files, so this is Band's album with one guest track, not a
    # compilation. Only the guest file has to move.
    mixed = _classify(
        [
            _f(1, albumartist=None, artist="Band"),
            _f(2, filename="b.mp3", albumartist=None, artist="Band"),
            _f(3, filename="c.mp3", albumartist=None, artist="Guest"),
        ],
    )
    assert mixed.flagged == 1
    assert mixed.rows[0].file_id == 3
    assert mixed.rows[0].reason != _REASON_NO_ALBUMARTIST


def test_a_compilation_flag_stands_in_for_a_missing_album_artist() -> None:
    report = _classify(
        [
            _f(1, albumartist=None, artist="One", compilation="1"),
            _f(2, filename="b.mp3", albumartist=None, artist="Two", compilation="1"),
        ],
    )

    assert report.flagged == 0


def test_year_disagreement_without_release_ids_is_medium() -> None:
    report = _classify(
        [
            _f(1, year="2005"),
            _f(2, filename="b.mp3", year="2005"),
            _f(3, filename="c.mp3", year="2005-06-01"),
        ],
    )

    assert report.medium == 1
    assert [r.file_id for r in report.rows] == [3]


def test_album_names_differing_only_in_case_and_spacing_agree() -> None:
    report = _classify(
        [_f(1, album="Fiction"), _f(2, filename="b.mp3", album="  FICTION ")],
    )

    assert report.flagged == 0


# --- low: a titled multi-disc medium -------------------------------------------------


def test_a_disc_suffix_on_a_shared_base_title_is_low() -> None:
    # Picard writes the medium title into album for a titled multi-disc release. It is
    # deliberate, and it still shows as several albums, so it is reported at the low tier.
    report = _classify(
        [
            _f(1, album="X.20: 1986-2006 (disc 2: remiX)"),
            _f(2, filename="b.mp3", album="X.20: 1986-2006 (disc 3: Best Of)"),
            _f(3, filename="c.mp3", album="X.20: 1986-2006 (disc 3: Best Of)"),
        ],
    )

    assert report.low == 1
    assert report.medium == 0
    assert "disc" in report.rows[0].reason


def test_unrelated_titles_are_not_read_as_a_disc_set() -> None:
    report = _classify(
        [
            _f(1, album="Telekon Live"),
            _f(2, filename="b.mp3", album="Dream Corrosion Disc 2"),
        ],
    )

    assert report.low == 0
    assert report.medium == 1


# --- folder context ------------------------------------------------------------------


def test_a_singles_folder_is_context_not_a_defect() -> None:
    report = _classify(
        [
            _f(1, folder=r"C:\m\Band\Singles", album="One"),
            _f(2, folder=r"C:\m\Band\Singles", filename="b.mp3", album="Two"),
        ],
    )

    assert report.flagged == 0
    assert report.folder_context == 1
    assert report.rows == []
    assert len(report.folder_context_rows) == 1


def test_tier_counts_always_sum_to_flagged() -> None:
    report = _classify(
        [
            _f(1, release_id="rel-1"),
            _f(2, filename="b.mp3", release_id=None),
            _f(3, folder=r"C:\m\B\Two", album="A"),
            _f(4, folder=r"C:\m\B\Two", filename="b.mp3", album="B"),
            _f(5, folder=r"C:\m\B\Singles", album="X"),
            _f(6, folder=r"C:\m\B\Singles", filename="b.mp3", album="Y"),
        ],
    )

    assert report.high + report.medium + report.low == report.flagged
    assert report.folder_context == 1


# --- grouped view + narrowing --------------------------------------------------------


def test_groups_report_the_identity_breakdown_per_folder() -> None:
    report = _classify(
        [
            _f(1, release_id="rel-1"),
            _f(2, filename="b.mp3", release_id="rel-1"),
            _f(3, filename="c.mp3", release_id="rel-2"),
        ],
    )

    group = report.groups[0]
    assert group.folder == r"C:\m\Band\Album"
    assert group.file_count == 3
    assert group.flagged == 1
    assert group.identities == 2
    assert group.majority_files == 2


def test_a_context_folder_never_appears_under_a_tier_filter() -> None:
    files = [
        _f(1, folder=r"C:\m\B\Singles", album="One"),
        _f(2, folder=r"C:\m\B\Singles", filename="b.mp3", album="Two"),
    ]
    report = album_conflicts._narrow(_classify(files), tier="medium", folder=None, limit=None)

    assert report.rows == []


# --- end to end through the real tool ------------------------------------------------


def test_detect_album_conflicts_end_to_end(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    album = music_dir / "Band" / "Album"
    album.mkdir(parents=True)
    make_track(album / "a.mp3", {"album": ["Real Album"], "albumartist": ["Band"]})
    make_track(album / "b.flac", {"album": ["Real Album"], "albumartist": ["Band"]})
    make_track(album / "c.mp3", {"album": ["Different Album"], "albumartist": ["Band"]})
    scan_library(engine_settings)

    report = album_conflicts.detect_album_conflicts(engine_settings)

    assert report.total_files == 3
    assert report.flagged == 1
    assert report.rows[0].filename == "c.mp3"
    assert report.medium == 1


# --- blank album belongs to detect_album_gaps ----------------------------------------


def test_a_blank_album_file_is_left_to_the_album_gaps_detector() -> None:
    # A file with no album has no release identity to conflict with. It is a gap, not a
    # conflict, and detect_album_gaps already reports it. Counting it here would double-report
    # it AND let the blank identity win the majority vote, pointing the fix the wrong way.
    report = _classify(
        [
            _f(1, album="Real Album"),
            _f(2, filename="b.mp3", album=None),
            _f(3, filename="c.mp3", album=""),
        ],
    )

    assert report.flagged == 0
    assert report.total_files == 3


def test_blank_album_files_never_win_the_majority() -> None:
    report = _classify(
        [
            _f(1, album=None, albumartist=None, artist="A"),
            _f(2, filename="b.mp3", album=None, albumartist=None, artist="B"),
            _f(3, filename="c.mp3", album=None, albumartist=None, artist="C"),
            _f(4, filename="d.mp3", album="Real Album"),
            _f(5, filename="e.mp3", album="Other Album"),
        ],
    )

    assert report.flagged == 1
    assert report.groups[0].majority_identity.endswith("Real Album")


# --- a bonus disc is the same phenomenon as a numbered one ---------------------------


def test_a_bonus_disc_suffix_is_low_like_a_numbered_one() -> None:
    report = _classify(
        [
            _f(1, album="X.20: 1986-2006 (disc 3: Best Of)"),
            _f(2, filename="b.mp3", album="X.20: 1986-2006 (disc 3: Best Of)"),
            _f(3, filename="c.mp3", album="X.20: 1986-2006 (bonus disc: Extras)"),
        ],
    )

    assert report.low == 1
    assert report.medium == 0


def test_a_parenthetical_without_the_word_disc_is_not_a_disc_set() -> None:
    report = _classify(
        [
            _f(1, album="Fiction"),
            _f(2, filename="b.mp3", album="Fiction (Deluxe Edition)"),
        ],
    )

    assert report.low == 0
    assert report.medium == 1


# --- the compilation shape: one album, many artists, no album artist -----------------


def test_one_album_many_artists_and_no_albumartist_flags_every_file() -> None:
    # Three real soundtrack folders look exactly like this: all 14 files agree on the album
    # title and none carries an albumartist, so each file falls back to its own track artist
    # and the one album shows as fourteen. Every file needs the same fix, so every file is a
    # row, and naming a "majority" track artist here would point the fix at one guest artist.
    report = _classify(
        [
            _f(
                i,
                filename=f"{i}.mp3",
                album="Hackers Soundtrack",
                albumartist=None,
                artist=f"Artist {i}",
            )
            for i in range(1, 8)
        ],
    )

    assert report.flagged == 7
    assert report.high == 7
    assert report.rows[0].reason == _REASON_NO_ALBUMARTIST
    assert report.groups[0].majority_identity == "Hackers Soundtrack"


def test_the_compilation_shape_needs_more_than_one_track_artist() -> None:
    report = _classify(
        [
            _f(1, album="One Album", albumartist=None, artist="Band"),
            _f(2, filename="b.mp3", album="One Album", albumartist=None, artist="Band"),
        ],
    )

    assert report.flagged == 0


def test_a_folder_where_some_files_have_an_albumartist_is_not_the_compilation_shape() -> None:
    # One file already carries an album artist, so the folder has a majority to normalize
    # toward and the ordinary minority rule applies.
    report = _classify(
        [
            _f(1, album="One Album", albumartist="Real Band", artist="A"),
            _f(2, filename="b.mp3", album="One Album", albumartist="Real Band", artist="B"),
            _f(3, filename="c.mp3", album="One Album", albumartist=None, artist="C"),
        ],
    )

    assert report.flagged == 1
    assert report.rows[0].file_id == 3
    assert report.rows[0].reason != _REASON_NO_ALBUMARTIST


def test_the_compilation_shape_needs_one_shared_album_title() -> None:
    # Different album titles as well as different artists is an ordinary split, not a
    # compilation missing its album artist.
    report = _classify(
        [
            _f(1, album="One Album", albumartist=None, artist="A"),
            _f(2, filename="b.mp3", album="Another Album", albumartist=None, artist="B"),
            _f(3, filename="c.mp3", album="Third Album", albumartist=None, artist="C"),
        ],
    )

    assert all(r.reason != _REASON_NO_ALBUMARTIST for r in report.rows)


def test_a_compilation_flag_already_set_is_not_the_missing_albumartist_shape() -> None:
    report = _classify(
        [
            _f(1, album="One Album", albumartist=None, artist="A", compilation="1"),
            _f(
                2,
                filename="b.mp3",
                album="One Album",
                albumartist=None,
                artist="B",
                compilation="1",
            ),
        ],
    )

    assert report.flagged == 0
