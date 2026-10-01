"""Tests for path coherence detection (:mod:`tagmend.engine.mismatch`).

Pure tests run the ``_classify`` core over constructed inputs, one per path layout and
comparison, flagged and clean. Integration tests scan generated audio, then detect, and cover
the dispositions, the CLI and the MCP wiring.
"""

from __future__ import annotations

import asyncio
import functools
import unicodedata
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FOLDER_SPELLINGS, make_track, spell_folder
from tagmend import config, mcp_server
from tagmend.cli import app
from tagmend.config import Settings
from tagmend.engine import artists, axis, mismatch, path_keys, staging, store, versioning
from tagmend.engine.db import connect
from tagmend.engine.detector_core import parse_position
from tagmend.engine.library import scan_library
from tagmend.engine.mismatch import (
    DISC_FOLDER_NUMBER,
    FILENAME_TITLE,
    FILENAME_TRACK,
    RELEASE_FOLDER_ALBUM,
    RELEASE_FOLDER_YEAR,
    TOP_FOLDER_ARTIST,
    detect_mismatches,
)
from tagmend.engine.path_text import clean_value

_MUSIC = Path("/library/music")
_SOUNDTRACKS = frozenset({"soundtracks"})
runner = CliRunner()


def _mk(file_id: int, rel: str, filename: str, **tags: str) -> mismatch._FileInput:
    return mismatch._FileInput(
        file_id=file_id,
        folder=str(_MUSIC / rel),
        filename=filename,
        tags=tags,
    )


def _padding(count: int = 20) -> list[mismatch._FileInput]:
    """Clean files that keep the top-folder difference rate under the reliability floor."""
    return [
        _mk(1000 + i, f"Clean{i}/Album", "x.mp3", albumartist=f"Clean{i}") for i in range(count)
    ]


def _flags(
    files: list[mismatch._FileInput],
    *,
    container: frozenset[str] = frozenset(),
) -> dict[int, set[str]]:
    report = mismatch._classify(files, _MUSIC, container_keys=container)
    return {r.file_id: {d.comparison for d in r.differences} for r in report.rows}


def _find(report: mismatch.MismatchesReport, file_id: int) -> mismatch.MismatchRow | None:
    return next((r for r in report.rows if r.file_id == file_id), None)


def _view(report: mismatch.MismatchesReport, **kwargs: object) -> mismatch.MismatchesReport:
    options: dict[str, object] = {
        "tier": None,
        "comparison": None,
        "folder_key": None,
        "limit": None,
        "group": False,
    }
    options.update(kwargs)
    return mismatch._narrow(report, **options)  # type: ignore[arg-type]


# --- top_folder_artist ---------------------------------------------------------------


def test_top_folder_artist_flags_another_artist_and_tolerates_formatting() -> None:
    files = [
        _mk(1, "Ozzy Osbourne/Down To Earth", "a.mp3", albumartist="Jem"),
        _mk(2, "Ozzy Osbourne/Down To Earth", "b.mp3", albumartist="ozzy osbourne"),
        _mk(3, "Royksopp/Melody AM", "a.mp3", albumartist="Röyksopp"),
        _mk(4, "Roeyksopp/Melody AM", "a.mp3", albumartist="Röyksopp"),
        _mk(5, "Doors, The/LA Woman", "a.mp3", albumartist="The Doors"),
        _mk(6, "Simon & Garfunkel/Bookends", "a.mp3", albumartist="Simon and Garfunkel"),
        _mk(7, "Neon Hitch/Single", "a.mp3", artist="Neon Hitch feat. Someone"),
        _mk(8, "!!!/Myth Takes", "a.mp3", albumartist="!!!"),
        # Equality, not containment: a collab extension of the folder's artist is a difference.
        _mk(9, "Lusine/Serial", "a.mp3", albumartist="Lusine ICL"),
        *_padding(),
    ]

    assert _flags(files) == {1: {TOP_FOLDER_ARTIST}, 9: {TOP_FOLDER_ARTIST}}


def test_top_folder_artist_reads_wrappers_containers_root_albums_and_curated_folders() -> None:
    files = [
        _mk(1, "Tool [Discography]/(1993) Undertow", "a.mp3", albumartist="Tool"),
        _mk(
            2,
            "Metallica - Discography 1983-2008/(1991) Metallica",
            "a.mp3",
            albumartist="Metallica",
        ),
        _mk(3, "Pink Floyd (1967-2014) FLAC/(1973) Dark Side", "a.mp3", albumartist="Pink Floyd"),
        _mk(4, "Soundtracks/Movie OST", "a.mp3", albumartist="Hans Zimmer"),
        # A root album folder has no top-folder signal.
        _mk(5, "The Doors", "a.mp3", albumartist="Someone Else"),
        # A curated file keeps the top-folder comparison.
        _mk(6, "Blue Stahli/Singles", "remix.mp3", albumartist="Celldweller"),
        *_padding(),
    ]

    report = mismatch._classify(files, _MUSIC, container_keys=_SOUNDTRACKS)

    assert {r.file_id: {d.comparison for d in r.differences} for r in report.rows} == {
        6: {TOP_FOLDER_ARTIST},
    }
    assert report.container_suppressed == {"Soundtracks": 1}


# --- release_folder_album ------------------------------------------------------------


@pytest.mark.parametrize(
    "release",
    [
        "(1999) Artist - Album Name",
        "1999 - Album Name",
        "Artist - 1999 - Album Name",
        "Album Name (1999)",
        "Album Name [FLAC] {Scene}",
        "(1999) Album Name (Deluxe Edition)",
        "(1999) ARTIST - ALBUM-NAME",
    ],
)
def test_release_folder_album_tolerates_each_release_layout(release: str) -> None:
    files = [
        _mk(1, f"Artist/{release}", "01 Song.mp3", albumartist="Artist", album="Album Name"),
        _mk(2, f"Artist/{release}/Cd1", "01 Song.mp3", albumartist="Artist", album="Album Name"),
    ]

    assert _flags(files) == {}


@pytest.mark.parametrize(
    ("release", "album"),
    [
        ("Limited Edition", "Limited Edition"),
        ("Disc 1", "Disc 1"),
        ("(2005) Artist - Bonus Tracks EP", "Bonus Tracks EP"),
        ("(2001) Artist - Limited Edition", "Limited Edition (Bonus CD)"),
        ("Limited Edition/CD 2", "Limited Edition (Disc 2)"),
    ],
)
def test_release_folder_album_tolerates_an_album_made_of_markers(release: str, album: str) -> None:
    files = [_mk(1, f"Artist/{release}", "01 Song.mp3", albumartist="Artist", album=album)]

    assert _flags(files) == {}


def test_release_folder_album_flags_an_album_made_of_markers_against_another_name() -> None:
    files = [
        _mk(1, "Artist/(2001) Artist - Other Thing", "a.mp3", albumartist="Artist", album="Disc 1"),
        _mk(2, "Artist/Real Album/CD 2", "a.mp3", albumartist="Artist", album="CD 2"),
        _mk(
            3,
            "Artist/(2001) Artist - Deluxe Edition",
            "a.mp3",
            albumartist="Artist",
            album="Limited Edition",
        ),
        *_padding(),
    ]

    report = mismatch._classify(files, _MUSIC)

    assert {r.file_id: _tier(report, r.file_id) for r in report.rows} == {
        1: "high",
        2: "high",
        3: "medium",
    }


def test_release_folder_album_flags_another_album_and_skips_curated_and_artist_roots() -> None:
    files = [
        _mk(1, "Artist/(1999) Artist - Other Record", "a.mp3", albumartist="Artist", album="Album"),
        # The disc subfolder is peeled off, and its parent is compared.
        _mk(2, "Artist/Other Record/CD 2", "a.mp3", albumartist="Artist", album="Album (Disc 2)"),
        _mk(3, "Artist/Album/CD 2", "a.mp3", albumartist="Artist", album="Album (Disc 2)"),
        _mk(4, "Artist/Singles", "a.mp3", albumartist="Artist", album="Some Single"),
        _mk(5, "The Doors", "a.mp3", albumartist="The Doors", album="L.A. Woman"),
        _mk(7, "Doors (1971)", "a.mp3", albumartist="The Doors", album="L.A. Woman"),
        _mk(6, "Some Record (1999)", "a.mp3", albumartist="Band", album="Other Record"),
    ]

    assert _flags(files) == {
        1: {RELEASE_FOLDER_ALBUM},
        2: {RELEASE_FOLDER_ALBUM},
        6: {RELEASE_FOLDER_ALBUM},
    }


# --- release_folder_year -------------------------------------------------------------


def test_release_folder_year_needs_one_tag_year_among_the_folder_tokens() -> None:
    files = [
        _mk(1, "A/(2001) A - Album", "a.mp3", albumartist="A", album="Album", date="1999"),
        # Either the release date or the original date agrees.
        _mk(2, "B/(1999) B - Album", "a.mp3", albumartist="B", date="2005", originaldate="1999"),
        _mk(3, "C/C - 1999 - Album", "a.mp3", albumartist="C", date="1999-04-01"),
        _mk(4, "D/Album (1999)", "a.mp3", albumartist="D", date="1999"),
        # A year inside the album title is not the release year.
        _mk(5, "E/(2014) E - 1989", "a.mp3", albumartist="E", album="1989", date="2014"),
        _mk(6, "F/(1995) F - Astro-Creep 2000", "a.mp3", albumartist="F", date="1995"),
        # No year token, or no tag year, gives no signal.
        _mk(7, "G/Album", "a.mp3", albumartist="G", date="1999"),
        _mk(8, "H/(2001) Album", "a.mp3", albumartist="H"),
        # A curated folder skips the year.
        _mk(9, "I/Singles/2001 Single", "a.mp3", albumartist="I", date="1999"),
    ]

    assert _flags(files) == {1: {RELEASE_FOLDER_YEAR}}


# --- disc_folder_number --------------------------------------------------------------


def test_disc_folder_number_compares_numbered_disc_subfolders_only() -> None:
    files = [
        _mk(1, "A/Album/Cd1", "a.mp3", albumartist="A", discnumber="2"),
        _mk(2, "A/Album/[DISC.02]", "a.mp3", albumartist="A", discnumber="2/2"),
        _mk(3, "A/Album/1", "a.mp3", albumartist="A", discnumber="1"),
        _mk(4, "A/Album/Disc 3 Bonus", "a.mp3", albumartist="A", discnumber="03"),
        # Bonus CD and track-range folders name no disc number.
        _mk(5, "A/Album/Bonus CD", "a.mp3", albumartist="A", discnumber="5"),
        _mk(6, "A/Album/01-04 Songs", "a.mp3", albumartist="A", discnumber="3"),
        # A blank disc tag gives no signal.
        _mk(7, "A/Album/Cd2", "a.mp3", albumartist="A"),
    ]

    assert _flags(files) == {1: {DISC_FOLDER_NUMBER}}


# --- filename_track ------------------------------------------------------------------


def test_filename_track_reads_each_numbering_shape() -> None:
    files = [
        _mk(1, "A/Album", "A - Album - 05 - Song.mp3", albumartist="A", tracknumber="5"),
        _mk(2, "A/Album", "A - Album - 06 - Song.mp3", albumartist="A", tracknumber="7"),
        _mk(3, "B/Album", "B - 05 - Song.mp3", albumartist="B", tracknumber="5/12"),
        _mk(4, "C/Album", "1-05 Song.mp3", albumartist="C", tracknumber="5", discnumber="1"),
        _mk(5, "D/Album", "2-05 Song.mp3", albumartist="D", tracknumber="5", discnumber="1"),
        _mk(6, "E/Album", "105 Song.mp3", albumartist="E", tracknumber="5", discnumber="1"),
        _mk(7, "F/Album", "05. Song.mp3", albumartist="F", tracknumber="005"),
        # 00 is a placeholder for no number.
        _mk(8, "G/Album", "00 Intro.mp3", albumartist="G", tracknumber="3"),
        # A leading artist number is never read first.
        _mk(9, "311/Album", "311 - Album - 04 - Amber.mp3", albumartist="311", tracknumber="4"),
        # A blank track tag gives no signal.
        _mk(10, "H/Album", "09 Song.mp3", albumartist="H"),
    ]

    assert _flags(files) == {2: {FILENAME_TRACK}, 5: {FILENAME_TRACK}}


def test_filename_track_accepts_a_continuous_count_only_across_discs() -> None:
    multi = [
        _mk(1, "A/Album", "01 a.mp3", albumartist="A", discnumber="1", tracknumber="1"),
        _mk(2, "A/Album", "02 b.mp3", albumartist="A", discnumber="1", tracknumber="2"),
        _mk(3, "A/Album", "03 c.mp3", albumartist="A", discnumber="2", tracknumber="1"),
        _mk(4, "A/Album", "04 d.mp3", albumartist="A", discnumber="2", tracknumber="2"),
    ]
    single = [
        _mk(5, "B/Album", "01 a.mp3", albumartist="B", tracknumber="2"),
        _mk(6, "B/Album", "02 b.mp3", albumartist="B", tracknumber="3"),
    ]

    assert _flags([*multi, *single]) == {5: {FILENAME_TRACK}, 6: {FILENAME_TRACK}}


# --- filename_title ------------------------------------------------------------------


def test_filename_title_compares_the_title_text() -> None:
    files = [
        _mk(1, "A/Album", "05 Iron Gland.mp3", albumartist="A", title="Angry Chair"),
        _mk(2, "A/Album", "06 The Song.mp3", albumartist="A", title="Song"),
        _mk(3, "A/Album", "07 Song (Live).mp3", albumartist="A", title="Song"),
        _mk(4, "A/Album", "08 Song (Remastered).mp3", albumartist="A", title="Song (feat. X)"),
        _mk(5, "A/Album", "A - Other.mp3", albumartist="A", artist="A", title="Other"),
        _mk(
            6,
            "A/Album",
            "A - Album - 09 - Waking City, The.mp3",
            albumartist="A",
            title="The Waking City",
        ),
        # A filename with no title text gives no signal.
        _mk(7, "A/Album", "10.mp3", albumartist="A", title="Anything"),
        _mk(8, "A/Album", "11 - 1999.mp3", albumartist="A", title="Anything"),
    ]

    assert _flags(files) == {1: {FILENAME_TITLE}}


# --- formatting, blanks and the shared value rule ------------------------------------


def test_formatting_alone_never_flags() -> None:
    files = [
        _mk(
            1,
            "ac-dc/(1980) AC-DC - BACK IN BLACK",
            "01 - hells bells.mp3",
            albumartist="AC/DC",
            album="Back in Black",
            date="1980",
            tracknumber="1",
            title="Hell's Bells",
        ),
        _mk(
            2,
            "Royksopp/Melodie A.M. (2001)",
            "2 So Easy.mp3",
            albumartist="Röyksopp",
            album="Mélodie A.M.",
            date="2001-09-03",
            tracknumber="02",
            title="So Easy",
        ),
        _mk(
            3,
            "Artist/2003 - Album/CD 2",
            "205 Song.mp3",
            albumartist="Artist",
            album="Album",
            date="2003",
            tracknumber="5",
            discnumber="2/2",
            title="Song",
        ),
        _mk(
            4,
            "Artist/Artist - 1999 - Hits Volume II",
            "Artist - Hits Vol. 2 - 007 - Rock and Roll.mp3",
            albumartist="Artist",
            album="Hits Vol. 2",
            date="1999",
            tracknumber="7",
            title="Rock & Roll",
        ),
        # A disc count marker never eats the last digit of a number before it.
        _mk(
            5,
            "Depeche Mode/(1998) Depeche Mode - The Singles 86 - 98 CD 1",
            "01 Song.mp3",
            albumartist="Depeche Mode",
            album="The Singles 86>98 (disc 1)",
            date="1998",
        ),
        # A contraction or an initialism is one word, whatever its apostrophes, dots or spaces.
        _mk(
            6,
            "Artist/(2004) Artist - I'm the Supervisor",
            "01 Im Alive.mp3",
            albumartist="Artist",
            album="IM the Supervisor",
            date="2004",
            tracknumber="1",
            title="I\u2019m Alive",
        ),
        _mk(
            7,
            "Artist/(2005) Artist - Ive Got It",
            "02 Ill Be There.mp3",
            albumartist="Artist",
            album="I've Got It",
            date="2005",
            tracknumber="2",
            title="I'll Be There",
        ),
        _mk(
            8,
            "Artist/(2006) Artist - IV",
            "03 i._e._d.mp3",
            albumartist="Artist",
            album="I.V.",
            date="2006",
            tracknumber="3",
            title="I.E.D.",
        ),
    ]

    assert _flags(files) == {}


def test_a_blank_tag_never_flags() -> None:
    files = [
        _mk(1, "Artist/(1999) Artist - Album/Cd1", "05 Song.mp3"),
        _mk(
            2,
            "Artist/(1999) Artist - Album/Cd2",
            "06 Song.mp3",
            albumartist=" ",
            album="",
            date=" ",
            discnumber="",
            tracknumber=" ",
            title="?",
        ),
    ]

    report = mismatch._classify(files, _MUSIC)

    assert report.rows == []
    assert report.exception_rows == []


def test_a_path_rendered_from_the_tags_never_flags() -> None:
    nfd = functools.partial(unicodedata.normalize, "NFD")
    tag_sets = [
        {
            "albumartist": "AC/DC",
            "album": "Live: 1999 - 2009?",
            "date": "2001-05-01",
            "tracknumber": "3/12",
            "discnumber": "1",
            "title": "What*Ever...",
        },
        {
            "albumartist": nfd("Röyksopp"),
            "album": nfd("Mélodie A.M..."),
            "date": "2002",
            "tracknumber": "7",
            "title": nfd("Café: Noir / Blanc"),
        },
        {
            "albumartist": "Sublime - Sinsemilla",
            "album": "Vol. 12 - 13 - Best*",
            "date": "1999",
            "originaldate": "1995",
            "tracknumber": "12",
            "title": "Track - 3 - <x>",
        },
        {
            "albumartist": "The Band",
            "album": '2001: A "Space" Odyssey',
            "date": "1968",
            "tracknumber": "1",
            "title": "1999|2000",
        },
        {
            "albumartist": "The Dreaming",
            "album": "Bonus Tracks EP",
            "date": "2005",
            "tracknumber": "2",
            "title": "Limited Edition",
        },
    ]
    files: list[mismatch._FileInput] = []
    for file_id, tags in enumerate(tag_sets, 1):
        artist, album, title = (
            clean_value(tags[name]) for name in ("albumartist", "album", "title")
        )
        number = f"{parse_position(tags['tracknumber']):02d}"
        release = f"({tags['date'][:4]}) {artist} - {album}"
        filename = f"{artist} - {album} - {number} - {title}.flac"
        files.append(_mk(file_id, f"{artist}/{release}", filename, **tags))
        # Windows drops a folder name's trailing dot, which must not flag either.
        files.append(_mk(file_id + 100, f"{artist}/{release.rstrip('.')}", filename, **tags))

    report = mismatch._classify(files, _MUSIC)

    assert report.rows == []


# --- exceptions ----------------------------------------------------------------------


def test_curated_and_nested_files_are_exceptions_outside_flagged() -> None:
    nested = "Lusine/2002 - Iron city/2002 - Iron City_320/Iron City"
    files = [
        _mk(1, "Artist/Singles", "a.mp3", albumartist="Artist", album="A Single", date="1999"),
        _mk(2, nested, "01 Song.mp3", albumartist="Lusine", album="Iron City", title="Song"),
        # An exception file that also differs is flagged and listed as an exception.
        _mk(3, nested, "02 Other.mp3", albumartist="Lusine", album="Iron City", title="Angry"),
        # Disc subfolders, wrappers and root albums are not exceptions.
        _mk(4, "Tool [Discography]/Undertow/CD1", "a.mp3", albumartist="Tool"),
        _mk(5, "Root Album", "a.mp3", albumartist="Band"),
        *_padding(),
    ]

    report = mismatch._classify(files, _MUSIC)

    assert [(r.file_id, r.exception) for r in report.exception_rows] == [
        (1, "curated"),
        (2, "nested"),
        (3, "nested"),
    ]
    assert report.exceptions_undecided == 3
    assert [r.file_id for r in report.rows] == [3]
    assert report.rows[0].exception == "nested"
    assert report.flagged == 1


# --- tiers and the reliability guard -------------------------------------------------


def _tier(report: mismatch.MismatchesReport, file_id: int) -> str | None:
    row = _find(report, file_id)
    return None if row is None else row.tier


def test_top_folder_artist_tiers_by_group_shape() -> None:
    files = [
        _mk(1, "Ozzy/Down", "a.mp3", albumartist="Jem"),
        _mk(2, "Ozzy/Down", "b.mp3", albumartist="Ozzy"),
        _mk(3, "Luna/Album", "a.mp3", albumartist="Wrong"),
        _mk(4, "Luna/Album", "b.mp3", albumartist="Wrong"),
        _mk(5, "Solo/Single", "a.mp3", albumartist="Other"),
        _mk(6, "Band/Album", "a.mp3", artist="Stranger"),
        _mk(7, "Band/Album", "b.mp3", artist="Band"),
        *_padding(),
    ]

    report = mismatch._classify(files, _MUSIC)

    assert report.path_signal_unreliable is False
    assert {fid: _tier(report, fid) for fid in (1, 3, 4, 5, 6)} == {
        1: "high",
        3: "medium",
        4: "medium",
        5: "low",
        6: "low",
    }


def test_name_and_number_tiers_and_the_most_severe_wins() -> None:
    files = [
        _mk(1, "A/Bark at the Moon", "a.mp3", albumartist="A", album="Down To Earth"),
        _mk(2, "A/Best Hits Collection", "a.mp3", albumartist="A", album="Greatest Hits Vol 2"),
        _mk(3, "A/Millenium", "a.mp3", albumartist="A", album="Millennium"),
        _mk(
            4,
            "A/Millenium (2001)",
            "02 Song.mp3",
            albumartist="A",
            album="Millennium",
            tracknumber="3",
        ),
    ]

    report = mismatch._classify(files, _MUSIC)

    assert {fid: _tier(report, fid) for fid in (1, 2, 3, 4)} == {
        1: "high",
        2: "medium",
        3: "low",
        4: "high",
    }


def test_reliability_guard_tiers_top_folder_differences_low_and_drops_nothing() -> None:
    files = [_mk(i, f"Folder{i}/Album", "a.mp3", albumartist=f"Someone{i}") for i in range(1, 5)]
    files.append(_mk(10, "Real/Album", "a.mp3", albumartist="Real"))

    report = mismatch._classify(files, _MUSIC)

    assert report.path_signal_unreliable is True
    assert report.disagreement_rate == pytest.approx(0.8)
    assert sorted(r.file_id for r in report.rows) == [1, 2, 3, 4]
    assert {r.tier for r in report.rows} == {"low"}
    assert "path signal unreliable" in report.summary


# --- report shape, groups and the view -----------------------------------------------

_ALBUM = "Artist/(1999) Artist - Album"


def _grouped_library() -> list[mismatch._FileInput]:
    common = {"albumartist": "Artist", "album": "Album", "date": "1999"}
    return [
        _mk(1, f"{_ALBUM}/CD1", "01 Song.mp3", discnumber="2", **common),
        _mk(2, f"{_ALBUM}/CD2", "01 Other.mp3", discnumber="2", **common),
        _mk(3, _ALBUM, "05 Togetger.mp3", title="Together", musicbrainz_albumid="mb", **common),
        _mk(4, "Artist/Singles", "single.mp3", albumartist="Artist"),
        *_padding(),
    ]


def test_report_rows_and_header_shape() -> None:
    report = mismatch._classify(_grouped_library(), _MUSIC)
    payload = report.to_dict()

    assert set(payload) == {
        "rows",
        "exception_rows",
        "groups",
        "total_files",
        "flagged",
        "group_count",
        "by_comparison",
        "high",
        "medium",
        "low",
        "exceptions_undecided",
        "disagreement_rate",
        "path_signal_unreliable",
        "suppressed",
        "container_suppressed",
        "summary",
    }
    assert payload["flagged"] == 2
    assert payload["group_count"] == 2
    assert payload["by_comparison"] == {DISC_FOLDER_NUMBER: 1, FILENAME_TITLE: 1}
    assert payload["exceptions_undecided"] == 1
    assert (payload["high"], payload["medium"], payload["low"]) == (1, 0, 1)
    rows = payload["rows"]
    assert isinstance(rows, list)
    assert rows[0] == {
        "file_id": 1,
        "folder": str(_MUSIC / _ALBUM / "CD1"),
        "filename": "01 Song.mp3",
        "tier": "high",
        "differences": [{"comparison": DISC_FOLDER_NUMBER, "tag_value": "2", "path_value": "CD1"}],
        "exception": None,
    }
    exception_rows = payload["exception_rows"]
    assert isinstance(exception_rows, list)
    assert [r["file_id"] for r in exception_rows] == [4]


def test_to_dict_rounds_disagreement_rate() -> None:
    # The engine keeps full precision for the RELIABILITY_FLOOR comparison, but the payload an
    # LLM reads on every call must not carry 17 digits of noise.
    report = replace(
        mismatch._classify(_grouped_library(), _MUSIC),
        disagreement_rate=0.01250861814242096,
    )

    assert report.to_dict()["disagreement_rate"] == 0.0125
    assert report.disagreement_rate == 0.01250861814242096


def test_groups_key_on_the_release_folder() -> None:
    report = mismatch._classify(_grouped_library(), _MUSIC)

    grouped = _view(report, group=True)

    assert grouped.rows == []
    album, singles = grouped.groups
    assert album.to_dict() == {
        "folder": str(_MUSIC / _ALBUM),
        "file_count": 3,
        "flagged": 2,
        "tier": "high",
        "comparisons": {
            DISC_FOLDER_NUMBER: {"files": 1, "tag": "2", "path": "CD1"},
            FILENAME_TITLE: {"files": 1, "tag": "Together", "path": "Togetger"},
        },
        "mb_stamped": False,
        "exception": None,
        "suppressed": {},
        "file_ids": [1, 3],
        "unflagged_ids": [2],
    }
    assert singles.folder == str(_MUSIC / "Artist" / "Singles")
    assert (singles.flagged, singles.exception, singles.file_ids) == (0, "curated", [4])
    assert singles.tier is None


def test_view_filters_by_tier_comparison_folder_and_limit() -> None:
    report = mismatch._classify(_grouped_library(), _MUSIC)
    album_key = path_keys.path_key(_MUSIC / _ALBUM)

    by_tier = _view(report, tier="low")
    by_comparison = _view(report, comparison=DISC_FOLDER_NUMBER)
    by_group = _view(report, folder_key=album_key, group=True)
    by_disc_folder = _view(report, folder_key=path_keys.path_key(_MUSIC / _ALBUM / "CD1"))
    by_parent = _view(report, folder_key=path_keys.path_key(_MUSIC / "Artist"))
    stamped = _view(report, comparison=FILENAME_TITLE, group=True)

    assert [r.file_id for r in by_tier.rows] == [3]
    assert by_tier.exception_rows == []
    assert [r.file_id for r in by_comparison.rows] == [1]
    assert by_comparison.exception_rows == []
    assert [r.file_id for r in by_group.rows] == [1, 3]
    assert by_group.groups == []
    assert [r.file_id for r in by_disc_folder.rows] == [1]
    assert by_parent.rows == []
    assert [g.mb_stamped for g in stamped.groups] == [True]
    assert len(_view(report, limit=1).rows) == 1
    assert len(_view(report, group=True, limit=1).groups) == 1
    # Counts describe the whole library in every view.
    assert by_tier.flagged == by_comparison.flagged == report.flagged == 2


# --- dispositions --------------------------------------------------------------------

_OZZY = "Ozzy Osbourne/(2001) Ozzy Osbourne - Down To Earth"


def _disposition_library() -> list[mismatch._FileInput]:
    return [
        _mk(100, _OZZY, "01 Gets Me Through.mp3", albumartist="Jem", artist="Ozzy Osbourne"),
        _mk(101, _OZZY, "03 Dreamer.mp3", albumartist="Ozzy Osbourne"),
        _mk(150, "Blue Stahli/Singles", "a.mp3", albumartist="Future Islands"),
        *_padding(),
    ]


def _disp(status: str, field: str | None, value: str | None) -> store.MismatchStatusRow:
    return store.MismatchStatusRow(status=status, source_field=field, source_value=value)


def test_fresh_disposition_silences_its_file_and_reports_it() -> None:
    dispositions = {100: _disp("legit_ignore", "albumartist", "Jem")}
    files = [
        *_disposition_library(),
        _mk(102, _OZZY, "04 Iron Gland.mp3", albumartist="Ozzy Osbourne", title="Angry Chair"),
    ]

    report = mismatch._classify(files, _MUSIC, dispositions=dispositions)

    assert _find(report, 100) is None
    assert report.flagged == 2
    assert report.suppressed == {"legit_ignore": 1}
    ozzy = next(g for g in _view(report, group=True).groups if g.folder == str(_MUSIC / _OZZY))
    assert ozzy.suppressed == {"legit_ignore": 1}
    assert ozzy.unflagged_ids == [100, 101]


def test_stale_disposition_resurfaces() -> None:
    dispositions = {100: _disp("legit_ignore", "albumartist", "Old Name")}

    report = mismatch._classify(_disposition_library(), _MUSIC, dispositions=dispositions)

    assert _tier(report, 100) == "high"
    assert report.suppressed == {}


def test_both_statuses_silence_flagged_and_exception_rows() -> None:
    dispositions = {
        100: _disp("legit_ignore", "albumartist", "Jem"),
        150: _disp("misfiled_deferred", "albumartist", "Future Islands"),
    }

    report = mismatch._classify(_disposition_library(), _MUSIC, dispositions=dispositions)

    assert report.rows == []
    assert report.exception_rows == []
    assert report.suppressed == {"legit_ignore": 1, "misfiled_deferred": 1}


# --- layout_of -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("Tool [Discography]/(1993) Undertow", ("Tool", "(1993) Undertow", None, None, None)),
        ("A/Album/Cd1", ("A", "Album", "Cd1", 1, None)),
        ("A/Album/[DISC.02]", ("A", "Album", "[DISC.02]", 2, None)),
        ("A/Album/1", ("A", "Album", "1", 1, None)),
        ("A/Album/Bonus CD", ("A", "Album", "Bonus CD", None, None)),
        ("A/Album/01-04 Songs", ("A", "Album", "01-04 Songs", None, None)),
        ("A/Singles", ("A", None, None, None, "curated")),
        ("A/Remixes/Club Mix", ("A", "Club Mix", None, None, "curated")),
        ("A/Box/Album", ("A", "Album", None, None, "nested")),
        ("Root Album", (None, "Root Album", None, None, None)),
    ],
)
def test_layout_of_names_each_level(
    engine_settings: Settings,
    music_dir: Path,
    rel: str,
    expected: tuple[str | None, str | None, str | None, int | None, str | None],
) -> None:
    layout = mismatch.layout_of(engine_settings, str(music_dir / rel), "01.mp3")

    assert (
        layout.top_folder,
        layout.release_folder,
        layout.disc_folder,
        layout.disc_number,
        layout.exception,
    ) == expected
    assert layout.disc_numbered is (expected[3] is not None)
    assert layout.filename == "01.mp3"


def test_layout_of_reads_the_release_years_and_container(tmp_path: Path, music_dir: Path) -> None:
    settings = Settings(
        music_path=music_dir,
        lastfm_api_key=None,
        db_path=tmp_path / "ledger.sqlite3",
        container_folders=("Soundtracks",),
    )

    dated = mismatch.layout_of(settings, str(music_dir / "A" / "A - 1999 - Live 2001"), "x.mp3")
    contained = mismatch.layout_of(settings, str(music_dir / "Soundtracks" / "Movie"), "x.mp3")

    assert dated.release_years == ("1999", "2001")
    assert dated.container is False
    assert contained.container is True


def test_layout_of_requires_music_path(tmp_path: Path) -> None:
    settings = Settings(music_path=None, lastfm_api_key=None, db_path=tmp_path / "l.sqlite3")

    with pytest.raises(ValueError, match="music_path not configured"):
        mismatch.layout_of(settings, str(tmp_path), "x.mp3")


# --- integration: scan real audio, then detect ---------------------------------------


def _make_mislabeled_library(music_dir: Path) -> Path:
    """Create clean, agreeing tracks and a Jem-mislabeled Ozzy folder. Return the Ozzy folder.

    The four clean folders keep the top-folder difference rate under the reliability floor,
    so the mislabeled ``Jem`` file surfaces as high (a mixed-albumartist group).
    """
    for name in ("CleanA", "CleanB", "CleanC", "CleanD"):
        make_track(
            music_dir / name / "Album" / "01.mp3",
            {"albumartist": [name], "artist": [name]},
        )
    ozzy = music_dir / "Ozzy Osbourne" / "(2001) Ozzy Osbourne - Down To Earth"
    make_track(
        ozzy / "01 Gets Me Through.mp3",
        {"albumartist": ["Jem"], "artist": ["Ozzy Osbourne"]},
    )
    make_track(ozzy / "03 Dreamer.mp3", {"albumartist": ["Ozzy Osbourne"], "artist": ["Ozzy"]})
    return ozzy


def _read_albumartist(settings: Settings, folder: Path, filename: str) -> list[str]:
    conn = connect(settings.db_path)
    try:
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        return store.get_tags(conn, row.id).get("albumartist", [])
    finally:
        conn.close()


def _file_id(settings: Settings, folder: Path, filename: str) -> int:
    conn = connect(settings.db_path)
    try:
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        return row.id
    finally:
        conn.close()


def test_detect_integration_flags_high_and_is_read_only(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    ozzy = _make_mislabeled_library(music_dir)

    scan_library(engine_settings)

    report = detect_mismatches(engine_settings)

    assert report.path_signal_unreliable is False
    jem_id = _file_id(engine_settings, ozzy, "01 Gets Me Through.mp3")
    row = _find(report, jem_id)
    assert row is not None
    assert row.tier == "high"
    assert [d.to_dict() for d in row.differences] == [
        {"comparison": TOP_FOLDER_ARTIST, "tag_value": "Jem", "path_value": "Ozzy Osbourne"},
    ]

    # Read-only: nothing staged and the file's tags are untouched on disk/in the ledger.
    conn = connect(engine_settings.db_path)
    try:
        assert store.any_staged(conn) is False
    finally:
        conn.close()
    assert _read_albumartist(engine_settings, ozzy, "01 Gets Me Through.mp3") == ["Jem"]


def test_detect_reads_every_comparison_from_scanned_tags(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    album = music_dir / "Artist" / "(2001) Artist - Album"
    make_track(
        album / "Cd1" / "Artist - Album - 02 - Other Song.flac",
        {
            "albumartist": ["Artist"],
            "album": ["Another Record"],
            "date": ["1999"],
            "discnumber": ["2"],
            "tracknumber": ["3"],
            "title": ["Angry Chair"],
        },
    )
    scan_library(engine_settings)

    report = detect_mismatches(engine_settings)

    assert report.flagged == 1
    assert {d.comparison for d in report.rows[0].differences} == {
        RELEASE_FOLDER_ALBUM,
        RELEASE_FOLDER_YEAR,
        DISC_FOLDER_NUMBER,
        FILENAME_TRACK,
        FILENAME_TITLE,
    }
    assert report.groups == []


@pytest.mark.parametrize("spelling", FOLDER_SPELLINGS)
def test_folder_argument_variants_match_the_same_rows(
    engine_settings: Settings,
    music_dir: Path,
    spelling: str,
) -> None:
    ozzy = _make_mislabeled_library(music_dir)
    scan_library(engine_settings)

    exact = detect_mismatches(engine_settings, folder=str(ozzy))
    variant = detect_mismatches(engine_settings, folder=spell_folder(ozzy, spelling))

    assert exact.rows
    assert [r.file_id for r in variant.rows] == [r.file_id for r in exact.rows]


def test_folder_outside_music_path_is_refused(
    engine_settings: Settings,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="outside music_path"):
        detect_mismatches(engine_settings, folder=str(tmp_path / "elsewhere"))


def test_detect_tier_filter_and_unknown_tier_or_comparison(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_mislabeled_library(music_dir)
    scan_library(engine_settings)

    high_only = detect_mismatches(engine_settings, tier="high")
    assert all(r.tier == "high" for r in high_only.rows)
    # Counts stay library-wide even when rows are filtered to one tier.
    assert high_only.high == 1

    with pytest.raises(ValueError, match="unknown tier"):
        detect_mismatches(engine_settings, tier="bogus")
    with pytest.raises(ValueError, match="unknown comparison"):
        detect_mismatches(engine_settings, comparison="folder_artist")


def test_detect_requires_music_path(tmp_path: Path) -> None:
    settings = Settings(
        music_path=None,
        lastfm_api_key=None,
        db_path=tmp_path / "ledger.sqlite3",
    )
    with pytest.raises(ValueError, match="music_path not configured"):
        detect_mismatches(settings)


# --- CLI + MCP wiring ----------------------------------------------------------------


def test_cli_detect_mismatches_reports_high(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    _make_mislabeled_library(music_dir)

    assert runner.invoke(app, ["scan-library", str(music_dir)]).exit_code == 0
    result = runner.invoke(app, ["detect-mismatches"])

    assert result.exit_code == 0
    assert "HIGH" in result.stdout
    assert "Jem" in result.stdout
    assert "exception file(s) undecided" in result.stdout


def test_mcp_detect_tool_listed_and_callable(music_dir: Path) -> None:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert "detect_mismatches" in names

    config.set_setting("music_path", str(music_dir))
    _make_mislabeled_library(music_dir)
    mcp_server.scan_library(path=str(music_dir))

    payload = mcp_server.detect_mismatches()
    assert payload["ok"] is True
    assert payload["high"] == 1
    assert payload["flagged"] == 1
    assert payload["by_comparison"] == {TOP_FOLDER_ARTIST: 1}
    rows = payload["rows"]
    assert isinstance(rows, list)
    assert rows[0]["differences"][0]["tag_value"] == "Jem"

    filtered = mcp_server.detect_mismatches(comparison="filename_title")
    assert filtered["rows"] == []
    assert filtered["flagged"] == 1


# --- disposition verbs + staleness (engine, real library) ---------------------------


def test_set_and_reset_mismatch_status_via_detect(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    ozzy = _make_mislabeled_library(music_dir)
    scan_library(engine_settings)
    jem_id = _file_id(engine_settings, ozzy, "01 Gets Me Through.mp3")

    assert _find(detect_mismatches(engine_settings), jem_id) is not None  # baseline flagged

    affected = mismatch.set_mismatch_status(
        engine_settings,
        file_ids=[jem_id],
        status="legit_ignore",
    )
    assert affected == 1
    report = detect_mismatches(engine_settings)
    assert _find(report, jem_id) is None  # silenced
    assert report.suppressed == {"legit_ignore": 1}

    assert mismatch.reset_mismatch_status(engine_settings, file_ids=[jem_id]) == 1
    assert _find(detect_mismatches(engine_settings), jem_id) is not None  # re-surfaced


def test_set_mismatch_status_by_value_matches_both_fields(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    ozzy = _make_mislabeled_library(music_dir)
    scan_library(engine_settings)
    jem_id = _file_id(engine_settings, ozzy, "01 Gets Me Through.mp3")

    # "Jem" is the albumartist of the flagged file -> value scope catches it.
    affected = mismatch.set_mismatch_status(engine_settings, value="Jem", status="legit_ignore")
    assert affected == 1
    assert _find(detect_mismatches(engine_settings), jem_id) is None


def test_set_mismatch_status_rejects_unknown_status(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_mislabeled_library(music_dir)
    scan_library(engine_settings)
    with pytest.raises(ValueError, match="unknown status"):
        mismatch.set_mismatch_status(engine_settings, file_ids=[1], status="no_match")


def test_disposition_goes_stale_when_albumartist_edited(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    ozzy = _make_mislabeled_library(music_dir)
    scan_library(engine_settings)
    jem_file = ozzy / "01 Gets Me Through.mp3"
    jem_id = _file_id(engine_settings, ozzy, "01 Gets Me Through.mp3")

    mismatch.set_mismatch_status(engine_settings, file_ids=[jem_id], status="legit_ignore")
    assert _find(detect_mismatches(engine_settings), jem_id) is None  # silenced

    # Edit the albumartist on disk (still disagreeing) + rescan -> snapshot changes -> stale.
    make_track(jem_file, {"albumartist": ["Jem Griffiths"], "artist": ["Ozzy Osbourne"]})
    scan_library(engine_settings)

    resurfaced = _find(detect_mismatches(engine_settings), jem_id)
    assert resurfaced is not None  # the stale disposition no longer silences it


# --- end-to-end mismatch-fix flow ----------------------------------------------------


def _make_fix_flow_library(music_dir: Path) -> dict[str, Path]:
    """Mixed poisoned folder (2 Jem-stamped + 1 clean exemplar) + a curated-folder single.

    Ten clean single-artist folders keep the top-folder difference rate under the reliability
    floor so the mis-stamped files surface as high.
    """
    for index in range(10):
        make_track(
            music_dir / f"Clean{index}" / "Album" / "01.mp3",
            {"albumartist": [f"Clean{index}"], "artist": [f"Clean{index}"]},
        )
    poisoned = music_dir / "Ozzy Osbourne" / "(2001) Ozzy Osbourne - Down To Earth"
    make_track(
        poisoned / "01 Gets Me Through.mp3",
        {"albumartist": ["Jem"], "artist": ["Ozzy Osbourne"], "genre": ["Pop"]},
    )
    make_track(
        poisoned / "02 Facing Hell.mp3",
        {"albumartist": ["Jem"], "artist": ["Ozzy Osbourne"], "genre": ["Pop"]},
    )
    make_track(
        poisoned / "03 Dreamer.mp3",
        {"albumartist": ["Ozzy Osbourne"], "artist": ["Ozzy Osbourne"], "genre": ["Rock"]},
    )
    # A legit remix single credited to another artist, in a curated folder.
    fp = music_dir / "Blue Stahli" / "Singles"
    make_track(fp / "remix.mp3", {"albumartist": ["Celldweller"], "artist": ["Celldweller"]})
    return {"poisoned": poisoned, "fp": fp}


def test_mismatch_fix_flow_end_to_end(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folders = _make_fix_flow_library(music_dir)
    poisoned = folders["poisoned"]
    scan_library(engine_settings)

    jem1 = _file_id(engine_settings, poisoned, "01 Gets Me Through.mp3")
    jem2 = _file_id(engine_settings, poisoned, "02 Facing Hell.mp3")
    fp_id = _file_id(engine_settings, folders["fp"], "remix.mp3")

    # Seed prior auto genre work + a sticky artist exclusion on the poisoned files, so
    # reopen has real axis state to act on. The files carry no album, so year has no identity.
    for fid in (jem1, jem2):
        staging.stage_tags(
            engine_settings,
            file_id=fid,
            tags={"genre": ["Metal"]},
            origin="auto",
        )
    _record_genre_done(engine_settings, [jem1, jem2])
    staging.commit_tags(engine_settings)
    artists.set_artist_status(engine_settings, file_ids=[jem1, jem2], status="manual")
    assert _derived(engine_settings, jem1) == ("done", "no_identity", "manual")

    # 1. detect flags the two Jem files (high) and the curated single.
    report = detect_mismatches(engine_settings)
    assert {r.file_id for r in report.rows} >= {jem1, jem2, fp_id}
    assert _find(report, jem1).tier == "high"  # type: ignore[union-attr]
    assert [r.file_id for r in report.exception_rows] == [fp_id]

    # 2. silence the curated single -> suppressed + reported, not in rows.
    assert (
        mismatch.set_mismatch_status(engine_settings, file_ids=[fp_id], status="legit_ignore") == 1
    )
    silenced = detect_mismatches(engine_settings)
    assert _find(silenced, fp_id) is None
    assert silenced.exception_rows == []
    assert silenced.suppressed == {"legit_ignore": 1}

    # 3. batch-stage the corrected identity for the flagged files (one atomic call).
    staged = staging.stage_tags_batch(
        engine_settings,
        entries=[
            (jem1, {"albumartist": ["Ozzy Osbourne"]}),
            (jem2, {"albumartist": ["Ozzy Osbourne"]}),
        ],
    )
    assert staged == [jem1, jem2]

    # 4. commit the folder as ONE revertible commit.
    result = staging.commit_tags(engine_settings, path=poisoned)
    assert result.committed == 2
    commit_id = result.commit_id
    assert commit_id is not None

    # 5. reopen the fixed files' outcomes. A manual row is a human decision and stays.
    reopen = staging.reopen_axes(engine_settings, commit_id=commit_id)
    assert reopen.files == 2
    assert reopen.to_dict()["genre"] == {"outcomes_reopened": 2, "manual_kept": 0}
    assert reopen.to_dict()["artist"] == {"outcomes_reopened": 0, "manual_kept": 2}
    assert _derived(engine_settings, jem1) == ("pending", "no_identity", "manual")

    # 6. detect no longer flags the poisoned folder (self-resolving accept, no row needed).
    assert detect_mismatches(engine_settings, folder=str(poisoned)).rows == []

    # 7. revert the whole commit -> the pre-fix albumartist is restored.
    versioning.revert_commit(engine_settings, commit_id)
    assert _read_albumartist(engine_settings, poisoned, "01 Gets Me Through.mp3") == ["Jem"]


def _record_genre_done(settings: Settings, file_ids: list[int]) -> None:
    """Record the resolver's ``done`` genre outcome for each staged file, as resolve_genres does."""
    conn = connect(settings.db_path)
    try:
        for file_id in file_ids:
            store.record_outcome(
                conn,
                axis.GENRE_AXIS,
                file_id=file_id,
                status="done",
                now="2026-09-30T00:00:00+00:00",
            )
        conn.commit()
    finally:
        conn.close()


def _derived(settings: Settings, file_id: int) -> tuple[str, str, str]:
    """Return ``(genre, year, artist)`` derived statuses for *file_id*."""
    conn = connect(settings.db_path)
    try:
        return (
            store.derived_status(conn, axis.GENRE_AXIS, file_id),
            store.derived_status(conn, axis.YEAR_AXIS, file_id),
            store.derived_status(conn, axis.ARTIST_AXIS, file_id),
        )
    finally:
        conn.close()


# --- MCP + CLI wiring for the disposition surface ------------------------------------


def test_mcp_new_mismatch_tools_listed() -> None:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert {
        "stage_tags_batch",
        "reopen_axes",
        "set_mismatch_status",
        "reset_mismatch_status",
    } <= names


def test_mcp_set_and_reset_mismatch_status_envelopes(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    ozzy = _make_mislabeled_library(music_dir)
    mcp_server.scan_library(path=str(music_dir))
    jem_id = _file_id(config.load_settings(), ozzy, "01 Gets Me Through.mp3")

    ok = mcp_server.set_mismatch_status("legit_ignore", file_ids=[jem_id])
    assert ok == {"ok": True, "affected": 1}
    payload = mcp_server.detect_mismatches()
    assert payload["suppressed"] == {"legit_ignore": 1}

    bad = mcp_server.set_mismatch_status("no_match", file_ids=[jem_id])
    assert bad["ok"] is False
    assert "error" in bad

    assert mcp_server.reset_mismatch_status(file_ids=[jem_id]) == {"ok": True, "affected": 1}


def test_mcp_detect_group_and_folder(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    ozzy = _make_mislabeled_library(music_dir)
    mcp_server.scan_library(path=str(music_dir))

    grouped = mcp_server.detect_mismatches(group=True)
    assert grouped["ok"] is True
    assert grouped["rows"] == []
    groups = grouped["groups"]
    assert isinstance(groups, list)
    assert any(g["folder"] == str(ozzy) for g in groups)

    expanded = mcp_server.detect_mismatches(folder=str(ozzy))
    assert expanded["ok"] is True
    assert expanded["groups"] == []
    rows = expanded["rows"]
    assert isinstance(rows, list)
    assert rows
    assert all(r["folder"] == str(ozzy) for r in rows)


def test_cli_detect_mismatches_group_lists_folders(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    _make_mislabeled_library(music_dir)
    assert runner.invoke(app, ["scan-library", str(music_dir)]).exit_code == 0

    result = runner.invoke(app, ["detect-mismatches", "--group"])
    assert result.exit_code == 0
    assert "Ozzy Osbourne" in result.stdout
    assert "flagged" in result.stdout
    assert TOP_FOLDER_ARTIST in result.stdout


def test_mcp_stage_tags_batch_atomic_and_commits_once(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    a = make_track(music_dir / "a.mp3", {"genre": ["Pop"]})
    b = make_track(music_dir / "b.mp3", {"genre": ["Pop"]})
    mcp_server.scan_library(path=str(music_dir))
    conn = connect(config.load_settings().db_path)
    try:
        a_id = store.get_file(conn, str(music_dir), a.name).id  # type: ignore[union-attr]
        b_id = store.get_file(conn, str(music_dir), b.name).id  # type: ignore[union-attr]
    finally:
        conn.close()

    payload = mcp_server.stage_tags_batch(
        [
            {"file_id": a_id, "tags": {"genre": ["Rock"]}},
            {"file_id": b_id, "tags": {"genre": ["Metal"]}},
        ],
    )
    assert payload == {"ok": True, "staged": 2, "file_ids": [a_id, b_id]}

    committed = mcp_server.commit_tags()
    assert committed["committed"] == 2
    # Both files landed under ONE commit id.
    conn = connect(config.load_settings().db_path)
    try:
        a_commit = store.get_revisions(conn, a_id)[-1].commit_id
        b_commit = store.get_revisions(conn, b_id)[-1].commit_id
    finally:
        conn.close()
    assert a_commit == b_commit is not None


def test_mcp_stage_tags_batch_rejects_unknown_file(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    make_track(music_dir / "a.mp3", {"genre": ["Pop"]})
    mcp_server.scan_library(path=str(music_dir))

    payload = mcp_server.stage_tags_batch([{"file_id": 9999, "tags": {"genre": ["Rock"]}}])
    assert payload["ok"] is False
    assert "error" in payload


def test_mcp_stage_tags_batch_rejects_bare_string_value(music_dir: Path) -> None:
    """A tag value that is a bare string (not a list) is rejected before anything is staged.

    Guards the mismatch-fix flow's most plausible caller mistake: ``{"albumartist": "Ozzy"}``
    instead of ``{"albumartist": ["Ozzy"]}``. Without the boundary check ``list("Ozzy")`` would
    split the string per character and silently corrupt the on-disk tag.
    """
    config.set_setting("music_path", str(music_dir))
    a = make_track(music_dir / "a.mp3", {"genre": ["Pop"]})
    mcp_server.scan_library(path=str(music_dir))
    conn = connect(config.load_settings().db_path)
    try:
        a_id = store.get_file(conn, str(music_dir), a.name).id  # type: ignore[union-attr]
    finally:
        conn.close()

    payload = mcp_server.stage_tags_batch([{"file_id": a_id, "tags": {"albumartist": "Ozzy"}}])
    assert payload["ok"] is False
    assert "must be a list of strings" in str(payload["error"])
    # Nothing was staged: a follow-up commit has nothing to write.
    assert mcp_server.commit_tags()["committed"] == 0


def test_mcp_stage_tags_batch_rejects_missing_file_id(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    make_track(music_dir / "a.mp3", {"genre": ["Pop"]})
    mcp_server.scan_library(path=str(music_dir))

    payload = mcp_server.stage_tags_batch([{"tags": {"genre": ["Rock"]}}])

    assert payload["ok"] is False
    assert "entry 0: file_id must be an integer" in str(payload["error"])


def test_mcp_stage_tags_batch_rejects_non_dict_tags(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    a = make_track(music_dir / "a.mp3", {"genre": ["Pop"]})
    mcp_server.scan_library(path=str(music_dir))
    conn = connect(config.load_settings().db_path)
    try:
        a_id = store.get_file(conn, str(music_dir), a.name).id  # type: ignore[union-attr]
    finally:
        conn.close()

    payload = mcp_server.stage_tags_batch([{"file_id": a_id, "tags": "x"}])

    assert payload["ok"] is False
    assert "tags must be a dict" in str(payload["error"])
    assert mcp_server.commit_tags()["committed"] == 0


def test_mcp_reopen_axes_rejects_auto_commit(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    track = make_track(music_dir / "a.mp3", {"genre": ["Pop"]})
    mcp_server.scan_library(path=str(music_dir))
    conn = connect(config.load_settings().db_path)
    try:
        file_id = store.get_file(conn, str(music_dir), track.name).id  # type: ignore[union-attr]
    finally:
        conn.close()

    staging.stage_tags(
        config.load_settings(),
        file_id=file_id,
        tags={"genre": ["Rock"]},
        origin="auto",
    )
    committed = mcp_server.commit_tags()
    auto_commit = committed["commit_id"]
    assert isinstance(auto_commit, int)
    assert mcp_server.get_commit(auto_commit)["commit"]["origin"] == "auto"

    payload = mcp_server.reopen_axes(auto_commit)
    assert payload["ok"] is False
    assert "auto-resolved" in str(payload["error"])

    assert mcp_server.reopen_axes(9999)["ok"] is False  # unknown commit id


# --- container folders ---------------------------------------------------------------


def _container_library() -> list[mismatch._FileInput]:
    """A Soundtracks container of composer albums, clean padding and a mislabeled artist."""
    files = [
        _mk(100, "Soundtracks/Album A", "01.mp3", albumartist="Composer A"),
        _mk(101, "Soundtracks/Album A", "02.mp3", albumartist="Composer A"),
        _mk(102, "Soundtracks/Album B", "01.mp3", albumartist="Composer B"),
        _mk(103, "Soundtracks/Album B", "02.mp3", albumartist="Composer B"),
        _mk(200, "The Luna Sequence/(2010) Album", "01.mp3", albumartist="Wrong Artist"),
        _mk(201, "The Luna Sequence/(2010) Album", "02.mp3", albumartist="Wrong Artist"),
    ]
    return [*files, *_padding()]


def test_container_folder_skips_only_the_top_folder_comparison() -> None:
    files = _container_library()
    files.append(_mk(104, "Soundtracks/Album C", "01.mp3", albumartist="Composer C", album="Z"))

    baseline = mismatch._classify(files, _MUSIC)
    report = mismatch._classify(files, _MUSIC, container_keys=_SOUNDTRACKS)

    assert {r.file_id for r in baseline.rows} >= {100, 101, 102, 103, 104}
    assert {r.file_id: {d.comparison for d in r.differences} for r in report.rows} == {
        104: {RELEASE_FOLDER_ALBUM},
        200: {TOP_FOLDER_ARTIST},
        201: {TOP_FOLDER_ARTIST},
    }
    assert report.container_suppressed == {"Soundtracks": 5}
    assert report.total_files == baseline.total_files
    assert "container folder" in report.summary


def test_container_files_leave_the_reliability_sample() -> None:
    files = _container_library()

    baseline = mismatch._classify(files, _MUSIC)
    report = mismatch._classify(files, _MUSIC, container_keys=_SOUNDTRACKS)

    # 20 clean + 4 soundtrack + 2 luna = 26 compared, 6 differing.
    assert baseline.disagreement_rate == pytest.approx(6 / 26)
    # The 4 container files leave the sample: 22 compared, 2 differing.
    assert report.disagreement_rate == pytest.approx(2 / 22)


def test_container_and_disposition_suppression_are_distinct() -> None:
    dispositions = {200: _disp("legit_ignore", "albumartist", "Wrong Artist")}

    report = mismatch._classify(
        _container_library(),
        _MUSIC,
        dispositions=dispositions,
        container_keys=_SOUNDTRACKS,
    )

    assert _find(report, 200) is None
    payload = report.to_dict()
    assert payload["container_suppressed"] == {"Soundtracks": 4}
    assert payload["suppressed"] == {"legit_ignore": 1}


def _make_container_real_library(music_dir: Path) -> None:
    """12 clean single-artist folders + a Soundtracks container of composer albums."""
    for index in range(12):
        make_track(
            music_dir / f"Clean{index}" / "Album" / "01.mp3",
            {"albumartist": [f"Clean{index}"], "artist": [f"Clean{index}"]},
        )
    soundtracks = music_dir / "Soundtracks"
    for album, composer in (("Album A", "Composer A"), ("Album B", "Composer B")):
        for track in ("01.mp3", "02.mp3"):
            make_track(soundtracks / album / track, {"albumartist": [composer]})


def test_detect_container_suppression_integration(tmp_path: Path, music_dir: Path) -> None:
    _make_container_real_library(music_dir)
    db_path = tmp_path / "ledger.sqlite3"
    plain = Settings(music_path=music_dir, lastfm_api_key=None, db_path=db_path)
    scan_library(plain)

    # Without the setting the Soundtracks composer albums differ from their top folder.
    assert detect_mismatches(plain).flagged > 0

    listed = Settings(
        music_path=music_dir,
        lastfm_api_key=None,
        db_path=db_path,
        container_folders=("Soundtracks",),
    )
    report = detect_mismatches(listed)
    assert report.flagged == 0
    assert report.container_suppressed == {"Soundtracks": 4}
    leaf = str(music_dir / "Soundtracks" / "Album A")
    assert detect_mismatches(listed, folder=leaf).rows == []
