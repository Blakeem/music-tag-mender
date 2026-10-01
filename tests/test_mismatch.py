"""Tests for path coherence detection (:mod:`tagmend.engine.mismatch`).

Pure tests run the ``_classify`` core over constructed inputs, one per path layout and
comparison, flagged and clean. Integration tests scan generated audio, then detect, and cover
the path decisions (one test per cell of their state table), the status-tool guards, the gate
API, the CLI and the MCP wiring. A committed path change is simulated by moving the file on
disk, appending its ``path_revisions`` row and repointing its ``files`` row.
"""

from __future__ import annotations

import asyncio
import functools
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from typer.testing import CliRunner

from conftest import FOLDER_SPELLINGS, make_track, spell_folder
from tagmend import config, mcp_server
from tagmend.cli import app
from tagmend.config import Settings
from tagmend.engine import artists, axis, mismatch, path_keys, staging, store, versioning
from tagmend.engine.db import connect
from tagmend.engine.detector_core import Tier, parse_position
from tagmend.engine.library import scan_library
from tagmend.engine.mismatch import (
    CHANGED_MOVED,
    CHANGED_TAGS,
    CHANGED_UNCOVERED,
    CURATED,
    DISC_FOLDER_NUMBER,
    FILENAME_TITLE,
    FILENAME_TRACK,
    LEGIT_IGNORE,
    MISFILED_DEFERRED,
    PENDING,
    RELEASE_FOLDER_ALBUM,
    RELEASE_FOLDER_YEAR,
    TOP_FOLDER_ARTIST,
    detect_mismatches,
)
from tagmend.engine.path_text import clean_value
from tagmend.engine.schema import apply_schema

if TYPE_CHECKING:
    from collections.abc import Callable

_MUSIC = Path("/library/music")
_NOW = "2026-10-01T00:00:00+00:00"
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
        "gate_open",
        "suppressed",
        "stale",
        "container_suppressed",
        "summary",
    }
    assert payload["gate_open"] is False
    assert payload["stale"] == 0
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
        "differences": [
            {
                "comparison": DISC_FOLDER_NUMBER,
                "tag_value": "2",
                "path_value": "CD1",
                "silenced": False,
            },
        ],
        "exception": None,
        "was": None,
        "changed": None,
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


def _decision(
    status: str,
    covers: list[str],
    tags: dict[str, str | None],
    *,
    folder: str | None = None,
    path_version: int = 0,
) -> store.MismatchStatusRow:
    """Build a stored decision, a keep bound to *folder* under the test music path."""
    return store.MismatchStatusRow(
        status=status,
        covers=tuple(covers),
        tags=tags,
        path_version=path_version,
        folder_key=None if folder is None else path_keys.path_key(_MUSIC / folder),
    )


_JEM_TAGS: dict[str, str | None] = {"albumartist": "Jem", "artist": "Ozzy Osbourne"}


def test_a_decision_silences_only_the_names_it_covers() -> None:
    dispositions = {100: _decision(LEGIT_IGNORE, [TOP_FOLDER_ARTIST], _JEM_TAGS, folder=_OZZY)}
    files = [
        *_disposition_library(),
        _mk(102, _OZZY, "04 Iron Gland.mp3", albumartist="Ozzy Osbourne", title="Angry Chair"),
    ]

    report = mismatch._classify(files, _MUSIC, dispositions=dispositions)

    assert _find(report, 100) is None
    assert report.flagged == 2
    assert report.suppressed == {LEGIT_IGNORE: 1}
    assert report.stale == 0
    ozzy = next(g for g in _view(report, group=True).groups if g.folder == str(_MUSIC / _OZZY))
    assert ozzy.suppressed == {LEGIT_IGNORE: 1}
    # A file whose every flag is silenced is decided, so it is neither flagged nor unflagged.
    assert (ozzy.file_ids, ozzy.unflagged_ids) == ([102], [101])


def test_a_decision_on_other_tags_resurfaces_with_was_and_changed() -> None:
    dispositions = {
        100: _decision(LEGIT_IGNORE, [TOP_FOLDER_ARTIST], {"albumartist": "Old"}, folder=_OZZY),
    }

    report = mismatch._classify(_disposition_library(), _MUSIC, dispositions=dispositions)

    row = _find(report, 100)
    assert row is not None
    assert (row.tier, row.was, row.changed) == ("high", LEGIT_IGNORE, CHANGED_TAGS)
    assert report.suppressed == {}
    assert report.stale == 0  # the keep still binds its folder


def test_a_decision_bound_to_another_location_is_stale() -> None:
    dispositions = {
        100: _decision(LEGIT_IGNORE, [TOP_FOLDER_ARTIST], _JEM_TAGS, folder="Elsewhere"),
        101: _decision(MISFILED_DEFERRED, [], {}, path_version=0),
    }

    report = mismatch._classify(
        _disposition_library(),
        _MUSIC,
        dispositions=dispositions,
        path_versions={101: 1},
    )

    row = _find(report, 100)
    assert row is not None
    assert (row.was, row.changed) == (LEGIT_IGNORE, CHANGED_MOVED)
    assert report.suppressed == {}
    assert report.stale == 2
    assert "2 decision(s) no longer in force" in report.summary


def test_both_statuses_silence_flagged_and_exception_rows() -> None:
    dispositions = {
        100: _decision(LEGIT_IGNORE, [TOP_FOLDER_ARTIST], _JEM_TAGS, folder=_OZZY),
        150: _decision(
            MISFILED_DEFERRED,
            [TOP_FOLDER_ARTIST, CURATED],
            {"albumartist": "Future Islands", "artist": None},
        ),
    }

    report = mismatch._classify(_disposition_library(), _MUSIC, dispositions=dispositions)

    assert report.rows == []
    assert report.exception_rows == []
    assert report.suppressed == {LEGIT_IGNORE: 1, MISFILED_DEFERRED: 1}
    assert report.gate_open is True


def test_a_class_outside_covers_keeps_the_exception_open() -> None:
    dispositions = {
        150: _decision(
            MISFILED_DEFERRED,
            [TOP_FOLDER_ARTIST],
            {"albumartist": "Future Islands", "artist": None},
        ),
    }

    report = mismatch._classify(_disposition_library(), _MUSIC, dispositions=dispositions)

    assert _find(report, 150) is None  # its top-folder difference is silenced
    (exception,) = report.exception_rows
    assert (exception.file_id, exception.was, exception.changed) == (
        150,
        MISFILED_DEFERRED,
        CHANGED_UNCOVERED,
    )
    assert report.gate_open is False


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
        {
            "comparison": TOP_FOLDER_ARTIST,
            "tag_value": "Jem",
            "path_value": "Ozzy Osbourne",
            "silenced": False,
        },
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


def _ozzy_ids(settings: Settings, ozzy: Path) -> tuple[int, int]:
    """Return the ids of the mislabeled Jem file and its clean Dreamer sibling."""
    return (
        _file_id(settings, ozzy, "01 Gets Me Through.mp3"),
        _file_id(settings, ozzy, "03 Dreamer.mp3"),
    )


def _stored(settings: Settings) -> dict[int, store.MismatchStatusRow]:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return store.load_mismatch_statuses(conn)
    finally:
        conn.close()


def test_set_and_reset_mismatch_status_via_detect(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    ozzy = _make_mislabeled_library(music_dir)
    scan_library(engine_settings)
    jem_id, dreamer_id = _ozzy_ids(engine_settings, ozzy)
    group = detect_mismatches(engine_settings, group=True).groups[0]
    assert (group.file_ids, group.unflagged_ids) == ([jem_id], [dreamer_id])

    result = mismatch.set_mismatch_status(
        engine_settings,
        status=LEGIT_IGNORE,
        covers=[*group.comparisons],
        file_ids=[*group.file_ids, *group.unflagged_ids],
    )

    assert result.to_dict() == {
        "status": LEGIT_IGNORE,
        "affected": 2,
        "skipped_unflagged": 0,
        "covers": {TOP_FOLDER_ARTIST: 1},
        "files": [
            {"file_id": jem_id, "folder": "kept", "filename": "rendered"},
            {"file_id": dreamer_id, "folder": "kept", "filename": "rendered"},
        ],
        "note": mismatch.FILENAME_NOTE,
    }
    report = detect_mismatches(engine_settings)
    assert _find(report, jem_id) is None
    assert report.suppressed == {LEGIT_IGNORE: 2}
    assert report.gate_open is True

    assert mismatch.reset_mismatch_status(engine_settings, file_ids=[jem_id]) == 1
    assert _find(detect_mismatches(engine_settings), jem_id) is not None


def test_a_kept_row_is_tiered_by_its_unsilenced_differences(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    ozzy = _make_mislabeled_library(music_dir)
    scan_library(engine_settings)
    jem_id, dreamer_id = _ozzy_ids(engine_settings, ozzy)
    mismatch.set_mismatch_status(
        engine_settings,
        status=LEGIT_IGNORE,
        covers=[TOP_FOLDER_ARTIST],
        file_ids=[jem_id, dreamer_id],
    )
    staging.stage_tags(engine_settings, file_id=jem_id, tags={"title": ["Gets Me Thru"]})
    staging.commit_tags(engine_settings)

    report = detect_mismatches(engine_settings)

    row = _find(report, jem_id)
    assert row is not None
    assert {d.comparison: d.silenced for d in row.differences} == {
        TOP_FOLDER_ARTIST: True,
        FILENAME_TITLE: False,
    }
    assert not row.carries(TOP_FOLDER_ARTIST)
    assert row.tier == Tier.LOW.value
    assert (report.high, report.medium, report.low) == (0, 0, 1)
    assert report.by_comparison == {FILENAME_TITLE: 1}
    assert _view(report, comparison=TOP_FOLDER_ARTIST).rows == []
    assert [r.file_id for r in _view(report, comparison=FILENAME_TITLE).rows] == [jem_id]


def test_set_writes_only_the_names_each_file_flags(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    ozzy = _make_mislabeled_library(music_dir)
    scan_library(engine_settings)
    jem_id, dreamer_id = _ozzy_ids(engine_settings, ozzy)

    mismatch.set_mismatch_status(
        engine_settings,
        status=LEGIT_IGNORE,
        covers=[TOP_FOLDER_ARTIST, FILENAME_TITLE, CURATED],
        file_ids=[jem_id, dreamer_id],
    )

    folder_key = path_keys.path_key(ozzy)
    assert _stored(engine_settings) == {
        jem_id: store.MismatchStatusRow(
            LEGIT_IGNORE, (TOP_FOLDER_ARTIST,), _JEM_TAGS, 0, folder_key
        ),
        dreamer_id: store.MismatchStatusRow(LEGIT_IGNORE, (), {}, 0, folder_key),
    }


def test_set_by_value_defers_only_the_carriers_that_flag(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    ozzy = _make_mislabeled_library(music_dir)
    scan_library(engine_settings)
    jem_id, _ = _ozzy_ids(engine_settings, ozzy)

    # Both Ozzy files carry the value, and only the Jem file's top folder differs.
    result = mismatch.set_mismatch_status(
        engine_settings,
        status=MISFILED_DEFERRED,
        covers=[TOP_FOLDER_ARTIST],
        value="Ozzy Osbourne",
    )

    assert (result.affected, result.skipped_unflagged) == (1, 1)
    assert [decided.folder for decided in result.files] == ["rendered"]
    assert set(_stored(engine_settings)) == {jem_id}


def _guard_library(settings: Settings, music_dir: Path) -> dict[str, int]:
    """The mislabeled Ozzy group plus a second flagged group. Return the ids by role."""
    ozzy = _make_mislabeled_library(music_dir)
    other = music_dir / "Other Artist" / "Album"
    make_track(other / "01 Song.mp3", {"albumartist": ["Nobody"]})
    scan_library(settings)
    jem_id, dreamer_id = _ozzy_ids(settings, ozzy)
    return {"jem": jem_id, "dreamer": dreamer_id, "other": _file_id(settings, other, "01 Song.mp3")}


_REFUSALS = [
    pytest.param(
        {"status": MISFILED_DEFERRED, "covers": [], "file_ids": ["jem"]},
        r"outside covers: \[\(\d+, 'top_folder_artist'\)\].*detect_mismatches\(folder=",
        id="name-outside-covers",
    ),
    pytest.param(
        {"status": MISFILED_DEFERRED, "covers": [TOP_FOLDER_ARTIST], "file_ids": ["jem", "other"]},
        "these files span 2",
        id="two-groups",
    ),
    pytest.param(
        {"status": LEGIT_IGNORE, "covers": [TOP_FOLDER_ARTIST], "file_ids": ["jem"]},
        r"member file_id\(s\) \[\d+\] would hold no keep.*unflagged_ids",
        id="keep-splits-the-group",
    ),
    pytest.param(
        {"status": MISFILED_DEFERRED, "covers": [], "file_ids": ["dreamer"]},
        "Omit unflagged_ids",
        id="defer-unflagged",
    ),
    pytest.param(
        {"status": LEGIT_IGNORE, "covers": [TOP_FOLDER_ARTIST], "value": "Jem"},
        "value scope takes only",
        id="value-keep",
    ),
    pytest.param(
        {"status": MISFILED_DEFERRED, "covers": [TOP_FOLDER_ARTIST, CURATED], "value": "Jem"},
        "value scope takes only",
        id="value-other-covers",
    ),
    pytest.param(
        {"status": PENDING, "covers": [], "file_ids": ["jem"]},
        "unknown status",
        id="pending-is-not-a-set-status",
    ),
    pytest.param(
        {"status": LEGIT_IGNORE, "covers": ["folder_artist"], "file_ids": ["jem"]},
        "unknown covers name",
        id="unknown-name",
    ),
    pytest.param(
        {"status": LEGIT_IGNORE, "covers": [], "file_ids": [99999]},
        "unknown file_id",
        id="unknown-id",
    ),
]


@pytest.mark.parametrize(("call", "match"), _REFUSALS)
def test_set_refuses_the_entire_call(
    engine_settings: Settings,
    music_dir: Path,
    call: dict[str, object],
    match: str,
) -> None:
    ids = _guard_library(engine_settings, music_dir)
    file_ids = call.get("file_ids")
    kwargs = dict(call)
    if isinstance(file_ids, list):
        kwargs["file_ids"] = [ids.get(name, name) for name in file_ids]

    with pytest.raises(ValueError, match=match):
        mismatch.set_mismatch_status(engine_settings, **kwargs)  # type: ignore[arg-type]

    assert _stored(engine_settings) == {}


def _stage_path_row(settings: Settings, file_id: int) -> None:
    conn = connect(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO path_revisions_staged (file_id, to_path, origin, staged_at) "
            "VALUES (?, '/elsewhere/a.mp3', 'manual', ?)",
            (file_id, _NOW),
        )
        conn.commit()
    finally:
        conn.close()


def test_both_tools_refuse_a_file_with_a_staged_path_change(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    ids = _guard_library(engine_settings, music_dir)
    _stage_path_row(engine_settings, ids["other"])

    with pytest.raises(ValueError, match="unstage_paths"):
        mismatch.set_mismatch_status(
            engine_settings,
            status=MISFILED_DEFERRED,
            covers=[TOP_FOLDER_ARTIST],
            file_ids=[ids["other"]],
        )
    with pytest.raises(ValueError, match="unstage_paths"):
        mismatch.reset_mismatch_status(engine_settings, file_ids=[ids["other"]])
    assert _stored(engine_settings) == {}


def test_both_tools_refuse_a_missing_file(engine_settings: Settings, music_dir: Path) -> None:
    ids = _guard_library(engine_settings, music_dir)
    (music_dir / "Other Artist" / "Album" / "01 Song.mp3").unlink()
    scan_library(engine_settings)

    with pytest.raises(ValueError, match=rf"\[{ids['other']}\] are missing from disk"):
        mismatch.set_mismatch_status(
            engine_settings,
            status=MISFILED_DEFERRED,
            covers=[TOP_FOLDER_ARTIST],
            file_ids=[ids["other"]],
        )
    with pytest.raises(ValueError, match="missing from disk"):
        mismatch.reset_mismatch_status(engine_settings, file_ids=[ids["other"]])


def test_a_keep_of_the_entire_group_reads_legit_ignore_on_every_member(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    ids = _guard_library(engine_settings, music_dir)
    ozzy = next(
        g for g in detect_mismatches(engine_settings, group=True).groups if ids["jem"] in g.file_ids
    )

    mismatch.set_mismatch_status(
        engine_settings,
        status=LEGIT_IGNORE,
        covers=[*ozzy.comparisons],
        file_ids=[*ozzy.file_ids, *ozzy.unflagged_ids],
    )

    conn = connect(engine_settings.db_path)
    try:
        states = mismatch.file_states(conn, engine_settings, [ids["jem"], ids["dreamer"]])
        assert {fid: state.status for fid, state in states.items()} == {
            ids["jem"]: LEGIT_IGNORE,
            ids["dreamer"]: LEGIT_IGNORE,
        }
        assert mismatch.planner_keep(conn, ids["jem"]) is True
        assert mismatch.planner_keep(conn, ids["other"]) is False
    finally:
        conn.close()


def test_gate_state_and_check_files_follow_the_decisions(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    ozzy = _make_mislabeled_library(music_dir)
    scan_library(engine_settings)
    jem_id, dreamer_id = _ozzy_ids(engine_settings, ozzy)

    def check() -> list[tuple[int, str]]:
        conn = connect(engine_settings.db_path)
        try:
            return mismatch.check_files(conn, engine_settings, [jem_id, dreamer_id])
        finally:
            conn.close()

    assert mismatch.gate_state(engine_settings) == mismatch.GateState(
        open=False,
        flagged=1,
        exceptions_undecided=0,
    )
    assert check() == [(jem_id, TOP_FOLDER_ARTIST)]

    mismatch.set_mismatch_status(
        engine_settings,
        status=MISFILED_DEFERRED,
        covers=[TOP_FOLDER_ARTIST],
        file_ids=[jem_id],
    )

    assert mismatch.gate_state(engine_settings).to_dict() == {
        "open": True,
        "flagged": 0,
        "exceptions_undecided": 0,
    }
    assert check() == []


def test_disposition_goes_stale_when_albumartist_edited(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    ozzy = _make_mislabeled_library(music_dir)
    scan_library(engine_settings)
    jem_file = ozzy / "01 Gets Me Through.mp3"
    jem_id, _ = _ozzy_ids(engine_settings, ozzy)

    mismatch.set_mismatch_status(
        engine_settings,
        status=MISFILED_DEFERRED,
        covers=[TOP_FOLDER_ARTIST],
        file_ids=[jem_id],
    )
    assert _find(detect_mismatches(engine_settings), jem_id) is None

    # Edit the albumartist on disk (still disagreeing) + rescan -> the covered tag changed.
    make_track(jem_file, {"albumartist": ["Jem Griffiths"], "artist": ["Ozzy Osbourne"]})
    scan_library(engine_settings)

    resurfaced = _find(detect_mismatches(engine_settings), jem_id)
    assert resurfaced is not None
    assert (resurfaced.was, resurfaced.changed) == (MISFILED_DEFERRED, CHANGED_TAGS)


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

    # Seed prior auto genre work + a sticky artist exclusion on the poisoned files, so the
    # fix has real axis state to re-open. The files carry no album, so year has no identity.
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

    # 2. keep the curated single's folder -> suppressed + reported, not in rows.
    kept = mismatch.set_mismatch_status(
        engine_settings,
        status=LEGIT_IGNORE,
        covers=[TOP_FOLDER_ARTIST, CURATED],
        file_ids=[fp_id],
    )
    assert kept.affected == 1
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

    # 5. the new album artist re-opens the genre outcome. The manual artist row stays.
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
        "set_mismatch_status",
        "reset_mismatch_status",
    } <= names


def test_mcp_set_and_reset_mismatch_status_envelopes(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    ozzy = _make_mislabeled_library(music_dir)
    mcp_server.scan_library(path=str(music_dir))
    jem_id = _file_id(config.load_settings(), ozzy, "01 Gets Me Through.mp3")
    dreamer_id = _file_id(config.load_settings(), ozzy, "03 Dreamer.mp3")

    split = mcp_server.set_mismatch_status("legit_ignore", ["top_folder_artist"], [jem_id])
    assert split["ok"] is False
    assert "unflagged_ids" in str(split["error"])

    ok = mcp_server.set_mismatch_status(
        "legit_ignore",
        ["top_folder_artist"],
        file_ids=[jem_id, dreamer_id],
    )
    assert (ok["ok"], ok["affected"], ok["note"]) == (True, 2, mismatch.FILENAME_NOTE)
    payload = mcp_server.detect_mismatches()
    assert (payload["suppressed"], payload["gate_open"]) == ({"legit_ignore": 2}, True)

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
    dispositions = {
        200: _decision(
            LEGIT_IGNORE,
            [TOP_FOLDER_ARTIST],
            {"albumartist": "Wrong Artist", "artist": None},
            folder="The Luna Sequence/(2010) Album",
        ),
    }

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


# --- the decision lifecycle: one test per cell of the state table ---------------------
#
# T flags the top folder (albumartist "Other" under "Artist") and its filename title. Its
# filename number 2 is its place in the folder's (disc, track) order while its sibling S sits
# on disc 1, so a disc edit on S decides whether T's track comparison flags. S flags nothing.

_T_FILE = "02 Song.mp3"
_S_FILE = "01 Other Song.mp3"
_T_NAMES = frozenset({TOP_FOLDER_ARTIST, FILENAME_TITLE})

_NO_ROW = "no_row"
_KEEP_IN_FORCE = "keep_in_force"
_MISFILED_CURRENT = "misfiled_current"
_KEEP_NOT_IN_FORCE = "keep_not_in_force"
_MISFILED_STALE = "misfiled_stale"
_STATES = (_NO_ROW, _KEEP_IN_FORCE, _MISFILED_CURRENT, _KEEP_NOT_IN_FORCE, _MISFILED_STALE)
_MOVED_STATES = frozenset({_KEEP_NOT_IN_FORCE, _MISFILED_STALE})
_KEEP_STATES = frozenset({_KEEP_IN_FORCE, _KEEP_NOT_IN_FORCE})


@dataclass(frozen=True, slots=True)
class _Lib:
    settings: Settings
    music: Path
    t: int
    s: int


@dataclass(frozen=True, slots=True)
class _Reading:
    """T's status, unsilenced names, planner keep, stored row status and change reason."""

    status: str
    unsilenced: frozenset[str]
    keep: bool
    row: str | None
    changed: str | None


def _lifecycle_library(settings: Settings, music_dir: Path, *, sibling_disc: str = "1") -> _Lib:
    album = music_dir / "Artist" / "Album"
    common = {"album": ["Album"], "tracknumber": ["1"]}
    make_track(
        album / _T_FILE,
        {
            "albumartist": ["Other"],
            "artist": ["Other"],
            "title": ["Different Title"],
            "discnumber": ["2"],
            **common,
        },
    )
    make_track(
        album / _S_FILE,
        {
            "albumartist": ["Artist"],
            "artist": ["Artist"],
            "title": ["Other Song"],
            "discnumber": [sibling_disc],
            **common,
        },
    )
    scan_library(settings)
    return _Lib(
        settings=settings,
        music=music_dir,
        t=_file_id(settings, album, _T_FILE),
        s=_file_id(settings, album, _S_FILE),
    )


def _path_of(lib: _Lib, file_id: int) -> Path:
    conn = connect(lib.settings.db_path)
    try:
        row = store.get_file_by_id(conn, file_id)
        assert row is not None
        return Path(row.folder) / row.filename
    finally:
        conn.close()


def _commit_move(lib: _Lib, targets: dict[int, Path]) -> None:
    """Move each file on disk, append its path revision and repoint its row, as C4 will."""
    sources = {file_id: _path_of(lib, file_id) for file_id in targets}
    conn = connect(lib.settings.db_path)
    try:
        versions = store.path_versions(conn)
        for file_id, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            sources[file_id].rename(target)
            store.insert_path_revision(
                conn,
                file_id=file_id,
                version=versions.get(file_id, 0) + 1,
                commit_id=None,
                origin="manual",
                from_path=str(sources[file_id]),
                to_path=str(target),
                now=_NOW,
            )
            store.relocate_file(
                conn, file_id, folder=str(target.parent), filename=target.name, now=_NOW
            )
        conn.commit()
    finally:
        conn.close()


def _move_group(lib: _Lib, folder: str) -> None:
    target = lib.music / "Artist" / folder
    _commit_move(lib, {fid: target / _path_of(lib, fid).name for fid in (lib.t, lib.s)})


def _retag(lib: _Lib, file_id: int, **tags: str) -> None:
    staging.stage_tags(lib.settings, file_id=file_id, tags={k: [v] for k, v in tags.items()})
    staging.commit_tags(lib.settings)


def _decide(lib: _Lib, status: str, covers: frozenset[str] = _T_NAMES) -> None:
    file_ids = [lib.t, lib.s] if status == LEGIT_IGNORE else [lib.t]
    mismatch.set_mismatch_status(
        lib.settings,
        status=status,
        covers=sorted(covers),
        file_ids=file_ids,
    )


def _enter(lib: _Lib, state: str, covers: frozenset[str] = _T_NAMES) -> None:
    if state in _KEEP_STATES:
        _decide(lib, LEGIT_IGNORE, covers)
    elif state != _NO_ROW:
        _decide(lib, MISFILED_DEFERRED, covers)
    if state in _MOVED_STATES:
        _move_group(lib, "Album Moved")


def _reading(lib: _Lib) -> _Reading:
    conn = connect(lib.settings.db_path)
    try:
        state = mismatch.file_states(conn, lib.settings, [lib.t])[lib.t]
        row = store.get_mismatch_status(conn, lib.t)
        keep = mismatch.planner_keep(conn, lib.t)
    finally:
        conn.close()
    return _Reading(
        status=state.status,
        unsilenced=state.unsilenced,
        keep=keep,
        row=None if row is None else row.status,
        changed=state.changed,
    )


def _human_sets(lib: _Lib, state: str) -> None:
    _decide(lib, MISFILED_DEFERRED if state in _KEEP_STATES else LEGIT_IGNORE)


def _covered_tag_changes(lib: _Lib, _state: str) -> None:
    _retag(lib, lib.t, title="Another Title")


def _covered_tags_return(lib: _Lib, _state: str) -> None:
    _retag(lib, lib.t, title="Another Title")
    _retag(lib, lib.t, title="Different Title")


def _move_folder(lib: _Lib, _state: str) -> None:
    _move_group(lib, "Album Elsewhere")


def _move_filename(lib: _Lib, _state: str) -> None:
    path = _path_of(lib, lib.t)
    _commit_move(lib, {lib.t: path.with_name("02 Song (Live).mp3")})


def _revert(lib: _Lib, state: str) -> None:
    if state not in _MOVED_STATES:
        _move_group(lib, "Album Moved")
    _move_group(lib, "Album")


def _human_resets(lib: _Lib, _state: str) -> None:
    mismatch.reset_mismatch_status(lib.settings, file_ids=[lib.t])


def _uncovered_name_flags(lib: _Lib, _state: str) -> None:
    _retag(lib, lib.t, album="Elsewhere")


def _sibling_flips(lib: _Lib, _state: str) -> None:
    _retag(lib, lib.s, discnumber="2")


_EVENTS: dict[str, Callable[[_Lib, str], None]] = {
    "human_sets": _human_sets,
    "covered_tag_changes": _covered_tag_changes,
    "covered_tags_return": _covered_tags_return,
    "committed_move_folder": _move_folder,
    "committed_move_filename": _move_filename,
    "revert_returns_old_path": _revert,
    "human_resets": _human_resets,
    "name_outside_covers_flags": _uncovered_name_flags,
    "sibling_edit_flips_a_flag": _sibling_flips,
}

_TOP: Final = TOP_FOLDER_ARTIST
_TITLE: Final = FILENAME_TITLE
_BOTH = frozenset({_TOP, _TITLE})
_NONE: frozenset[str] = frozenset()


def _pending(*names: str, row: str | None = None, changed: str | None = None) -> _Reading:
    """A reading of T that flags *names* and so reads pending, the planner keeping nothing."""
    return _Reading(PENDING, frozenset(names), keep=False, row=row, changed=changed)


_K, _M = LEGIT_IGNORE, MISFILED_DEFERRED
_TABLE: dict[tuple[str, str], _Reading] = {
    # no row: flags follow the tags and the path.
    (_NO_ROW, "human_sets"): _Reading(_K, _NONE, keep=True, row=_K, changed=None),
    (_NO_ROW, "covered_tag_changes"): _pending(_TOP, _TITLE),
    (_NO_ROW, "covered_tags_return"): _pending(_TOP, _TITLE),
    (_NO_ROW, "committed_move_folder"): _pending(_TOP, _TITLE),
    (_NO_ROW, "committed_move_filename"): _pending(_TOP, _TITLE),
    (_NO_ROW, "revert_returns_old_path"): _pending(_TOP, _TITLE),
    (_NO_ROW, "human_resets"): _pending(_TOP, _TITLE),
    (_NO_ROW, "name_outside_covers_flags"): _pending(_TOP, _TITLE, RELEASE_FOLDER_ALBUM),
    (_NO_ROW, "sibling_edit_flips_a_flag"): _pending(_TOP, _TITLE, FILENAME_TRACK),
    # keep in force: the folder stays kept while its key matches, whatever the tags do.
    (_KEEP_IN_FORCE, "human_sets"): _Reading(_M, _NONE, keep=False, row=_M, changed=None),
    (_KEEP_IN_FORCE, "covered_tag_changes"): _Reading(
        PENDING, frozenset({_TITLE}), keep=True, row=_K, changed=CHANGED_TAGS
    ),
    (_KEEP_IN_FORCE, "covered_tags_return"): _Reading(_K, _NONE, keep=True, row=_K, changed=None),
    (_KEEP_IN_FORCE, "committed_move_folder"): _pending(
        _TOP, _TITLE, row=_K, changed=CHANGED_MOVED
    ),
    (_KEEP_IN_FORCE, "committed_move_filename"): _Reading(
        PENDING, frozenset({_TITLE}), keep=True, row=_K, changed=CHANGED_MOVED
    ),
    (_KEEP_IN_FORCE, "revert_returns_old_path"): _Reading(
        PENDING, frozenset({_TITLE}), keep=True, row=_K, changed=CHANGED_MOVED
    ),
    (_KEEP_IN_FORCE, "human_resets"): _pending(_TOP, _TITLE),
    (_KEEP_IN_FORCE, "name_outside_covers_flags"): _Reading(
        PENDING, frozenset({RELEASE_FOLDER_ALBUM}), keep=True, row=_K, changed=CHANGED_UNCOVERED
    ),
    (_KEEP_IN_FORCE, "sibling_edit_flips_a_flag"): _Reading(
        PENDING, frozenset({FILENAME_TRACK}), keep=True, row=_K, changed=CHANGED_UNCOVERED
    ),
    # misfiled current: every name binds to the path version.
    (_MISFILED_CURRENT, "human_sets"): _Reading(_K, _NONE, keep=True, row=_K, changed=None),
    (_MISFILED_CURRENT, "covered_tag_changes"): _pending(_TITLE, row=_M, changed=CHANGED_TAGS),
    (_MISFILED_CURRENT, "covered_tags_return"): _Reading(
        _M, _NONE, keep=False, row=_M, changed=None
    ),
    (_MISFILED_CURRENT, "committed_move_folder"): _pending(
        _TOP, _TITLE, row=_M, changed=CHANGED_MOVED
    ),
    (_MISFILED_CURRENT, "committed_move_filename"): _pending(
        _TOP, _TITLE, row=_M, changed=CHANGED_MOVED
    ),
    (_MISFILED_CURRENT, "revert_returns_old_path"): _pending(
        _TOP, _TITLE, row=_M, changed=CHANGED_MOVED
    ),
    (_MISFILED_CURRENT, "human_resets"): _pending(_TOP, _TITLE),
    (_MISFILED_CURRENT, "name_outside_covers_flags"): _pending(
        RELEASE_FOLDER_ALBUM, row=_M, changed=CHANGED_UNCOVERED
    ),
    (_MISFILED_CURRENT, "sibling_edit_flips_a_flag"): _pending(
        FILENAME_TRACK, row=_M, changed=CHANGED_UNCOVERED
    ),
    # keep not in force: reads as no row until a revert returns the kept folder.
    (_KEEP_NOT_IN_FORCE, "human_sets"): _Reading(_M, _NONE, keep=False, row=_M, changed=None),
    (_KEEP_NOT_IN_FORCE, "covered_tag_changes"): _pending(
        _TOP, _TITLE, row=_K, changed=CHANGED_MOVED
    ),
    (_KEEP_NOT_IN_FORCE, "covered_tags_return"): _pending(
        _TOP, _TITLE, row=_K, changed=CHANGED_MOVED
    ),
    (_KEEP_NOT_IN_FORCE, "committed_move_folder"): _pending(
        _TOP, _TITLE, row=_K, changed=CHANGED_MOVED
    ),
    (_KEEP_NOT_IN_FORCE, "committed_move_filename"): _pending(
        _TOP, _TITLE, row=_K, changed=CHANGED_MOVED
    ),
    (_KEEP_NOT_IN_FORCE, "revert_returns_old_path"): _Reading(
        PENDING, frozenset({_TITLE}), keep=True, row=_K, changed=CHANGED_MOVED
    ),
    (_KEEP_NOT_IN_FORCE, "human_resets"): _pending(_TOP, _TITLE),
    (_KEEP_NOT_IN_FORCE, "name_outside_covers_flags"): _pending(
        _TOP, _TITLE, RELEASE_FOLDER_ALBUM, row=_K, changed=CHANGED_UNCOVERED
    ),
    (_KEEP_NOT_IN_FORCE, "sibling_edit_flips_a_flag"): _pending(
        _TOP, _TITLE, FILENAME_TRACK, row=_K, changed=CHANGED_UNCOVERED
    ),
    # misfiled stale: never re-arms, even when a revert returns the old path.
    (_MISFILED_STALE, "human_sets"): _Reading(_K, _NONE, keep=True, row=_K, changed=None),
    (_MISFILED_STALE, "covered_tag_changes"): _pending(_TOP, _TITLE, row=_M, changed=CHANGED_MOVED),
    (_MISFILED_STALE, "covered_tags_return"): _pending(_TOP, _TITLE, row=_M, changed=CHANGED_MOVED),
    (_MISFILED_STALE, "committed_move_folder"): _pending(
        _TOP, _TITLE, row=_M, changed=CHANGED_MOVED
    ),
    (_MISFILED_STALE, "committed_move_filename"): _pending(
        _TOP, _TITLE, row=_M, changed=CHANGED_MOVED
    ),
    (_MISFILED_STALE, "revert_returns_old_path"): _pending(
        _TOP, _TITLE, row=_M, changed=CHANGED_MOVED
    ),
    (_MISFILED_STALE, "human_resets"): _pending(_TOP, _TITLE),
    (_MISFILED_STALE, "name_outside_covers_flags"): _pending(
        _TOP, _TITLE, RELEASE_FOLDER_ALBUM, row=_M, changed=CHANGED_UNCOVERED
    ),
    (_MISFILED_STALE, "sibling_edit_flips_a_flag"): _pending(
        _TOP, _TITLE, FILENAME_TRACK, row=_M, changed=CHANGED_UNCOVERED
    ),
}


def test_the_state_table_has_no_blank_cell() -> None:
    assert set(_TABLE) == {(state, event) for state in _STATES for event in _EVENTS}


@pytest.mark.parametrize(("state", "event"), list(_TABLE), ids=[f"{s}-{e}" for s, e in _TABLE])
def test_decision_lifecycle_cell(
    engine_settings: Settings,
    music_dir: Path,
    state: str,
    event: str,
) -> None:
    lib = _lifecycle_library(engine_settings, music_dir)
    _enter(lib, state)

    _EVENTS[event](lib, state)

    expected = _TABLE[(state, event)]
    assert _reading(lib) == expected
    # S never flags, so the gate is open exactly when T flags nothing unsilenced.
    assert mismatch.gate_state(engine_settings).open is (not expected.unsilenced)


_COVERED_SIBLING: dict[str, _Reading] = {
    _NO_ROW: _pending(_TOP, _TITLE, FILENAME_TRACK),
    _KEEP_IN_FORCE: _Reading(_K, _NONE, keep=True, row=_K, changed=None),
    _MISFILED_CURRENT: _Reading(_M, _NONE, keep=False, row=_M, changed=None),
    _KEEP_NOT_IN_FORCE: _pending(_TOP, _TITLE, FILENAME_TRACK, row=_K, changed=CHANGED_MOVED),
    _MISFILED_STALE: _pending(_TOP, _TITLE, FILENAME_TRACK, row=_M, changed=CHANGED_MOVED),
}


@pytest.mark.parametrize("state", _STATES)
def test_a_sibling_edit_that_flips_a_covered_flag_back_is_silenced(
    engine_settings: Settings,
    music_dir: Path,
    state: str,
) -> None:
    # With S on T's disc, T's track flags at decision time, so the decision covers it.
    lib = _lifecycle_library(engine_settings, music_dir, sibling_disc="2")
    _enter(lib, state, _T_NAMES | {FILENAME_TRACK})

    _retag(lib, lib.s, discnumber="1")  # T's track comparison clears
    _retag(lib, lib.s, discnumber="2")  # and flags again

    assert _reading(lib) == _COVERED_SIBLING[state]


# --- v23 decisions after the v24 upgrade ------------------------------------------------


def _legacy_ledger(settings: Settings, rows: list[tuple[int, str, str | None, str | None]]) -> None:
    """Rebuild ``file_mismatch_status`` in its v23 shape holding *rows*, stamped v23."""
    conn = connect(settings.db_path)
    try:
        conn.execute("DROP TABLE file_mismatch_status")
        conn.execute(
            "CREATE TABLE file_mismatch_status (file_id INTEGER PRIMARY KEY REFERENCES files(id) "
            "ON DELETE CASCADE, status TEXT NOT NULL, source_field TEXT, source_value TEXT, "
            "updated_at TEXT NOT NULL)",
        )
        conn.executemany(
            "INSERT INTO file_mismatch_status VALUES (?, ?, ?, ?, ?)",
            [(*row, _NOW) for row in rows],
        )
        conn.execute("PRAGMA user_version = 23")
        conn.commit()
    finally:
        conn.close()


def test_migrated_decisions_silence_only_the_top_folder_comparison(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = {
        "keep": ("Artist/Album", {"albumartist": ["Jem"], "artist": ["Artist"]}),
        "null_keep": ("Artist/Blank", {}),
        "fix": ("Ozzy/Tour", {"artist": ["Ozzy Osbourne"], "title": ["Other"]}),
        "album_keep": ("Artist/Record", {"albumartist": ["Someone"], "album": ["Different"]}),
        "null_fix": ("Artist/Quiet", {}),
        "defer": ("Ozzy/Record", {"artist": ["Jem"]}),
    }
    for folder, tags in tracks.values():
        make_track(music_dir / folder / "01 Song.mp3", tags or None)
    scan_library(engine_settings)
    ids = {
        name: _file_id(engine_settings, music_dir / folder, "01 Song.mp3")
        for name, (folder, _) in tracks.items()
    }
    _legacy_ledger(
        engine_settings,
        [
            (ids["keep"], LEGIT_IGNORE, "albumartist", "Jem"),
            (ids["null_keep"], LEGIT_IGNORE, None, None),
            (ids["fix"], MISFILED_DEFERRED, "artist", "Ozzy Osbourne"),
            (ids["album_keep"], LEGIT_IGNORE, "albumartist", "Someone"),
            (ids["null_fix"], MISFILED_DEFERRED, None, None),
            (ids["defer"], MISFILED_DEFERRED, "artist", "Jem"),
        ],
    )

    def readings() -> dict[str, tuple[str, frozenset[str], str | None]]:
        conn = connect(engine_settings.db_path)
        try:
            apply_schema(conn)
            states = mismatch.file_states(conn, engine_settings, list(ids.values()))
        finally:
            conn.close()
        return {
            name: (states[fid].status, states[fid].unsilenced, states[fid].changed)
            for name, fid in ids.items()
        }

    assert readings() == {
        "keep": (LEGIT_IGNORE, frozenset(), None),
        "null_keep": (LEGIT_IGNORE, frozenset(), None),
        "fix": (PENDING, frozenset({FILENAME_TITLE}), CHANGED_UNCOVERED),
        "album_keep": (PENDING, frozenset({RELEASE_FOLDER_ALBUM}), CHANGED_UNCOVERED),
        "null_fix": (MISFILED_DEFERRED, frozenset(), None),
        "defer": (MISFILED_DEFERRED, frozenset(), None),
    }

    staging.stage_tags(engine_settings, file_id=ids["keep"], tags={"albumartist": ["Jem G"]})
    staging.stage_tags(engine_settings, file_id=ids["null_keep"], tags={"artist": ["Nobody"]})
    staging.stage_tags(engine_settings, file_id=ids["defer"], tags={"albumartist": ["Someone New"]})
    staging.commit_tags(engine_settings)

    changed = readings()
    assert changed["keep"] == (PENDING, frozenset({TOP_FOLDER_ARTIST}), CHANGED_TAGS)
    assert changed["null_keep"] == (PENDING, frozenset({TOP_FOLDER_ARTIST}), CHANGED_TAGS)
    assert changed["defer"] == (PENDING, frozenset({TOP_FOLDER_ARTIST}), CHANGED_TAGS)
