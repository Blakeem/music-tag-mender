"""Integration tests for the genre-tagging orchestration (:mod:`tagmend.engine.genres`).

These use real temp audio files (the silent templates) across all four formats and a real
temp ledger via the ``engine_settings`` fixture, so they exercise the full loop end to
end — scan → ``stage_genres`` → ``diff_tags`` → ``commit_tags`` → ``read_tags`` /
``revert`` — with **no network**: a fake :class:`TagSource` is injected at the
``stage_genres(client=...)` signature (the documented DI seam), mapping artist → tags.

Coverage mirrors PLAN — Last.fm genre tagging § "Verification (Integration)":
the happy path on every format (incl. the m4a multi-value ``genre`` round-trip), the P0
no-accidental-deletion guarantee, "done" derivation, ``no_match`` + staleness, ``manual``
status + reset, the ``limit``/``more`` loop, and revert.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from conftest import make_track
from tagmend.engine import genres, staging, store, versioning
from tagmend.engine.db import connect
from tagmend.engine.lastfm import Tag
from tagmend.engine.library import ScanMode, scan_library
from tagmend.engine.schema import apply_schema
from tagmend.engine.tags import read_tags, write_managed_tags

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

_FORMATS = [".mp3", ".flac", ".m4a", ".ogg"]

# Real vocabulary genres so ``resolve_genres`` yields a deterministic, multi-value result.
_DAFT_PUNK_TAGS = [
    Tag("electronic", 100),
    Tag("house", 63),
    Tag("dance", 36),
    Tag("techno", 26),
]
_EXPECTED_DAFT_PUNK = ["electronic", "house", "dance", "techno"]


class FakeTagSource:
    """An in-memory :class:`tagmend.engine.lastfm.TagSource` for DI in tests.

    Maps artist name → top-tag list (or ``None`` for "not on Last.fm"); ``album_top_tags``
    is keyed by ``(artist, album)`` and defaults to ``None`` (no album tags). Records the
    artist lookups it received so tests can assert on the identity used.
    """

    def __init__(
        self,
        artists: dict[str, list[Tag] | None],
        albums: dict[tuple[str, str], list[Tag] | None] | None = None,
    ) -> None:
        self._artists = artists
        self._albums = albums or {}
        self.artist_lookups: list[str] = []

    def artist_top_tags(
        self,
        name: str | None = None,
        *,
        mbid: str | None = None,  # protocol parity; phase 1 always looks up by name
    ) -> list[Tag] | None:
        assert name is not None
        self.artist_lookups.append(name)
        return self._artists.get(name)

    def album_top_tags(self, artist: str, album: str) -> list[Tag] | None:
        return self._albums.get((artist, album))


def _file_id(settings: Settings, folder: Path, filename: str) -> int:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        return row.id
    finally:
        conn.close()


def _genre_status(settings: Settings, file_id: int) -> store.GenreStatusRow | None:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return store.get_genre_status(conn, file_id)
    finally:
        conn.close()


# --- happy path across all four formats ----------------------------------------------


@pytest.mark.parametrize("suffix", _FORMATS)
def test_happy_path_stages_diffs_commits_multivalue_genre(
    engine_settings: Settings,
    music_dir: Path,
    suffix: str,
) -> None:
    track = make_track(
        music_dir / f"track{suffix}",
        {"artist": ["Daft Punk"], "genre": ["Old"], "title": ["Song"]},
    )
    scan_library(engine_settings)

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS})
    result = genres.stage_genres(engine_settings, client=fake)

    assert result.processed == 1
    assert result.staged == 1
    assert result.no_match == 0
    assert fake.artist_lookups == ["Daft Punk"]

    # diff_tags shows genre replaced with the resolved multi-value list.
    views = staging.diff_tags(engine_settings)
    assert len(views) == 1
    assert views[0].diff == {"genre": {"from": ["Old"], "to": _EXPECTED_DAFT_PUNK}}
    assert views[0].origin == "auto"

    commit_result = staging.commit_tags(engine_settings, origin="auto")
    assert commit_result.committed == 1

    on_disk = read_tags(track).tags
    assert on_disk["genre"] == _EXPECTED_DAFT_PUNK
    # The unmanaged title tag is untouched by the surgical write.
    assert on_disk.get("title") == ["Song"]
    # The known m4a weak spot: ≥2 genres must survive the ©gen round-trip.
    assert len(on_disk["genre"]) >= 2  # ©gen multi-value must round-trip


def test_albumartist_is_preferred_lookup_identity(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "t.mp3",
        {"artist": ["Various"], "albumartist": ["Daft Punk"], "genre": ["Old"]},
    )
    scan_library(engine_settings)

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS})
    result = genres.stage_genres(engine_settings, client=fake)

    assert result.staged == 1
    assert fake.artist_lookups == ["Daft Punk"]  # albumartist beat artist


# --- P0: no accidental deletion ------------------------------------------------------


def test_file_with_no_artist_is_skipped_and_untouched(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.flac", {"genre": ["Old"]})  # no artist at all
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    fake = FakeTagSource({})
    result = genres.stage_genres(engine_settings, client=fake)

    assert result.skipped["no_artist"] == 1
    assert result.processed == 0
    assert fake.artist_lookups == []
    # Left pending (no status row) and untouched on disk.
    assert _genre_status(engine_settings, file_id) is None
    assert len(staging.diff_tags(engine_settings)) == 0
    assert read_tags(track).tags["genre"] == ["Old"]


def test_commit_preserves_albumartist_and_replaces_only_genre(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(
        music_dir / "t.mp3",
        {"artist": ["Various"], "albumartist": ["Daft Punk"], "genre": ["Old"]},
    )
    scan_library(engine_settings)

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS})
    genres.stage_genres(engine_settings, client=fake)
    staging.commit_tags(engine_settings, origin="auto")

    on_disk = read_tags(track).tags
    assert on_disk["genre"] == _EXPECTED_DAFT_PUNK
    # P0-1: the managed albumartist/artist survive the delete-on-absent write.
    assert on_disk["albumartist"] == ["Daft Punk"]
    assert on_disk["artist"] == ["Various"]


# --- "done" derivation ---------------------------------------------------------------


def test_committed_auto_revision_is_skipped_as_done_on_rerun(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t.mp3", {"artist": ["Daft Punk"], "genre": ["Old"]})
    scan_library(engine_settings)

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS})
    genres.stage_genres(engine_settings, client=fake)
    staging.commit_tags(engine_settings, origin="auto")

    # Re-run: the committed auto revision derives "done" with no stored flag.
    second = genres.stage_genres(engine_settings, client=fake)
    assert second.processed == 0
    assert second.skipped["done"] == 1


def test_already_staged_file_is_skipped_as_done(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t.mp3", {"artist": ["Daft Punk"], "genre": ["Old"]})
    scan_library(engine_settings)

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS})
    genres.stage_genres(engine_settings, client=fake)  # stages but does NOT commit

    # Without committing, the staged row alone derives "done".
    second = genres.stage_genres(engine_settings, client=fake)
    assert second.processed == 0
    assert second.skipped["done"] == 1


# --- no_match + staleness ------------------------------------------------------------


def test_no_match_recorded_with_source_then_skipped(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"artist": ["Obscure Band"], "genre": ["Old"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    fake = FakeTagSource({"Obscure Band": None})  # not on Last.fm
    result = genres.stage_genres(engine_settings, client=fake)

    assert result.no_match == 1
    assert result.staged == 0
    assert result.no_match_artists == ["Obscure Band"]

    decision = _genre_status(engine_settings, file_id)
    assert decision is not None
    assert decision.status == "no_match"
    assert decision.source_artist == "Obscure Band"
    assert decision.source_album is None

    # Nothing staged; re-run skips it as a non-stale no_match.
    assert len(staging.diff_tags(engine_settings)) == 0
    second = genres.stage_genres(engine_settings, client=fake)
    assert second.processed == 0
    assert second.skipped["no_match"] == 1


def test_stale_no_match_is_reprocessed_after_artist_changes(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"artist": ["Wrong Name"], "genre": ["Old"]})
    scan_library(engine_settings)

    # First pass: artist unknown → no_match against "Wrong Name".
    fake = FakeTagSource({"Wrong Name": None, "Daft Punk": _DAFT_PUNK_TAGS})
    genres.stage_genres(engine_settings, client=fake)

    # Fix the artist tag on disk and rescan so the snapshot identity changes.
    write_managed_tags(track, {"artist": ["Daft Punk"], "genre": ["Old"]})
    scan_library(engine_settings, mode=ScanMode.FULL)

    # The stale no_match (source_artist="Wrong Name" != "Daft Punk") is reprocessed.
    result = genres.stage_genres(engine_settings, client=fake)
    assert result.processed == 1
    assert result.staged == 1
    assert result.skipped["no_match"] == 0


# --- manual status + reset -----------------------------------------------------------


def test_manual_status_skips_then_reset_requeues(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"artist": ["Daft Punk"], "genre": ["Old"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    affected = genres.set_genre_status(engine_settings, file_ids=[file_id], status="manual")
    assert affected == 1

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS})
    first = genres.stage_genres(engine_settings, client=fake)
    assert first.processed == 0
    assert first.skipped["manual"] == 1
    assert fake.artist_lookups == []  # sticky: never even looked up

    # reset re-queues it.
    assert genres.reset_genre_status(engine_settings, file_ids=[file_id]) == 1
    second = genres.stage_genres(engine_settings, client=fake)
    assert second.processed == 1
    assert second.staged == 1


def test_set_genre_status_rejects_unknown_status(engine_settings: Settings) -> None:
    with pytest.raises(ValueError, match="invalid status"):
        genres.set_genre_status(engine_settings, file_ids=[1], status="no_match")


# --- limit / more loop ---------------------------------------------------------------


def test_limit_caps_and_reports_pending_then_continues(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # Three distinct artists so each is its own group (one stage per file).
    for index, artist in enumerate(["Alpha", "Bravo", "Charlie"]):
        make_track(music_dir / f"t{index}.mp3", {"artist": [artist], "genre": ["Old"]})
    scan_library(engine_settings)

    fake = FakeTagSource(
        {"Alpha": _DAFT_PUNK_TAGS, "Bravo": _DAFT_PUNK_TAGS, "Charlie": _DAFT_PUNK_TAGS},
    )

    first = genres.stage_genres(engine_settings, client=fake, limit=2)
    assert first.processed == 2
    assert first.staged == 2
    assert first.pending_remaining == 1
    assert first.more is True

    # A second call continues with the remaining candidate (the first two are now "done").
    second = genres.stage_genres(engine_settings, client=fake, limit=2)
    assert second.processed == 1
    assert second.staged == 1
    assert second.pending_remaining == 0
    assert second.more is False


# --- revert --------------------------------------------------------------------------


def test_revert_restores_original_genre(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.flac", {"artist": ["Daft Punk"], "genre": ["Old"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS})
    genres.stage_genres(engine_settings, client=fake)
    staging.commit_tags(engine_settings, origin="auto")
    assert read_tags(track).tags["genre"] == _EXPECTED_DAFT_PUNK

    # Revert to the version-0 baseline restores the original genre.
    versioning.revert(engine_settings, file_id, 0)
    assert read_tags(track).tags["genre"] == ["Old"]


# --- list_artists --------------------------------------------------------------------


def test_list_artists_reports_distinct_values_with_counts(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Daft Punk"]})
    make_track(music_dir / "b.flac", {"artist": ["Daft Punk"]})
    make_track(music_dir / "c.m4a", {"artist": ["Justice"]})
    scan_library(engine_settings)

    rows = genres.list_artists(engine_settings)
    counts = {row.artist: row.file_count for row in rows}
    assert counts == {"Daft Punk": 2, "Justice": 1}
