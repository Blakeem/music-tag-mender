"""Tests for ``detect_path_deviations`` and the planner holds it shares with ``stage_paths``.

Generated libraries in ``tmp_path`` shaped like the layouts the naming pattern meets: the
owner's dominant layout, multi-disc albums, a container, a ``[Discography]`` wrapper, a root
album, and one scenario per hold reason. Each held file is checked in both reports.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from conftest import make_track
from tagmend import config
from tagmend.engine import mismatch, naming, path_deviations, path_keys, paths, staging, store
from tagmend.engine.db import connect
from tagmend.engine.library import scan_library
from tagmend.engine.schema import apply_schema

if TYPE_CHECKING:
    from collections.abc import Callable

    from tagmend.config import Settings

_DOMINANT = Path("Artist") / "(2001) Artist - Album"


def _tags(**overrides: str) -> dict[str, list[str]]:
    base = {
        "albumartist": "Artist",
        "artist": "Artist",
        "album": "Album",
        "date": "2001",
        "tracknumber": "1",
        "title": "Song",
    }
    merged = {**base, **overrides}
    return {name: [value] for name, value in merged.items() if value}


def _build(
    settings: Settings, music: Path, files: dict[Path, dict[str, list[str]]]
) -> dict[Path, int]:
    for relative, tags in files.items():
        make_track(music / relative, tags)
    scan_library(settings)
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        ids: dict[Path, int] = {}
        for relative in files:
            row = store.get_file(conn, str((music / relative).parent), relative.name)
            assert row is not None
            ids[relative] = row.id
        return ids
    finally:
        conn.close()


def _report(settings: Settings, **kwargs: object) -> path_deviations.DeviationsReport:
    return path_deviations.detect_path_deviations(settings, **kwargs)  # type: ignore[arg-type]


def _reasons(plans: tuple[paths.FilePlan, ...] | list[paths.FilePlan], file_id: int) -> set[str]:
    plan = next(plan for plan in plans if plan.file_id == file_id)
    return {reason for reason, _ in plan.reasons}


# --- layouts ---------------------------------------------------------------------------


def test_the_default_pattern_reproduces_the_dominant_layout(
    engine_settings: Settings, music_dir: Path
) -> None:
    files = {
        _DOMINANT / f"Artist - Album - 0{n} - Song {n}.flac": _tags(
            tracknumber=str(n), title=f"Song {n}"
        )
        for n in (1, 2, 3)
    }
    _build(engine_settings, music_dir, files)
    assert mismatch.gate_state(engine_settings).open

    report = _report(engine_settings)
    staged = paths.stage_paths(engine_settings)

    assert report.counts[paths.STATUS_AT_TARGET] == 3
    assert report.total_files == 3
    assert (report.group_count, report.groups) == (0, ())
    assert report.fit[1].exact == 3
    assert (staged.staged, staged.at_target) == (0, 3)


def test_a_multi_disc_album_renders_flat_with_prefixes_and_a_shared_slot_holds(
    engine_settings: Settings, music_dir: Path
) -> None:
    files = {
        Path("Artist") / "Album" / "CD1" / "01 A.flac": _tags(discnumber="1/2", title="A"),
        Path("Artist") / "Album" / "CD2" / "01 B.flac": _tags(discnumber="2/2", title="B"),
        Path("Artist") / "Single" / "01 C.flac": _tags(album="Single", title="C"),
        Path("Artist") / "Clash" / "01 D.flac": _tags(album="Clash", title="D"),
        Path("Artist") / "Clash" / "01 E.flac": _tags(album="Clash", title="E"),
    }
    ids = _build(engine_settings, music_dir, files)

    rows = _report(engine_settings, group=False, limit=None).rows
    targets = {plan.file_id: plan.to_path for plan in rows}

    album = Path("Artist") / "(2001) Artist - Album"
    assert targets[ids[Path("Artist") / "Album" / "CD1" / "01 A.flac"]] == str(
        album / "Artist - Album - 1-01 - A.flac"
    )
    assert targets[ids[Path("Artist") / "Album" / "CD2" / "01 B.flac"]] == str(
        album / "Artist - Album - 2-01 - B.flac"
    )
    assert targets[ids[Path("Artist") / "Single" / "01 C.flac"]] == str(
        Path("Artist") / "(2001) Artist - Single" / "Artist - Single - 01 - C.flac"
    )
    for name in ("01 D.flac", "01 E.flac"):
        assert paths.TRACK_CONFLICT in _reasons(rows, ids[Path("Artist") / "Clash" / name])


def test_containers_stay_first_and_wrappers_and_root_albums_unwrap(
    engine_settings: Settings, music_dir: Path
) -> None:
    files = {
        Path("Soundtracks") / "Film" / "01 Theme.flac": _tags(
            albumartist="Composer", artist="Composer", album="Film", title="Theme"
        ),
        Path("Artist [Discography]") / "Album" / "01 Song.flac": _tags(),
        Path("Other - Record") / "01 Tune.flac": _tags(
            albumartist="Other", artist="Other", album="Record", title="Tune"
        ),
    }
    ids = _build(engine_settings, music_dir, files)
    settings = dataclasses.replace(engine_settings, container_folders=("Soundtracks",))

    rows = _report(settings, group=False, limit=None).rows
    targets = {plan.file_id: plan.to_path for plan in rows}

    assert targets[ids[Path("Soundtracks") / "Film" / "01 Theme.flac"]] == str(
        Path("Soundtracks") / "(2001) Composer - Film" / "Composer - Film - 01 - Theme.flac"
    )
    assert targets[ids[Path("Artist [Discography]") / "Album" / "01 Song.flac"]] == str(
        _DOMINANT / "Artist - Album - 01 - Song.flac"
    )
    assert targets[ids[Path("Other - Record") / "01 Tune.flac"]] == str(
        Path("Other") / "(2001) Other - Record" / "Other - Record - 01 - Tune.flac"
    )


def test_a_kept_folder_renders_only_the_filename(
    engine_settings: Settings, music_dir: Path
) -> None:
    relative = Path("Artist") / "Odd Folder" / "01 Song.flac"
    ids = _build(engine_settings, music_dir, {relative: _tags()})
    mismatch.set_mismatch_status(
        engine_settings,
        status=mismatch.LEGIT_IGNORE,
        covers=[mismatch.RELEASE_FOLDER_ALBUM],
        file_ids=[ids[relative]],
    )

    plan = _report(engine_settings, group=False).rows[0]

    assert plan.to_path == str(relative.parent / "Artist - Album - 01 - Song.flac")
    assert plan.kind == paths.KIND_RENAME


def test_a_keep_lifts_the_blank_year_and_disc_holds(
    engine_settings: Settings, music_dir: Path
) -> None:
    relative = Path("Artist") / "(2001) Odd" / "CD1" / "01 Song.flac"
    ids = _build(engine_settings, music_dir, {relative: _tags(date="")})
    plan = _report(engine_settings, group=False).rows[0]
    assert {paths.MISSING_YEAR, paths.MISSING_DISC} <= _reasons([plan], ids[relative])
    mismatch.set_mismatch_status(
        engine_settings,
        status=mismatch.LEGIT_IGNORE,
        covers=[mismatch.RELEASE_FOLDER_ALBUM],
        file_ids=[ids[relative]],
    )

    kept = _report(engine_settings, group=False).rows[0]

    assert (kept.status, kept.reasons) == (paths.STATUS_WILL_MOVE, ())
    assert kept.to_path == str(relative.parent / "Artist - Album - 01 - Song.flac")


# --- holds ---------------------------------------------------------------------------

_LOOSE = Path("Artist") / "Album"
_X = _LOOSE / "01 Song.flac"
_X_TARGET = _DOMINANT / "Artist - Album - 01 - Song.flac"


def _missing_title(settings: Settings, music: Path) -> int:
    return _build(settings, music, {_X: _tags(title="")})[_X]


def _missing_year(settings: Settings, music: Path) -> int:
    relative = Path("Artist") / "(2001) Album" / "01 Song.flac"
    return _build(settings, music, {relative: _tags(date="")})[relative]


def _missing_disc(settings: Settings, music: Path) -> int:
    relative = _LOOSE / "CD1" / "01 Song.flac"
    return _build(settings, music, {relative: _tags()})[relative]


def _album_split(settings: Settings, music: Path) -> int:
    other = _LOOSE / "02 Other.flac"
    ids = _build(settings, music, {_X: _tags(), other: _tags(album="Other", tracknumber="2")})
    return ids[_X]


def _unit_member(settings: Settings, music: Path) -> int:
    blank = _LOOSE / "02.flac"
    return _build(settings, music, {_X: _tags(), blank: _tags(tracknumber="2", title="")})[_X]


def _track_conflict(settings: Settings, music: Path) -> int:
    other = _LOOSE / "01 Other.flac"
    return _build(settings, music, {_X: _tags(), other: _tags(title="Other")})[_X]


def _merge(settings: Settings, music: Path) -> int:
    other = Path("Artist") / "Album (copy)" / "02 Two.flac"
    return _build(settings, music, {_X: _tags(), other: _tags(tracknumber="2", title="Two")})[_X]


def _duplicate_render(settings: Settings, music: Path) -> int:
    copy = Path("Artist") / "Copy" / "01 Song.flac"
    return _build(settings, music, {_X: _tags(), copy: _tags()})[_X]


def _occupied(settings: Settings, music: Path) -> int:
    file_id = _build(settings, music, {_X: _tags()})[_X]
    (music / _X_TARGET).parent.mkdir(parents=True)
    (music / _X_TARGET).write_bytes(b"untracked")
    return file_id


def _shared_target(settings: Settings, music: Path) -> int:
    other = Path("Other") / "Thing" / "01 Thing.flac"
    ids = _build(
        settings,
        music,
        {
            _X: _tags(),
            other: _tags(albumartist="Other", artist="Other", album="Thing", title="Thing"),
        },
    )
    paths.stage_paths_batch(settings, entries=[(ids[other], str(_X_TARGET))])
    return ids[_X]


def _too_long(settings: Settings, music: Path) -> int:
    return _build(settings, music, {_X: _tags(title="L" * 240)})[_X]


def _too_long_folder(settings: Settings, music: Path) -> int:
    long_name = "A" * 300
    return _build(settings, music, {_X: _tags(albumartist=long_name, artist=long_name)})[_X]


def _cue_reference(settings: Settings, music: Path) -> int:
    file_id = _build(settings, music, {_X: _tags()})[_X]
    (music / _LOOSE / "album.cue").write_text('FILE "01 Song.flac" WAVE\n', encoding="utf-8")
    return file_id


def _staged_tag(settings: Settings, music: Path) -> int:
    file_id = _build(settings, music, {_X: _tags()})[_X]
    staging.stage_tags(settings, file_id=file_id, tags={"genre": ["Rock"]})
    return file_id


def _landed_move(settings: Settings, music: Path) -> int:
    file_id = _build(settings, music, {_X: _tags()})[_X]
    assert paths.stage_paths(settings).staged == 1
    (music / _X_TARGET).parent.mkdir(parents=True)
    (music / _X).rename(music / _X_TARGET)
    return file_id


def _missing(settings: Settings, music: Path) -> int:
    file_id = _build(settings, music, {_X: _tags()})[_X]
    (music / _X).rename(music / _X.with_suffix(".bak"))
    return file_id


_HOLDS: dict[str, Callable[[Settings, Path], int]] = {
    "missing_title": _missing_title,
    paths.MISSING_YEAR: _missing_year,
    paths.MISSING_DISC: _missing_disc,
    paths.ALBUM_SPLIT: _album_split,
    paths.UNIT_MEMBER: _unit_member,
    paths.TRACK_CONFLICT: _track_conflict,
    paths.MERGE: _merge,
    paths.DUPLICATE_RENDER: _duplicate_render,
    paths.OCCUPIED: _occupied,
    paths.SHARED_TARGET: _shared_target,
    paths.TOO_LONG: _too_long,
    paths.CUE_REFERENCE: _cue_reference,
    paths.STAGED_TAG: _staged_tag,
    paths.LANDED_MOVE: _landed_move,
    paths.MISSING: _missing,
}


def test_a_folder_name_too_long_for_the_volume_holds_instead_of_failing(
    engine_settings: Settings, music_dir: Path
) -> None:
    file_id = _too_long_folder(engine_settings, music_dir)

    report = _report(engine_settings, group=False)
    dry_run = paths.stage_paths(engine_settings, dry_run=True)

    assert paths.TOO_LONG in _reasons(report.rows, file_id)
    assert dry_run.held == {paths.TOO_LONG: 1}


@pytest.mark.parametrize("hold", sorted(_HOLDS))
def test_every_hold_is_reported_by_both_tools(
    engine_settings: Settings, music_dir: Path, hold: str
) -> None:
    file_id = _HOLDS[hold](engine_settings, music_dir)

    report = _report(engine_settings, group=False, limit=None)
    dry_run = paths.stage_paths(engine_settings, dry_run=True)

    assert hold in _reasons(report.rows, file_id)
    assert report.held[hold] >= 1
    assert hold in _reasons(list(dry_run.held_files), file_id)
    assert dry_run.held[hold] >= 1


def test_a_disjoint_disc_set_merges_without_a_hold(
    engine_settings: Settings, music_dir: Path
) -> None:
    second = Path("Artist") / "Album CD2" / "01 Two.flac"
    _build(
        engine_settings,
        music_dir,
        {_X: _tags(discnumber="1/2"), second: _tags(discnumber="2/2", title="Two")},
    )

    report = _report(engine_settings)

    assert report.counts[paths.STATUS_WILL_MOVE] == 2
    assert report.held == {}


def test_a_staged_tag_and_a_held_member_hold_the_entire_folder(
    engine_settings: Settings, music_dir: Path
) -> None:
    sibling = _LOOSE / "02 Two.flac"
    ids = _build(
        engine_settings, music_dir, {_X: _tags(), sibling: _tags(tracknumber="2", title="Two")}
    )
    staging.stage_tags(engine_settings, file_id=ids[_X], tags={"genre": ["Rock"]})

    rows = _report(engine_settings, group=False, limit=None).rows

    assert _reasons(rows, ids[sibling]) == {paths.UNIT_MEMBER}


# --- the header and the views ---------------------------------------------------------


def test_a_preview_renders_a_candidate_without_saving_it(
    engine_settings: Settings, music_dir: Path
) -> None:
    _build(engine_settings, music_dir, {_X: _tags()})
    candidate = "{albumartist}/{album}/{tracknumber:02} {title}"

    preview = _report(engine_settings, pattern=candidate, container_folders=["Box"])

    assert (preview.pattern, preview.persisted) == (candidate, False)
    assert preview.container_folders == ("Box",)
    assert (preview.counts[paths.STATUS_AT_TARGET], preview.groups) == (1, ())
    assert config.load_settings().naming_pattern == ""
    assert config.load_settings().container_folders == ()
    persisted = _report(engine_settings)
    assert persisted.persisted
    assert persisted.groups[0].example == {"from": str(_X), "to": str(_X_TARGET)}


def test_the_header_reads_the_library(engine_settings: Settings, music_dir: Path) -> None:
    files = {
        _DOMINANT / "Artist - Album - 01 - Song.flac": _tags(),
        Path("Soundtracks") / "Film" / "01 Theme.flac": _tags(
            albumartist="Composer", artist="Composer", album="Film", title="Theme"
        ),
    }
    _build(engine_settings, music_dir, files)

    report = _report(engine_settings)

    assert report.default_pattern == report.pattern
    assert report.shapes[path_deviations.LEVEL_FILENAME][0]["files"] == 1
    shapes = {row["shape"] for row in report.shapes[path_deviations.LEVEL_FILENAME]}
    assert "{albumartist} - {album} - {tracknumber} - {title}" in shapes
    assert "{tracknumber} {title}" in shapes
    leaf = {row["shape"] for row in report.shapes[path_deviations.LEVEL_LEAF]}
    assert "({year}) {albumartist} - {album}" in leaf
    candidates = [c.folder for c in report.container_candidates]
    assert candidates == ["Soundtracks"]
    assert report.container_candidates[0].top_album_artist == "Composer"
    assert report.gate.open is False
    assert report.volume_refusal is None
    assert [level.component for level in report.fit] == list(
        naming.parse_pattern(naming.DEFAULT_PATTERN).component_texts
    )
    assert [(level.exact, level.case, level.differs) for level in report.fit] == [(1, 0, 1)] * 3
    assert report.groups[0].folder == str(Path("Soundtracks") / "Film")
    assert report.groups[0].kind == paths.KIND_MOVE


def test_folder_expands_one_folder_to_every_file(
    engine_settings: Settings, music_dir: Path
) -> None:
    files = {
        _DOMINANT / "Artist - Album - 01 - Song.flac": _tags(),
        _LOOSE / "02 Two.flac": _tags(album="Other", tracknumber="2", title="Two"),
    }
    _build(engine_settings, music_dir, files)

    report = _report(engine_settings, folder=str(_DOMINANT))

    assert [row.status for row in report.rows] == [paths.STATUS_AT_TARGET]
    assert report.groups == ()
    assert report.total_files == 2


def test_a_folder_case_difference_is_case_only_and_never_staged(
    engine_settings: Settings, music_dir: Path
) -> None:
    _build(engine_settings, music_dir, {Path("artist") / _DOMINANT.name / _X_TARGET.name: _tags()})

    report = _report(engine_settings)
    staged = paths.stage_paths(engine_settings)

    if path_keys.path_key("A") == path_keys.path_key("a"):
        assert report.counts[paths.STATUS_CASE_ONLY] == 1
        assert report.groups[0].kind == paths.KIND_CASE
        assert report.fit[0].case == 1
        assert (staged.staged, staged.case_only) == (0, 1)
    else:
        assert report.counts[paths.STATUS_WILL_MOVE] == 1


def test_detect_path_deviations_refuses_bad_input(
    engine_settings: Settings, music_dir: Path
) -> None:
    with pytest.raises(ValueError, match="unknown field name"):
        _report(engine_settings, pattern="{nope}")
    with pytest.raises(ValueError, match="invalid container_folders"):
        _report(engine_settings, container_folders=["a;b"])
    with pytest.raises(ValueError, match="must be >= 0"):
        _report(engine_settings, limit=-1)
    with pytest.raises(ValueError, match="music_path not configured"):
        _report(dataclasses.replace(engine_settings, music_path=None))


# --- the render round trip --------------------------------------------------------------

_TRICKY = {
    Path("Sixx_A.M") / "loose" / "1.flac": _tags(
        albumartist="Sixx:A.M.", artist="Sixx:A.M.", album="What? Now: Part/2", title="M.I.A."
    ),
    Path("Björk") / "x" / "2.flac": _tags(
        albumartist="Björk", artist="Björk", album="Homogenic...", title="Jóga?", tracknumber="2"
    ),
    Path("Ünïcode") / "y" / "3.flac": _tags(
        albumartist="Ünïcode", artist="Ünïcode", album='Say "Yes"', title="Trail. ", tracknumber="3"
    ),
    Path("Multi") / "d1" / "4.flac": _tags(
        albumartist="36 Crazyfists",
        artist="36 Crazyfists",
        album="Disc/Set",
        discnumber="1/2",
        title="One: Two",
        tracknumber="4",
    ),
    Path("Multi") / "d2" / "5.flac": _tags(
        albumartist="36 Crazyfists",
        artist="36 Crazyfists",
        album="Disc/Set",
        discnumber="2/2",
        title="Three?",
        tracknumber="5",
    ),
}


def test_a_rendered_path_never_flags_in_the_comparator(
    engine_settings: Settings, music_dir: Path
) -> None:
    _build(engine_settings, music_dir, _TRICKY)
    plans = _report(engine_settings, group=False, limit=None).rows
    assert all(plan.status == paths.STATUS_WILL_MOVE for plan in plans), plans
    # Move each file to its render outside the ledger, then read it as a fresh library.
    for plan in plans:
        assert plan.to_path is not None
        target = music_dir / plan.to_path
        target.parent.mkdir(parents=True, exist_ok=True)
        (music_dir / plan.from_path).rename(target)
    fresh = dataclasses.replace(engine_settings, db_path=engine_settings.db_path.with_name("rt.db"))
    scan_library(fresh)

    report = mismatch.detect_mismatches(fresh)

    assert report.flagged == 0, [row.to_dict() for row in report.rows]
    assert _report(fresh).counts[paths.STATUS_AT_TARGET] == len(_TRICKY)
