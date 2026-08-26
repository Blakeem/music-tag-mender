"""Integration tests for the artist-name normalization (:mod:`tagmend.engine.artists`).

These use real temp audio files (the silent templates) across all four formats and a real
temp ledger via the ``engine_settings`` fixture, so they exercise the full loop end to end
— scan → ``resolve_artists`` → ``diff_tags`` → ``commit_tags`` → ``read_tags`` /
``revert_commit`` — with **no network**: a fake :class:`CorrectionSource` is injected at the
``resolve_artists(client=...)`` signature (the documented DI seam), mapping value →
``ArtistCorrection`` (or ``None`` for "no correction").

Coverage mirrors the approved acceptance criteria: the happy-path cascade, per-file
accumulation of both name fields, genre preservation (P0), MBID-on-change-only, the
feat/sentinel/empty + per-file multi-artist guards, idempotent re-run, the no_correction
list, dry-run, the empty-staging precondition, the limit/more loop, and revert. The
post-lookup correction gate (case-only, credit shrink, no MBID) has its own section.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import pytest

from conftest import make_track
from tagmend.engine import artists, staging, store, versioning
from tagmend.engine.db import connect
from tagmend.engine.lastfm import ArtistCorrection
from tagmend.engine.library import list_files as library_list
from tagmend.engine.library import scan_library
from tagmend.engine.schema import apply_schema
from tagmend.engine.tags import read_tags

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

_FORMATS = [".mp3", ".flac", ".m4a", ".ogg"]


class FakeCorrectionSource:
    """An in-memory :class:`tagmend.engine.lastfm.CorrectionSource` for DI in tests.

    Maps a value → :class:`ArtistCorrection` (or ``None`` for "no correction on Last.fm").
    A value absent from the map also yields ``None``. Records the lookups it received so
    tests can assert on what was queried.
    """

    def __init__(self, table: dict[str, ArtistCorrection | None]) -> None:
        self._table = table
        self.lookups: list[str] = []

    def artist_correction(self, name: str) -> ArtistCorrection | None:
        self.lookups.append(name)
        return self._table.get(name)


def _file_id(settings: Settings, folder: Path, filename: str) -> int:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        return row.id
    finally:
        conn.close()


# The per-value outcome buckets, which must always sum to ``processed``.
_BUCKET_NAMES = (
    "corrected_values",
    "already_canonical",
    "shrinks_credit",
    "needs_review",
    "no_correction",
    "errors",
)


def _buckets(result: artists.ResolveArtistsResult) -> dict[str, int]:
    return {
        "corrected_values": result.corrected_values,
        "already_canonical": result.already_canonical,
        "shrinks_credit": result.shrinks_credit,
        "needs_review": result.needs_review,
        "no_correction": result.no_correction,
        "errors": result.errors,
    }


# --- (1) happy-path cascade + (3) genre preserved ------------------------------------


def test_happy_path_cascades_canonical_name_across_matching_files(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Miami Nights '84"], "genre": ["synthwave"]})
    make_track(music_dir / "b.flac", {"artist": ["Miami Nights '84"], "genre": ["retro"]})
    make_track(music_dir / "c.mp3", {"artist": ["Daft Punk"], "genre": ["house"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {
            "Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1"),
            "Daft Punk": ArtistCorrection("Daft Punk", None),  # already canonical
        },
    )
    result = artists.resolve_artists(engine_settings, client=fake)

    assert result.corrected_values == 1
    assert result.staged_files == 2
    assert {m["from"]: m["to"] for m in result.mappings} == {
        "Miami Nights '84": "Miami Nights 1984",
    }

    views = {v.file_id: v for v in staging.diff_tags(engine_settings)}
    assert len(views) == 2
    for view in views.values():
        assert view.origin == "auto"
        assert view.diff["artist"] == {"from": ["Miami Nights '84"], "to": ["Miami Nights 1984"]}
        # (3) genre is untouched — only artist (+ mbid) changes.
        assert "genre" not in view.diff


# --- (2) per-file accumulation of artist + albumartist -------------------------------


def test_artist_and_albumartist_corrected_in_one_staged_row(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "t.mp3",
        {"artist": ["Miami Nights '84"], "albumartist": ["VA '84"], "genre": ["synthwave"]},
    )
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {
            "Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1"),
            "VA '84": ArtistCorrection("Various Artists 1984", "mbid-2"),
        },
    )
    result = artists.resolve_artists(engine_settings, client=fake)

    assert result.staged_files == 1
    views = staging.diff_tags(engine_settings)
    assert len(views) == 1  # one row, both fields
    diff = views[0].diff
    assert diff["artist"]["to"] == ["Miami Nights 1984"]
    assert diff["albumartist"]["to"] == ["Various Artists 1984"]


# --- (4) MBID rides along on changed files only --------------------------------------


def test_mbid_written_on_changed_files_only(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    changed = make_track(music_dir / "changed.mp3", {"artist": ["Miami Nights '84"]})
    canonical = make_track(music_dir / "canonical.mp3", {"artist": ["Daft Punk"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {
            "Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1"),
            "Daft Punk": ArtistCorrection("Daft Punk", "mbid-daft"),  # canonical: no change
        },
    )
    artists.resolve_artists(engine_settings, client=fake)
    staging.commit_tags(engine_settings, origin="auto")

    assert read_tags(changed).tags["musicbrainz_artistid"] == ["mbid-1"]
    # The already-canonical file is not touched just to backfill an MBID.
    assert "musicbrainz_artistid" not in read_tags(canonical).tags
    _ = canonical


# --- (5) feat / sentinel / empty guards (distinct-value scan) ------------------------


def test_feat_sentinel_and_empty_values_are_skipped_and_reported(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "feat.mp3", {"artist": ["Kavinsky feat. Lovefoxxx"]})
    make_track(music_dir / "sent.mp3", {"artist": ["Various Artists"]})
    make_track(music_dir / "empty.mp3", {"artist": ["   "]})
    make_track(music_dir / "good.mp3", {"artist": ["Miami Nights '84"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1")},
    )
    result = artists.resolve_artists(engine_settings, client=fake)

    assert result.skipped_sentinel == 3
    assert result.staged_files == 1
    # The guarded values were never looked up.
    assert fake.lookups == ["Miami Nights '84"]


# --- (5b) per-file multi-value guard -------------------------------------------------


def test_file_with_two_artist_values_is_skipped_as_multi_artist(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    multi = make_track(
        music_dir / "multi.flac",
        {"artist": ["Miami Nights '84", "Daft Punk"]},
    )
    scan_library(engine_settings)
    multi_id = _file_id(engine_settings, music_dir, multi.name)

    fake = FakeCorrectionSource(
        {
            "Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1"),
            "Daft Punk": ArtistCorrection("Daft Punk", None),
        },
    )
    result = artists.resolve_artists(engine_settings, client=fake)

    assert result.skipped_multi_artist == 1
    assert result.multi_artist_files == [multi_id]
    assert result.staged_files == 0
    assert len(staging.diff_tags(engine_settings)) == 0


# --- (6) already-canonical + idempotent re-run ---------------------------------------


def test_already_canonical_stages_nothing_and_rerun_is_noop(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t.mp3", {"artist": ["Daft Punk"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource({"Daft Punk": ArtistCorrection("Daft Punk", None)})
    first = artists.resolve_artists(engine_settings, client=fake)
    assert first.staged_files == 0
    assert len(staging.diff_tags(engine_settings)) == 0

    # An already-canonical value is visible (not silently invisible) and the per-value
    # outcome buckets sum to processed.
    assert first.processed == 1
    assert first.corrected_values == 0
    assert first.no_correction == 0
    assert first.already_canonical == 1
    assert first.already_canonical_values == ["Daft Punk"]
    assert sum(_buckets(first).values()) == first.processed
    assert "1 already canonical" in first.summary


def test_rerun_after_commit_is_idempotent(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t.mp3", {"artist": ["Miami Nights '84"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {
            "Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1"),
            "Miami Nights 1984": ArtistCorrection("Miami Nights 1984", "mbid-1"),
        },
    )
    artists.resolve_artists(engine_settings, client=fake)
    staging.commit_tags(engine_settings, origin="auto")
    scan_library(engine_settings)

    # The canonical value now equals the correction → no further change.
    second = artists.resolve_artists(engine_settings, client=fake)
    assert second.staged_files == 0
    assert len(staging.diff_tags(engine_settings)) == 0


# --- (7) no correction ---------------------------------------------------------------


def test_no_correction_is_reported_not_an_error(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t.mp3", {"artist": ["Obscure Band"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource({"Obscure Band": None})
    result = artists.resolve_artists(engine_settings, client=fake)

    assert result.no_correction == 1
    assert result.no_correction_values == ["Obscure Band"]
    assert result.staged_files == 0
    assert result.errors == 0


# --- (7b) MusicBrainz placeholder guard ----------------------------------------------


@pytest.mark.parametrize("placeholder", ["[unknown]", "[no artist]", "[anonymous]"])
def test_placeholder_correction_is_treated_as_no_correction(
    engine_settings: Settings,
    music_dir: Path,
    placeholder: str,
) -> None:
    # A junk album-artist label whose getCorrection is a MB special-purpose placeholder
    # (+MBID) must NOT cascade-stage — it is treated exactly like "no correction".
    make_track(music_dir / "ost.mp3", {"albumartist": ["Original Soundtrack"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {"Original Soundtrack": ArtistCorrection(placeholder, "125ec42a-mbid")},
    )
    result = artists.resolve_artists(engine_settings, client=fake)

    assert result.corrected_values == 0
    assert result.staged_files == 0
    assert result.no_correction == 1
    assert result.no_correction_values == ["Original Soundtrack"]
    assert result.already_canonical == 0
    assert result.mappings == []
    assert len(staging.diff_tags(engine_settings)) == 0


def test_placeholder_correction_dry_run_parity(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "ost.mp3", {"albumartist": ["Original Soundtrack"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {"Original Soundtrack": ArtistCorrection("[unknown]", "125ec42a-mbid")},
    )
    result = artists.resolve_artists(engine_settings, client=fake, dry_run=True)

    assert result.corrected_values == 0
    assert result.staged_files == 0
    assert result.no_correction_values == ["Original Soundtrack"]
    assert result.mappings == []
    assert len(staging.diff_tags(engine_settings)) == 0


def test_normal_correction_still_stages_with_mbid_alongside_placeholder(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # A placeholder value is dropped while a normal correction in the same run behaves
    # exactly as before, MBID enrichment included.
    make_track(music_dir / "ost.mp3", {"albumartist": ["Original Soundtrack"]})
    good = make_track(music_dir / "good.mp3", {"artist": ["Miami Nights '84"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {
            "Original Soundtrack": ArtistCorrection("[unknown]", "125ec42a-mbid"),
            "Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1"),
        },
    )
    result = artists.resolve_artists(engine_settings, client=fake)
    staging.commit_tags(engine_settings, origin="auto")

    assert result.corrected_values == 1
    assert result.staged_files == 1
    assert result.no_correction_values == ["Original Soundtrack"]
    assert read_tags(good).tags["artist"] == ["Miami Nights 1984"]
    assert read_tags(good).tags["musicbrainz_artistid"] == ["mbid-1"]


# --- (7c) the correction gate: only substantive, MusicBrainz-backed names stage ------


class _GateCase(NamedTuple):
    """One correction routed through the gate, and the single bucket it must land in."""

    value: str
    canonical: str
    mbid: str | None
    bucket: str
    staged: int


_GATE_CASES = [
    # A collapsed multi-artist credit is held whether or not MusicBrainz backs it.
    _GateCase("Skrillex & The Doors", "Skrillex", "mbid-skrillex", "shrinks_credit", 0),
    _GateCase("The Offspring & Redman", "The Offspring", None, "shrinks_credit", 0),
    # Last.fm casing is not trustworthy, so a case-only difference is already canonical.
    _GateCase("Dååth", "DÅÅTH", "mbid-daath", "already_canonical", 0),
    _GateCase("ChthoniC", "Chthonic", None, "already_canonical", 0),
    # A rename MusicBrainz does not corroborate is held for review, never silent.
    _GateCase("Travis Scott", "Travi$ Scott", None, "needs_review", 0),
    # Diacritics are a spelling fix, not casing — with an MBID it stages.
    _GateCase("Antonio Carlos Jobim", "Antônio Carlos Jobim", "mbid-jobim", "corrected_values", 1),
    _GateCase("Offspring", "The Offspring", "mbid-offspring", "corrected_values", 1),
]


@pytest.mark.parametrize("case", _GATE_CASES, ids=[c.value for c in _GATE_CASES])
def test_correction_gate_routes_each_class_to_exactly_one_bucket(
    engine_settings: Settings,
    music_dir: Path,
    case: _GateCase,
) -> None:
    make_track(music_dir / "t.mp3", {"artist": [case.value]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource({case.value: ArtistCorrection(case.canonical, case.mbid)})
    result = artists.resolve_artists(engine_settings, client=fake)

    expected = dict.fromkeys(_BUCKET_NAMES, 0)
    expected[case.bucket] = 1
    assert _buckets(result) == expected
    assert sum(_buckets(result).values()) == result.processed
    assert result.staged_files == case.staged
    assert len(staging.diff_tags(engine_settings)) == case.staged


def test_held_corrections_are_reported_with_from_and_to(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "shrink.mp3", {"artist": ["Sepultura with Mike Patton"]})
    make_track(music_dir / "review.mp3", {"artist": ["Travis Scott"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {
            "Sepultura with Mike Patton": ArtistCorrection("Sepultura", "mbid-sep"),
            "Travis Scott": ArtistCorrection("Travi$ Scott", None),
        },
    )
    result = artists.resolve_artists(engine_settings, client=fake)

    assert result.shrinks_credit_values == [
        {"from": "Sepultura with Mike Patton", "to": "Sepultura"},
    ]
    assert result.needs_review_values == [{"from": "Travis Scott", "to": "Travi$ Scott"}]
    assert result.mappings == []
    assert result.staged_files == 0
    assert len(staging.diff_tags(engine_settings)) == 0


# The value → correction pairs measured against the real library, one per gate outcome.
_LIVE_CORRECTIONS: dict[str, ArtistCorrection | None] = {
    "Kruder Dorfmeister": ArtistCorrection("Kruder & Dorfmeister", "mbid-kd"),
    "Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-mn"),
    "Offspring": ArtistCorrection("The Offspring", "mbid-off"),
    "Orb": ArtistCorrection("The Orb", "mbid-orb"),
    "Smashing Pumpkins": ArtistCorrection("The Smashing Pumpkins", "mbid-sp"),
    "Antonio Carlos Jobim": ArtistCorrection("Antônio Carlos Jobim", "mbid-acj"),
    "Skrillex & The Doors": ArtistCorrection("Skrillex", "mbid-skr"),
    "Ellie Goulding & Madeon": ArtistCorrection("Ellie Goulding", "mbid-eg"),
    "Dååth": ArtistCorrection("DÅÅTH", "mbid-daath"),
    "Course of Empire": ArtistCorrection("Course Of Empire", "mbid-coe"),
    "ChthoniC": ArtistCorrection("Chthonic", "mbid-cht"),
    "Travis Scott": ArtistCorrection("Travi$ Scott", None),
}

_LIVE_ACCEPTED = {
    "Kruder Dorfmeister": "Kruder & Dorfmeister",
    "Miami Nights '84": "Miami Nights 1984",
    "Offspring": "The Offspring",
    "Orb": "The Orb",
    "Smashing Pumpkins": "The Smashing Pumpkins",
    "Antonio Carlos Jobim": "Antônio Carlos Jobim",
}


def _make_live_library(music_dir: Path) -> None:
    for index, value in enumerate(_LIVE_CORRECTIONS):
        make_track(music_dir / f"t{index}.mp3", {"artist": [value]})


def test_gate_stages_only_the_verified_corrections_from_the_live_data(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_live_library(music_dir)
    scan_library(engine_settings)

    fake = FakeCorrectionSource(dict(_LIVE_CORRECTIONS))
    result = artists.resolve_artists(engine_settings, client=fake)

    assert {m["from"]: m["to"] for m in result.mappings} == _LIVE_ACCEPTED
    assert result.staged_files == len(_LIVE_ACCEPTED)
    assert _buckets(result) == {
        "corrected_values": 6,
        "already_canonical": 3,
        "shrinks_credit": 2,
        "needs_review": 1,
        "no_correction": 0,
        "errors": 0,
    }
    assert sum(_buckets(result).values()) == result.processed


def test_gate_dry_run_reports_identical_buckets_and_stages_nothing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_live_library(music_dir)
    scan_library(engine_settings)
    fake = FakeCorrectionSource(dict(_LIVE_CORRECTIONS))

    preview = artists.resolve_artists(engine_settings, client=fake, dry_run=True)
    assert len(staging.diff_tags(engine_settings)) == 0

    applied = artists.resolve_artists(engine_settings, client=fake)

    assert _buckets(preview) == _buckets(applied)
    assert preview.staged_files == applied.staged_files
    assert preview.mappings == applied.mappings
    assert preview.shrinks_credit_values == applied.shrinks_credit_values
    assert preview.needs_review_values == applied.needs_review_values


# --- (8) dry-run ---------------------------------------------------------------------


def test_dry_run_returns_mappings_but_stages_nothing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Miami Nights '84"]})
    make_track(music_dir / "b.mp3", {"artist": ["Miami Nights '84"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1")},
    )
    result = artists.resolve_artists(engine_settings, client=fake, dry_run=True)

    assert result.corrected_values == 1
    assert result.staged_files == 2  # would-stage count
    assert result.mappings == [
        {"from": "Miami Nights '84", "to": "Miami Nights 1984", "mbid": "mbid-1"},
    ]
    assert len(staging.diff_tags(engine_settings)) == 0  # nothing actually staged


def test_dry_run_ignores_empty_staging_precondition(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Miami Nights '84"]})
    other = make_track(music_dir / "other.mp3", {"artist": ["Someone"]})
    scan_library(engine_settings)

    # Stage an unrelated manual change so the staging area is non-empty.
    other_id = _file_id(engine_settings, music_dir, other.name)
    staging.stage_tags(
        engine_settings,
        file_id=other_id,
        managed_tags={"artist": ["Someone Else"]},
        origin="manual",
    )

    fake = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1")},
    )
    # dry_run must NOT raise despite pending changes.
    result = artists.resolve_artists(engine_settings, client=fake, dry_run=True)
    assert result.corrected_values == 1


# --- (9) empty-staging precondition --------------------------------------------------


def test_non_dry_run_requires_empty_staging(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"artist": ["Miami Nights '84"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    staging.stage_tags(
        engine_settings,
        file_id=file_id,
        managed_tags={"artist": ["Anything"]},
        origin="manual",
    )

    fake = FakeCorrectionSource({})
    with pytest.raises(ValueError, match="commit or unstage pending changes first"):
        artists.resolve_artists(engine_settings, client=fake)


# --- (10) limit / more loop ----------------------------------------------------------


def test_limit_caps_values_and_reports_pending(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Alpha '84"]})
    make_track(music_dir / "b.mp3", {"artist": ["Bravo '84"]})
    make_track(music_dir / "c.mp3", {"artist": ["Charlie '84"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {
            "Alpha '84": ArtistCorrection("Alpha 1984", "mbid-a"),
            "Bravo '84": ArtistCorrection("Bravo 1984", "mbid-b"),
            "Charlie '84": ArtistCorrection("Charlie 1984", "mbid-c"),
        },
    )
    first = artists.resolve_artists(engine_settings, client=fake, limit=2, dry_run=True)
    assert first.processed == 2
    assert first.pending_remaining == 1
    assert first.more is True


def test_two_identical_dry_runs_reprocess_the_same_values(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Alpha '84"]})
    make_track(music_dir / "b.mp3", {"artist": ["Bravo '84"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {
            "Alpha '84": ArtistCorrection("Alpha 1984", "mbid-a"),
            "Bravo '84": ArtistCorrection("Bravo 1984", "mbid-b"),
        },
    )
    first = artists.resolve_artists(engine_settings, client=fake, limit=1, dry_run=True)
    second = artists.resolve_artists(engine_settings, client=fake, limit=1, dry_run=True)

    # A dry run stages nothing, so the second call sees the identical frontier.
    assert first.to_dict() == second.to_dict()
    assert fake.lookups == ["Alpha '84", "Alpha '84"]
    assert second.pending_remaining == 1
    assert "call again to continue" not in second.summary
    assert "Raise limit above 1" in second.summary
    assert "artist= / file_ids=" in second.summary


def test_two_non_dry_runs_with_a_commit_between_do_not_advance_the_frontier(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Alpha '84"]})
    make_track(music_dir / "b.mp3", {"artist": ["Bravo '84"]})
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {
            "Alpha '84": ArtistCorrection("Alpha 1984", "mbid-a"),
            "Alpha 1984": ArtistCorrection("Alpha 1984", "mbid-a"),
            "Bravo '84": ArtistCorrection("Bravo 1984", "mbid-b"),
        },
    )
    first = artists.resolve_artists(engine_settings, client=fake, limit=1)
    assert first.mappings == [{"from": "Alpha '84", "to": "Alpha 1984", "mbid": "mbid-a"}]
    assert first.pending_remaining == 1
    staging.commit_tags(engine_settings, origin="auto")

    second = artists.resolve_artists(engine_settings, client=fake, limit=1)

    # The committed value is now canonical, so the limit re-spends itself on that same
    # value and "Bravo '84" is still out of reach.
    assert second.already_canonical_values == ["Alpha 1984"]
    assert second.corrected_values == 0
    assert second.pending_remaining == 1
    assert second.staged_files == 0
    assert "call again to continue" not in second.summary
    assert "Raise limit above 1" in second.summary


# --- (11) revert round-trip across all four formats ----------------------------------


# --- (12) sticky manual exclusion: skipped by resolve_artists ------------------------


def test_manual_excluded_file_is_skipped_and_reported(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    excluded = make_track(music_dir / "excluded.mp3", {"artist": ["Miami Nights '84"]})
    kept = make_track(music_dir / "kept.mp3", {"artist": ["Miami Nights '84"]})
    scan_library(engine_settings)
    excluded_id = _file_id(engine_settings, music_dir, excluded.name)
    kept_id = _file_id(engine_settings, music_dir, kept.name)

    affected = artists.set_artist_status(
        engine_settings,
        file_ids=[excluded_id],
        status="manual",
    )
    assert affected == 1

    fake = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1")},
    )
    result = artists.resolve_artists(engine_settings, client=fake)

    assert result.skipped_manual == 1
    assert result.manual_files == [excluded_id]
    # The non-excluded file with the same data still stages.
    assert result.staged_files == 1
    staged_ids = {v.file_id for v in staging.diff_tags(engine_settings)}
    assert staged_ids == {kept_id}


def test_set_artist_status_by_value_matches_albumartist(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # The value appears only as ``albumartist`` (artist is already canonical).
    track = make_track(
        music_dir / "comp.mp3",
        {"artist": ["DJ Canonical"], "albumartist": ["Miami Nights 84"]},
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    affected = artists.set_artist_status(
        engine_settings,
        value="Miami Nights 84",
        status="manual",
    )
    assert affected == 1

    view = next(v for v in library_list(engine_settings) if v.file_id == file_id)
    assert view.artist_status == "manual"
    assert view.artist_source_albumartist == "Miami Nights 84"


def test_manual_is_sticky_across_rerun(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"artist": ["Miami Nights '84"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    artists.set_artist_status(engine_settings, file_ids=[file_id], status="manual")

    fake = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1")},
    )
    first = artists.resolve_artists(engine_settings, client=fake)
    second = artists.resolve_artists(engine_settings, client=fake)
    assert first.skipped_manual == 1
    assert second.skipped_manual == 1
    assert len(staging.diff_tags(engine_settings)) == 0


def test_reset_artist_status_requeues(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"artist": ["Miami Nights '84"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    artists.set_artist_status(engine_settings, file_ids=[file_id], status="manual")

    assert artists.reset_artist_status(engine_settings, file_ids=[file_id]) == 1

    fake = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1")},
    )
    result = artists.resolve_artists(engine_settings, client=fake)
    assert result.skipped_manual == 0
    assert result.staged_files == 1


def test_reset_artist_status_by_value_matches_albumartist(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(
        music_dir / "comp.mp3",
        {"artist": ["DJ Canonical"], "albumartist": ["Miami Nights 84"]},
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    artists.set_artist_status(engine_settings, value="Miami Nights 84", status="manual")

    assert artists.reset_artist_status(engine_settings, value="Miami Nights 84") == 1
    view = next(v for v in library_list(engine_settings) if v.file_id == file_id)
    assert view.artist_status == "pending"


def test_set_artist_status_rejects_unknown_status(engine_settings: Settings) -> None:
    with pytest.raises(ValueError, match="invalid status"):
        artists.set_artist_status(engine_settings, file_ids=[1], status="no_match")


@pytest.mark.parametrize("suffix", _FORMATS)
def test_commit_then_revert_commit_restores_original_name(
    engine_settings: Settings,
    music_dir: Path,
    suffix: str,
) -> None:
    track = make_track(
        music_dir / f"track{suffix}",
        {"artist": ["Miami Nights '84"], "genre": ["synthwave"]},
    )
    scan_library(engine_settings)

    fake = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1")},
    )
    artists.resolve_artists(engine_settings, client=fake)
    commit_result = staging.commit_tags(engine_settings, origin="auto")
    assert commit_result.commit_id is not None

    on_disk = read_tags(track).tags
    assert on_disk["artist"] == ["Miami Nights 1984"]
    assert on_disk["musicbrainz_artistid"] == ["mbid-1"]
    # Genre is preserved through the commit.
    assert on_disk["genre"] == ["synthwave"]

    versioning.revert_commit(engine_settings, commit_result.commit_id)

    restored = read_tags(track).tags
    assert restored["artist"] == ["Miami Nights '84"]
    assert restored["genre"] == ["synthwave"]
