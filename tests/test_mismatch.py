"""Tests for mislabeled-file detection (:mod:`tagmend.engine.mismatch`).

Pure-function tests for ``fold`` / ``_top_artist`` / ``_primary_artist`` / ``_disagrees``
and the pure ``_classify`` core over constructed inputs covering every labeled class
(HIGH/MEDIUM/LOW, the diacritic/alias/VA/non-album false-positive classes, the reliability
guard, the artist fallback, and a library-root file that must not crash), plus an
integration test through ``scan_library`` on real audio and CLI/MCP wiring smoke checks.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import make_track
from tagmend import config, mcp_server
from tagmend.cli import app
from tagmend.config import Settings
from tagmend.engine import mismatch, store
from tagmend.engine.db import connect
from tagmend.engine.library import scan_library
from tagmend.engine.mismatch import detect_mismatches

_MUSIC = Path("/library/music")
runner = CliRunner()


def _make_mislabeled_library(music_dir: Path) -> Path:
    """Create clean, agreeing tracks + a Jem-mislabeled Ozzy folder; return the Ozzy folder.

    The four clean folders keep the library-wide disagreement rate under the reliability
    floor, so the mislabeled ``Jem`` file surfaces as HIGH (mixed-albumartist folder).
    """
    for name in ("CleanA", "CleanB", "CleanC", "CleanD"):
        make_track(music_dir / name / "01.mp3", {"albumartist": [name], "artist": [name]})
    ozzy = music_dir / "Ozzy Osbourne" / "(2001) Ozzy Osbourne - Down To Earth"
    make_track(
        ozzy / "01 Gets Me Through.mp3",
        {"albumartist": ["Jem"], "artist": ["Ozzy Osbourne"]},
    )
    make_track(ozzy / "03 Dreamer.mp3", {"albumartist": ["Ozzy Osbourne"], "artist": ["Ozzy"]})
    return ozzy


def _mk(
    file_id: int,
    folder: Path,
    filename: str,
    *,
    albumartist: str | None = None,
    artist: str | None = None,
) -> mismatch._FileInput:
    return mismatch._FileInput(
        file_id=file_id,
        folder=str(folder),
        filename=filename,
        albumartist=albumartist,
        artist=artist,
    )


def _find(report: mismatch.MismatchReport, file_id: int) -> mismatch.MismatchRow | None:
    return next((r for r in report.rows if r.file_id == file_id), None)


# --- fold ---------------------------------------------------------------------------


def test_fold_folds_ligatures_and_diacritics_equal() -> None:
    assert mismatch.fold("Leæther Strip") == mismatch.fold("Leaether Strip")
    assert mismatch.fold("Dååth") == mismatch.fold("Daath")
    assert mismatch.fold("Röyksopp") == mismatch.fold("Royksopp")


def test_fold_strips_case_space_punctuation() -> None:
    assert mismatch.fold("  Ozzy  Osbourne! ") == "ozzyosbourne"
    assert mismatch.fold("A.B. & C") == "abc"


# --- _top_artist --------------------------------------------------------------------


def test_top_artist_extracts_and_strips_discography() -> None:
    assert mismatch._top_artist(str(_MUSIC / "Ozzy Osbourne" / "Album"), _MUSIC) == "Ozzy Osbourne"
    stp = _MUSIC / "Stone Temple Pilots [Discography]" / "MP3"
    assert mismatch._top_artist(str(stp), _MUSIC) == "Stone Temple Pilots"


def test_top_artist_none_at_root_or_outside() -> None:
    # A file directly under music_path has empty relative parts -> None (no path signal).
    assert mismatch._top_artist(str(_MUSIC), _MUSIC) is None
    # A folder not under music_path -> relative_to ValueError -> None.
    assert mismatch._top_artist(str(Path("/somewhere/else")), _MUSIC) is None


# --- _primary_artist ----------------------------------------------------------------


def test_primary_artist_splits_feat_and_separators() -> None:
    assert mismatch._primary_artist("Neon Hitch feat. Someone") == "Neon Hitch"
    assert mismatch._primary_artist("A & B") == "A"
    assert mismatch._primary_artist("A, B, C") == "A"
    assert mismatch._primary_artist("Solo Artist") == "Solo Artist"


# --- _disagrees (bidirectional containment) -----------------------------------------


def test_disagrees_bidirectional_and_none_top() -> None:
    track = _MUSIC / "Lusine" / "(2005) Lusine - Album" / "01.mp3"
    # Alias/suffix in the folder's artist -> agrees (top-artist contained in albumartist).
    assert mismatch._disagrees("Lusine ICL", str(track), "Lusine") is False
    reverse = _MUSIC / "Lusine ICL" / "Album" / "01.mp3"
    # And the reverse containment (albumartist contained in top-artist) also agrees.
    assert mismatch._disagrees("Lusine", str(reverse), "Lusine ICL") is False
    # A genuinely unrelated albumartist disagrees.
    ozzy = _MUSIC / "Ozzy Osbourne" / "Down To Earth" / "01.mp3"
    assert mismatch._disagrees("Jem", str(ozzy), "Ozzy Osbourne") is True
    # No top-artist means no path signal -> never disagrees.
    assert mismatch._disagrees("Jem", str(ozzy), None) is False


# --- pure classifier: every labeled class -------------------------------------------


def _all_classes_library() -> list[mismatch._FileInput]:
    """Constructed inputs reproducing each labeled class with clean padding.

    Enough clean, agreeing files keep the library-wide disagreement rate below the
    reliability floor so the HIGH/MEDIUM path tiers stay active.
    """
    ozzy = _MUSIC / "Ozzy Osbourne" / "(2001) Ozzy Osbourne - Down To Earth"
    chiasm = _MUSIC / "Chiasm" / "(2003) Chiasm - Divided We Fall"
    singles = _MUSIC / "Blue Stahli" / "Singles"
    leaether = _MUSIC / "Leaether Strip" / "(1990) Album"
    lusine = _MUSIC / "Lusine" / "(2005) Lusine - Serial"
    various = _MUSIC / "Various Artists" / "(1999) Comp"
    neon_single = _MUSIC / "Neon Hitch" / "(2012) Single"
    gym = _MUSIC / "Gym Class Heroes" / "(2011) Album"
    files = [_mk(i, _MUSIC / f"Clean{i}", "01.mp3", albumartist=f"Clean{i}") for i in range(1, 19)]
    files += [
        # Ozzy: mixed albumartist folder -> Jem=HIGH, Ozzy=LOW (folder-consistency).
        _mk(100, ozzy, "01 Gets Me Through.mp3", albumartist="Jem", artist="Ozzy Osbourne"),
        _mk(101, ozzy, "03 Dreamer.mp3", albumartist="Ozzy Osbourne", artist="Ozzy Osbourne"),
        # Chiasm: uniformly mis-stamped folder -> MEDIUM.
        _mk(110, chiasm, "01.mp3", albumartist="Bill Leverty"),
        _mk(111, chiasm, "02.mp3", albumartist="Bill Leverty"),
        # Diacritic/ligature variant -> agrees, not flagged.
        _mk(120, leaether, "01.mp3", albumartist="Leæther Strip"),
        # Alias/suffix -> bidirectional containment, not flagged.
        _mk(130, lusine, "01.mp3", albumartist="Lusine ICL"),
        # Various Artists -> excluded entirely.
        _mk(140, various, "01.mp3", albumartist="Various Artists"),
        # Singles (non-album guard) -> path disagreement demoted to LOW.
        _mk(150, singles, "a.mp3", albumartist="Future Islands"),
        _mk(151, singles, "b.mp3", albumartist="Celldweller"),
        # 1-file folder (non-album guard) -> LOW.
        _mk(160, neon_single, "01.mp3", albumartist="Gym Class Heroes"),
        # No albumartist, artist disagrees -> artist fallback LOW.
        _mk(170, gym, "01.mp3", artist="Neon Hitch feat. X"),
    ]
    return files


def test_classify_every_labeled_class() -> None:
    report = mismatch._classify(_all_classes_library(), _MUSIC)

    assert report.path_signal_suppressed is False

    # HIGH: Ozzy's Jem-stamped file (mixed-albumartist folder + path disagreement).
    jem = _find(report, 100)
    assert jem is not None
    assert jem.tier == "high"
    assert jem.field == "albumartist"
    assert jem.tag_value == "Jem"
    assert jem.path_artist == "Ozzy Osbourne"

    # MEDIUM: uniformly mis-stamped folder.
    assert (_find(report, 110), _find(report, 111)) != (None, None)
    assert report.medium == 2
    for fid in (110, 111):
        row = _find(report, fid)
        assert row is not None
        assert row.tier == "medium"

    # NOT flagged: diacritic variant, alias/suffix, Various Artists.
    assert _find(report, 120) is None
    assert _find(report, 130) is None
    assert _find(report, 140) is None

    # LOW: non-album guards (Singles + 1-file), artist fallback, and the mixed-folder
    # clean sibling (folder-consistency).
    for fid in (150, 151, 160, 101):
        row = _find(report, fid)
        assert row is not None
        assert row.tier == "low"
    fallback = _find(report, 170)
    assert fallback is not None
    assert fallback.tier == "low"
    assert fallback.field == "artist"

    assert report.high == 1
    assert report.low == 5
    assert report.flagged == 8


# --- reliability guard --------------------------------------------------------------


def test_reliability_guard_suppresses_path_tiers() -> None:
    genres = ["Rock", "Pop", "Jazz", "Metal", "Blues"]
    artists = ["Foo Fighters", "Madonna", "Miles Davis", "Metallica", "B.B. King"]
    files = [
        _mk(index, _MUSIC / genre / "Album", "01.mp3", albumartist=artist)
        for index, (genre, artist) in enumerate(zip(genres, artists, strict=True), start=1)
    ]
    mixed = _MUSIC / "Mixed" / "Comp"
    files += [
        _mk(10, mixed, "a.mp3", albumartist="X Artist"),
        _mk(11, mixed, "b.mp3", albumartist="Y Artist"),
        # An artist-fallback candidate: must also be suppressed (it is a path signal).
        _mk(20, _MUSIC / "Genre7" / "Album", "01.mp3", artist="Some Artist"),
    ]

    report = mismatch._classify(files, _MUSIC)

    assert report.path_signal_suppressed is True
    assert report.high == 0
    assert report.medium == 0
    # The genre-organized single-artist folders and the artist fallback are all suppressed.
    for fid in (1, 2, 3, 4, 5, 20):
        assert _find(report, fid) is None
    # Only the naming-agnostic folder-consistency LOW survives.
    assert report.low == 2
    for fid in (10, 11):
        row = _find(report, fid)
        assert row is not None
        assert row.tier == "low"


# --- library-root file must not crash -----------------------------------------------


def test_root_level_file_no_crash_no_path_flag() -> None:
    files = [
        _mk(1, _MUSIC, "loose.mp3", albumartist="Random Artist"),  # folder == music_path
        _mk(2, _MUSIC / "CleanA", "01.mp3", albumartist="CleanA"),
    ]
    report = mismatch._classify(files, _MUSIC)
    # No path signal for the root file -> never HIGH/MEDIUM, and no crash.
    assert _find(report, 1) is None
    assert report.high == 0
    assert report.medium == 0


# --- integration: scan real audio, then detect --------------------------------------


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

    assert report.path_signal_suppressed is False
    jem_id = _file_id(engine_settings, ozzy, "01 Gets Me Through.mp3")
    row = _find(report, jem_id)
    assert row is not None
    assert row.tier == "high"
    assert row.field == "albumartist"
    assert row.tag_value == "Jem"

    # Read-only: nothing staged and the file's tags are untouched on disk/in the ledger.
    conn = connect(engine_settings.db_path)
    try:
        assert store.any_staged(conn) is False
    finally:
        conn.close()
    assert _read_albumartist(engine_settings, ozzy, "01 Gets Me Through.mp3") == ["Jem"]


def test_detect_tier_filter_and_unknown_tier(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_mislabeled_library(music_dir)
    scan_library(engine_settings)

    high_only = detect_mismatches(engine_settings, tier="high")
    assert all(r.tier == "high" for r in high_only.rows)
    # Counts stay library-wide even when rows are filtered to one tier.
    assert high_only.high == 1
    assert high_only.low >= 1

    with pytest.raises(ValueError, match="unknown tier"):
        detect_mismatches(engine_settings, tier="bogus")


def test_detect_requires_music_path(tmp_path: Path) -> None:
    settings = Settings(
        music_path=None,
        lastfm_api_key=None,
        db_path=tmp_path / "ledger.sqlite3",
    )
    with pytest.raises(ValueError, match="music_path not configured"):
        detect_mismatches(settings)


# --- CLI + MCP wiring ---------------------------------------------------------------


def test_cli_detect_reports_high(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    _make_mislabeled_library(music_dir)

    assert runner.invoke(app, ["scan", str(music_dir)]).exit_code == 0
    result = runner.invoke(app, ["detect"])

    assert result.exit_code == 0
    assert "HIGH" in result.stdout
    assert "Jem" in result.stdout


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
    rows = payload["rows"]
    assert isinstance(rows, list)
    assert any(r["tag_value"] == "Jem" and r["tier"] == "high" for r in rows)
