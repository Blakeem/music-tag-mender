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
from tagmend.engine import artists, mismatch, staging, store, versioning
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


# --- disposition skip-filter (pure classifier) --------------------------------------

_OZZY_FOLDER = str(_MUSIC / "Ozzy Osbourne" / "(2001) Ozzy Osbourne - Down To Earth")


def _disp(status: str, field: str | None, value: str | None) -> store.MismatchStatusRow:
    return store.MismatchStatusRow(status=status, source_field=field, source_value=value)


def test_zero_disposition_output_is_byte_compatible() -> None:
    files = _all_classes_library()
    base = mismatch._classify(files, _MUSIC)
    explicit_empty = mismatch._classify(files, _MUSIC, dispositions={})

    assert base.to_dict() == explicit_empty.to_dict()
    # New fields present, empty, and the existing fields unchanged from the legacy detector.
    assert base.suppressed == {}
    assert base.groups == []
    payload = base.to_dict()
    assert payload["suppressed"] == {}
    assert payload["groups"] == []
    assert payload["flagged"] == 8
    assert payload["high"] == 1
    assert payload["low"] == 5


def test_to_dict_rounds_disagreement_rate() -> None:
    # The engine keeps full float precision (for the RELIABILITY_FLOOR comparison), but
    # the JSON payload an LLM reads on every call must not carry 17 digits of noise.
    report = mismatch.MismatchReport(
        rows=[],
        total_files=80,
        flagged=1,
        high=1,
        medium=0,
        low=0,
        disagreement_rate=0.01250861814242096,
        path_signal_suppressed=False,
        summary="1 flagged",
    )

    assert report.to_dict()["disagreement_rate"] == 0.0125
    assert report.disagreement_rate == 0.01250861814242096  # engine float untouched


def test_fresh_disposition_suppresses_row_and_reports_it() -> None:
    files = _all_classes_library()
    dispositions = {100: _disp("legit_ignore", "albumartist", "Jem")}

    report = mismatch._classify(files, _MUSIC, dispositions=dispositions)

    assert _find(report, 100) is None  # the HIGH Jem row is silenced
    assert report.high == 0
    assert report.flagged == 7  # was 8
    assert report.suppressed == {"legit_ignore": 1}


def test_stale_disposition_resurfaces() -> None:
    files = _all_classes_library()
    # Snapshot recorded "Old Name" but the file's current albumartist is "Jem" -> stale.
    dispositions = {100: _disp("legit_ignore", "albumartist", "Old Name")}

    report = mismatch._classify(files, _MUSIC, dispositions=dispositions)

    jem = _find(report, 100)
    assert jem is not None
    assert jem.tier == "high"
    assert report.suppressed == {}


def test_both_disposition_statuses_suppress_when_fresh() -> None:
    files = _all_classes_library()
    dispositions = {
        100: _disp("legit_ignore", "albumartist", "Jem"),
        150: _disp("misfiled_deferred", "albumartist", "Future Islands"),
    }

    report = mismatch._classify(files, _MUSIC, dispositions=dispositions)

    assert _find(report, 100) is None
    assert _find(report, 150) is None
    assert report.suppressed == {"legit_ignore": 1, "misfiled_deferred": 1}


# --- grouped view -------------------------------------------------------------------


def test_grouped_view_shape() -> None:
    files = _all_classes_library()
    report = mismatch._classify(files, _MUSIC)
    grouped = mismatch._grouped_report(report, mismatch._folder_stats(files), tier=None, limit=None)

    assert grouped.rows == []
    assert grouped.groups  # non-empty

    ozzy = next(g for g in grouped.groups if g.folder == _OZZY_FOLDER)
    assert ozzy.path_artist == "Ozzy Osbourne"
    assert ozzy.file_count == 2  # two tracked files in the folder
    assert ozzy.flagged == 2  # Jem (HIGH) + clean sibling (LOW folder-consistency)
    assert ozzy.file_ids == [100, 101]
    assert ozzy.tiers == {"high": 1, "low": 1}
    assert ozzy.fields == ["albumartist"]
    assert ozzy.tag_values == {"Jem": 1, "Ozzy Osbourne": 1}
    assert ozzy.suppressed == {}


def test_grouped_view_reports_suppressed_per_folder() -> None:
    files = _all_classes_library()
    dispositions = {100: _disp("legit_ignore", "albumartist", "Jem")}
    report = mismatch._classify(files, _MUSIC, dispositions=dispositions)
    grouped = mismatch._grouped_report(report, mismatch._folder_stats(files), tier=None, limit=None)

    ozzy = next(g for g in grouped.groups if g.folder == _OZZY_FOLDER)
    assert ozzy.flagged == 1  # only the clean-sibling LOW row remains
    assert ozzy.file_ids == [101]
    assert ozzy.suppressed == {"legit_ignore": 1}


def test_grouped_tier_filters_rows_before_grouping() -> None:
    files = _all_classes_library()
    report = mismatch._classify(files, _MUSIC)
    grouped = mismatch._grouped_report(
        report,
        mismatch._folder_stats(files),
        tier="high",
        limit=None,
    )
    assert len(grouped.groups) == 1  # only the Ozzy folder holds a HIGH row
    assert grouped.groups[0].folder == _OZZY_FOLDER
    assert grouped.groups[0].tiers == {"high": 1}


def test_grouped_limit_caps_groups() -> None:
    files = _all_classes_library()
    report = mismatch._classify(files, _MUSIC)
    all_groups = mismatch._grouped_report(
        report,
        mismatch._folder_stats(files),
        tier=None,
        limit=None,
    )
    assert len(all_groups.groups) > 1
    capped = mismatch._grouped_report(report, mismatch._folder_stats(files), tier=None, limit=1)
    assert len(capped.groups) == 1


# --- folder expansion (exact path equality, never a prefix/LIKE) --------------------


def test_folder_expansion_is_exact_equality() -> None:
    files = _all_classes_library()
    report = mismatch._classify(files, _MUSIC)

    expanded = mismatch._expand_folder(report, _OZZY_FOLDER, tier=None, limit=None)
    assert {r.file_id for r in expanded.rows} == {100, 101}
    assert expanded.groups == []

    # A parent prefix must NOT match (equality, not substring/LIKE).
    prefix = str(_MUSIC / "Ozzy Osbourne")
    assert mismatch._expand_folder(report, prefix, tier=None, limit=None).rows == []


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
    with pytest.raises(ValueError, match="invalid status"):
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


# --- end-to-end mismatch-fix flow (criterion 11) ------------------------------------


def _make_fix_flow_library(music_dir: Path) -> dict[str, Path]:
    """Mixed poisoned folder (2 Jem-stamped + 1 clean exemplar) + a container-FP single.

    Ten clean single-artist folders keep the library-wide disagreement rate under the
    reliability floor so the mis-stamped files surface as HIGH.
    """
    for index in range(10):
        make_track(
            music_dir / f"Clean{index}" / "01.mp3",
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
    # Container false positive: a legit remix single credited to another artist.
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

    # Seed prior auto genre/year work + a sticky artist exclusion on the poisoned files, so
    # repend has real derived-axis state to re-open.
    for fid in (jem1, jem2):
        staging.stage_tags(
            engine_settings,
            file_id=fid,
            managed_tags={"genre": ["Metal"], "originaldate": ["2001"]},
            origin="auto",
        )
    staging.commit_tags(engine_settings, origin="auto")
    artists.set_artist_status(engine_settings, file_ids=[jem1, jem2], status="manual")
    assert _derived(engine_settings, jem1) == ("done", "done", "manual")

    # 1. detect flags the two Jem files (HIGH) and the container FP.
    report = detect_mismatches(engine_settings)
    assert {r.file_id for r in report.rows} >= {jem1, jem2, fp_id}
    assert _find(report, jem1).tier == "high"  # type: ignore[union-attr]

    # 2. silence the container FP -> suppressed + reported, not in rows.
    assert (
        mismatch.set_mismatch_status(engine_settings, file_ids=[fp_id], status="legit_ignore") == 1
    )
    silenced = detect_mismatches(engine_settings)
    assert _find(silenced, fp_id) is None
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
    result = staging.commit_tags(engine_settings, root=poisoned)
    assert result.committed == 2
    commit_id = result.commit_id
    assert commit_id is not None

    # 5. repend the derived axes + clear the artist status for the fixed files.
    repend = staging.repend_axes(engine_settings, commit_id=commit_id)
    assert repend.files == 2
    assert repend.artist_status_cleared == 2
    # genre/album flip done -> pending; the artist status row is gone.
    assert _derived(engine_settings, jem1) == ("pending", "pending", "pending")

    # 6. detect no longer flags the poisoned folder (self-resolving accept, no row needed).
    assert detect_mismatches(engine_settings, folder=str(poisoned)).rows == []

    # 7. revert the whole commit -> the pre-fix albumartist is restored.
    versioning.revert_commit(engine_settings, commit_id)
    assert _read_albumartist(engine_settings, poisoned, "01 Gets Me Through.mp3") == ["Jem"]


def _derived(settings: Settings, file_id: int) -> tuple[str, str, str]:
    """Return ``(genre, album, artist)`` derived statuses for *file_id*."""
    conn = connect(settings.db_path)
    try:
        return (
            store.derived_genre_status(conn, file_id),
            store.derived_album_status(conn, file_id),
            store.derived_artist_status(conn, file_id),
        )
    finally:
        conn.close()


# --- MCP + CLI wiring for the new surface --------------------------------------------


def test_mcp_new_mismatch_tools_listed() -> None:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert {
        "stage_tags_batch",
        "repend_axes",
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
    assert all(r["folder"] == str(ozzy) for r in rows)


def test_cli_detect_group_lists_folders(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    _make_mislabeled_library(music_dir)
    assert runner.invoke(app, ["scan", str(music_dir)]).exit_code == 0

    result = runner.invoke(app, ["detect", "--group"])
    assert result.exit_code == 0
    assert "Ozzy Osbourne" in result.stdout
    assert "flagged" in result.stdout


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


def test_mcp_repend_axes_rejects_auto_commit(music_dir: Path) -> None:
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
        managed_tags={"genre": ["Rock"]},
        origin="auto",
    )
    auto_commit = staging.commit_tags(config.load_settings(), origin="auto").commit_id
    assert auto_commit is not None

    payload = mcp_server.repend_axes(auto_commit)
    assert payload["ok"] is False
    assert "auto" in str(payload["error"])

    assert mcp_server.repend_axes(9999)["ok"] is False  # unknown commit id


# --- container-folder path-signal suppression ---------------------------------------

_CONTAINER = frozenset({mismatch.fold("Soundtracks")})


def _container_library() -> list[mismatch._FileInput]:
    """Soundtracks container (uniform composer leaves) + clean padding + a genuine artist.

    Without the container setting the Soundtracks leaves flag MEDIUM (uniform path
    disagreement); the padding keeps the library-wide rate under the reliability floor. The
    genuine ``The Luna Sequence`` folder flags MEDIUM regardless of the setting.
    """
    files = [_mk(i, _MUSIC / f"Clean{i}", "01.mp3", albumartist=f"Clean{i}") for i in range(1, 21)]
    soundtracks = _MUSIC / "Soundtracks"
    files += [
        _mk(100, soundtracks / "Album A", "01.mp3", albumartist="Composer A"),
        _mk(101, soundtracks / "Album A", "02.mp3", albumartist="Composer A"),
        _mk(102, soundtracks / "Album B", "01.mp3", albumartist="Composer B"),
        _mk(103, soundtracks / "Album B", "02.mp3", albumartist="Composer B"),
    ]
    luna = _MUSIC / "The Luna Sequence" / "(2010) Album"
    files += [
        _mk(200, luna, "01.mp3", albumartist="Wrong Artist"),
        _mk(201, luna, "02.mp3", albumartist="Wrong Artist"),
    ]
    return files


def test_container_folder_suppresses_path_signal() -> None:
    files = _container_library()
    soundtracks_ids = (100, 101, 102, 103)

    # Baseline (no setting): the Soundtracks composer leaves flag MEDIUM on the path signal.
    baseline = mismatch._classify(files, _MUSIC)
    assert baseline.path_signal_suppressed is False
    for fid in soundtracks_ids:
        row = _find(baseline, fid)
        assert row is not None
        assert row.tier == "medium"

    # With Soundtracks listed: zero rows for those files, still counted + visibly suppressed.
    report = mismatch._classify(files, _MUSIC, container_folders=_CONTAINER)
    for fid in soundtracks_ids:
        assert _find(report, fid) is None
    assert report.container_suppressed == {"Soundtracks": 4}
    assert report.total_files == baseline.total_files  # suppressed files still count
    assert report.suppressed == {}  # distinct from the disposition map
    assert "container folder" in report.summary

    # A genuine artist folder (The Luna Sequence) is untouched -> still flags MEDIUM.
    for fid in (200, 201):
        row = _find(report, fid)
        assert row is not None
        assert row.tier == "medium"


def test_container_mixed_leaf_still_flags_variant_low() -> None:
    """A mixed-albumartist album folder INSIDE a container still surfaces (documented scope).

    The path signal is suppressed for both files, but the folder-consistency variant-LOW
    fallback keys on the leaf folder's albumartist variance (independent of the path signal),
    so an in-container misfile is still flagged.
    """
    mixed = _MUSIC / "Soundtracks" / "Weird Album"
    files = [
        _mk(1, _MUSIC / "CleanA", "01.mp3", albumartist="CleanA"),
        _mk(300, mixed, "01.mp3", albumartist="Artist One"),
        _mk(301, mixed, "02.mp3", albumartist="Artist Two"),
    ]

    report = mismatch._classify(files, _MUSIC, container_folders=_CONTAINER)

    for fid in (300, 301):
        row = _find(report, fid)
        assert row is not None
        assert row.tier == "low"
    assert report.container_suppressed == {"Soundtracks": 2}


def test_container_reliability_excludes_suppressed_files() -> None:
    files = _container_library()

    baseline = mismatch._classify(files, _MUSIC)
    report = mismatch._classify(files, _MUSIC, container_folders=_CONTAINER)

    # 20 clean + 4 soundtrack + 2 luna = 26 considered, 6 disagreeing -> ~0.231.
    assert baseline.disagreement_rate == pytest.approx(6 / 26)
    # The 4 container files leave the sample -> 22 considered, 2 disagreeing -> ~0.091.
    assert report.disagreement_rate == pytest.approx(2 / 22)


def test_container_and_disposition_suppression_are_distinct() -> None:
    files = _container_library()
    dispositions = {200: _disp("legit_ignore", "albumartist", "Wrong Artist")}

    report = mismatch._classify(
        files,
        _MUSIC,
        dispositions=dispositions,
        container_folders=_CONTAINER,
    )

    assert report.container_suppressed == {"Soundtracks": 4}
    assert report.suppressed == {"legit_ignore": 1}  # the Luna row, disposition-silenced
    assert _find(report, 200) is None
    payload = report.to_dict()
    assert payload["container_suppressed"] == {"Soundtracks": 4}
    assert payload["suppressed"] == {"legit_ignore": 1}


def _make_container_real_library(music_dir: Path) -> None:
    """12 clean single-artist folders + a Soundtracks container of uniform composer albums."""
    for index in range(12):
        make_track(
            music_dir / f"Clean{index}" / "01.mp3",
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

    # Without the setting the Soundtracks composer albums flag on the path signal.
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
    # Exact-folder expansion of a container leaf yields no rows.
    leaf = str(music_dir / "Soundtracks" / "Album A")
    assert detect_mismatches(listed, folder=leaf).rows == []
