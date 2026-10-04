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

import dataclasses
import unicodedata
from typing import TYPE_CHECKING, NamedTuple

import mutagen
import pytest

from conftest import make_rvad_mp3, make_track, rejecting_lastfm_client
from tagmend.engine import artists, axis, axis_status, staging, store, versioning
from tagmend.engine.db import connect
from tagmend.engine.lastfm import ArtistCorrection, LastfmError, LastfmKeyError
from tagmend.engine.library import list_files as library_list
from tagmend.engine.library import scan_library
from tagmend.engine.musicbrainz import MBArtist, MusicBrainzError
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


def _artist_outcome(settings: Settings, file_id: int) -> axis.OutcomeRow | None:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return axis.get_outcome(conn, axis.ARTIST_AXIS, file_id)
    finally:
        conn.close()


# The per-value outcome buckets: every value looked up lands in exactly one.
_BUCKET_NAMES = (
    "corrected_values",
    "already_canonical",
    "shrinks_credit",
    "needs_review",
    "name_id_disagreement",
    "no_correction",
    "errors",
)


def _buckets(result: artists.ResolveArtistsResult) -> dict[str, int]:
    return {
        "corrected_values": result.corrected_values,
        "already_canonical": result.already_canonical,
        "shrinks_credit": result.shrinks_credit,
        "needs_review": result.needs_review,
        "name_id_disagreement": result.name_id_disagreement,
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
    staging.commit_tags(engine_settings)

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

    # The blank-artist file has no artist identity, so it is never selected and its value
    # never reaches the guard count.
    assert result.skipped_sentinel == 2
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

    # An already-canonical value is visible (not silently invisible) and its file settles.
    assert first.settled == 1
    assert first.corrected_values == 0
    assert first.no_correction == 0
    assert first.already_canonical == 1
    assert first.already_canonical_values == ["Daft Punk"]
    assert sum(_buckets(first).values()) == 1
    assert "1 already canonical" in first.summary

    # The file is done, so a re-run selects nothing and looks nothing up.
    second = artists.resolve_artists(engine_settings, client=fake)
    assert second.settled == 0
    assert fake.lookups == ["Daft Punk"]


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
    # The commit stamps mbid-1 onto the file, so the re-run reaches the MusicBrainz tier.
    mb = FakeArtistSource({"mbid-1": _mb("Miami Nights 1984", mbid="mbid-1")})
    artists.resolve_artists(engine_settings, client=fake, mb_client=mb)
    staging.commit_tags(engine_settings)
    scan_library(engine_settings)

    # The canonical value now equals the correction → no further change.
    second = artists.resolve_artists(engine_settings, client=fake, mb_client=mb)
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


class _FailingCorrectionSource(FakeCorrectionSource):
    """A :class:`FakeCorrectionSource` whose lookup for the given values always fails."""

    def __init__(self, table: dict[str, ArtistCorrection | None], failing: set[str]) -> None:
        super().__init__(table)
        self._failing = failing

    def artist_correction(self, name: str) -> ArtistCorrection | None:
        if name in self._failing:
            message = "transport error"
            raise LastfmError(message)
        return super().artist_correction(name)


def test_lookup_error_is_counted_and_itemized(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t.mp3", {"artist": ["Obscure Band"]})
    scan_library(engine_settings)

    fake = _FailingCorrectionSource({}, failing={"Obscure Band"})
    result = artists.resolve_artists(engine_settings, client=fake)

    assert result.errors == 1
    assert result.error_items == [{"key": "Obscure Band", "message": "transport error"}]
    assert result.to_dict()["error_items"] == result.error_items
    assert result.staged_files == 0
    # A transient error writes no outcome row, so the file stays pending for a re-run.
    assert _artist_outcome(engine_settings, _file_id(engine_settings, music_dir, "t.mp3")) is None


def test_a_rejected_lastfm_key_stops_the_call(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "t.mp3", {"artist": ["Obscure Band"]})
    make_track(music_dir / "u.mp3", {"artist": ["Other Band"]})
    scan_library(engine_settings)

    conn = connect(engine_settings.db_path)
    try:
        with (
            rejecting_lastfm_client(conn) as client,
            pytest.raises(LastfmKeyError, match="lastfm_api_key"),
        ):
            artists.resolve_artists(engine_settings, client=client)
    finally:
        conn.close()

    assert staging.diff_tags(engine_settings) == []


def test_result_follows_the_resolver_contract(engine_settings: Settings) -> None:
    result = artists.resolve_artists(engine_settings, client=FakeCorrectionSource({}))

    keys = set(result.to_dict())
    assert {"settled", "staged_files", "errors", "error_items", "pending_remaining"} <= keys
    assert {"more", "summary", "mappings"} <= keys
    assert not keys & {"processed", "processed_unit", "skipped_manual", "skipped_missing"}


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
    staging.commit_tags(engine_settings)

    assert result.corrected_values == 1
    assert result.staged_files == 1
    assert result.no_correction_values == ["Original Soundtrack"]
    assert read_tags(good).tags["artist"] == ["Miami Nights 1984"]
    assert read_tags(good).tags["musicbrainz_artistid"] == ["mbid-1"]


# --- (7c) the correction gate: a substantive name Last.fm pairs with an MBID stages --


class _GateCase(NamedTuple):
    """One correction routed through the gate, and the single bucket it must land in."""

    value: str
    canonical: str
    mbid: str | None
    bucket: str
    staged: int


_GATE_CASES = [
    # A collapsed multi-artist credit is held whether or not Last.fm pairs it with an MBID.
    _GateCase("Skrillex & The Doors", "Skrillex", "mbid-skrillex", "shrinks_credit", 0),
    _GateCase("The Offspring & Redman", "The Offspring", None, "shrinks_credit", 0),
    # Last.fm casing is not trustworthy, so a case-only difference is already canonical.
    _GateCase("Dååth", "DÅÅTH", "mbid-daath", "already_canonical", 0),
    _GateCase("ChthoniC", "Chthonic", None, "already_canonical", 0),
    # A rename Last.fm pairs with no MBID is held for review, never silent.
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
        "name_id_disagreement": 0,
        "no_correction": 0,
        "errors": 0,
    }
    assert sum(_buckets(result).values()) == len(_LIVE_CORRECTIONS)


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
        {
            "from": "Miami Nights '84",
            "to": "Miami Nights 1984",
            "mbid": "mbid-1",
            "source": "lastfm",
        },
    ]
    assert len(staging.diff_tags(engine_settings)) == 0  # nothing actually staged


def test_dry_run_itemizes_a_file_the_writer_refuses(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "plain.mp3", {"artist": ["Miami Nights '84"]})
    make_rvad_mp3(music_dir / "loud.mp3", {"artist": ["Miami Nights '84"]})
    scan_library(engine_settings)
    loud_id = _file_id(engine_settings, music_dir, "loud.mp3")
    fake = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1")},
    )

    preview = artists.resolve_artists(engine_settings, client=fake, dry_run=True)
    real = artists.resolve_artists(engine_settings, client=fake)

    assert preview.staged_files == real.staged_files == 1
    assert [item["key"] for item in preview.error_items] == [f"file_id={loud_id}"]
    assert "RVAD" in preview.error_items[0]["message"]
    assert preview.error_items == real.error_items


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
        tags={"artist": ["Someone Else"]},
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
        tags={"artist": ["Anything"]},
        origin="manual",
    )

    fake = FakeCorrectionSource({})
    with pytest.raises(ValueError, match="commit or unstage pending changes first"):
        artists.resolve_artists(engine_settings, client=fake)


# --- (10) limit / more loop ----------------------------------------------------------


def test_limit_caps_files_and_reports_pending(
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
    first = artists.resolve_artists(engine_settings, client=fake, limit=2)
    assert first.settled == 2
    assert first.staged_files == 2
    assert first.pending_remaining == 1
    assert first.more is True


def test_an_omitted_limit_caps_the_selection_at_the_setting(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        make_track(music_dir / name, {"artist": [f"{name[0].upper()} '84"]})
    scan_library(engine_settings)
    capped = dataclasses.replace(engine_settings, artist_stage_limit=2)
    fake = FakeCorrectionSource({})

    result = artists.resolve_artists(capped, client=fake)

    assert (result.settled, result.pending_remaining, result.more) == (2, 1, True)
    assert fake.lookups == ["A '84", "B '84"]
    assert "Call again to continue" in result.summary


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

    # A dry run records nothing, so the second call sees the identical frontier.
    assert first.to_dict() == second.to_dict()
    assert fake.lookups == ["Alpha '84", "Alpha '84"]
    assert second.pending_remaining == 2
    assert second.more is False
    assert "Call again to continue" not in second.summary
    assert "A dry run records nothing" in second.summary


def test_two_capped_runs_with_a_commit_between_advance_the_frontier(
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
    # The commit stamps mbid-a onto the file, so the second run reaches the MusicBrainz
    # tier: it must be faked too, or the test would call the live API.
    mb = FakeArtistSource({"mbid-a": _mb("Alpha 1984", mbid="mbid-a")})
    first = artists.resolve_artists(engine_settings, client=fake, mb_client=mb, limit=1)
    assert first.mappings == [
        {"from": "Alpha '84", "to": "Alpha 1984", "mbid": "mbid-a", "source": "lastfm"},
    ]
    assert first.pending_remaining == 1
    staging.commit_tags(engine_settings)

    second = artists.resolve_artists(engine_settings, client=fake, mb_client=mb, limit=1)

    # The corrected file is done, so the limit is spent on the next pending file.
    assert second.mappings == [
        {"from": "Bravo '84", "to": "Bravo 1984", "mbid": "mbid-b", "source": "lastfm"},
    ]
    assert second.settled == 1
    assert second.pending_remaining == 0
    assert second.more is False
    assert mb.lookups == []


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

    affected = axis_status.set_manual_status(
        engine_settings, axis.ARTIST_AXIS, file_ids=[excluded_id], value=None, status="manual"
    )
    assert affected == 1

    fake = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1")},
    )
    result = artists.resolve_artists(engine_settings, client=fake)

    # The manual file is never selected and no cascade stages on it.
    assert result.settled == 1
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

    affected = axis_status.set_manual_status(
        engine_settings, axis.ARTIST_AXIS, file_ids=None, value="Miami Nights 84", status="manual"
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
    axis_status.set_manual_status(
        engine_settings, axis.ARTIST_AXIS, file_ids=[file_id], value=None, status="manual"
    )

    fake = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1")},
    )
    first = artists.resolve_artists(engine_settings, client=fake)
    second = artists.resolve_artists(engine_settings, client=fake)
    assert first.settled == 0
    assert second.settled == 0
    assert fake.lookups == []
    assert len(staging.diff_tags(engine_settings)) == 0


def test_reset_artist_status_requeues(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "t.mp3", {"artist": ["Miami Nights '84"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)
    axis_status.set_manual_status(
        engine_settings, axis.ARTIST_AXIS, file_ids=[file_id], value=None, status="manual"
    )

    assert (
        axis_status.reset_status(engine_settings, axis.ARTIST_AXIS, file_ids=[file_id], value=None)
        == 1
    )

    fake = FakeCorrectionSource(
        {"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "mbid-1")},
    )
    result = artists.resolve_artists(engine_settings, client=fake)
    assert result.settled == 1
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
    axis_status.set_manual_status(
        engine_settings, axis.ARTIST_AXIS, file_ids=None, value="Miami Nights 84", status="manual"
    )

    assert (
        axis_status.reset_status(
            engine_settings, axis.ARTIST_AXIS, file_ids=None, value="Miami Nights 84"
        )
        == 1
    )
    view = next(v for v in library_list(engine_settings) if v.file_id == file_id)
    assert view.artist_status == "pending"


@pytest.mark.parametrize("status", ["no_match", "pending"])
def test_set_artist_status_accepts_manual_only(engine_settings: Settings, status: str) -> None:
    with pytest.raises(ValueError, match="unknown status"):
        axis_status.set_manual_status(
            engine_settings, axis.ARTIST_AXIS, file_ids=[1], value=None, status=status
        )


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
    commit_result = staging.commit_tags(engine_settings)
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


# --- (11) the MusicBrainz name tier ---------------------------------------------------
#
# The tier that runs BEFORE Last.fm. Its key is the ``musicbrainz_artistid`` the file
# already carries, so it is a direct lookup with no candidate ranking. Unlike Last.fm, its
# casing IS trusted, so a case-only difference from the canonical name is staged here and
# ignored there.


class FakeArtistSource:
    """An in-memory :class:`tagmend.engine.musicbrainz.MBArtistSource` for DI in tests.

    Maps an MBID -> :class:`MBArtist` (or ``None`` for "MusicBrainz has no such artist").
    Records the lookups it received so tests can assert what was queried.
    """

    def __init__(self, table: dict[str, MBArtist | None]) -> None:
        self._table = table
        self.lookups: list[str] = []

    def artist_by_mbid(self, mbid: str) -> MBArtist | None:
        self.lookups.append(mbid)
        return self._table.get(mbid)


def _mb(name: str, *aliases: str, mbid: str = "mbid-1", sort_name: str = "") -> MBArtist:
    """Build an :class:`MBArtist` with *name* canonical and *aliases* registered."""
    return MBArtist(
        mbid=mbid,
        name=name,
        sort_name=sort_name or name,
        disambiguation="",
        aliases=tuple(aliases),
    )


def test_mb_tier_fixes_casing_that_the_lastfm_tier_would_ignore(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "a.mp3",
        {"artist": ["SOLAR FIELDS"], "musicbrainz_artistid": ["mbid-1"]},
    )
    scan_library(engine_settings)

    mb = FakeArtistSource({"mbid-1": _mb("Solar Fields")})
    lastfm = FakeCorrectionSource({})
    result = artists.resolve_artists(engine_settings, client=lastfm, mb_client=mb)

    assert result.corrected_values == 1
    assert result.staged_files == 1
    assert result.mappings == [
        {"from": "SOLAR FIELDS", "to": "Solar Fields", "mbid": "mbid-1", "source": "musicbrainz"},
    ]
    # The MBID answered it, so Last.fm was never asked.
    assert lastfm.lookups == []


class _FailingArtistSource(FakeArtistSource):
    """A :class:`FakeArtistSource` whose every lookup fails transiently."""

    def artist_by_mbid(self, mbid: str) -> MBArtist | None:
        self.lookups.append(mbid)
        message = "transport error"
        raise MusicBrainzError(message)


def test_mb_tier_transient_error_keeps_the_value_from_lastfm(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "a.mp3",
        {"artist": ["Solar Feelds"], "musicbrainz_artistid": ["mbid-1"]},
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "a.mp3")

    lastfm = FakeCorrectionSource({"Solar Feelds": ArtistCorrection("Solar Fields", "other-mbid")})
    mb = _FailingArtistSource({})
    result = artists.resolve_artists(engine_settings, client=lastfm, mb_client=mb)

    assert mb.lookups == ["mbid-1"]
    # A Last.fm answer would overwrite the file's own MBID with Last.fm's id.
    assert lastfm.lookups == []
    assert result.errors == 1
    assert result.error_items == [{"key": "Solar Feelds", "message": "transport error"}]
    assert result.staged_files == 0
    assert _artist_outcome(engine_settings, file_id) is None


def test_mb_tier_merges_a_registered_alias_onto_the_canonical_name(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "a.mp3",
        {"artist": ["Smashing Pumpkins"], "musicbrainz_artistid": ["mbid-1"]},
    )
    scan_library(engine_settings)

    mb = FakeArtistSource({"mbid-1": _mb("The Smashing Pumpkins", "Smashing Pumpkins")})
    result = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=mb,
    )

    assert result.corrected_values == 1
    assert result.mappings[0]["to"] == "The Smashing Pumpkins"
    assert result.mappings[0]["source"] == "musicbrainz_alias"


def test_mb_tier_matches_an_alias_under_typographic_folding(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # The tag carries an ASCII hyphen where MusicBrainz's alias uses U+2010, and lowercases
    # a letter. Same name, different glyphs, so it merges onto MusicBrainz's spelling.
    make_track(
        music_dir / "a.mp3",
        {"artist": ["jean-michel jarre"], "musicbrainz_artistid": ["mbid-1"]},
    )
    scan_library(engine_settings)

    mb = FakeArtistSource({"mbid-1": _mb("Jean\u2010Michel Jarre", "Jean Michel Jarre")})
    result = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=mb,
    )

    assert result.corrected_values == 1
    assert result.mappings[0]["to"] == "Jean\u2010Michel Jarre"
    assert result.mappings[0]["source"] == "musicbrainz"


def test_mb_tier_stages_a_decomposed_spelling_of_the_canonical_name(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    composed = unicodedata.normalize("NFC", "Björk")
    make_track(
        music_dir / "a.mp3",
        {
            "artist": [unicodedata.normalize("NFD", composed)],
            "musicbrainz_artistid": ["mbid-1"],
        },
    )
    scan_library(engine_settings)

    result = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=FakeArtistSource({"mbid-1": _mb(composed)}),
    )

    assert [(m["to"], m["source"]) for m in result.mappings] == [(composed, "musicbrainz")]
    assert result.name_id_disagreement_values == []


def test_lastfm_tier_reads_a_decomposed_casing_difference_as_canonical(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    decomposed = unicodedata.normalize("NFD", "björk")
    make_track(music_dir / "a.mp3", {"artist": [decomposed]})
    scan_library(engine_settings)

    correction = ArtistCorrection(unicodedata.normalize("NFC", "Björk"), "mbid-1")
    fake = FakeCorrectionSource({decomposed: correction})
    result = artists.resolve_artists(engine_settings, client=fake)

    assert result.already_canonical_values == [decomposed]
    assert result.staged_files == 0


def test_mb_tier_leaves_an_exactly_canonical_name_alone(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "a.mp3",
        {"artist": ["Solar Fields"], "musicbrainz_artistid": ["mbid-1"]},
    )
    scan_library(engine_settings)

    mb = FakeArtistSource({"mbid-1": _mb("Solar Fields")})
    result = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=mb,
    )

    assert result.already_canonical == 1
    assert result.staged_files == 0


def test_mb_tier_holds_a_credit_that_collapses_onto_one_member(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # "Linkin Park & Adema" carries Linkin Park's MBID and is not a registered alias, so the
    # canonical name is a strict substring: a real multi-artist credit, never auto-collapsed.
    make_track(
        music_dir / "a.mp3",
        {"artist": ["Linkin Park & Adema"], "musicbrainz_artistid": ["mbid-1"]},
    )
    scan_library(engine_settings)

    mb = FakeArtistSource({"mbid-1": _mb("Linkin Park")})
    result = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=mb,
    )

    assert result.shrinks_credit == 1
    assert result.staged_files == 0
    assert result.shrinks_credit_values == [
        {"from": "Linkin Park & Adema", "to": "Linkin Park"},
    ]


def test_mb_tier_reports_a_name_that_is_no_name_for_its_own_mbid(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # The forensic case: the file names one artist and points its MBID at another. Neither a
    # credit nor an alias, so it is reported and never staged.
    make_track(
        music_dir / "a.mp3",
        {"artist": ["Tattooed Corpse"], "musicbrainz_artistid": ["mbid-1"]},
    )
    scan_library(engine_settings)

    mb = FakeArtistSource({"mbid-1": _mb("Emily Browning")})
    result = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=mb,
    )

    assert result.name_id_disagreement == 1
    assert result.staged_files == 0
    assert result.name_id_disagreement_values == [
        {
            "from": "Tattooed Corpse",
            "to": "Emily Browning",
            "mbids": ["mbid-1"],
            "reason": "no name MusicBrainz records for this id",
        },
    ]


def test_a_value_with_no_mbid_still_falls_through_to_the_lastfm_tier(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Offspring"]})
    scan_library(engine_settings)

    mb = FakeArtistSource({})
    lastfm = FakeCorrectionSource({"Offspring": ArtistCorrection("The Offspring", "mbid-9")})
    result = artists.resolve_artists(engine_settings, client=lastfm, mb_client=mb)

    assert result.corrected_values == 1
    assert result.mappings[0]["source"] == "lastfm"
    assert lastfm.lookups == ["Offspring"]
    assert mb.lookups == []


def test_an_mbid_musicbrainz_does_not_know_falls_through_to_the_lastfm_tier(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "a.mp3",
        {"artist": ["Offspring"], "musicbrainz_artistid": ["mbid-gone"]},
    )
    scan_library(engine_settings)

    mb = FakeArtistSource({"mbid-gone": None})
    lastfm = FakeCorrectionSource({"Offspring": ArtistCorrection("The Offspring", "mbid-9")})
    result = artists.resolve_artists(engine_settings, client=lastfm, mb_client=mb)

    assert mb.lookups == ["mbid-gone"]
    assert lastfm.lookups == ["Offspring"]
    assert result.corrected_values == 1
    assert result.mappings[0]["source"] == "lastfm"


def test_a_value_carrying_two_different_mbids_is_reported_not_staged(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["Ambiguous"], "musicbrainz_artistid": ["mbid-1"]})
    make_track(music_dir / "b.mp3", {"artist": ["Ambiguous"], "musicbrainz_artistid": ["mbid-2"]})
    scan_library(engine_settings)

    mb = FakeArtistSource({"mbid-1": _mb("One"), "mbid-2": _mb("Two", mbid="mbid-2")})
    lastfm = FakeCorrectionSource({"Ambiguous": ArtistCorrection("Something Else", "mbid-3")})
    result = artists.resolve_artists(engine_settings, client=lastfm, mb_client=mb)

    assert result.staged_files == 0
    assert result.name_id_disagreement == 1
    assert result.name_id_disagreement_values == [
        {
            "from": "Ambiguous",
            "to": None,
            "mbids": ["mbid-1", "mbid-2"],
            "reason": "the library pairs this name with more than one MusicBrainz id",
        },
    ]
    # Neither tier may act on a value the library cannot even identify consistently.
    assert mb.lookups == []
    assert lastfm.lookups == []


def test_mb_tier_pairs_albumartist_with_the_albumartist_mbid(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "a.mp3",
        {
            "artist": ["Solo Act"],
            "musicbrainz_artistid": ["mbid-track"],
            "albumartist": ["VARIOUS BAND"],
            "musicbrainz_albumartistid": ["mbid-album"],
        },
    )
    scan_library(engine_settings)

    mb = FakeArtistSource(
        {
            "mbid-track": _mb("Solo Act", mbid="mbid-track"),
            "mbid-album": _mb("Various Band", mbid="mbid-album"),
        },
    )
    result = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=mb,
    )

    assert sorted(mb.lookups) == ["mbid-album", "mbid-track"]
    assert result.corrected_values == 1
    view = next(iter(staging.diff_tags(engine_settings)))
    assert view.diff["albumartist"] == {"from": ["VARIOUS BAND"], "to": ["Various Band"]}
    assert "artist" not in view.diff


def test_an_albumartist_only_correction_never_stamps_the_track_artist_id(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # The two id fields describe different artists. A correction to one must never write the
    # other's id, which would silently rebind the track artist.
    make_track(
        music_dir / "a.mp3",
        {
            "artist": ["Guest Singer"],
            "musicbrainz_artistid": ["mbid-track"],
            "albumartist": ["Headline Act"],
        },
    )
    scan_library(engine_settings)

    lastfm = FakeCorrectionSource(
        {
            "Guest Singer": None,
            "Headline Act": ArtistCorrection("The Headline Act", "mbid-album"),
        },
    )
    result = artists.resolve_artists(
        engine_settings,
        client=lastfm,
        mb_client=FakeArtistSource({}),
    )

    assert result.staged_files == 1
    view = next(iter(staging.diff_tags(engine_settings)))
    assert view.diff["albumartist"] == {"from": ["Headline Act"], "to": ["The Headline Act"]}
    assert view.diff["musicbrainz_albumartistid"] == {"from": [], "to": ["mbid-album"]}
    assert "musicbrainz_artistid" not in view.diff


def test_mb_tier_buckets_still_sum_to_the_values_looked_up(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["SOLAR FIELDS"], "musicbrainz_artistid": ["m1"]})
    make_track(music_dir / "b.mp3", {"artist": ["Solar Fields"], "musicbrainz_artistid": ["m1"]})
    make_track(music_dir / "c.mp3", {"artist": ["Korn & Nas"], "musicbrainz_artistid": ["m2"]})
    make_track(music_dir / "d.mp3", {"artist": ["Wrong Name"], "musicbrainz_artistid": ["m3"]})
    make_track(music_dir / "e.mp3", {"artist": ["No Identity Here"]})
    scan_library(engine_settings)

    mb = FakeArtistSource(
        {
            "m1": _mb("Solar Fields", mbid="m1"),
            "m2": _mb("Korn", mbid="m2"),
            "m3": _mb("Somebody Else", mbid="m3"),
        },
    )
    result = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=mb,
    )

    buckets = _buckets(result)
    assert sum(buckets.values()) == 5
    assert buckets["corrected_values"] == 1
    assert buckets["already_canonical"] == 1
    assert buckets["shrinks_credit"] == 1
    assert buckets["name_id_disagreement"] == 1
    assert buckets["no_correction"] == 1


def test_mb_tier_dry_run_reports_the_same_buckets_and_stages_nothing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["SOLAR FIELDS"], "musicbrainz_artistid": ["m1"]})
    scan_library(engine_settings)

    mb = FakeArtistSource({"m1": _mb("Solar Fields", mbid="m1")})
    preview = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=mb,
        dry_run=True,
    )

    assert preview.corrected_values == 1
    assert preview.staged_files == 1
    assert list(staging.diff_tags(engine_settings)) == []

    real = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=FakeArtistSource({"m1": _mb("Solar Fields", mbid="m1")}),
    )
    assert _buckets(real) == _buckets(preview)
    assert real.staged_files == preview.staged_files


def test_mb_tier_skips_a_manual_file_like_every_other_tier(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["SOLAR FIELDS"], "musicbrainz_artistid": ["m1"]})
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, "a.mp3")
    axis_status.set_manual_status(
        engine_settings, axis.ARTIST_AXIS, file_ids=[file_id], value=None, status="manual"
    )

    mb = FakeArtistSource({"m1": _mb("Solar Fields", mbid="m1")})
    result = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=mb,
    )

    assert result.settled == 0
    assert result.staged_files == 0
    assert mb.lookups == []


def test_mb_tier_treats_a_dash_and_a_space_as_the_same_separator(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # 16 real files carry U+2010 HYPHEN where MusicBrainz writes a space.
    # Navidrome folds the dash to ASCII but never to a space, so the two stay two artists.
    make_track(
        music_dir / "a.mp3",
        {"artist": ["Mindless Self\u2010Indulgence"], "musicbrainz_artistid": ["m1"]},
    )
    scan_library(engine_settings)

    mb = FakeArtistSource({"m1": _mb("Mindless Self Indulgence", mbid="m1")})
    result = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=mb,
    )

    assert result.corrected_values == 1
    assert result.mappings[0]["to"] == "Mindless Self Indulgence"


def test_mb_tier_still_refuses_a_name_that_is_not_this_artist(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # The widened separator fold must not turn an unrelated name into a match.
    make_track(music_dir / "a.mp3", {"artist": ["ATOI"], "musicbrainz_artistid": ["m1"]})
    scan_library(engine_settings)

    mb = FakeArtistSource({"m1": _mb("Ambient Temple of Imagination", mbid="m1")})
    result = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=mb,
    )

    assert result.name_id_disagreement == 1
    assert result.staged_files == 0


# --- the sort name travels with the name it sorts ------------------------------------


def test_mb_tier_writes_the_sort_name_alongside_the_name(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # A rewritten name leaves its sort name describing the OLD spelling, and a server keeps a
    # stored sort name even after the tag goes empty, so the stale one has to be replaced in
    # the same commit rather than cleared later.
    make_track(
        music_dir / "a.mp3",
        {
            "artist": ["Smashing Pumpkins"],
            "artistsort": ["Smashing Pumpkins"],
            "musicbrainz_artistid": ["m1"],
        },
    )
    scan_library(engine_settings)

    mb = FakeArtistSource(
        {
            "m1": _mb(
                "The Smashing Pumpkins",
                "Smashing Pumpkins",
                mbid="m1",
                sort_name="Smashing Pumpkins, The",
            ),
        },
    )
    result = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=mb,
    )

    assert result.staged_files == 1
    view = next(iter(staging.diff_tags(engine_settings)))
    assert view.diff["artist"]["to"] == ["The Smashing Pumpkins"]
    assert view.diff["artistsort"]["to"] == ["Smashing Pumpkins, The"]


def test_mb_tier_writes_the_albumartist_sort_name_to_its_own_field(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "a.mp3",
        {
            "artist": ["Guest"],
            "albumartist": ["Smashing Pumpkins"],
            "musicbrainz_albumartistid": ["m1"],
        },
    )
    scan_library(engine_settings)

    mb = FakeArtistSource(
        {
            "m1": _mb(
                "The Smashing Pumpkins",
                "Smashing Pumpkins",
                mbid="m1",
                sort_name="Smashing Pumpkins, The",
            ),
        },
    )
    artists.resolve_artists(engine_settings, client=FakeCorrectionSource({}), mb_client=mb)

    view = next(iter(staging.diff_tags(engine_settings)))
    assert view.diff["albumartistsort"]["to"] == ["Smashing Pumpkins, The"]
    assert "artistsort" not in view.diff


def test_the_lastfm_tier_leaves_the_sort_name_alone(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # Last.fm has no sort name, so it has no authority to replace one. Guessing, or clearing
    # what is there, would destroy a value on nothing.
    make_track(
        music_dir / "a.mp3",
        {"artist": ["Offspring"], "artistsort": ["Offspring"]},
    )
    scan_library(engine_settings)

    lastfm = FakeCorrectionSource({"Offspring": ArtistCorrection("The Offspring", "m9")})
    artists.resolve_artists(engine_settings, client=lastfm, mb_client=FakeArtistSource({}))

    view = next(iter(staging.diff_tags(engine_settings)))
    assert view.diff["artist"]["to"] == ["The Offspring"]
    assert "artistsort" not in view.diff


def _edit_title_on_disk(path: Path, title: str) -> None:
    """Change ``title`` the way an external tagger would, with no rescan afterwards."""
    audio = mutagen.File(path, easy=True)  # type: ignore[attr-defined]
    audio["title"] = [title]
    audio.save()


def _committed_diff_fields(settings: Settings, commit_id: int | None) -> list[set[str]]:
    assert commit_id is not None
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return [set(revision.diff) for revision in store.revisions_for_commit(conn, commit_id)]
    finally:
        conn.close()


def test_resolve_artists_keeps_disk_values_the_mirror_lacks(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(
        music_dir / "a.mp3",
        {"artist": ["Miami Nights '84"], "album": ["Turbulence"], "title": ["Old Title"]},
    )
    scan_library(engine_settings)
    _edit_title_on_disk(track, "Title Edited In Picard")

    fake = FakeCorrectionSource({"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "m1")})
    artists.resolve_artists(engine_settings, client=fake)
    result = staging.commit_tags(engine_settings)

    on_disk = read_tags(track).tags
    assert on_disk["title"] == ["Title Edited In Picard"]
    assert on_disk["artist"] == ["Miami Nights 1984"]
    assert _committed_diff_fields(engine_settings, result.commit_id) == [
        {"artist", "musicbrainz_artistid"},
    ]


def test_resolve_artists_skips_missing_files(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    kept = make_track(music_dir / "kept.mp3", {"artist": ["Miami Nights '84"]})
    gone = make_track(music_dir / "gone.mp3", {"artist": ["Miami Nights '84"]})
    scan_library(engine_settings)
    gone.unlink()
    scan_library(engine_settings)  # flags the deleted file missing

    fake = FakeCorrectionSource({"Miami Nights '84": ArtistCorrection("Miami Nights 1984", "m1")})
    result = artists.resolve_artists(engine_settings, client=fake)

    # A missing file is never selected, never a cascade carrier and never counted pending.
    assert result.settled == 1
    assert result.pending_remaining == 0
    assert result.staged_files == 1
    assert [view.filename for view in staging.diff_tags(engine_settings)] == [kept.name]


def test_a_file_that_cannot_be_staged_is_reported_and_stays_pending(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(music_dir / "a.mp3", {"artist": ["foo"]})
    gone = make_track(music_dir / "b.mp3", {"artist": ["foo"]})
    make_track(music_dir / "c.mp3", {"artist": ["foo"]})
    scan_library(engine_settings)
    ids = {name: _file_id(engine_settings, music_dir, name) for name in ("a.mp3", "b.mp3", "c.mp3")}
    gone.unlink()  # no rescan, so the file is still a carrier

    fake = FakeCorrectionSource({"foo": ArtistCorrection("Foo Canon", "mbid-foo")})
    result = artists.resolve_artists(engine_settings, client=fake)

    assert result.staged_files == 2
    assert [item["key"] for item in result.error_items] == [f"file_id={ids['b.mp3']}"]
    conn = connect(engine_settings.db_path)
    try:
        apply_schema(conn)
        outcomes = {
            name: axis.get_outcome(conn, axis.ARTIST_AXIS, file_id) for name, file_id in ids.items()
        }
    finally:
        conn.close()
    assert outcomes["b.mp3"] is None
    assert outcomes["a.mp3"] is not None
    assert outcomes["a.mp3"].status == "done"
    assert outcomes["c.mp3"] is not None
    assert outcomes["c.mp3"].status == "done"


# --- (12) the multi-value artists list ---------------------------------------------
#
# A library server builds its artist entities from ``artists`` when a file carries it, so each
# element is a name value of its own, aligned with ``musicbrainz_artistid`` by position.


@pytest.mark.parametrize("suffix", _FORMATS)
def test_an_artist_correction_renames_the_equal_artists_element(
    engine_settings: Settings,
    music_dir: Path,
    suffix: str,
) -> None:
    # The live drift: an artist-only rewrite left the server linking the old spelling.
    track = make_track(
        music_dir / f"a{suffix}",
        {
            "artist": ["Bryan El"],
            "artists": ["Bryan El"],
            "musicbrainz_artistid": ["mbid-bryan"],
        },
    )
    scan_library(engine_settings)

    mb = FakeArtistSource({"mbid-bryan": _mb("Bryan EL", mbid="mbid-bryan")})
    result = artists.resolve_artists(
        engine_settings,
        client=FakeCorrectionSource({}),
        mb_client=mb,
    )
    staging.commit_tags(engine_settings)

    assert result.staged_files == 1
    on_disk = read_tags(track).tags
    assert on_disk["artist"] == ["Bryan EL"]
    assert on_disk["artists"] == ["Bryan EL"]
    assert on_disk["musicbrainz_artistid"] == ["mbid-bryan"]


def test_a_collaboration_element_is_resolved_by_its_aligned_mbid(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(
        music_dir / "a.flac",
        {
            "artist": ["A & B"],
            "artists": ["A", "b"],
            "musicbrainz_artistid": ["id-a", "id-b"],
        },
    )
    scan_library(engine_settings)

    mb = FakeArtistSource({"id-a": _mb("A", mbid="id-a"), "id-b": _mb("B", mbid="id-b")})
    lastfm = FakeCorrectionSource({})
    result = artists.resolve_artists(engine_settings, client=lastfm, mb_client=mb)
    staging.commit_tags(engine_settings)

    # The joined credit pairs with no single id, so only Last.fm sees it.
    assert sorted(mb.lookups) == ["id-a", "id-b"]
    assert lastfm.lookups == ["A & B"]
    assert result.mappings == [
        {"from": "b", "to": "B", "mbid": "id-b", "source": "musicbrainz"},
    ]
    on_disk = read_tags(track).tags
    assert on_disk["artists"] == ["A", "B"]
    assert on_disk["artist"] == ["A & B"]
    assert on_disk["musicbrainz_artistid"] == ["id-a", "id-b"]


def test_an_element_correction_rewrites_its_aligned_id_only(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    make_track(
        music_dir / "a.flac",
        {
            "artist": ["A & Bee"],
            "artists": ["A", "Bee"],
            "musicbrainz_artistid": ["id-a", "id-stale"],
        },
    )
    scan_library(engine_settings)

    # MusicBrainz does not know the stale id, so Last.fm supplies the name and a new id.
    lastfm = FakeCorrectionSource(
        {"A": None, "Bee": ArtistCorrection("The Bee", "id-bee"), "A & Bee": None},
    )
    mb = FakeArtistSource({"id-a": _mb("A", mbid="id-a")})
    artists.resolve_artists(engine_settings, client=lastfm, mb_client=mb)

    view = next(iter(staging.diff_tags(engine_settings)))
    assert view.diff["artists"] == {"from": ["A", "Bee"], "to": ["A", "The Bee"]}
    assert view.diff["musicbrainz_artistid"] == {
        "from": ["id-a", "id-stale"],
        "to": ["id-a", "id-bee"],
    }
    assert "artist" not in view.diff


def test_an_unaligned_element_uses_only_the_lastfm_tier_and_keeps_the_ids(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # Two elements and one id: the id belongs to the single-valued artist, not to an element.
    make_track(
        music_dir / "a.flac",
        {
            "artist": ["A & b"],
            "artists": ["A", "b"],
            "musicbrainz_artistid": ["id-duo"],
        },
    )
    scan_library(engine_settings)

    mb = FakeArtistSource({"id-duo": _mb("A & b", mbid="id-duo")})
    lastfm = FakeCorrectionSource(
        {"A": ArtistCorrection("A", None), "b": ArtistCorrection("Bea", "id-bea")},
    )
    artists.resolve_artists(engine_settings, client=lastfm, mb_client=mb)

    assert mb.lookups == ["id-duo"]
    assert sorted(lastfm.lookups) == ["A", "b"]
    view = next(iter(staging.diff_tags(engine_settings)))
    assert view.diff["artists"] == {"from": ["A", "b"], "to": ["A", "Bea"]}
    assert "musicbrainz_artistid" not in view.diff


def test_a_whole_credit_correction_leaves_a_collaboration_list_ids_alone(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # One id written for the display credit would misalign the two-element list.
    make_track(
        music_dir / "a.flac",
        {"artist": ["A & B"], "artists": ["A", "B"], "musicbrainz_artistid": ["id-a", "id-b"]},
    )
    scan_library(engine_settings)

    lastfm = FakeCorrectionSource({"A & B": ArtistCorrection("A and B", "id-duo")})
    mb = FakeArtistSource({"id-a": _mb("A", mbid="id-a"), "id-b": _mb("B", mbid="id-b")})
    artists.resolve_artists(engine_settings, client=lastfm, mb_client=mb)

    view = next(iter(staging.diff_tags(engine_settings)))
    assert view.diff["artist"] == {"from": ["A & B"], "to": ["A and B"]}
    assert "musicbrainz_artistid" not in view.diff
    assert "artists" not in view.diff


def test_guards_and_held_buckets_apply_to_each_artists_element(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(
        music_dir / "a.flac",
        {"artist": ["Solo"], "artists": ["Solo", "Various Artists", "Guest feat. Other", "Odd"]},
    )
    scan_library(engine_settings)
    file_id = _file_id(engine_settings, music_dir, track.name)

    lastfm = FakeCorrectionSource(
        {"Solo": ArtistCorrection("Solo", None), "Odd": ArtistCorrection("Oddity", None)},
    )
    result = artists.resolve_artists(
        engine_settings,
        client=lastfm,
        mb_client=FakeArtistSource({}),
    )

    # The sentinel and the feat credit are never looked up, and the unverified correction is held.
    assert result.skipped_sentinel == 2
    assert sorted(lastfm.lookups) == ["Odd", "Solo"]
    assert result.needs_review_values == [{"from": "Odd", "to": "Oddity"}]
    assert result.staged_files == 0
    view = next(v for v in library_list(engine_settings) if v.file_id == file_id)
    assert view.artist_status == "no_match"
