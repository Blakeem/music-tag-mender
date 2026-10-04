"""Every engine result that serializes its own fields matches the payload its MCP tool returns.

Each expected dict is written out key by key, so a reordered or dropped field fails here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import pytest

from tagmend.engine import (
    album_conflicts,
    album_gaps,
    artists,
    genres,
    library,
    mismatch,
    path_deviations,
    paths,
    release_disagreements,
    serialize,
    staging,
    track_conflicts,
    versioning,
    year_disagreements,
    years,
)


class _Payload(Protocol):
    def to_dict(self) -> dict[str, object]: ...


# --- shared nested values ---------------------------------------------------------------

_ERROR_ITEMS = [{"value": "Artist", "error": "timeout"}]

_FILE_PLAN = paths.FilePlan(
    file_id=7,
    folder_key="artist/album",
    from_path="Artist/Album/01.flac",
    to_path="Artist/Album (2001)/01 Song.flac",
    rendered=("Artist", "Album (2001)", "01 Song"),
    levels=(0, 1, 2),
    status="held",
    kind="move",
    reasons=(("mismatch", "folder differs"), ("collision", "target taken")),
    replaces=False,
)
_FILE_PLAN_DICT: dict[str, object] = {
    "file_id": 7,
    "from_path": "Artist/Album/01.flac",
    "to_path": "Artist/Album (2001)/01 Song.flac",
    "status": "held",
    "kind": "move",
    "reasons": [
        {"reason": "mismatch", "detail": "folder differs"},
        {"reason": "collision", "detail": "target taken"},
    ],
}

# --- album_conflicts --------------------------------------------------------------------

_CONFLICT_ROW = album_conflicts.AlbumConflictRow(
    file_id=1,
    folder="Artist/Album",
    filename="01.flac",
    album="Album",
    albumartist="Artist",
    release_mbid="rel-1",
    date="2001",
    identity="artist|album|2001",
    majority_identity="artist|album|2002",
    tier="medium",
    reason="year differs",
)
_CONFLICT_ROW_DICT: dict[str, object] = {
    "file_id": 1,
    "folder": "Artist/Album",
    "filename": "01.flac",
    "album": "Album",
    "albumartist": "Artist",
    "release_mbid": "rel-1",
    "date": "2001",
    "identity": "artist|album|2001",
    "majority_identity": "artist|album|2002",
    "tier": "medium",
    "reason": "year differs",
}
_CONFLICT_CONTEXT_ROW = album_conflicts.AlbumConflictRow(
    file_id=2,
    folder="Artist/Singles",
    filename="02.flac",
    album=None,
    albumartist=None,
    release_mbid=None,
    date=None,
    identity="artist|single|",
    majority_identity="artist|other|",
    tier="low",
    reason="context",
)
_CONFLICT_CONTEXT_ROW_DICT: dict[str, object] = {
    "file_id": 2,
    "folder": "Artist/Singles",
    "filename": "02.flac",
    "album": None,
    "albumartist": None,
    "release_mbid": None,
    "date": None,
    "identity": "artist|single|",
    "majority_identity": "artist|other|",
    "tier": "low",
    "reason": "context",
}
_CONFLICT_GROUP = album_conflicts.AlbumConflictGroup(
    folder="Artist/Album",
    file_count=5,
    flagged=2,
    folder_context=1,
    identities=3,
    majority_identity="artist|album|2002",
    majority_files=4,
    tiers={"medium": 2},
    file_ids=[1, 6],
)
_CONFLICT_GROUP_DICT: dict[str, object] = {
    "folder": "Artist/Album",
    "file_count": 5,
    "flagged": 2,
    "folder_context": 1,
    "identities": 3,
    "majority_identity": "artist|album|2002",
    "majority_files": 4,
    "tiers": {"medium": 2},
    "file_ids": [1, 6],
}
_CONFLICTS_REPORT = album_conflicts.AlbumConflictsReport(
    rows=[_CONFLICT_ROW],
    total_files=10,
    flagged=2,
    high=0,
    medium=2,
    low=0,
    summary="2 of 10 files split from their folder",
    folder_context=1,
    folder_context_rows=[_CONFLICT_CONTEXT_ROW],
    groups=[_CONFLICT_GROUP],
)
_CONFLICTS_REPORT_DICT: dict[str, object] = {
    "rows": [_CONFLICT_ROW_DICT],
    "total_files": 10,
    "flagged": 2,
    "high": 0,
    "medium": 2,
    "low": 0,
    "folder_context": 1,
    "folder_context_rows": [_CONFLICT_CONTEXT_ROW_DICT],
    "groups": [_CONFLICT_GROUP_DICT],
    "summary": "2 of 10 files split from their folder",
}

# --- album_gaps -------------------------------------------------------------------------

_GAP_PROPOSAL = album_gaps.AlbumGapProposal(
    file_id=3,
    filename="03.mp3",
    proposed="Album",
    confidence="confirm",
    reason="one sibling disagrees",
    note="sibling: majority n=2",
)
_GAP_PROPOSAL_DICT: dict[str, object] = {
    "file_id": 3,
    "filename": "03.mp3",
    "proposed": "Album",
    "confidence": "confirm",
    "reason": "one sibling disagrees",
    "note": "sibling: majority n=2",
}
_GAP_GROUP = album_gaps.AlbumGapGroup(
    folder="Artist/Album",
    blank_count=1,
    file_count=3,
    file_ids=[3],
    sibling_histogram={"Album": 2},
    source="sibling",
    proposals=[_GAP_PROPOSAL],
    errors=4,
    unwritable=5,
)
_GAP_GROUP_DICT: dict[str, object] = {
    "folder": "Artist/Album",
    "blank_count": 1,
    "file_count": 3,
    "file_ids": [3],
    "sibling_histogram": {"Album": 2},
    "source": "sibling",
    "proposals": [_GAP_PROPOSAL_DICT],
    "errors": 4,
    "unwritable": 5,
}
_GAPS_REPORT = album_gaps.AlbumGapsReport(
    groups=[_GAP_GROUP],
    total_files=20,
    total_blank=9,
    green=1,
    confirm=2,
    review=3,
    stays_blank=1,
    summary="9 blank albums",
    errors=1,
    error_items=_ERROR_ITEMS,
    unwritable=1,
)
_GAPS_REPORT_DICT: dict[str, object] = {
    "groups": [_GAP_GROUP_DICT],
    "total_files": 20,
    "total_blank": 9,
    "green": 1,
    "confirm": 2,
    "review": 3,
    "stays_blank": 1,
    "errors": 1,
    "error_items": [{"value": "Artist", "error": "timeout"}],
    "unwritable": 1,
    "summary": "9 blank albums",
}

# --- resolvers --------------------------------------------------------------------------

_ARTISTS_RESULT = artists.ResolveArtistsResult(
    settled=1,
    staged_files=2,
    corrected_values=3,
    skipped_multi_artist=4,
    skipped_sentinel=5,
    no_correction=6,
    already_canonical=7,
    shrinks_credit=8,
    needs_review=9,
    name_id_disagreement=10,
    errors=11,
    pending_remaining=12,
    more=True,
    mappings=[{"from": "artist", "to": "Artist", "mbid": None}],
    multi_artist_files=[13, 14],
    no_correction_values=["Unknown"],
    already_canonical_values=["Artist"],
    shrinks_credit_values=[{"from": "A & B", "to": "A"}],
    needs_review_values=[{"value": "C", "reason": "casing"}],
    name_id_disagreement_values=[{"from": "D", "to": None, "mbids": ["m-1"], "reason": "r"}],
    error_items=_ERROR_ITEMS,
    summary="3 values corrected",
)
_ARTISTS_RESULT_DICT: dict[str, object] = {
    "settled": 1,
    "staged_files": 2,
    "corrected_values": 3,
    "skipped_multi_artist": 4,
    "skipped_sentinel": 5,
    "no_correction": 6,
    "already_canonical": 7,
    "shrinks_credit": 8,
    "needs_review": 9,
    "name_id_disagreement": 10,
    "errors": 11,
    "pending_remaining": 12,
    "more": True,
    "mappings": [{"from": "artist", "to": "Artist", "mbid": None}],
    "multi_artist_files": [13, 14],
    "no_correction_values": ["Unknown"],
    "already_canonical_values": ["Artist"],
    "shrinks_credit_values": [{"from": "A & B", "to": "A"}],
    "needs_review_values": [{"value": "C", "reason": "casing"}],
    "name_id_disagreement_values": [{"from": "D", "to": None, "mbids": ["m-1"], "reason": "r"}],
    "error_items": [{"value": "Artist", "error": "timeout"}],
    "summary": "3 values corrected",
}
_GENRES_RESULT = genres.ResolveGenresResult(
    settled=1,
    staged_files=2,
    no_match=3,
    pending_remaining=4,
    more=False,
    errors=5,
    error_items=_ERROR_ITEMS,
    no_match_artists=["Nobody"],
    summary="2 staged",
)
_GENRES_RESULT_DICT: dict[str, object] = {
    "settled": 1,
    "staged_files": 2,
    "no_match": 3,
    "pending_remaining": 4,
    "more": False,
    "errors": 5,
    "error_items": [{"value": "Artist", "error": "timeout"}],
    "no_match_artists": ["Nobody"],
    "summary": "2 staged",
}
_YEARS_RESULT = years.ResolveYearsResult(
    settled=1,
    staged_files=2,
    no_match=3,
    pending_remaining=4,
    more=True,
    mappings=[{"album": "Album", "year": "2001", "mbid": None}],
    summary="2 staged",
    errors=5,
    error_items=_ERROR_ITEMS,
)
_YEARS_RESULT_DICT: dict[str, object] = {
    "settled": 1,
    "staged_files": 2,
    "no_match": 3,
    "pending_remaining": 4,
    "more": True,
    "mappings": [{"album": "Album", "year": "2001", "mbid": None}],
    "errors": 5,
    "error_items": [{"value": "Artist", "error": "timeout"}],
    "summary": "2 staged",
}

# --- library ----------------------------------------------------------------------------

_FILE_VIEW = library.FileView(
    file_id=1,
    folder="Artist/Album",
    filename="01.flac",
    ext=".flac",
    is_missing=False,
    managed_tags={"artist": ["Artist"], "genre": ["Rock", "Pop"]},
    genre_status="done",
    genre_source_artist="Artist",
    genre_source_album="Album",
    artist_status="manual",
    artist_source_artist="artist",
    artist_source_albumartist="albumartist",
    year_status="no_match",
    year_source_artist="Year Artist",
    year_source_album="Year Album",
    song_status="done",
    song_source_release_mbid="rel-1",
    song_source_release_track_mbid="track-1",
    mismatch_status="legit_ignore",
    mismatch_source_value={"covers": ["album"], "inputs": {"album": "Album"}},
)
_FILE_VIEW_DICT: dict[str, object] = {
    "file_id": 1,
    "folder": "Artist/Album",
    "filename": "01.flac",
    "ext": ".flac",
    "is_missing": False,
    "managed_tags": {"artist": ["Artist"], "genre": ["Rock", "Pop"]},
    "genre_status": "done",
    "genre_source_artist": "Artist",
    "genre_source_album": "Album",
    "artist_status": "manual",
    "artist_source_artist": "artist",
    "artist_source_albumartist": "albumartist",
    "year_status": "no_match",
    "year_source_artist": "Year Artist",
    "year_source_album": "Year Album",
    "song_status": "done",
    "song_source_release_mbid": "rel-1",
    "song_source_release_track_mbid": "track-1",
    "mismatch_status": "legit_ignore",
    "mismatch_source_value": {"covers": ["album"], "inputs": {"album": "Album"}},
}
_ARTIST_ROW = library.ArtistRow(artist="Artist", file_count=3)
_ARTIST_ROW_DICT: dict[str, object] = {"artist": "Artist", "file_count": 3}
_ALBUM_ROW = library.AlbumRow(
    artist=None,
    album="Album",
    file_count=4,
    year_status="pending",
    blank_originaldate=2,
)
_ALBUM_ROW_DICT: dict[str, object] = {
    "artist": None,
    "album": "Album",
    "file_count": 4,
    "year_status": "pending",
    "blank_originaldate": 2,
}
_SCAN_RESULT = library.ScanResult(
    total_seen=1,
    added=2,
    updated=3,
    unchanged=4,
    tags_read=5,
    missing_flagged=6,
    restored=7,
    errors=8,
    error_items=({"key": "a.mp3", "message": "unreadable"},),
    respelled=9,
    pending_commit=10,
)
_SCAN_RESULT_DICT: dict[str, object] = {
    "total_seen": 1,
    "added": 2,
    "updated": 3,
    "unchanged": 4,
    "tags_read": 5,
    "missing_flagged": 6,
    "restored": 7,
    "errors": 8,
    "error_items": [{"key": "a.mp3", "message": "unreadable"}],
    "respelled": 9,
    "pending_commit": 10,
}

# --- mismatch ---------------------------------------------------------------------------

_COMPARISON = mismatch.ComparisonSummary(files=2, tag="Album", path="Albums")
_COMPARISON_DICT: dict[str, object] = {"files": 2, "tag": "Album", "path": "Albums"}
_MISMATCH_GROUP = mismatch.MismatchGroup(
    folder="Artist/Album",
    file_count=5,
    flagged=2,
    tier="high",
    comparisons={"album": _COMPARISON},
    mb_stamped=True,
    exception=None,
    suppressed={"legit_ignore": 1},
    file_ids=[1, 2],
    unflagged_ids=[3],
)
_MISMATCH_GROUP_DICT: dict[str, object] = {
    "folder": "Artist/Album",
    "file_count": 5,
    "flagged": 2,
    "tier": "high",
    "comparisons": {"album": _COMPARISON_DICT},
    "mb_stamped": True,
    "exception": None,
    "suppressed": {"legit_ignore": 1},
    "file_ids": [1, 2],
    "unflagged_ids": [3],
}
_GATE = mismatch.GateState(open=False, flagged=3, exceptions_undecided=4)
_GATE_DICT: dict[str, object] = {"open": False, "flagged": 3, "exceptions_undecided": 4}
_DECIDED_FILE = mismatch.DecidedFile(file_id=1, folder="Artist/Album", filename="01.flac")
_DECIDED_FILE_DICT: dict[str, object] = {
    "file_id": 1,
    "folder": "Artist/Album",
    "filename": "01.flac",
}

# --- path_deviations --------------------------------------------------------------------

_LEVEL_FIT = path_deviations.LevelFit(component="{albumartist}", exact=1, case=2, differs=3)
_LEVEL_FIT_DICT: dict[str, object] = {
    "component": "{albumartist}",
    "exact": 1,
    "case": 2,
    "differs": 3,
}
_CANDIDATE = path_deviations.ContainerCandidate(
    folder="Soundtracks",
    files=12,
    album_artists=5,
    top_album_artist="Composer",
    listed=True,
)
_CANDIDATE_DICT: dict[str, object] = {
    "folder": "Soundtracks",
    "files": 12,
    "album_artists": 5,
    "top_album_artist": "Composer",
    "listed": True,
}
_DEVIATION_GROUP = path_deviations.DeviationGroup(
    folder="Artist/Album",
    files=2,
    destinations=("Artist/Album (2001)", "Artist/Other"),
    kind="move",
    held={"mismatch": 1},
    example={"from_path": "Artist/Album/01.flac", "to_path": None},
)
_DEVIATION_GROUP_DICT: dict[str, object] = {
    "folder": "Artist/Album",
    "files": 2,
    "destinations": ["Artist/Album (2001)", "Artist/Other"],
    "kind": "move",
    "held": {"mismatch": 1},
    "example": {"from_path": "Artist/Album/01.flac", "to_path": None},
}
_DEVIATIONS_REPORT = path_deviations.DeviationsReport(
    pattern="{albumartist}/{album}",
    persisted=False,
    default_pattern="{albumartist}/{album} ({year})",
    container_folders=("Soundtracks", "Compilations"),
    total_files=30,
    counts={"move": 2, "at_target": 28},
    held={"mismatch": 1},
    fit=(_LEVEL_FIT,),
    shapes={"top": [{"shape": "{albumartist}", "files": 30}]},
    container_candidates=(_CANDIDATE,),
    gate=_GATE,
    volume_refusal=None,
    group_count=1,
    groups=(_DEVIATION_GROUP,),
    rows=(_FILE_PLAN,),
)
_DEVIATIONS_REPORT_DICT: dict[str, object] = {
    "pattern": "{albumartist}/{album}",
    "persisted": False,
    "default_pattern": "{albumartist}/{album} ({year})",
    "container_folders": ["Soundtracks", "Compilations"],
    "total_files": 30,
    "counts": {"move": 2, "at_target": 28},
    "held": {"mismatch": 1},
    "fit": [_LEVEL_FIT_DICT],
    "shapes": {"top": [{"shape": "{albumartist}", "files": 30}]},
    "container_candidates": [_CANDIDATE_DICT],
    "gate": _GATE_DICT,
    "volume_refusal": None,
    "group_count": 1,
    "groups": [_DEVIATION_GROUP_DICT],
    "rows": [_FILE_PLAN_DICT],
}

# --- paths ------------------------------------------------------------------------------

_SIDECAR_HOLD = paths.SidecarHold(
    from_path="Artist/Album/cover.jpg",
    to_path="Artist/Album (2001)/cover.jpg",
    detail="target taken",
)
_SIDECAR_HOLD_DICT: dict[str, object] = {
    "from_path": "Artist/Album/cover.jpg",
    "to_path": "Artist/Album (2001)/cover.jpg",
    "detail": "target taken",
}
_SIDECAR_OUTCOME = paths.SidecarOutcome(
    from_path="Artist/Album/album.cue",
    to_path="Artist/Album (2001)/album.cue",
    status="error",
    detail="locked",
)
_SIDECAR_OUTCOME_DICT: dict[str, object] = {
    "from_path": "Artist/Album/album.cue",
    "to_path": "Artist/Album (2001)/album.cue",
    "status": "error",
    "detail": "locked",
}
_STAGE_PATHS_RESULT = paths.StagePathsResult(
    dry_run=True,
    pattern="{albumartist}/{album}",
    matched=1,
    staged=2,
    folders=3,
    kinds={"move": 2},
    at_target=4,
    case_only=5,
    kept_staged=6,
    unstaged=7,
    held_count=8,
    held={"mismatch": 8},
    held_files=(_FILE_PLAN,),
    staged_targets_under_path=None,
    sidecars_staged=9,
    sidecars_held=(_SIDECAR_HOLD,),
)
_STAGE_PATHS_RESULT_DICT: dict[str, object] = {
    "dry_run": True,
    "pattern": "{albumartist}/{album}",
    "matched": 1,
    "staged": 2,
    "folders": 3,
    "kinds": {"move": 2},
    "at_target": 4,
    "case_only": 5,
    "kept_staged": 6,
    "unstaged": 7,
    "held_count": 8,
    "held": {"mismatch": 8},
    "held_files": [_FILE_PLAN_DICT],
    "staged_targets_under_path": None,
    "sidecars_staged": 9,
    "sidecars_held": [_SIDECAR_HOLD_DICT],
}
_NAMING_SETTINGS = paths.NamingSettings(
    pattern="",
    default_pattern="{albumartist}/{album}",
    container_folders=("Soundtracks",),
    settings_path="C:/settings.json",
)
_NAMING_SETTINGS_DICT: dict[str, object] = {
    "pattern": "",
    "default_pattern": "{albumartist}/{album}",
    "container_folders": ["Soundtracks"],
    "settings_path": "C:/settings.json",
}
_UNSTAGE_PATHS_RESULT = paths.UnstagePathsResult(removed=3, sidecars_removed=1, sidecars_staged=2)
_UNSTAGE_PATHS_RESULT_DICT: dict[str, object] = {
    "removed": 3,
    "sidecars_removed": 1,
    "sidecars_staged": 2,
}
_PATH_DIFF = paths.PathDiffView(
    file_id=1,
    from_path="Artist/Album/01.flac",
    to_path="Artist/Album (2001)/01.flac",
    origin="auto",
    note=None,
    staged_at="2026-01-01T00:00:00Z",
    state="ready",
    stale=True,
)
_PATH_DIFF_DICT: dict[str, object] = {
    "file_id": 1,
    "from_path": "Artist/Album/01.flac",
    "to_path": "Artist/Album (2001)/01.flac",
    "origin": "auto",
    "note": None,
    "staged_at": "2026-01-01T00:00:00Z",
    "state": "ready",
    "stale": True,
}
_SIDECAR_DIFF = paths.SidecarDiffView(
    from_path="Artist/Album/cover.jpg",
    to_path="Artist/Album (2001)/cover.jpg",
    origin="manual",
    note="art",
    staged_at="2026-01-02T00:00:00Z",
    state="moved",
)
_SIDECAR_DIFF_DICT: dict[str, object] = {
    "from_path": "Artist/Album/cover.jpg",
    "to_path": "Artist/Album (2001)/cover.jpg",
    "origin": "manual",
    "note": "art",
    "staged_at": "2026-01-02T00:00:00Z",
    "state": "moved",
}
_PATH_PROBLEM = paths.PathProblem(
    file_id=2,
    status="changed_since_stage",
    to_path="Artist/Album (2001)/02.flac",
    detail="rescan",
)
_PATH_PROBLEM_DICT: dict[str, object] = {
    "file_id": 2,
    "status": "changed_since_stage",
    "to_path": "Artist/Album (2001)/02.flac",
    "detail": "rescan",
}
_PATH_COMMIT_RESULT = paths.PathCommitResult(
    commit_id=40,
    committed=1,
    noop=2,
    missing=3,
    changed_since_stage=4,
    errors=5,
    problems=(_PATH_PROBLEM,),
    sidecars_moved=6,
    sidecars_waiting=7,
    sidecars_held=("Artist/Album/scan.png",),
    folders_pruned=8,
    sidecar_problems=(_SIDECAR_OUTCOME,),
)
_PATH_COMMIT_RESULT_DICT: dict[str, object] = {
    "commit_id": 40,
    "committed": 1,
    "noop": 2,
    "missing": 3,
    "changed_since_stage": 4,
    "errors": 5,
    "problems": [_PATH_PROBLEM_DICT],
    "sidecars_moved": 6,
    "sidecars_waiting": 7,
    "sidecars_held": ["Artist/Album/scan.png"],
    "folders_pruned": 8,
    "sidecar_problems": [_SIDECAR_OUTCOME_DICT],
}
_PATH_REVERT_RESULT = paths.PathRevertResult(
    file_id=1,
    target_version=2,
    new_version=None,
    commit_id=None,
    to_path="Artist/Album/01.flac",
    status="reverted",
    detail=None,
    dry_run=True,
)
_PATH_REVERT_RESULT_DICT: dict[str, object] = {
    "file_id": 1,
    "target_version": 2,
    "new_version": None,
    "commit_id": None,
    "to_path": "Artist/Album/01.flac",
    "status": "reverted",
    "detail": None,
    "dry_run": True,
}

# --- release_disagreements --------------------------------------------------------------

_RELEASE_ROW = release_disagreements.ReleaseDisagreementRow(
    file_id=1,
    folder="Artist/Album",
    filename="01.flac",
    release_mbid="rel-1",
    release_title="Album",
    field="title",
    have="Song",
    want="Song (Live)",
    tier="high",
    reason="title differs",
)
_RELEASE_ROW_DICT: dict[str, object] = {
    "file_id": 1,
    "folder": "Artist/Album",
    "filename": "01.flac",
    "release_mbid": "rel-1",
    "release_title": "Album",
    "field": "title",
    "have": "Song",
    "want": "Song (Live)",
    "tier": "high",
    "reason": "title differs",
}
_RELEASE_GROUP = release_disagreements.ReleaseDisagreementGroup(
    folder="Artist/Album",
    file_count=10,
    flagged=1,
    tiers={"high": 1},
    file_ids=[1],
    flagged_fields=3,
    fills=4,
    fields={"title": 1},
    releases=[{"release_mbid": "rel-1", "release_title": "Album", "file_count": 10}],
)
_RELEASE_GROUP_DICT: dict[str, object] = {
    "folder": "Artist/Album",
    "file_count": 10,
    "flagged": 1,
    "tiers": {"high": 1},
    "file_ids": [1],
    "flagged_fields": 3,
    "fills": 4,
    "fields": {"title": 1},
    "releases": [{"release_mbid": "rel-1", "release_title": "Album", "file_count": 10}],
}
_RELEASE_FILL_ROW = release_disagreements.ReleaseDisagreementRow(
    file_id=2,
    folder="Artist/Album",
    filename="02.flac",
    release_mbid="rel-1",
    release_title="Album",
    field="tracknumber",
    have="",
    want="2",
    tier="medium",
    reason="",
)
_RELEASE_FILL_ROW_DICT: dict[str, object] = {
    "file_id": 2,
    "folder": "Artist/Album",
    "filename": "02.flac",
    "release_mbid": "rel-1",
    "release_title": "Album",
    "field": "tracknumber",
    "have": "",
    "want": "2",
    "tier": "medium",
    "reason": "",
}
_RELEASES_REPORT = release_disagreements.ReleaseDisagreementsReport(
    rows=[_RELEASE_ROW],
    total_files=10,
    flagged=1,
    flagged_fields=1,
    high=1,
    medium=0,
    low=0,
    fills=1,
    fill_rows=[_RELEASE_FILL_ROW],
    releases_attempted=2,
    releases_checked=1,
    releases_remaining=3,
    more=True,
    skipped_no_release_mbid=4,
    unknown_releases=0,
    unmatched_tracks=5,
    errors=1,
    error_items=[{"key": "rel-2", "message": "timeout"}],
    groups=[_RELEASE_GROUP],
    summary="1 file disagrees",
)
_RELEASES_REPORT_DICT: dict[str, object] = {
    "rows": [_RELEASE_ROW_DICT],
    "total_files": 10,
    "flagged": 1,
    "flagged_fields": 1,
    "high": 1,
    "medium": 0,
    "low": 0,
    "fills": 1,
    "fill_rows": [_RELEASE_FILL_ROW_DICT],
    "releases_attempted": 2,
    "releases_checked": 1,
    "releases_remaining": 3,
    "more": True,
    "skipped_no_release_mbid": 4,
    "unknown_releases": 0,
    "unmatched_tracks": 5,
    "errors": 1,
    "error_items": [{"key": "rel-2", "message": "timeout"}],
    "groups": [_RELEASE_GROUP_DICT],
    "summary": "1 file disagrees",
}

# --- staging ----------------------------------------------------------------------------

_TAG_DIFF = staging.TagDiffView(
    file_id=1,
    folder="Artist/Album",
    filename="01.flac",
    is_missing=False,
    origin="auto",
    note="genre",
    staged_at="2026-01-01T00:00:00Z",
    current={"genre": ["Rock"]},
    target={"genre": ["Pop"]},
    diff={"genre": {"from": ["Rock"], "to": ["Pop"]}},
    stale_identity=[{"changed": "artist", "stale_field": "artistsort", "stale_value": ["A"]}],
)
_TAG_DIFF_DICT: dict[str, object] = {
    "file_id": 1,
    "folder": "Artist/Album",
    "filename": "01.flac",
    "is_missing": False,
    "origin": "auto",
    "note": "genre",
    "staged_at": "2026-01-01T00:00:00Z",
    "current": {"genre": ["Rock"]},
    "target": {"genre": ["Pop"]},
    "diff": {"genre": {"from": ["Rock"], "to": ["Pop"]}},
    "stale_identity": [{"changed": "artist", "stale_field": "artistsort", "stale_value": ["A"]}],
}

# --- track_conflicts --------------------------------------------------------------------

_TRACK_ROW = track_conflicts.TrackConflictRow(
    file_id=1,
    folder="Artist/Album",
    filename="01.flac",
    disc="1",
    track=1,
    title=None,
    tier="high",
    reason="slot shared",
    peers=[2, 3],
)
_TRACK_ROW_DICT: dict[str, object] = {
    "file_id": 1,
    "folder": "Artist/Album",
    "filename": "01.flac",
    "disc": "1",
    "track": 1,
    "title": None,
    "tier": "high",
    "reason": "slot shared",
    "peers": [2, 3],
}
_TRACK_CONTEXT_ROW = track_conflicts.TrackConflictRow(
    file_id=4,
    folder="Artist/Singles",
    filename="04.flac",
    disc="",
    track=4,
    title="Single",
    tier="low",
    reason="context",
    peers=[5],
)
_TRACK_CONTEXT_ROW_DICT: dict[str, object] = {
    "file_id": 4,
    "folder": "Artist/Singles",
    "filename": "04.flac",
    "disc": "",
    "track": 4,
    "title": "Single",
    "tier": "low",
    "reason": "context",
    "peers": [5],
}
_TRACK_GROUP = track_conflicts.TrackConflictGroup(
    folder="Artist/Album",
    file_count=12,
    flagged=3,
    folder_context=1,
    slots={"1/1": 3},
    tiers={"high": 3},
    file_ids=[1, 2, 3],
)
_TRACK_GROUP_DICT: dict[str, object] = {
    "folder": "Artist/Album",
    "file_count": 12,
    "flagged": 3,
    "folder_context": 1,
    "slots": {"1/1": 3},
    "tiers": {"high": 3},
    "file_ids": [1, 2, 3],
}
_TRACKS_REPORT = track_conflicts.TrackConflictsReport(
    rows=[_TRACK_ROW],
    total_files=40,
    flagged=3,
    high=3,
    medium=0,
    low=0,
    summary="3 files share a slot",
    folder_context=1,
    folder_context_rows=[_TRACK_CONTEXT_ROW],
    groups=[_TRACK_GROUP],
)
_TRACKS_REPORT_DICT: dict[str, object] = {
    "rows": [_TRACK_ROW_DICT],
    "total_files": 40,
    "flagged": 3,
    "high": 3,
    "medium": 0,
    "low": 0,
    "folder_context": 1,
    "folder_context_rows": [_TRACK_CONTEXT_ROW_DICT],
    "groups": [_TRACK_GROUP_DICT],
    "summary": "3 files share a slot",
}

# --- versioning -------------------------------------------------------------------------

_REVERT_RESULT = versioning.RevertResult(
    file_id=1,
    target_version=2,
    new_version=3,
    commit_id=4,
    status="reverted",
    dry_run=False,
)
_REVERT_RESULT_DICT: dict[str, object] = {
    "file_id": 1,
    "target_version": 2,
    "new_version": 3,
    "commit_id": 4,
    "status": "reverted",
    "dry_run": False,
}

# --- year_disagreements -----------------------------------------------------------------

_YEAR_ROW = year_disagreements.YearDisagreementRow(
    file_id=1,
    folder="Artist/Album",
    filename="01.flac",
    artist="Artist",
    album="Album",
    field="originaldate",
    have="2005",
    first_release_year="2001",
    release_group_mbid="rg-1",
    release_group_title="Album",
    tier="high",
    reason="later than first release",
)
_YEAR_ROW_DICT: dict[str, object] = {
    "file_id": 1,
    "folder": "Artist/Album",
    "filename": "01.flac",
    "artist": "Artist",
    "album": "Album",
    "field": "originaldate",
    "have": "2005",
    "first_release_year": "2001",
    "release_group_mbid": "rg-1",
    "release_group_title": "Album",
    "tier": "high",
    "reason": "later than first release",
}
_YEAR_GROUP = year_disagreements.YearDisagreementGroup(
    folder="Artist/Album",
    artist="Artist",
    album="Album",
    first_release_year="2001",
    release_group_mbid="rg-1",
    release_group_title="Album",
    file_count=9,
    flagged=2,
    folder_context=1,
    tiers={"high": 2},
    file_ids=[1, 2],
    fields={"originaldate": 2},
)
_YEAR_GROUP_DICT: dict[str, object] = {
    "folder": "Artist/Album",
    "artist": "Artist",
    "album": "Album",
    "first_release_year": "2001",
    "release_group_mbid": "rg-1",
    "release_group_title": "Album",
    "file_count": 9,
    "flagged": 2,
    "folder_context": 1,
    "tiers": {"high": 2},
    "file_ids": [1, 2],
    "fields": {"originaldate": 2},
}
_YEARS_REPORT = year_disagreements.YearDisagreementsReport(
    rows=[_YEAR_ROW],
    total_files=9,
    flagged=1,
    flagged_fields=1,
    high=1,
    medium=0,
    low=0,
    folder_context=1,
    folder_context_rows=[_YEAR_ROW],
    release_groups_checked=2,
    release_groups_remaining=3,
    more=True,
    unknown_release_groups=4,
    skipped_no_identity=5,
    errors=1,
    error_items=[{"key": "Artist - Album", "message": "timeout"}],
    groups=[_YEAR_GROUP],
    summary="1 file carries a contradicting year",
)
_YEARS_REPORT_DICT: dict[str, object] = {
    "rows": [_YEAR_ROW_DICT],
    "total_files": 9,
    "flagged": 1,
    "flagged_fields": 1,
    "high": 1,
    "medium": 0,
    "low": 0,
    "folder_context": 1,
    "folder_context_rows": [_YEAR_ROW_DICT],
    "release_groups_checked": 2,
    "release_groups_remaining": 3,
    "more": True,
    "unknown_release_groups": 4,
    "skipped_no_identity": 5,
    "errors": 1,
    "error_items": [{"key": "Artist - Album", "message": "timeout"}],
    "groups": [_YEAR_GROUP_DICT],
    "summary": "1 file carries a contradicting year",
}

_CASES: list[tuple[_Payload, dict[str, object]]] = [
    (_CONFLICT_ROW, _CONFLICT_ROW_DICT),
    (_CONFLICT_GROUP, _CONFLICT_GROUP_DICT),
    (_CONFLICTS_REPORT, _CONFLICTS_REPORT_DICT),
    (_GAP_PROPOSAL, _GAP_PROPOSAL_DICT),
    (_GAP_GROUP, _GAP_GROUP_DICT),
    (_GAPS_REPORT, _GAPS_REPORT_DICT),
    (_ARTISTS_RESULT, _ARTISTS_RESULT_DICT),
    (_GENRES_RESULT, _GENRES_RESULT_DICT),
    (_YEARS_RESULT, _YEARS_RESULT_DICT),
    (_FILE_VIEW, _FILE_VIEW_DICT),
    (_ARTIST_ROW, _ARTIST_ROW_DICT),
    (_ALBUM_ROW, _ALBUM_ROW_DICT),
    (_SCAN_RESULT, _SCAN_RESULT_DICT),
    (_COMPARISON, _COMPARISON_DICT),
    (_MISMATCH_GROUP, _MISMATCH_GROUP_DICT),
    (_GATE, _GATE_DICT),
    (_DECIDED_FILE, _DECIDED_FILE_DICT),
    (_LEVEL_FIT, _LEVEL_FIT_DICT),
    (_CANDIDATE, _CANDIDATE_DICT),
    (_DEVIATION_GROUP, _DEVIATION_GROUP_DICT),
    (_DEVIATIONS_REPORT, _DEVIATIONS_REPORT_DICT),
    (_SIDECAR_HOLD, _SIDECAR_HOLD_DICT),
    (_SIDECAR_OUTCOME, _SIDECAR_OUTCOME_DICT),
    (_STAGE_PATHS_RESULT, _STAGE_PATHS_RESULT_DICT),
    (_NAMING_SETTINGS, _NAMING_SETTINGS_DICT),
    (_UNSTAGE_PATHS_RESULT, _UNSTAGE_PATHS_RESULT_DICT),
    (_PATH_DIFF, _PATH_DIFF_DICT),
    (_SIDECAR_DIFF, _SIDECAR_DIFF_DICT),
    (_PATH_PROBLEM, _PATH_PROBLEM_DICT),
    (_PATH_COMMIT_RESULT, _PATH_COMMIT_RESULT_DICT),
    (_PATH_REVERT_RESULT, _PATH_REVERT_RESULT_DICT),
    (_RELEASE_ROW, _RELEASE_ROW_DICT),
    (_RELEASE_GROUP, _RELEASE_GROUP_DICT),
    (_RELEASES_REPORT, _RELEASES_REPORT_DICT),
    (_TAG_DIFF, _TAG_DIFF_DICT),
    (_TRACK_ROW, _TRACK_ROW_DICT),
    (_TRACK_GROUP, _TRACK_GROUP_DICT),
    (_TRACKS_REPORT, _TRACKS_REPORT_DICT),
    (_REVERT_RESULT, _REVERT_RESULT_DICT),
    (_YEAR_ROW, _YEAR_ROW_DICT),
    (_YEAR_GROUP, _YEAR_GROUP_DICT),
    (_YEARS_REPORT, _YEARS_REPORT_DICT),
]


@pytest.mark.parametrize(
    ("result", "expected"),
    _CASES,
    ids=[type(result).__name__ for result, _ in _CASES],
)
def test_payload_keys_order_and_values(result: _Payload, expected: dict[str, object]) -> None:
    payload = result.to_dict()

    assert list(payload.items()) == list(expected.items())


@dataclass(frozen=True, slots=True)
class _Plain:
    name: str
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Walked(serialize.FieldDict):
    plain: _Plain
    by_key: dict[str, _Plain]
    pairs: list[tuple[int, int]]
    gate: mismatch.GateState


def test_field_dict_walks_nested_values_and_keeps_slots() -> None:
    walked = _Walked(
        plain=_Plain(name="a", tags=("x", "y")),
        by_key={"k": _Plain(name="b", tags=())},
        pairs=[(1, 2)],
        gate=_GATE,
    )
    expected: dict[str, object] = {
        "plain": {"name": "a", "tags": ["x", "y"]},
        "by_key": {"k": {"name": "b", "tags": []}},
        "pairs": [[1, 2]],
        "gate": _GATE_DICT,
    }

    payload = walked.to_dict()

    assert list(payload.items()) == list(expected.items())
    assert not hasattr(walked, "__dict__")
