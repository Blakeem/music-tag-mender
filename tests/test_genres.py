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
from tagmend.engine.library import ScanMode, list_files, scan_library
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


def test_genre_pipeline_is_field_aware_artist_only_change_stays_processable(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    """An artist-only auto/staged change must NOT mark a file genre-``done``.

    Regression for the field-awareness fix in ``genres._select``: a committed
    ``origin='auto'`` revision that changed only ``artist`` (or a staged change that
    touches only ``artist``) must leave the file genre-processable — not bucketed into
    ``skipped['done']`` — and must agree with ``derived_genre_status == 'pending'`` so the
    status view and ``stage_genres`` can no longer disagree.
    """
    make_track(music_dir / "committed.mp3", {"artist": ["Daft Punk"], "genre": ["Old"]})
    make_track(music_dir / "staged.mp3", {"artist": ["daft punk"], "genre": ["Old"]})
    scan_library(engine_settings)

    committed_id = _file_id(engine_settings, music_dir, "committed.mp3")
    staged_id = _file_id(engine_settings, music_dir, "staged.mp3")

    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        # An artist-ONLY committed auto revision (genre untouched).
        store.insert_revision(
            conn,
            file_id=committed_id,
            version=1,
            origin="auto",
            managed_tags={"artist": ["Daft Punk"], "genre": ["Old"]},
            diff={"artist": {"from": ["daft punk"], "to": ["Daft Punk"]}},
            now="2026-06-08T00:00:00+00:00",
        )
        # An artist-ONLY staged change: canonicalize the artist while PRESERVING the
        # current genre value, so only the ``artist`` field differs from disk.
        store.upsert_staged_tag(
            conn,
            file_id=staged_id,
            managed_tags={"artist": ["Daft Punk"], "genre": ["Old"]},
            origin="auto",
            now="2026-06-08T00:00:00+00:00",
        )
        conn.commit()

        # The status view agrees: an artist-only change is genre-PENDING, not done.
        assert store.derived_genre_status(conn, committed_id) == "pending"
        assert store.derived_genre_status(conn, staged_id) == "pending"
    finally:
        conn.close()

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS})
    result = genres.stage_genres(engine_settings, client=fake)

    # Neither file is skipped as done; the committed one is processed + genre-staged.
    # (The staged-id already has a staged genre? No — its staged row is artist-only, so it
    # is also processable.) Both are processed; none land in skipped['done'].
    assert result.skipped["done"] == 0
    assert result.processed == 2


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


def test_list_artists_limit_caps_rows_after_ordering(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Alpha"]})
    make_track(music_dir / "b.mp3", {"artist": ["Bravo"]})
    make_track(music_dir / "c.mp3", {"artist": ["Charlie"]})
    scan_library(engine_settings)

    rows = genres.list_artists(engine_settings, limit=2)
    assert [row.artist for row in rows] == ["Alpha", "Bravo"]


# --- genre-status visibility: end-to-end coherence -----------------------------------


def _status_of(settings: Settings, file_id: int) -> str:
    """Read back the derived genre status surfaced by ``list_files`` for one file."""
    view = next(v for v in list_files(settings) if v.file_id == file_id)
    return view.genre_status


def test_listing_status_tracks_stage_then_commit_and_no_match_source(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    matched = make_track(music_dir / "matched.mp3", {"artist": ["Daft Punk"], "genre": ["Old"]})
    unknown = make_track(music_dir / "unknown.mp3", {"artist": ["Obscure Band"], "genre": ["Old"]})
    scan_library(engine_settings)
    matched_id = _file_id(engine_settings, music_dir, matched.name)
    unknown_id = _file_id(engine_settings, music_dir, unknown.name)

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS, "Obscure Band": None})
    genres.stage_genres(engine_settings, client=fake)

    # The matched file lists as 'staged' before commit; the no-match file as 'no_match'
    # carrying the artist its lookup was recorded against.
    assert _status_of(engine_settings, matched_id) == "staged"
    no_match_view = next(v for v in list_files(engine_settings) if v.file_id == unknown_id)
    assert no_match_view.genre_status == "no_match"
    assert no_match_view.genre_source_artist == "Obscure Band"

    # After committing the staged change, the matched file derives 'done'.
    staging.commit_tags(engine_settings, origin="auto")
    assert _status_of(engine_settings, matched_id) == "done"
    # The no-match file is unaffected by the commit.
    assert _status_of(engine_settings, unknown_id) == "no_match"


def test_no_match_listing_is_pinned_while_stage_genres_reprocesses_stale(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    """Staleness pin: a stored no_match keeps listing as 'no_match' (recorded decision),

    even after the on-disk artist is fixed and rescanned — while a fresh
    ``stage_genres`` run reprocesses the now-stale file rather than skipping it.
    """
    track = make_track(music_dir / "t.mp3", {"artist": ["Wrong Name"], "genre": ["Old"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    fake = FakeTagSource({"Wrong Name": None, "Daft Punk": _DAFT_PUNK_TAGS})
    genres.stage_genres(engine_settings, client=fake)
    assert _status_of(engine_settings, file_id) == "no_match"

    # Fix the artist on disk and rescan so the snapshot identity changes.
    write_managed_tags(track, {"artist": ["Daft Punk"], "genre": ["Old"]})
    scan_library(engine_settings, mode=ScanMode.FULL)

    # Pinned behavior: the listing STILL reports the stored 'no_match' decision, now
    # against the stale "Wrong Name" identity.
    stale_view = next(v for v in list_files(engine_settings) if v.file_id == file_id)
    assert stale_view.genre_status == "no_match"
    assert stale_view.genre_source_artist == "Wrong Name"

    # A fresh stage_genres run, however, reprocesses the stale file (does not skip it).
    result = genres.stage_genres(engine_settings, client=fake)
    assert result.processed == 1
    assert result.staged == 1
    assert result.skipped["no_match"] == 0
    # And now it lists as 'staged'.
    assert _status_of(engine_settings, file_id) == "staged"
