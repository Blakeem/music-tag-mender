"""Integration tests for the genre-tagging orchestration (:mod:`tagmend.engine.genres`).

These use real temp audio files (the silent templates) across all four formats and a real
temp ledger via the ``engine_settings`` fixture, so they exercise the full loop end to
end — scan → ``resolve_genres`` → ``diff_tags`` → ``commit_tags`` → ``read_tags`` /
``revert`` — with **no network**: a fake :class:`TagSource` is injected at the
``resolve_genres(client=...)` signature (the documented DI seam), mapping artist → tags.

Coverage mirrors PLAN — Last.fm genre tagging § "Verification (Integration)":
the happy path on every format (incl. the m4a multi-value ``genre`` round-trip), the P0
no-accidental-deletion guarantee, "done" derivation, ``no_match`` + staleness, ``manual``
status + reset, the ``limit``/``more`` loop, and revert. The outcome-row scenarios shared by
every axis live in ``test_axis_status.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import mutagen
import pytest

from conftest import make_track
from tagmend.engine import axis, genres, staging, store, versioning
from tagmend.engine.db import connect
from tagmend.engine.lastfm import LastfmError, Tag
from tagmend.engine.library import ScanMode, list_files, scan_library
from tagmend.engine.schema import apply_schema
from tagmend.engine.tags import read_tags, write_managed_tags

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

_FORMATS = [".mp3", ".flac", ".m4a", ".ogg"]

# Real vocabulary genres so ``classify_genres`` yields a deterministic, multi-value result.
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


def _genre_status(settings: Settings, file_id: int) -> axis.OutcomeRow | None:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return axis.get_outcome(conn, axis.GENRE_AXIS, file_id)
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
    result = genres.resolve_genres(engine_settings, client=fake)

    assert result.settled == 1
    assert result.staged_files == 1
    assert result.no_match == 0
    assert fake.artist_lookups == ["Daft Punk"]

    # diff_tags shows genre replaced with the resolved multi-value list.
    views = staging.diff_tags(engine_settings)
    assert len(views) == 1
    assert views[0].diff == {"genre": {"from": ["Old"], "to": _EXPECTED_DAFT_PUNK}}
    assert views[0].origin == "auto"

    commit_result = staging.commit_tags(engine_settings)
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
    result = genres.resolve_genres(engine_settings, client=fake)

    assert result.staged_files == 1
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
    result = genres.resolve_genres(engine_settings, client=fake)

    # A no_identity file is never selected, so nothing settles and nothing stays pending.
    assert result.settled == 0
    assert result.pending_remaining == 0
    assert fake.artist_lookups == []
    # No status row, and untouched on disk.
    assert _genre_status(engine_settings, file_id) is None
    assert len(staging.diff_tags(engine_settings)) == 0
    assert read_tags(track).tags["genre"] == ["Old"]


def test_whitespace_only_artist_is_skipped_no_identity(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # A tab-only artist strips to blank, so the genre identity is None, the file derives
    # ``no_identity`` and is NEVER looked up as ``"\t"`` junk (the live-testing artifact).
    track = make_track(music_dir / "ws.mp3", {"artist": ["\t"], "genre": ["Old"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    fake = FakeTagSource({})
    result = genres.resolve_genres(engine_settings, client=fake)

    assert result.settled == 0
    assert result.pending_remaining == 0
    assert fake.artist_lookups == []  # no lookup for the whitespace junk
    assert _genre_status(engine_settings, file_id) is None
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
    genres.resolve_genres(engine_settings, client=fake)
    staging.commit_tags(engine_settings)

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
    genres.resolve_genres(engine_settings, client=fake)
    staging.commit_tags(engine_settings)

    # Re-run: the done row still matches the committed tags, so nothing is selected.
    second = genres.resolve_genres(engine_settings, client=fake)
    assert second.settled == 0
    assert second.pending_remaining == 0
    assert fake.artist_lookups == ["Daft Punk"]


def test_album_without_value_is_refused(engine_settings: Settings) -> None:
    with pytest.raises(ValueError, match="needs value"):
        genres.resolve_genres(engine_settings, album="Discovery", client=FakeTagSource({}))


def test_already_staged_file_refuses_a_second_run_and_reads_staged(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"artist": ["Daft Punk"], "genre": ["Old"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS})
    genres.resolve_genres(engine_settings, client=fake)  # stages but does NOT commit

    # A second run refuses rather than restaging over pending work.
    with pytest.raises(ValueError, match="commit or unstage pending changes first"):
        genres.resolve_genres(engine_settings, client=fake)

    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        assert store.derived_status(conn, axis.GENRE_AXIS, file_id) == "staged"
    finally:
        conn.close()


def test_resolve_genres_refuses_while_anything_is_staged(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Daft Punk"], "album": ["Discovery"]})
    make_track(music_dir / "b.mp3", {"artist": ["Daft Punk"], "genre": ["Old"]})
    scan_library(engine_settings)
    fixed_id = _file_id(engine_settings, music_dir, "a.mp3")
    staging.stage_tags_batch(
        engine_settings,
        entries=[(fixed_id, {"album": ["Discovery (Remastered)"]})],
    )
    before = staging.diff_tags(engine_settings)

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS})
    with pytest.raises(ValueError, match="commit or unstage pending changes first"):
        genres.resolve_genres(engine_settings, client=fake)

    after = staging.diff_tags(engine_settings)
    assert fake.artist_lookups == []
    assert [(v.file_id, v.origin, v.target) for v in after] == [
        (v.file_id, v.origin, v.target) for v in before
    ]
    assert after[0].origin == "manual"
    assert after[0].target["album"] == ["Discovery (Remastered)"]


def test_genre_pipeline_is_field_aware_artist_only_change_stays_processable(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    """An artist-only auto/staged change must NOT mark a file genre-``done``.

    A committed ``origin='auto'`` revision that changed only ``artist`` (or a staged change
    that touches only ``artist``) must leave the file genre-``pending``, and the resolver's
    selection is that same classifier, so the status view and ``resolve_genres`` agree.
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
        assert store.derived_status(conn, axis.GENRE_AXIS, committed_id) == "pending"
        assert store.derived_status(conn, axis.GENRE_AXIS, staged_id) == "pending"

        # resolve_genres refuses while anything is staged, so the staged half is checked at
        # the selection itself: an artist-only staged row leaves the file selectable.
        assert store.pending_file_ids(conn, axis.GENRE_AXIS, [staged_id]) == [staged_id]

        store.delete_staged_tag(conn, staged_id)
        conn.commit()
    finally:
        conn.close()

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS})
    result = genres.resolve_genres(engine_settings, client=fake, file_ids=[committed_id])

    # The committed artist-only revision does not mark the file genre-done.
    assert result.settled == 1


# --- no_match + staleness ------------------------------------------------------------


def test_no_match_recorded_with_source_then_skipped(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"artist": ["Obscure Band"], "genre": ["Old"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    fake = FakeTagSource({"Obscure Band": None})  # not on Last.fm
    result = genres.resolve_genres(engine_settings, client=fake)

    assert result.no_match == 1
    assert result.staged_files == 0
    assert result.no_match_artists == ["Obscure Band"]

    decision = _genre_status(engine_settings, file_id)
    assert decision is not None
    assert decision.status == "no_match"
    assert decision.identity == axis.Identity(primary="Obscure Band", secondary=None)

    # Nothing staged, and a re-run selects nothing while the no_match still matches.
    assert len(staging.diff_tags(engine_settings)) == 0
    second = genres.resolve_genres(engine_settings, client=fake)
    assert second.settled == 0
    assert fake.artist_lookups == ["Obscure Band"]


def test_stale_no_match_is_reprocessed_after_artist_changes(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"artist": ["Wrong Name"], "genre": ["Old"]})
    scan_library(engine_settings)

    # First pass: artist unknown → no_match against "Wrong Name".
    fake = FakeTagSource({"Wrong Name": None, "Daft Punk": _DAFT_PUNK_TAGS})
    genres.resolve_genres(engine_settings, client=fake)

    # Fix the artist tag on disk and rescan so the snapshot identity changes.
    write_managed_tags(track, {"artist": ["Daft Punk"], "genre": ["Old"]})
    scan_library(engine_settings, mode=ScanMode.FULL)

    # The stale no_match (identity "Wrong Name" != "Daft Punk") is reprocessed.
    result = genres.resolve_genres(engine_settings, client=fake)
    assert result.settled == 1
    assert result.staged_files == 1


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
    first = genres.resolve_genres(engine_settings, client=fake)
    assert first.settled == 0
    assert first.pending_remaining == 0
    assert fake.artist_lookups == []  # sticky: never even looked up

    # reset re-queues it.
    assert genres.reset_genre_status(engine_settings, file_ids=[file_id]) == 1
    second = genres.resolve_genres(engine_settings, client=fake)
    assert second.settled == 1
    assert second.staged_files == 1


@pytest.mark.parametrize("status", ["no_match", "pending", "done"])
def test_set_genre_status_accepts_manual_only(engine_settings: Settings, status: str) -> None:
    # A resolver alone decides done/no_match, and reset is the only hand-back of manual.
    with pytest.raises(ValueError, match="unknown status"):
        genres.set_genre_status(engine_settings, file_ids=[1], status=status)


def _genre_status_rows(settings: Settings) -> list[tuple[int, str]]:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        rows = conn.execute("SELECT file_id, status FROM file_genre_status ORDER BY file_id")
        return [(int(row[0]), str(row[1])) for row in rows.fetchall()]
    finally:
        conn.close()


def test_set_genre_status_without_scope_changes_nothing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Daft Punk"]})
    make_track(music_dir / "b.mp3", {"artist": ["Justice"]})
    scan_library(engine_settings)

    assert genres.set_genre_status(engine_settings, status="manual") == 0
    assert _genre_status_rows(engine_settings) == []


def test_reset_genre_status_without_scope_keeps_manual_rows(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "a.mp3", {"artist": ["Daft Punk"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    genres.set_genre_status(engine_settings, file_ids=[file_id], status="manual")

    assert genres.reset_genre_status(engine_settings) == 0
    assert _genre_status_rows(engine_settings) == [(file_id, "manual")]


def test_resolve_genres_keeps_disk_values_the_mirror_lacks(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # An external edit with no rescan since leaves the snapshot mirror behind the file.
    track = make_track(
        music_dir / "t.mp3",
        {"artist": ["Daft Punk"], "album": ["Discovery"], "title": ["Old Title"]},
    )
    scan_library(engine_settings)
    audio = mutagen.File(track, easy=True)  # type: ignore[attr-defined]
    audio["title"] = ["Title Edited In Picard"]
    audio.save()

    genres.resolve_genres(engine_settings, client=FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS}))
    result = staging.commit_tags(engine_settings)

    assert read_tags(track).tags["title"] == ["Title Edited In Picard"]
    assert result.commit_id is not None
    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        revisions = store.revisions_for_commit(conn, result.commit_id)
    finally:
        conn.close()
    assert [set(revision.diff) for revision in revisions] == [{"genre"}]


def test_resolve_genres_skips_missing_files(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    kept = make_track(music_dir / "kept.mp3", {"artist": ["Daft Punk"], "genre": ["Old"]})
    gone = make_track(music_dir / "gone.mp3", {"artist": ["Justice"], "genre": ["Old"]})
    scan_library(engine_settings)
    gone.unlink()
    scan_library(engine_settings)  # flags the deleted file missing

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS, "Justice": _DAFT_PUNK_TAGS})
    result = genres.resolve_genres(engine_settings, client=fake)

    # A missing file is never selected and never counted as pending.
    assert result.settled == 1
    assert result.pending_remaining == 0
    assert result.staged_files == 1
    assert [view.filename for view in staging.diff_tags(engine_settings)] == [kept.name]


class _FailingTagSource(FakeTagSource):
    """A :class:`FakeTagSource` whose lookups for the given artists fail like a dropped link."""

    def __init__(self, artists: dict[str, list[Tag] | None], failing: set[str]) -> None:
        super().__init__(artists)
        self._failing = failing

    def artist_top_tags(
        self,
        name: str | None = None,
        *,
        mbid: str | None = None,
    ) -> list[Tag] | None:
        if name in self._failing:
            message = "Last.fm artist.gettoptags failed after 3 attempt(s): transport error"
            raise LastfmError(message)
        return super().artist_top_tags(name, mbid=mbid)


def test_lastfm_transport_failure_is_reported_not_raised(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Daft Punk"], "genre": ["Old"]})
    make_track(music_dir / "b.mp3", {"artist": ["Justice"], "genre": ["Old"]})
    scan_library(engine_settings)

    fake = _FailingTagSource({"Daft Punk": _DAFT_PUNK_TAGS}, failing={"Justice"})
    result = genres.resolve_genres(engine_settings, client=fake)

    assert result.staged_files == 1
    assert [error["key"] for error in result.error_items] == ["Justice"]
    assert "transport error" in result.error_items[0]["message"]


def test_lastfm_error_is_counted_and_itemized(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "b.mp3", {"artist": ["Justice"], "genre": ["Old"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "b.mp3")

    fake = _FailingTagSource({}, failing={"Justice"})
    result = genres.resolve_genres(engine_settings, client=fake)

    assert result.errors == 1
    assert result.error_items == [
        {
            "key": "Justice",
            "message": "Last.fm artist.gettoptags failed after 3 attempt(s): transport error",
        },
    ]
    assert _genre_status(engine_settings, file_id) is None  # still pending
    assert staging.diff_tags(engine_settings) == []


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

    first = genres.resolve_genres(engine_settings, client=fake, limit=2)
    assert first.settled == 2
    assert first.staged_files == 2
    assert first.pending_remaining == 1
    assert first.more is True

    # A second call continues with the remaining candidate (the first two are now "done").
    staging.commit_tags(engine_settings)
    second = genres.resolve_genres(engine_settings, client=fake, limit=2)
    assert second.settled == 1
    assert second.staged_files == 1
    assert second.pending_remaining == 0
    assert second.more is False


def test_result_follows_the_resolver_contract(engine_settings: Settings) -> None:
    result = genres.resolve_genres(engine_settings, client=FakeTagSource({}))

    assert set(result.to_dict()) == {
        "settled",
        "staged_files",
        "no_match",
        "pending_remaining",
        "more",
        "errors",
        "error_items",
        "no_match_artists",
        "summary",
    }


# --- dry run -------------------------------------------------------------------------


def test_dry_run_stages_nothing_and_records_no_status(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Daft Punk"], "genre": ["Old"]})
    make_track(music_dir / "b.mp3", {"artist": ["Obscure Band"], "genre": ["Old"]})
    scan_library(engine_settings)
    missed_id = _file_id(engine_settings, music_dir, "b.mp3")

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS, "Obscure Band": None})
    result = genres.resolve_genres(engine_settings, client=fake, dry_run=True)

    assert result.settled == 2
    assert result.staged_files == 1
    assert result.no_match == 1
    assert result.no_match_artists == ["Obscure Band"]
    assert result.pending_remaining == 2
    assert result.more is False
    assert staging.diff_tags(engine_settings) == []
    assert _genre_status(engine_settings, missed_id) is None


def test_dry_run_ignores_the_staging_precondition(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Daft Punk"], "album": ["Discovery"]})
    make_track(music_dir / "b.mp3", {"artist": ["Daft Punk"], "genre": ["Old"]})
    scan_library(engine_settings)
    fixed_id = _file_id(engine_settings, music_dir, "a.mp3")
    staging.stage_tags(engine_settings, file_id=fixed_id, tags={"album": ["Discovery (Live)"]})
    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS})

    preview = genres.resolve_genres(engine_settings, client=fake, dry_run=True)

    assert preview.staged_files == 2
    assert [view.target["album"] for view in staging.diff_tags(engine_settings)] == [
        ["Discovery (Live)"],
    ]
    with pytest.raises(ValueError, match="commit or unstage"):
        genres.resolve_genres(engine_settings, client=fake)


# --- revert --------------------------------------------------------------------------


def test_revert_restores_original_genre(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.flac", {"artist": ["Daft Punk"], "genre": ["Old"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    fake = FakeTagSource({"Daft Punk": _DAFT_PUNK_TAGS})
    genres.resolve_genres(engine_settings, client=fake)
    staging.commit_tags(engine_settings)
    assert read_tags(track).tags["genre"] == _EXPECTED_DAFT_PUNK

    # Revert to the version-0 baseline restores the original genre.
    versioning.revert_tags(engine_settings, file_id, 0)
    assert read_tags(track).tags["genre"] == ["Old"]


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
    genres.resolve_genres(engine_settings, client=fake)

    # The matched file lists as 'staged' before commit; the no-match file as 'no_match'
    # carrying the artist its lookup was recorded against.
    assert _status_of(engine_settings, matched_id) == "staged"
    no_match_view = next(v for v in list_files(engine_settings) if v.file_id == unknown_id)
    assert no_match_view.genre_status == "no_match"
    assert no_match_view.genre_source_artist == "Obscure Band"

    # After committing the staged change, the matched file derives 'done'.
    staging.commit_tags(engine_settings)
    assert _status_of(engine_settings, matched_id) == "done"
    # The no-match file is unaffected by the commit.
    assert _status_of(engine_settings, unknown_id) == "no_match"


def test_stale_no_match_lists_pending_and_is_reprocessed(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    """A no_match decided against an identity the file no longer has lists as ``pending``.

    The listing and ``resolve_genres`` read one classifier, so the file the listing calls
    ``pending`` is exactly the file the next run reprocesses.
    """
    track = make_track(music_dir / "t.mp3", {"artist": ["Wrong Name"], "genre": ["Old"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    fake = FakeTagSource({"Wrong Name": None, "Daft Punk": _DAFT_PUNK_TAGS})
    genres.resolve_genres(engine_settings, client=fake)
    assert _status_of(engine_settings, file_id) == "no_match"

    # Fix the artist on disk and rescan so the snapshot identity changes.
    write_managed_tags(track, {"artist": ["Daft Punk"], "genre": ["Old"]})
    scan_library(engine_settings, mode=ScanMode.FULL)

    # The stale row no longer decides the status, so its identity does not ride along.
    stale_view = next(v for v in list_files(engine_settings) if v.file_id == file_id)
    assert stale_view.genre_status == "pending"
    assert stale_view.genre_source_artist is None

    result = genres.resolve_genres(engine_settings, client=fake)
    assert result.settled == 1
    assert result.staged_files == 1
    # And now it lists as 'staged'.
    assert _status_of(engine_settings, file_id) == "staged"
