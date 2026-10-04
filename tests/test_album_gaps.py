"""Tests for blank-album gap detection (:mod:`tagmend.engine.album_gaps`).

Pure-classifier tests over constructed ``_FileInput`` inputs (mirroring ``test_mismatch``'s
``_mk`` style) covering the sibling decision table (green / genre_like / placeholder /
n1_weak / mixed-stays-blank), the folder-parse self-corroboration gate (corroborated
proposes, uncorroborated stays blank, partial fractions bracketing the 0.6 threshold), the
binding blank-only safety guarantee (a non-blank file — including one non-blank only at a
later ordinal — is never proposed), the limit/folder narrowing, and ``to_dict`` shape; plus
an integration pass through ``scan_library`` asserting read-only behaviour and one
end-to-end stage -> diff -> commit flow, and the MCP wiring smoke check.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from conftest import FOLDER_SPELLINGS, make_track, spell_folder
from tagmend import config, mcp_server
from tagmend.config import Settings
from tagmend.engine import album_gaps, classify, path_keys, schema, staging, store
from tagmend.engine.album_gaps import (
    AlbumGapGroup,
    AlbumGapProposal,
    AlbumGapsReport,
    detect_album_gaps,
)
from tagmend.engine.db import connect
from tagmend.engine.library import scan_library
from tagmend.engine.musicbrainz import MBRecording, MusicBrainzError

_MUSIC = Path("/library/music")
_VOCAB = classify.load_vocabulary()
_NOW = "2026-01-01T00:00:00+00:00"


class FakeMBRecordingSource:
    """An in-memory :class:`tagmend.engine.musicbrainz.MBRecordingSource` for DI in tests.

    Maps ``(artist, title)`` → :class:`MBRecording` (or ``None`` for "no usable release
    group"). A pair absent from the map also yields ``None``. Records the lookups it received,
    so a test can assert the source was (or was not) exercised.
    """

    def __init__(self, table: dict[tuple[str, str], MBRecording | None]) -> None:
        self._table = table
        self.lookups: list[tuple[str, str]] = []

    def recording_search(self, artist: str, title: str) -> MBRecording | None:
        self.lookups.append((artist, title))
        return self._table.get((artist, title))


def _rec(album_title: str) -> MBRecording:
    return MBRecording(album_title=album_title, release_group_mbid="rg-1", recording_mbid="rec-1")


def _mk(  # noqa: PLR0913 - cohesive keyword-only test-input fields
    file_id: int,
    folder: Path,
    filename: str,
    *,
    album: str | None = None,
    artist: str | None = None,
    title: str | None = None,
    writable: bool = True,
) -> album_gaps._FileInput:
    return album_gaps._FileInput(
        file_id=file_id,
        folder=str(folder),
        filename=filename,
        album=album,
        artist=artist,
        title=title,
        writable=writable,
    )


def _group_for(report: AlbumGapsReport, folder: Path) -> AlbumGapGroup | None:
    return next((g for g in report.groups if g.folder == str(folder)), None)


def _find_proposal(report: AlbumGapsReport, file_id: int) -> AlbumGapProposal | None:
    for group in report.groups:
        for proposal in group.proposals:
            if proposal.file_id == file_id:
                return proposal
    return None


def _proposal_ids(report: AlbumGapsReport) -> set[int]:
    return {p.file_id for g in report.groups for p in g.proposals}


# --- sibling source: the decision table ----------------------------------------------


def test_unanimous_clean_sibling_is_green() -> None:
    folder = _MUSIC / "Sublime" / "Stand By Your Van"
    files = [
        _mk(1, folder, "01.mp3", album="Stand By Your Van"),
        _mk(2, folder, "02.mp3", album="Stand By Your Van"),
        _mk(3, folder, "03.mp3", album=None),
    ]
    report = album_gaps._classify(files, _VOCAB)

    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "sibling"
    assert group.blank_count == 1
    assert group.file_count == 3
    assert group.sibling_histogram == {"Stand By Your Van": 2}

    proposal = _find_proposal(report, 3)
    assert proposal is not None
    assert proposal.proposed == "Stand By Your Van"
    assert proposal.confidence == "green"
    assert proposal.reason is None
    assert proposal.note == "sibling: unanimous n=2"

    assert report.green == 1
    assert report.confirm == 0
    assert report.stays_blank == 0


def test_genre_like_sibling_is_confirm_never_green() -> None:
    # "Reggaeton" is a controlled-vocabulary genre -> a genre string in the album field.
    assert _VOCAB.match("Reggaeton") is not None
    folder = _MUSIC / "Sublime" / "Sinsemilla"
    files = [
        _mk(1, folder, "01.mp3", album="Reggaeton"),
        _mk(2, folder, "02.mp3", album="Reggaeton"),
        _mk(3, folder, "03.mp3", album=None),
    ]
    report = album_gaps._classify(files, _VOCAB)

    proposal = _find_proposal(report, 3)
    assert proposal is not None
    assert proposal.confidence == "confirm"
    assert proposal.reason == "genre_like"
    assert report.green == 0
    assert report.confirm == 1


def test_all_words_genre_string_is_confirm_never_green() -> None:
    # The live Sinsemilla trap: the compound has no vocabulary key, but EVERY word is a
    # genre — the all-words rule flags it genre_like (user decision 2026-07-06).
    assert _VOCAB.match("Reggaeton Dembow") is None
    assert _VOCAB.match("Reggaeton") is not None
    assert _VOCAB.match("Dembow") is not None
    folder = _MUSIC / "Sublime" / "Sublime - Sinsemilla"
    files = [
        _mk(1, folder, "01.mp3", album="Reggaeton Dembow"),
        _mk(2, folder, "02.mp3", album="Reggaeton Dembow"),
        _mk(3, folder, "03.mp3", album=None),
    ]
    report = album_gaps._classify(files, _VOCAB)

    proposal = _find_proposal(report, 3)
    assert proposal is not None
    assert proposal.confidence == "confirm"
    assert proposal.reason == "genre_like"
    assert report.green == 0


def test_title_containing_one_genre_word_stays_green() -> None:
    # A real album title that merely CONTAINS a genre word must keep the green label:
    # 'house' is a genre, but 'of'/'balloons' are not, so the all-words rule passes it.
    assert _VOCAB.match("house") is not None
    assert _VOCAB.match("House of Balloons") is None
    folder = _MUSIC / "The Weeknd" / "House of Balloons"
    files = [
        _mk(1, folder, "01.mp3", album="House of Balloons"),
        _mk(2, folder, "02.mp3", album="House of Balloons"),
        _mk(3, folder, "03.mp3", album=None),
    ]
    report = album_gaps._classify(files, _VOCAB)

    proposal = _find_proposal(report, 3)
    assert proposal is not None
    assert proposal.confidence == "green"
    assert proposal.reason is None
    assert report.green == 1


def test_placeholder_sibling_is_confirm() -> None:
    folder = _MUSIC / "Neon Hitch" / "Album"
    files = [
        _mk(1, folder, "01.mp3", album="Unreleased"),
        _mk(2, folder, "02.mp3", album="Unreleased"),
        _mk(3, folder, "03.mp3", album=None),
    ]
    report = album_gaps._classify(files, _VOCAB)

    proposal = _find_proposal(report, 3)
    assert proposal is not None
    assert proposal.confidence == "confirm"
    assert proposal.reason == "placeholder"
    assert report.confirm == 1


def test_single_witness_sibling_is_confirm_n1_weak() -> None:
    folder = _MUSIC / "Blue Stahli" / "Remixes"
    files = [
        _mk(1, folder, "01.mp3", album="Robbin the Hood"),
        _mk(2, folder, "02.mp3", album=None),
    ]
    report = album_gaps._classify(files, _VOCAB)

    proposal = _find_proposal(report, 2)
    assert proposal is not None
    assert proposal.confidence == "confirm"
    assert proposal.reason == "n1_weak"
    assert proposal.note == "sibling: unanimous n=1"
    assert report.confirm == 1


def test_mixed_siblings_stay_blank_no_proposal() -> None:
    folder = _MUSIC / "Comp" / "Album"
    files = [
        _mk(1, folder, "01.mp3", album="Album One"),
        _mk(2, folder, "02.mp3", album="Album Two"),
        _mk(3, folder, "03.mp3", album=None),
    ]
    report = album_gaps._classify(files, _VOCAB)

    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "stays_blank"
    assert group.proposals == []
    assert _proposal_ids(report) == set()
    assert report.stays_blank == 1
    assert report.green == 0
    assert report.confirm == 0


# --- folder-parse source: self-corroboration gate ------------------------------------


def test_folder_parse_corroborated_proposes_confirm() -> None:
    folder = _MUSIC / "Sublime" / "Sublime - 1998 - Acoustic Bradley Nowell and Friends"
    files = [
        _mk(
            i,
            folder,
            f"Sublime - Acoustic Bradley Nowell and Friends - {i:02d} - Track {i}.mp3",
            album=None,
        )
        for i in range(1, 15)  # 14 files, all blank, all fold-containing the album token
    ]
    report = album_gaps._classify(files, _VOCAB)

    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "folder_parse"
    assert group.blank_count == 14
    assert len(group.proposals) == 14
    assert all(p.proposed == "Acoustic Bradley Nowell and Friends" for p in group.proposals)
    assert all(p.confidence == "confirm" for p in group.proposals)
    assert all(p.reason == "folder_parse" for p in group.proposals)
    assert group.proposals[0].note == "folder-parse: self-corroborated 14/14"
    assert report.confirm == 14


def test_folder_parse_uncorroborated_stays_blank() -> None:
    folder = _MUSIC / "Maphra" / "Maphra - YouTube"
    files = [_mk(i, folder, f"Maphra - Some Song {i}.mp3", album=None) for i in range(1, 9)]
    report = album_gaps._classify(files, _VOCAB)

    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "stays_blank"
    assert group.proposals == []
    assert report.stays_blank == 8


def _bracket_folder(hits: int) -> tuple[Path, list[album_gaps._FileInput]]:
    """A 5-file all-blank folder where *hits* filenames fold-contain the parsed album token."""
    folder = _MUSIC / "Some Artist" / "Some Artist - Zephyr"
    files: list[album_gaps._FileInput] = []
    fid = 1
    for _ in range(hits):
        files.append(_mk(fid, folder, f"Some Artist - Zephyr - {fid:02d} - Song.mp3", album=None))
        fid += 1
    for _ in range(5 - hits):
        files.append(_mk(fid, folder, f"Random Name - {fid:02d}.mp3", album=None))
        fid += 1
    return folder, files


def test_folder_parse_partial_fraction_meets_threshold() -> None:
    # 3/5 = 0.6 == threshold -> proposes.
    folder, files = _bracket_folder(3)
    report = album_gaps._classify(files, _VOCAB)
    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "folder_parse"
    assert group.proposals[0].note == "folder-parse: self-corroborated 3/5"


def test_folder_parse_partial_fraction_below_threshold() -> None:
    # 1/5 = 0.2 < threshold -> stays blank.
    folder, files = _bracket_folder(1)
    report = album_gaps._classify(files, _VOCAB)
    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "stays_blank"
    assert group.proposals == []


# --- mb_recording source: review-only, injected fake client --------------------------


def test_recording_source_review_proposal_never_green() -> None:
    # A blank file with artist+title in a folder the sibling and folder-parse sources leave
    # blank (leaf has no " - ") gets a review-only proposal from the recording search.
    folder = _MUSIC / "Sublime" / "Loose Tracks"
    files = [_mk(1, folder, "01.mp3", album=None, artist="Sublime", title="Doin Time")]
    fake = FakeMBRecordingSource({("Sublime", "Doin Time"): _rec("40oz. to Freedom")})

    report = album_gaps._classify(files, _VOCAB, client=fake)

    assert fake.lookups == [("Sublime", "Doin Time")]
    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "mb_recording"
    proposal = _find_proposal(report, 1)
    assert proposal is not None
    assert proposal.proposed == "40oz. to Freedom"
    assert proposal.confidence == "review"  # never green
    assert proposal.reason == "mb_recording"
    assert proposal.note == "musicbrainz: recording search"
    assert report.review == 1
    assert report.green == 0
    assert report.stays_blank == 0


def test_recording_source_skips_files_without_artist_or_title() -> None:
    # Files lacking artist OR title are never sent to MusicBrainz.
    folder = _MUSIC / "Loose" / "Odd Folder"
    files = [
        _mk(1, folder, "01.mp3", album=None, artist="Sublime", title=None),  # no title
        _mk(2, folder, "02.mp3", album=None, artist=None, title="Song"),  # no artist
    ]
    fake = FakeMBRecordingSource({("Sublime", "Doin Time"): _rec("40oz. to Freedom")})

    report = album_gaps._classify(files, _VOCAB, client=fake)

    assert fake.lookups == []  # neither file was eligible
    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "stays_blank"
    assert group.proposals == []
    assert report.review == 0
    assert report.stays_blank == 2


def test_recording_source_miss_stays_blank() -> None:
    # A recording search that resolves to nothing leaves the file blank (no proposal).
    folder = _MUSIC / "Sublime" / "Loose Tracks"
    files = [_mk(1, folder, "01.mp3", album=None, artist="Sublime", title="Unknown B-Side")]
    fake = FakeMBRecordingSource({("Sublime", "Unknown B-Side"): None})

    report = album_gaps._classify(files, _VOCAB, client=fake)

    assert fake.lookups == [("Sublime", "Unknown B-Side")]
    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "stays_blank"
    assert report.review == 0
    assert report.stays_blank == 1


class _RaisingRecordingSource:
    """A recording source whose every lookup fails, as during a MusicBrainz outage."""

    def recording_search(self, artist: str, title: str) -> MBRecording | None:
        message = f"503 for {artist} - {title}"
        raise MusicBrainzError(message)


def test_a_recording_lookup_error_is_not_stays_blank() -> None:
    # An unreachable MusicBrainz is not "no defensible ground", so it gets its own bucket.
    folder = _MUSIC / "Sublime" / "Loose Tracks"
    files = [_mk(1, folder, "01.mp3", album=None, artist="Sublime", title="Doin Time")]

    report = album_gaps._classify(files, _VOCAB, client=_RaisingRecordingSource())

    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "lookup_error"
    assert group.errors == 1
    assert report.errors == 1
    assert report.error_items == [
        {"key": "Sublime - Doin Time", "message": "503 for Sublime - Doin Time"},
    ]
    assert report.stays_blank == 0
    buckets = report.green + report.confirm + report.review + report.stays_blank + report.errors
    assert buckets == report.total_blank


def test_recording_source_only_runs_when_earlier_sources_produced_nothing() -> None:
    # A folder resolved by the sibling source never reaches the recording source.
    folder = _MUSIC / "Sublime" / "Album"
    files = [
        _mk(1, folder, "01.mp3", album="Stand By Your Van"),
        _mk(2, folder, "02.mp3", album="Stand By Your Van"),
        _mk(3, folder, "03.mp3", album=None, artist="Sublime", title="Doin Time"),
    ]
    fake = FakeMBRecordingSource({("Sublime", "Doin Time"): _rec("Some Other Album")})

    report = album_gaps._classify(files, _VOCAB, client=fake)

    assert fake.lookups == []  # the sibling source already proposed for the blank file
    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "sibling"
    proposal = _find_proposal(report, 3)
    assert proposal is not None
    assert proposal.confidence == "green"


# --- mb_recording source: detector wiring (real ledger) ------------------------------


def _blank_track_library(music_dir: Path, engine_settings: Settings) -> Path:
    """Scan a one-file, blank-album folder whose leaf name won't parse (recording source)."""
    folder = music_dir / "Sublime" / "Loose Tracks"
    make_track(folder / "01.mp3", {"artist": ["Sublime"], "title": ["Doin Time"]})
    scan_library(engine_settings)
    return folder


def test_detect_recording_source_end_to_end(engine_settings: Settings, music_dir: Path) -> None:
    folder = _blank_track_library(music_dir, engine_settings)
    fake = FakeMBRecordingSource({("Sublime", "Doin Time"): _rec("40oz. to Freedom")})

    report = detect_album_gaps(engine_settings, client=fake)

    assert fake.lookups == [("Sublime", "Doin Time")]
    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "mb_recording"
    assert report.review == 1
    proposal = group.proposals[0]
    assert proposal.proposed == "40oz. to Freedom"
    assert proposal.confidence == "review"


def test_detect_use_musicbrainz_false_skips_all_lookups(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _blank_track_library(music_dir, engine_settings)
    fake = FakeMBRecordingSource({("Sublime", "Doin Time"): _rec("40oz. to Freedom")})

    report = detect_album_gaps(engine_settings, use_musicbrainz=False, client=fake)

    assert fake.lookups == []  # the source is skipped entirely
    assert report.review == 0
    assert report.stays_blank == 1


def test_detect_lazy_no_candidate_builds_no_client(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stays_blank folder whose blank file lacks a title is NOT a recording-source candidate,
    # so the real client is never constructed. The fake factory raises if it were.
    folder = music_dir / "NoTitle" / "Loose Tracks"
    make_track(folder / "01.mp3", {"artist": ["X"]})  # blank album, no title
    scan_library(engine_settings)

    def _boom(*_args: object, **_kwargs: object) -> object:
        message = "MusicBrainzClient must not be built without a recording-source candidate"
        raise AssertionError(message)

    monkeypatch.setattr(album_gaps, "MusicBrainzClient", _boom)

    report = detect_album_gaps(engine_settings)  # use_musicbrainz defaults True

    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "stays_blank"
    assert report.review == 0


# --- binding blank-only safety -------------------------------------------------------


def test_non_blank_files_never_appear_in_proposals() -> None:
    folder = _MUSIC / "Mix" / "Album"
    files = [
        _mk(1, folder, "01.mp3", album="Real Album"),  # non-blank
        _mk(2, folder, "02.mp3", album=None),  # blank -> the only proposable file
        _mk(3, folder, "03.mp3", album="Real Album"),  # non-blank
    ]
    report = album_gaps._classify(files, _VOCAB)

    assert _proposal_ids(report) == {2}
    assert 1 not in _proposal_ids(report)
    assert 3 not in _proposal_ids(report)


def test_later_ordinal_album_is_not_blank_via_gather(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    # A file blank at ordinal 0 but carrying a real album at a LATER ordinal must be read
    # as non-blank by the any-ordinal gather (NOT proposed). Build the ledger rows directly.
    folder = music_dir / "OrdArtist" / "OrdAlbum"
    conn = connect(engine_settings.db_path)
    try:
        schema.apply_schema(conn)
        blank_id = store.insert_file(
            conn,
            folder=str(folder),
            filename="01.mp3",
            ext=".mp3",
            size_bytes=None,
            mtime_ns=None,
            now=_NOW,
        )
        late_id = store.insert_file(
            conn,
            folder=str(folder),
            filename="02.mp3",
            ext=".mp3",
            size_bytes=None,
            mtime_ns=None,
            now=_NOW,
        )
        # ordinal 0 blank, ordinal 1 a real album -> axis.first_nonblank -> "Real Album".
        store.replace_tags(conn, late_id, {"album": ["", "Real Album"]}, _NOW)
        conn.commit()
    finally:
        conn.close()

    # use_musicbrainz=False keeps this ordinal-gather assertion network-free (no recording
    # source call).
    report = detect_album_gaps(engine_settings, use_musicbrainz=False)

    # The later-ordinal file is a non-blank sibling; only the truly-blank file is proposed.
    assert _proposal_ids(report) == {blank_id}
    assert late_id not in _proposal_ids(report)


# --- narrowing (library-wide counts preserved) ---------------------------------------


def _two_group_report() -> AlbumGapsReport:
    folder_a = _MUSIC / "A" / "Album"
    folder_b = _MUSIC / "B" / "Album"
    files = [
        _mk(1, folder_a, "01.mp3", album="Album A Name"),
        _mk(2, folder_a, "02.mp3", album=None),
        _mk(3, folder_b, "01.mp3", album="Album B Name"),
        _mk(4, folder_b, "02.mp3", album=None),
    ]
    return album_gaps._classify(files, _VOCAB)


def test_limit_caps_groups_but_keeps_counts() -> None:
    report = _two_group_report()
    assert len(report.groups) == 2

    limited = album_gaps._limit_report(report, 1)
    assert len(limited.groups) == 1
    # Counts still describe the whole library.
    assert limited.total_blank == report.total_blank == 2
    assert limited.confirm == report.confirm


def test_folder_filter_is_exact_equality() -> None:
    report = _two_group_report()
    folder_a = _MUSIC / "A" / "Album"

    expanded = album_gaps._expand_folder(report, path_keys.path_key(folder_a))
    assert len(expanded.groups) == 1
    assert expanded.groups[0].folder == str(folder_a)
    # Counts preserved; a parent prefix must NOT match (equality, not substring).
    assert expanded.total_blank == 2
    assert album_gaps._expand_folder(report, path_keys.path_key(_MUSIC / "A")).groups == []


# --- to_dict shape -------------------------------------------------------------------


def test_total_files_counts_every_present_file() -> None:
    files = [
        _mk(1, _MUSIC / "A" / "One", "01.mp3", album="One"),
        _mk(2, _MUSIC / "A" / "One", "02.mp3", album="One"),
        _mk(3, _MUSIC / "A" / "One", "03.mp3", album=None),
        _mk(4, _MUSIC / "B" / "Two", "01.mp3", album="Two"),
        _mk(5, _MUSIC / "B" / "Two", "02.mp3", album="Two"),
    ]

    report = album_gaps._classify(files, _VOCAB)

    assert report.total_files == 5
    assert report.total_blank == 1
    group = _group_for(report, _MUSIC / "A" / "One")
    assert group is not None
    assert group.file_ids == [3]


def test_to_dict_shape() -> None:
    report = _two_group_report()
    payload = report.to_dict()
    assert set(payload) == {
        "groups",
        "total_files",
        "total_blank",
        "green",
        "confirm",
        "review",
        "stays_blank",
        "errors",
        "error_items",
        "unwritable",
        "summary",
    }
    groups = payload["groups"]
    assert isinstance(groups, list)
    group = groups[0]
    assert set(group) == {
        "folder",
        "blank_count",
        "file_count",
        "file_ids",
        "sibling_histogram",
        "source",
        "proposals",
        "errors",
        "unwritable",
    }
    proposals = group["proposals"]
    assert isinstance(proposals, list)
    assert set(proposals[0]) == {
        "file_id",
        "filename",
        "proposed",
        "confidence",
        "reason",
        "note",
    }


# --- integration: scan real audio, detect, then the fix flow -------------------------


def _file_id(settings: Settings, folder: Path, filename: str) -> int:
    conn = connect(settings.db_path)
    try:
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        return row.id
    finally:
        conn.close()


def _read_album(settings: Settings, folder: Path, filename: str) -> list[str]:
    conn = connect(settings.db_path)
    try:
        row = store.get_file(conn, str(folder), filename)
        assert row is not None
        return store.get_tags(conn, row.id).get("album", [])
    finally:
        conn.close()


@pytest.mark.parametrize("spelling", FOLDER_SPELLINGS)
def test_folder_argument_variants_match_the_same_rows(
    engine_settings: Settings,
    music_dir: Path,
    spelling: str,
) -> None:
    folder = music_dir / "Green Artist" / "Album"
    other = music_dir / "Green Artist" / "Other"
    for target in (folder, other):
        make_track(target / "01.mp3", {"album": ["Stand By Your Van"], "artist": ["Green Artist"]})
        make_track(target / "02.mp3", {"album": ["Stand By Your Van"], "artist": ["Green Artist"]})
        make_track(target / "03.mp3", {"artist": ["Green Artist"]})
    scan_library(engine_settings)

    exact = detect_album_gaps(engine_settings, folder=str(folder), use_musicbrainz=False)
    variant = detect_album_gaps(
        engine_settings,
        folder=spell_folder(folder, spelling),
        use_musicbrainz=False,
    )

    assert [g.folder for g in exact.groups] == [str(folder)]
    assert [g.folder for g in variant.groups] == [g.folder for g in exact.groups]


def test_detect_integration_read_only_then_fix_flow(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = music_dir / "Green Artist" / "Album"
    make_track(folder / "01.mp3", {"album": ["Stand By Your Van"], "artist": ["Green Artist"]})
    make_track(folder / "02.mp3", {"album": ["Stand By Your Van"], "artist": ["Green Artist"]})
    make_track(folder / "03.mp3", {"artist": ["Green Artist"]})  # blank album

    scan_library(engine_settings)
    # Sibling-green fixture (no recording-source candidate). use_musicbrainz=False keeps it
    # network-free.
    report = detect_album_gaps(engine_settings, use_musicbrainz=False)

    assert report.green == 1
    group = _group_for(report, folder)
    assert group is not None
    assert group.source == "sibling"
    blank_id = _file_id(engine_settings, folder, "03.mp3")
    proposal = _find_proposal(report, blank_id)
    assert proposal is not None
    assert proposal.proposed == "Stand By Your Van"
    assert proposal.confidence == "green"

    # Read-only: nothing staged and the blank file's album is untouched on disk.
    conn = connect(engine_settings.db_path)
    try:
        assert store.any_staged(conn) is False
    finally:
        conn.close()
    assert _read_album(engine_settings, folder, "03.mp3") == []

    # End-to-end: stage the green proposal -> diff shows it -> commit.
    staging.stage_tags_batch(
        engine_settings,
        entries=[(blank_id, {"album": ["Stand By Your Van"]})],
        note=proposal.note,
    )
    diffs = staging.diff_tags(engine_settings, path=folder)
    assert any(d.file_id == blank_id for d in diffs)

    result = staging.commit_tags(engine_settings, path=folder)
    assert result.committed == 1
    assert result.commit_id is not None
    assert _read_album(engine_settings, folder, "03.mp3") == ["Stand By Your Van"]


def test_a_folder_of_only_unwritable_blank_files_proposes_nothing() -> None:
    folder = _MUSIC / "A" / "Wav"
    files = [
        _mk(1, folder, "01.wav", album="LP", writable=False),
        _mk(2, folder, "02.wav", artist="A", title="Two", writable=False),
    ]
    client = FakeMBRecordingSource({("A", "Two"): _rec("LP")})

    report = album_gaps._classify(files, _VOCAB, client=client)

    group = _group_for(report, folder)
    assert group is not None
    assert (group.source, group.proposals, group.unwritable) == ("unwritable", [], 1)
    assert (report.total_blank, report.stays_blank, report.unwritable) == (1, 0, 1)
    assert client.lookups == []


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(bytes(1600))


def test_a_blank_file_the_writer_cannot_verify_is_counted_never_proposed(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = music_dir / "Artist" / "Album"
    make_track(folder / "01.mp3", {"album": ["Album"]})
    make_track(folder / "02.mp3", {"album": ["Album"]})
    make_track(folder / "03.mp3", {"title": ["Three"]})
    _write_wav(folder / "04.wav")
    scan_library(engine_settings)

    report = detect_album_gaps(engine_settings, use_musicbrainz=False)

    group = _group_for(report, folder)
    assert group is not None
    assert [proposal.filename for proposal in group.proposals] == ["03.mp3"]
    assert (group.blank_count, group.unwritable) == (2, 1)
    assert (report.green, report.stays_blank, report.unwritable) == (1, 0, 1)
    assert "1 file(s) get no proposal" in report.summary


def test_a_file_deleted_outside_tagmend_is_no_sibling_witness(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = music_dir / "Band" / "Album"
    make_track(folder / "a.mp3", {"artist": ["Band"], "title": ["A"]})
    make_track(folder / "b.mp3", {"artist": ["Band"], "album": ["Paranoid"]})
    make_track(folder / "c.mp3", {"artist": ["Band"], "album": ["Paranoid"]})
    scan_library(engine_settings)
    (folder / "b.mp3").unlink()
    (folder / "c.mp3").unlink()
    scan_library(engine_settings)

    report = detect_album_gaps(engine_settings, use_musicbrainz=False)

    assert report.total_files == 1
    assert report.green == 0
    group = _group_for(report, folder)
    assert group is not None
    assert group.sibling_histogram == {}


# --- MCP wiring ----------------------------------------------------------------------


def test_mcp_detect_album_gaps_listed_and_callable(music_dir: Path) -> None:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert "detect_album_gaps" in names

    config.set_setting("music_path", str(music_dir))
    folder = music_dir / "Green Artist" / "Album"
    make_track(folder / "01.mp3", {"album": ["Stand By Your Van"]})
    make_track(folder / "02.mp3", {"album": ["Stand By Your Van"]})
    make_track(folder / "03.mp3", {"artist": ["Green Artist"]})
    mcp_server.scan_library(path=str(music_dir))

    # Sibling-green fixture; use_musicbrainz=False keeps the smoke check network-free.
    payload = mcp_server.detect_album_gaps(use_musicbrainz=False)
    assert payload["ok"] is True
    assert payload["green"] == 1
    groups = payload["groups"]
    assert isinstance(groups, list)
    assert any(g["source"] == "sibling" for g in groups)
