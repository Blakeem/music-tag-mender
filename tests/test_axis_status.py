"""End-to-end tests for the outcome-row axis status model (:mod:`tagmend.engine.axis`).

Every scenario runs the real engine on generated audio files with a real temp ledger: the
resolvers with injected fake lookup clients, the staging and commit engine, the revert tools
and the scanner. Each asserts the status the shared classifier
(:func:`tagmend.engine.store.derived_status`) derives afterwards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import mutagen
import pytest

from conftest import make_track
from tagmend.engine import (
    artists,
    axis,
    axis_status,
    genres,
    library,
    staging,
    store,
    versioning,
    years,
)
from tagmend.engine.db import connect
from tagmend.engine.lastfm import ArtistCorrection, LastfmError, Tag
from tagmend.engine.musicbrainz import MBReleaseGroup
from tagmend.engine.schema import apply_schema
from tagmend.engine.tags import write_managed_tags
from test_artists import FakeCorrectionSource
from test_genres import FakeTagSource
from test_years import FakeMBReleaseGroupSource

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

# Last.fm top tags whose classified genres are exactly _GENRES.
_TAGS = [Tag("electronic", 100), Tag("house", 63)]
_GENRES = ["electronic", "house"]


def _release(title: str, date: str) -> MBReleaseGroup:
    return MBReleaseGroup(
        album_title=title,
        original_date=date,
        release_group_mbid=f"rg-{title}",
    )


def _ids(settings: Settings) -> dict[str, int]:
    """Return ``{filename: file_id}`` for every tracked file."""
    return {view.filename: view.file_id for view in library.list_files(settings)}


def _status(settings: Settings, file_id: int, tag_axis: axis.Axis = axis.GENRE_AXIS) -> str:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return store.derived_status(conn, tag_axis, file_id)
    finally:
        conn.close()


def _outcome(
    settings: Settings,
    file_id: int,
    tag_axis: axis.Axis = axis.GENRE_AXIS,
) -> axis.OutcomeRow | None:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return axis.get_outcome(conn, tag_axis, file_id)
    finally:
        conn.close()


def _edit_on_disk(path: Path, **tags: list[str]) -> None:
    """Change tags the way an external tagger would."""
    audio = mutagen.File(path, easy=True)  # type: ignore[attr-defined]
    for name, values in tags.items():
        audio[name] = values
    audio.save()


def _resolve_genres(settings: Settings, *, limit: int | None = None) -> genres.ResolveGenresResult:
    source = FakeTagSource({"Daft Punk": _TAGS, "Obscure Band": None})
    return genres.resolve_genres(settings, client=source, limit=limit)


# --- the outcome stage ---------------------------------------------------------------


def test_equal_value_resolve_records_done(engine_settings: Settings, music_dir: Path) -> None:
    make_track(music_dir / "t.flac", {"artist": ["Daft Punk"], "genre": _GENRES})
    library.scan_library(engine_settings)

    result = _resolve_genres(engine_settings)

    assert (result.settled, result.staged_files) == (1, 0)
    assert staging.diff_tags(engine_settings) == []
    assert _status(engine_settings, _ids(engine_settings)["t.flac"]) == "done"


def test_differing_value_reads_staged_then_done(engine_settings: Settings, music_dir: Path) -> None:
    make_track(music_dir / "t.flac", {"artist": ["Daft Punk"], "genre": ["Old"]})
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]

    result = _resolve_genres(engine_settings)
    assert (result.settled, result.staged_files) == (1, 1)
    assert _status(engine_settings, file_id) == "staged"

    staging.commit_tags(engine_settings)
    assert _status(engine_settings, file_id) == "done"


def test_lookup_with_nothing_records_no_match(engine_settings: Settings, music_dir: Path) -> None:
    make_track(music_dir / "t.flac", {"artist": ["Obscure Band"], "genre": ["Old"]})
    library.scan_library(engine_settings)

    result = _resolve_genres(engine_settings)

    assert result.no_match == 1
    assert _status(engine_settings, _ids(engine_settings)["t.flac"]) == "no_match"


def test_transient_error_writes_nothing(engine_settings: Settings, music_dir: Path) -> None:
    make_track(music_dir / "t.flac", {"artist": ["Daft Punk"], "genre": ["Old"]})
    library.scan_library(engine_settings)

    class _Down(FakeTagSource):
        def artist_top_tags(self, name: str) -> None:
            del name
            message = "transport error"
            raise LastfmError(message)

    result = genres.resolve_genres(engine_settings, client=_Down({}))

    assert (result.settled, result.errors, result.more) == (0, 1, False)
    assert _outcome(engine_settings, _ids(engine_settings)["t.flac"]) is None


# --- the human writers ---------------------------------------------------------------


def test_human_commit_on_an_axis_field_records_manual(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t.flac", {"artist": ["Daft Punk"], "genre": ["Old"]})
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]

    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Hand Picked"]})
    staging.commit_tags(engine_settings)

    assert _status(engine_settings, file_id) == "manual"
    # A human genre edit says nothing about the other axes.
    assert _status(engine_settings, file_id, axis.ARTIST_AXIS) == "pending"


def test_reapplied_human_commit_still_records_manual(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.flac", {"artist": ["Daft Punk"], "genre": ["Old"]})
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]
    staging.stage_tags(engine_settings, file_id=file_id, tags={"genre": ["Hand Picked"]})
    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        staged = store.get_staged_tag(conn, file_id)
    finally:
        conn.close()
    assert staged is not None

    # A first attempt that wrote the file and then rolled back its transaction.
    write_managed_tags(track, staged.managed_tags)
    staging.commit_tags(engine_settings)

    assert _status(engine_settings, file_id) == "manual"
    assert _status(engine_settings, file_id, axis.ARTIST_AXIS) == "pending"


@pytest.mark.parametrize("tag_axis", axis.TAG_AXES, ids=lambda a: a.name)
def test_set_status_manual_records_manual(
    engine_settings: Settings,
    music_dir: Path,
    tag_axis: axis.Axis,
) -> None:
    make_track(music_dir / "t.flac", {"artist": ["Daft Punk"], "album": ["Discovery"]})
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]

    affected = axis_status.set_manual_status(
        engine_settings,
        tag_axis,
        file_ids=[file_id],
        value=None,
        status="manual",
    )

    assert affected == 1
    assert _status(engine_settings, file_id, tag_axis) == "manual"


def test_external_edit_over_a_human_value_stays_manual(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.flac", {"artist": ["Daft Punk"], "genre": ["Old"]})
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]
    axis_status.set_manual_status(
        engine_settings, axis.GENRE_AXIS, file_ids=[file_id], value=None, status="manual"
    )

    _edit_on_disk(track, genre=["Overwritten Elsewhere"], artist=["Renamed"])
    library.scan_library(engine_settings)

    assert _status(engine_settings, file_id) == "manual"


# --- re-opening with no writer -------------------------------------------------------


def test_revert_of_a_resolver_commit_reopens_and_its_revert_settles_again(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t.flac", {"artist": ["Daft Punk"], "genre": ["Old"]})
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]
    _resolve_genres(engine_settings)
    resolved = staging.commit_tags(engine_settings)
    assert resolved.commit_id is not None

    undo = versioning.revert_commit(engine_settings, resolved.commit_id)
    assert undo.commit_id is not None
    assert _status(engine_settings, file_id) == "pending"

    versioning.revert_commit(engine_settings, undo.commit_id)
    assert _status(engine_settings, file_id) == "done"


def test_album_identity_fix_reopens(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "t.flac",
        {"artist": ["Daft Punk"], "album": ["Discovry"], "genre": _GENRES},
    )
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]
    _resolve_genres(engine_settings)
    assert _status(engine_settings, file_id) == "done"

    staging.stage_tags(engine_settings, file_id=file_id, tags={"album": ["Discovery"]})
    staging.commit_tags(engine_settings)

    assert _status(engine_settings, file_id) == "pending"


def test_rescan_after_an_external_edit_of_the_field_reopens(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.flac", {"artist": ["Daft Punk"], "genre": _GENRES})
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]
    _resolve_genres(engine_settings)
    assert _status(engine_settings, file_id) == "done"

    _edit_on_disk(track, genre=["Edited In Picard"])
    library.scan_library(engine_settings)

    assert _status(engine_settings, file_id) == "pending"


def test_genre_stage_over_an_album_edited_on_disk_since_the_scan_reopens(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(
        music_dir / "t.flac",
        {"artist": ["Daft Punk"], "album": ["Discovery"], "genre": ["Old"]},
    )
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]
    _edit_on_disk(track, album=["Homework"])

    assert _resolve_genres(engine_settings).staged_files == 1
    staging.commit_tags(engine_settings)
    library.scan_library(engine_settings)

    assert _status(engine_settings, file_id) == "pending"


def test_year_fill_over_an_album_edited_on_disk_since_the_scan_reopens(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.flac", {"artist": ["Black Sabbath"], "album": ["Paranoid"]})
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]
    _edit_on_disk(track, album=["Vol. 4"])
    source = FakeMBReleaseGroupSource(
        {("Black Sabbath", "Paranoid"): _release("Paranoid", "1970")},
    )

    assert years.resolve_years(engine_settings, client=source).staged_files == 1
    staging.commit_tags(engine_settings)
    library.scan_library(engine_settings)

    assert _status(engine_settings, file_id, axis.YEAR_AXIS) == "pending"


def test_artist_stage_over_an_albumartist_added_on_disk_since_the_scan_reopens(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.flac", {"artist": ["Miami Nights '84"]})
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]
    _edit_on_disk(track, albumartist=["Someone Else"])
    source = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-mn")},
    )

    assert artists.resolve_artists(engine_settings, client=source).staged_files == 1
    staging.commit_tags(engine_settings)
    library.scan_library(engine_settings)

    assert _status(engine_settings, file_id, axis.ARTIST_AXIS) == "pending"


def test_unstage_after_a_resolver_stage_reopens(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t.flac", {"artist": ["Daft Punk"], "genre": ["Old"]})
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]
    _resolve_genres(engine_settings)

    assert staging.unstage_tags(engine_settings, file_id=file_id) == 1

    assert _status(engine_settings, file_id) == "pending"


# --- the commit writer's corrections -------------------------------------------------


def test_auto_artist_correction_reopens_a_genre_no_match_under_the_old_name(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t.flac", {"artist": ["Obscure Band"], "genre": ["Old"]})
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]
    _resolve_genres(engine_settings)
    assert _status(engine_settings, file_id) == "no_match"

    fix = FakeCorrectionSource({"Obscure Band": ArtistCorrection("Famous Band", "mbid-fb")})
    artists.resolve_artists(engine_settings, client=fix)
    staging.commit_tags(engine_settings)

    # The genre row snapshots the old name, and an auto commit never re-stamps it across
    # an identity change. The artist row is the one the commit re-stamps.
    assert _status(engine_settings, file_id) == "pending"
    assert _status(engine_settings, file_id, axis.ARTIST_AXIS) == "done"


def test_human_album_commit_after_an_external_genre_edit_never_reads_manual(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(
        music_dir / "t.flac",
        {"artist": ["Daft Punk"], "album": ["Discovery"], "genre": ["Old"]},
    )
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]
    staging.stage_tags(engine_settings, file_id=file_id, tags={"title": ["One More Time"]})
    staging.commit_tags(engine_settings)

    _edit_on_disk(track, genre=["Edited Elsewhere"])
    staging.stage_tags(engine_settings, file_id=file_id, tags={"album": ["Discovery (Live)"]})
    result = staging.commit_tags(engine_settings)

    # Staging records the external genre edit in its own scan revision, so the commit revision
    # holds only the album, and the commit writer never reads the genre as a human change.
    assert result.commit_id is not None
    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        (revision,) = store.revisions_for_commit(conn, result.commit_id)
    finally:
        conn.close()
    assert set(revision.diff) == {"album"}
    assert _status(engine_settings, file_id) == "pending"


def test_year_run_settles_present_values_without_a_lookup(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "a.flac",
        {"artist": ["Black Sabbath"], "album": ["Paranoid"], "originaldate": ["1970"]},
    )
    make_track(music_dir / "b.flac", {"artist": ["Black Sabbath"], "album": ["Vol. 4"]})
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    source = FakeMBReleaseGroupSource(
        {
            ("Black Sabbath", "Paranoid"): _release("Paranoid", "1971"),
            ("Black Sabbath", "Vol. 4"): _release("Vol. 4", "1972"),
        },
    )

    result = years.resolve_years(engine_settings, client=source)

    assert source.lookups == [("Black Sabbath", "Vol. 4")]
    assert [view.file_id for view in staging.diff_tags(engine_settings)] == [ids["b.flac"]]
    assert (result.settled, result.staged_files) == (2, 1)
    assert _status(engine_settings, ids["a.flac"], axis.YEAR_AXIS) == "done"


# --- termination ---------------------------------------------------------------------


def test_capped_genre_runs_over_settled_values_terminate(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    for index in range(5):
        make_track(music_dir / f"t{index}.flac", {"artist": ["Daft Punk"], "genre": _GENRES})
    library.scan_library(engine_settings)

    calls = 0
    more = True
    while more:
        calls += 1
        assert calls <= 3
        more = _resolve_genres(engine_settings, limit=2).more

    statuses = {_status(engine_settings, file_id) for file_id in _ids(engine_settings).values()}
    assert statuses == {"done"}


def test_capped_year_runs_over_present_values_terminate(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    for index in range(5):
        make_track(
            music_dir / f"t{index}.flac",
            {"artist": ["Band"], "album": ["LP"], "originaldate": ["1999"]},
        )
    library.scan_library(engine_settings)
    source = FakeMBReleaseGroupSource({})

    calls = 0
    more = True
    while more:
        calls += 1
        assert calls <= 3
        more = years.resolve_years(engine_settings, client=source, limit=2).more

    assert source.lookups == []
    statuses = {
        _status(engine_settings, file_id, axis.YEAR_AXIS)
        for file_id in _ids(engine_settings).values()
    }
    assert statuses == {"done"}


# --- the artist file rule ------------------------------------------------------------


def test_held_value_records_no_match_while_the_other_field_is_staged(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "t.flac",
        {"artist": ["Travis Scott"], "albumartist": ["Miami Nights '84"]},
    )
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)["t.flac"]
    source = FakeCorrectionSource(
        {
            "Travis Scott": ArtistCorrection("Travi$ Scott", None),  # held: needs review
            "Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-mn"),
        },
    )

    result = artists.resolve_artists(engine_settings, client=source)

    assert (result.settled, result.staged_files, result.needs_review) == (1, 1, 1)
    assert _status(engine_settings, file_id, axis.ARTIST_AXIS) == "staged"
    staging.commit_tags(engine_settings)
    assert _status(engine_settings, file_id, axis.ARTIST_AXIS) == "no_match"


def test_unselected_carrier_is_staged_but_gets_no_row(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.flac", {"artist": ["Miami Nights '84"]})
    make_track(music_dir / "b.flac", {"artist": ["Miami Nights '84"]})
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    source = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-mn")},
    )

    result = artists.resolve_artists(engine_settings, client=source, limit=1)

    assert (result.settled, result.staged_files) == (1, 2)
    assert _outcome(engine_settings, ids["a.flac"], axis.ARTIST_AXIS) is not None
    assert _outcome(engine_settings, ids["b.flac"], axis.ARTIST_AXIS) is None


def test_multi_value_file_records_no_match_and_stages_nothing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t.flac", {"artist": ["Miami Nights '84", "Daft Punk"]})
    library.scan_library(engine_settings)
    source = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-mn")},
    )

    result = artists.resolve_artists(engine_settings, client=source)

    assert (result.settled, result.staged_files, result.skipped_multi_artist) == (1, 0, 1)
    file_id = _ids(engine_settings)["t.flac"]
    assert _status(engine_settings, file_id, axis.ARTIST_AXIS) == "no_match"


# --- the present-file domain ---------------------------------------------------------


def test_missing_file_has_no_axis_status(engine_settings: Settings, music_dir: Path) -> None:
    make_track(music_dir / "kept.flac", {"artist": ["Daft Punk"], "genre": _GENRES})
    gone = make_track(music_dir / "gone.flac", {"artist": ["Daft Punk"], "genre": _GENRES})
    make_track(music_dir / "orphan.flac", {"genre": ["Old"]})
    library.scan_library(engine_settings)
    _resolve_genres(engine_settings)
    gone.unlink()
    library.scan_library(engine_settings)

    stats = library.get_library_stats(engine_settings)

    assert stats["present"] == 2
    for tag_axis in axis.TAG_AXES:
        gauge = stats[tag_axis.name]
        assert isinstance(gauge, dict)
        assert sum(gauge.values()) == stats["present"]
    assert [v.filename for v in library.list_files(engine_settings, genre_status="done")] == [
        "kept.flac",
    ]
    for status in axis.GENRE_AXIS.workflow_statuses:
        listed = [
            *library.list_files(engine_settings, genre_status=status),
            *library.list_files(engine_settings, artist_status=status),
            *library.list_files(engine_settings, year_status=status),
        ]
        assert "gone.flac" not in {view.filename for view in listed}
