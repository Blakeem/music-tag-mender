"""Integration tests for scan orchestration (:mod:`tagmend.engine.library`).

These use real temp audio files (the silent templates) and a real temp SQLite ledger
via the ``engine_settings`` fixture, so they exercise the full scan/reconcile path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from conftest import make_track
from tagmend.config import Settings
from tagmend.engine import artists, mismatch, store
from tagmend.engine.db import connect
from tagmend.engine.library import (
    ScanMode,
    get_file_view,
    get_library_stats,
    list_files,
    scan_library,
)
from tagmend.engine.schema import apply_schema
from tagmend.engine.tags import TAG_READER_VERSION

if TYPE_CHECKING:
    from pathlib import Path

_N = 3


def _populate(music_dir: Path, count: int) -> list[Path]:
    """Create *count* real tagged tracks (track00.mp3 ...) under *music_dir*."""
    tracks: list[Path] = []
    for index in range(count):
        track = make_track(
            music_dir / f"track{index:02d}.mp3",
            {"artist": [f"Artist {index}"], "genre": ["Synthwave"]},
        )
        tracks.append(track)
    return tracks


def _stored_genre(settings: Settings, folder: Path, filename: str) -> list[str]:
    """Read back the stored ``genre`` values for one tracked file."""
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        return store.get_tags(conn, row.id).get("genre", [])
    finally:
        conn.close()


def _make_snapshot_stale(settings: Settings, folder: Path, filename: str, genre: str) -> None:
    """Rewrite one file's stored genre to *genre* and mark the row as an older reader's.

    Reproduces the live-run failure: the snapshot disagrees with the file while its
    size/mtime signature is untouched, so nothing but the reader stamp can bring an
    incremental scan back to it.
    """
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        conn.execute(
            "UPDATE file_tags SET value = ? WHERE file_id = ? AND name = 'genre'",
            (genre, row.id),
        )
        conn.execute("UPDATE files SET reader_version = 0 WHERE id = ?", (row.id,))
        conn.commit()
    finally:
        conn.close()


def _mark_snapshot_wrong(settings: Settings, folder: Path, filename: str, genre: str) -> None:
    """Rewrite one file's stored genre but leave its reader stamp current."""
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        conn.execute(
            "UPDATE file_tags SET value = ? WHERE file_id = ? AND name = 'genre'",
            (genre, row.id),
        )
        conn.commit()
    finally:
        conn.close()


def _file_row(settings: Settings, folder: Path, filename: str) -> store.FileRow:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        return row
    finally:
        conn.close()


def _set_no_match(
    settings: Settings,
    file_id: int,
    *,
    source_artist: str | None,
    source_album: str | None = None,
) -> None:
    """Persist a terminal ``no_match`` genre decision for *file_id*."""
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        store.set_genre_status(
            conn,
            file_id=file_id,
            status="no_match",
            source_artist=source_artist,
            source_album=source_album,
            now="2026-06-09T00:00:00+00:00",
        )
        conn.commit()
    finally:
        conn.close()


def test_list_files_returns_views_with_managed_tags(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _populate(music_dir, _N)
    scan_library(engine_settings)

    views = list_files(engine_settings)

    assert len(views) == _N
    first = views[0]
    assert first.filename == "track00.mp3"
    assert first.is_missing is False
    # Only managed tags surface (artist/genre managed; title would be dropped).
    assert first.managed_tags["genre"] == ["Synthwave"]
    assert first.managed_tags["artist"] == ["Artist 0"]


def test_list_files_respects_limit_and_root(
    engine_settings: Settings,
    music_dir: Path,
    tmp_path: Path,
) -> None:
    _populate(music_dir, _N)
    other = tmp_path / "elsewhere"
    make_track(other / "x.mp3", {"genre": ["Jazz"]})
    scan_library(engine_settings, path=music_dir)
    scan_library(engine_settings, path=other)

    assert len(list_files(engine_settings, limit=2)) == 2
    under_music = list_files(engine_settings, root=music_dir)
    assert {v.filename for v in under_music} == {"track00.mp3", "track01.mp3", "track02.mp3"}


def test_get_file_view(engine_settings: Settings, music_dir: Path) -> None:
    track = _populate(music_dir, 1)[0]
    scan_library(engine_settings)
    file_id = _file_row(engine_settings, music_dir, track.name).id

    view = get_file_view(engine_settings, file_id)
    assert view is not None
    assert view.file_id == file_id
    assert view.managed_tags["genre"] == ["Synthwave"]
    assert get_file_view(engine_settings, 9999) is None


def test_first_scan_adds_and_reads(engine_settings: Settings, music_dir: Path) -> None:
    _populate(music_dir, _N)

    result = scan_library(engine_settings)

    assert result.added == _N
    assert result.tags_read == _N
    assert result.total_seen == _N

    stats = get_library_stats(engine_settings)
    assert stats["present"] == _N
    assert stats["missing"] == 0
    assert stats["unprocessed"] == 0


def test_rescan_is_noop(engine_settings: Settings, music_dir: Path) -> None:
    _populate(music_dir, _N)
    scan_library(engine_settings)

    result = scan_library(engine_settings)

    assert result.unchanged == _N
    assert result.added == 0
    assert result.updated == 0
    assert result.tags_read == 0


def test_stale_reader_version_forces_an_incremental_reread(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)
    _make_snapshot_stale(engine_settings, music_dir, tracks[0].name, "Tattooed Corpse")

    reread = scan_library(engine_settings)

    # No signature moved: the stale stamp alone brought the scan back to the file.
    assert reread.updated == 0
    assert reread.tags_read == 1
    assert _stored_genre(engine_settings, music_dir, tracks[0].name) == ["Synthwave"]
    assert _file_row(engine_settings, music_dir, tracks[0].name).reader_version == (
        TAG_READER_VERSION
    )


def test_current_reader_version_settles_after_one_reread(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)
    _make_snapshot_stale(engine_settings, music_dir, tracks[0].name, "Tattooed Corpse")
    scan_library(engine_settings)  # the one re-read the stale stamp buys

    # Corrupt the snapshot again, this time leaving the stamp current. An unchanged re-read
    # is never tallied in tags_read, so the surviving wrong value is what proves the third
    # scan read nothing off disk.
    _mark_snapshot_wrong(engine_settings, music_dir, tracks[0].name, "Tattooed Corpse")
    settled = scan_library(engine_settings)

    assert settled.tags_read == 0
    assert _stored_genre(engine_settings, music_dir, tracks[0].name) == ["Tattooed Corpse"]
    assert _file_row(engine_settings, music_dir, tracks[0].name).reader_version == (
        TAG_READER_VERSION
    )


def test_presence_mode_ignores_a_stale_reader_version(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)
    _make_snapshot_stale(engine_settings, music_dir, tracks[0].name, "Tattooed Corpse")

    result = scan_library(engine_settings, mode=ScanMode.PRESENCE)

    assert result.tags_read == 0
    assert _stored_genre(engine_settings, music_dir, tracks[0].name) == ["Tattooed Corpse"]
    assert _file_row(engine_settings, music_dir, tracks[0].name).reader_version == 0


def test_changed_tags_are_reread(engine_settings: Settings, music_dir: Path) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)

    # Rewrite one track's genre; this changes its size/mtime signature.
    make_track(tracks[0], {"artist": ["Artist 0"], "genre": ["Darksynth"]})

    result = scan_library(engine_settings)

    assert result.updated >= 1
    assert result.tags_read == 1
    assert _stored_genre(engine_settings, music_dir, tracks[0].name) == ["Darksynth"]


def test_untagged_new_file_is_processed(engine_settings: Settings, music_dir: Path) -> None:
    # An untagged file still counts as processed: read_tags returns {}, which still
    # stamps tags_updated_at, so the file is NOT left "unprocessed".
    make_track(music_dir / "blank.mp3")

    scan_library(engine_settings)

    stats = get_library_stats(engine_settings)
    assert stats["unprocessed"] == 0


def test_missing_then_restored(engine_settings: Settings, music_dir: Path) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)

    gone = tracks[0]
    gone.unlink()
    missing_result = scan_library(engine_settings)
    assert missing_result.missing_flagged == 1

    stats = get_library_stats(engine_settings)
    assert stats["missing"] == 1
    assert stats["present"] == _N - 1
    assert _file_row(engine_settings, music_dir, gone.name).is_missing is True

    # Restore the file on disk and re-scan.
    make_track(gone, {"artist": ["Artist 0"], "genre": ["Synthwave"]})
    restored_result = scan_library(engine_settings)
    assert restored_result.restored == 1
    assert _file_row(engine_settings, music_dir, gone.name).is_missing is False


def test_presence_mode_skips_tag_reads(engine_settings: Settings, music_dir: Path) -> None:
    _populate(music_dir, _N)

    result = scan_library(engine_settings, mode=ScanMode.PRESENCE)

    assert result.added == _N
    assert result.tags_read == 0

    stats = get_library_stats(engine_settings)
    assert stats["unprocessed"] == _N  # tags never read in presence mode


def test_full_mode_honest_noop_then_reread(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings, mode=ScanMode.FULL)

    # Re-reading identical tags is an honest no-op: nothing actually changed.
    noop = scan_library(engine_settings, mode=ScanMode.FULL)
    assert noop.tags_read == 0
    assert noop.unchanged == _N

    # Change a tag on disk; full re-read now persists exactly one file.
    make_track(tracks[0], {"artist": ["Artist 0"], "genre": ["Ambient"]})
    changed = scan_library(engine_settings, mode=ScanMode.FULL)
    assert changed.tags_read == 1


def test_subfolder_scan_scopes_missing(engine_settings: Settings, music_dir: Path) -> None:
    folder_a = music_dir / "folderA"
    folder_b = music_dir / "folderB"
    track_a = make_track(folder_a / "a.mp3", {"genre": ["Synthwave"]})
    track_b = make_track(folder_b / "b.mp3", {"genre": ["Synthwave"]})

    scan_library(engine_settings, mode=ScanMode.FULL)

    # Delete the folderB file, then scan ONLY folderA.
    track_b.unlink()
    result = scan_library(engine_settings, path=folder_a)

    # The deleted folderB file must NOT be flagged missing — reconcile is scoped to
    # the scanned root (folderA), proving _reconcile_missing respects the path arg.
    assert result.missing_flagged == 0
    assert _file_row(engine_settings, folder_b, track_b.name).is_missing is False
    assert _file_row(engine_settings, folder_a, track_a.name).is_missing is False


def test_scan_continues_past_corrupt_file(engine_settings: Settings, music_dir: Path) -> None:
    _populate(music_dir, _N)
    # A garbage .mp3 raises MutagenError on tag read; the scan must keep going.
    (music_dir / "broken.mp3").write_bytes(b"not an audio file")

    result = scan_library(engine_settings)

    assert result.errors >= 1
    # All good tracks were still added (added counts the corrupt file too; tags_read
    # is what excludes it). The good tracks have their tags stored.
    stats = get_library_stats(engine_settings)
    total_tag_values = stats["total_tag_values"]
    assert isinstance(total_tag_values, int)
    assert total_tag_values >= _N


def test_scan_requires_music_path(tmp_path: Path) -> None:
    settings = Settings(
        music_path=None,
        lastfm_api_key=None,
        db_path=tmp_path / "ledger.sqlite3",
    )
    with pytest.raises(ValueError, match="music_path not configured"):
        scan_library(settings)


def test_scan_rejects_nonexistent_path(engine_settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        scan_library(engine_settings, path=tmp_path / "does-not-exist")


# --- genre_status filter + new FileView fields --------------------------------------


def test_plain_file_defaults_to_pending_with_no_sources(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _populate(music_dir, 1)
    scan_library(engine_settings)

    view = list_files(engine_settings)[0]
    assert view.genre_status == "pending"
    assert view.genre_source_artist is None
    assert view.genre_source_album is None


def test_list_files_filters_to_no_match_with_sources(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)

    # Flag only the middle track as no_match, recorded against its identity.
    target = _file_row(engine_settings, music_dir, tracks[1].name)
    _set_no_match(
        engine_settings,
        target.id,
        source_artist="Artist 1",
        source_album="Some Album",
    )

    views = list_files(engine_settings, genre_status="no_match")

    assert [v.file_id for v in views] == [target.id]
    only = views[0]
    assert only.genre_status == "no_match"
    assert only.genre_source_artist == "Artist 1"
    assert only.genre_source_album == "Some Album"


def test_list_files_filter_limit_counts_matching_lowest_ids(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # Five tracks; flag tracks 0, 2, 3 as no_match (track 1, 4 are pending non-matches).
    tracks = _populate(music_dir, 5)
    scan_library(engine_settings)
    matching_ids: list[int] = []
    for index in (0, 2, 3):
        row = _file_row(engine_settings, music_dir, tracks[index].name)
        _set_no_match(engine_settings, row.id, source_artist=f"Artist {index}")
        matching_ids.append(row.id)

    # limit=2 returns exactly the two lowest-id MATCHING files; non-matches (track 1)
    # do not consume the cap.
    views = list_files(engine_settings, genre_status="no_match", limit=2)

    assert [v.file_id for v in views] == sorted(matching_ids)[:2]
    assert all(v.genre_status == "no_match" for v in views)


def test_list_files_unknown_genre_status_raises(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _populate(music_dir, 1)
    scan_library(engine_settings)
    with pytest.raises(ValueError, match="unknown genre_status"):
        list_files(engine_settings, genre_status="bogus")


def test_get_library_stats_includes_genre_block(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)
    row = _file_row(engine_settings, music_dir, tracks[0].name)
    _set_no_match(engine_settings, row.id, source_artist="Artist 0")

    stats = get_library_stats(engine_settings)

    assert "genre" in stats
    genre = stats["genre"]
    assert isinstance(genre, dict)
    assert set(genre) == store.GENRE_WORKFLOW_STATUSES
    assert genre["no_match"] == 1
    assert genre["pending"] == _N - 1


# --- artist_status filter + composition + new FileView fields -----------------------


def test_list_files_filters_to_artist_manual_with_genre_status_none(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)

    # Exclude exactly the middle track on the artist axis.
    target = _file_row(engine_settings, music_dir, tracks[1].name)
    artists.set_artist_status(engine_settings, file_ids=[target.id], status="manual")

    # genre_status defaults to None; only the artist filter applies.
    views = list_files(engine_settings, artist_status="manual")

    assert [v.file_id for v in views] == [target.id]
    only = views[0]
    assert only.artist_status == "manual"
    assert only.genre_status == "pending"  # axes are independent


def test_list_files_unknown_artist_status_raises(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _populate(music_dir, 1)
    scan_library(engine_settings)
    with pytest.raises(ValueError, match="unknown artist_status"):
        list_files(engine_settings, artist_status="bogus")


def test_list_files_composes_both_status_filters(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)

    # Track 0: artist-manual only. Track 1: genre-no_match only. Track 2: BOTH.
    row0 = _file_row(engine_settings, music_dir, tracks[0].name)
    row1 = _file_row(engine_settings, music_dir, tracks[1].name)
    row2 = _file_row(engine_settings, music_dir, tracks[2].name)
    artists.set_artist_status(engine_settings, file_ids=[row0.id, row2.id], status="manual")
    _set_no_match(engine_settings, row1.id, source_artist="Artist 1")
    _set_no_match(engine_settings, row2.id, source_artist="Artist 2")

    # Both filters set → a file must satisfy BOTH; only track 2 qualifies.
    views = list_files(engine_settings, genre_status="no_match", artist_status="manual")
    assert [v.file_id for v in views] == [row2.id]


def test_get_library_stats_includes_artist_block(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)
    row = _file_row(engine_settings, music_dir, tracks[0].name)
    artists.set_artist_status(engine_settings, file_ids=[row.id], status="manual")

    stats = get_library_stats(engine_settings)

    assert "artist" in stats
    artist = stats["artist"]
    assert isinstance(artist, dict)
    assert set(artist) == store.ARTIST_WORKFLOW_STATUSES
    assert artist["manual"] == 1


# --- year_status filter + composition + new FileView fields -------------------------


def _set_year_no_match(
    settings: Settings,
    file_id: int,
    *,
    source_artist: str,
    source_album: str,
) -> None:
    """Persist a terminal ``no_match`` year decision in the isolated ledger."""
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        store.set_year_status(
            conn,
            file_id=file_id,
            status="no_match",
            source_artist=source_artist,
            source_album=source_album,
            now="2026-06-17T00:00:00+00:00",
        )
        conn.commit()
    finally:
        conn.close()


def test_list_files_filters_to_year_no_match_with_sources(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)

    target = _file_row(engine_settings, music_dir, tracks[1].name)
    _set_year_no_match(
        engine_settings,
        target.id,
        source_artist="Artist 1",
        source_album="Some Album",
    )

    views = list_files(engine_settings, year_status="no_match")

    assert [v.file_id for v in views] == [target.id]
    only = views[0]
    assert only.year_status == "no_match"
    assert only.year_source_artist == "Artist 1"
    assert only.year_source_album == "Some Album"


def test_list_files_unknown_year_status_raises(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _populate(music_dir, 1)
    scan_library(engine_settings)
    with pytest.raises(ValueError, match="unknown year_status"):
        list_files(engine_settings, year_status="bogus")


def test_list_files_composes_year_with_other_filters(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)

    # Track 0: year-no_match only. Track 2: year-no_match AND artist-manual.
    row0 = _file_row(engine_settings, music_dir, tracks[0].name)
    row2 = _file_row(engine_settings, music_dir, tracks[2].name)
    _set_year_no_match(engine_settings, row0.id, source_artist="A0", source_album="X")
    _set_year_no_match(engine_settings, row2.id, source_artist="A2", source_album="Y")
    artists.set_artist_status(engine_settings, file_ids=[row2.id], status="manual")

    # Both filters set → only track 2 satisfies BOTH.
    views = list_files(engine_settings, year_status="no_match", artist_status="manual")
    assert [v.file_id for v in views] == [row2.id]


def test_get_library_stats_includes_year_block(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)
    row = _file_row(engine_settings, music_dir, tracks[0].name)
    _set_year_no_match(engine_settings, row.id, source_artist="Artist 0", source_album="Alb")

    stats = get_library_stats(engine_settings)

    assert "year" in stats
    year = stats["year"]
    assert isinstance(year, dict)
    assert set(year) == store.YEAR_WORKFLOW_STATUSES
    assert year["no_match"] == 1
    assert year["pending"] == _N - 1


# --- no_identity worklist (files carrying neither artist nor albumartist) ------------


def test_no_identity_file_derives_no_identity_on_all_axes(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "orphan.mp3", {"genre": ["Synthwave"]})  # no artist/albumartist
    scan_library(engine_settings)

    view = list_files(engine_settings)[0]
    assert view.genre_status == "no_identity"
    assert view.artist_status == "no_identity"
    assert view.year_status == "no_identity"


def test_list_files_filters_to_no_identity_on_each_axis(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # orphan (no identity) + one with artist + one with only albumartist (both have identity).
    make_track(music_dir / "orphan.mp3", {"genre": ["Synthwave"]})
    make_track(music_dir / "byartist.mp3", {"artist": ["Alpha"], "genre": ["Rock"]})
    make_track(music_dir / "byaa.mp3", {"albumartist": ["Bravo"], "genre": ["Jazz"]})
    scan_library(engine_settings)

    by_genre = list_files(engine_settings, genre_status="no_identity")
    by_artist = list_files(engine_settings, artist_status="no_identity")
    by_year = list_files(engine_settings, year_status="no_identity")
    assert [v.filename for v in by_genre] == ["orphan.mp3"]
    assert [v.filename for v in by_artist] == ["orphan.mp3"]
    assert [v.filename for v in by_year] == ["orphan.mp3"]


def test_whitespace_only_artist_derives_no_identity(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # A tab-only artist + newline-only albumartist round-trip through scan and are treated as
    # blank (``str.strip``), so the file lands on the no_identity worklist (proves the
    # Python-side blankness rule beyond the ASCII-space case).
    make_track(
        music_dir / "ws.mp3",
        {"artist": ["\t"], "albumartist": ["\n"], "genre": ["Rock"]},
    )
    scan_library(engine_settings)

    views = list_files(engine_settings, genre_status="no_identity")
    assert [v.filename for v in views] == ["ws.mp3"]


def test_no_identity_composes_with_other_filters(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # An orphan is no_identity on BOTH the genre and year axes → it matches an AND of both.
    make_track(music_dir / "orphan.mp3", {"genre": ["Synthwave"]})
    make_track(music_dir / "byartist.mp3", {"artist": ["Alpha"], "genre": ["Rock"]})
    scan_library(engine_settings)

    views = list_files(engine_settings, genre_status="no_identity", year_status="no_identity")
    assert [v.filename for v in views] == ["orphan.mp3"]


# --- mismatch_status filter + new FileView fields -----------------------------------


def test_plain_file_defaults_to_pending_mismatch(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _populate(music_dir, 1)
    scan_library(engine_settings)

    view = list_files(engine_settings)[0]
    assert view.mismatch_status == "pending"
    assert view.mismatch_source_field is None
    assert view.mismatch_source_value is None


def test_list_files_filters_to_mismatch_status_with_sources(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)

    # _populate gives each track an artist but no albumartist, so the disposition snapshots
    # the artist field/value.
    target = _file_row(engine_settings, music_dir, tracks[1].name)
    mismatch.set_mismatch_status(engine_settings, file_ids=[target.id], status="legit_ignore")

    views = list_files(engine_settings, mismatch_status="legit_ignore")

    assert [v.file_id for v in views] == [target.id]
    only = views[0]
    assert only.mismatch_status == "legit_ignore"
    assert only.mismatch_source_field == "artist"
    assert only.mismatch_source_value == "Artist 1"


def test_list_files_unknown_mismatch_status_raises(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _populate(music_dir, 1)
    scan_library(engine_settings)
    with pytest.raises(ValueError, match="unknown mismatch_status"):
        list_files(engine_settings, mismatch_status="bogus")


def test_list_files_composes_mismatch_with_other_filters(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)

    row0 = _file_row(engine_settings, music_dir, tracks[0].name)
    row2 = _file_row(engine_settings, music_dir, tracks[2].name)
    mismatch.set_mismatch_status(
        engine_settings, file_ids=[row0.id, row2.id], status="legit_ignore"
    )
    artists.set_artist_status(engine_settings, file_ids=[row2.id], status="manual")

    # Both filters set -> only track 2 satisfies BOTH.
    views = list_files(engine_settings, mismatch_status="legit_ignore", artist_status="manual")
    assert [v.file_id for v in views] == [row2.id]


def test_get_library_stats_includes_mismatch_block(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _populate(music_dir, _N)
    scan_library(engine_settings)
    row = _file_row(engine_settings, music_dir, tracks[0].name)
    mismatch.set_mismatch_status(engine_settings, file_ids=[row.id], status="misfiled_deferred")

    stats = get_library_stats(engine_settings)

    assert "mismatch" in stats
    block = stats["mismatch"]
    assert isinstance(block, dict)
    assert set(block) == store.MISMATCH_WORKFLOW_STATUSES
    assert block["misfiled_deferred"] == 1
    assert block["pending"] == _N - 1
