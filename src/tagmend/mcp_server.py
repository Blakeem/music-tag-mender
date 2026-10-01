"""FastMCP server — a thin wrapper exposing the engine over MCP (stdio).

Contains no business logic: each tool marshals arguments, calls into
:mod:`tagmend.engine`, and returns a JSON-serializable result. Launch it with
``tagmend mcp`` (or point an MCP client / the MCP Inspector at that command).
Every tool returns ``{"ok": False, "error", "error_type"}`` for an expected failure.
"""

from __future__ import annotations

import functools
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import mutagen
from mcp.server.fastmcp import FastMCP

from tagmend import configui
from tagmend.config import load_settings
from tagmend.engine import (
    album_conflicts,
    album_gaps,
    artists,
    commits,
    disagreements,
    genres,
    health,
    library,
    mismatch,
    staging,
    track_conflicts,
    versioning,
    years,
)
from tagmend.engine.lastfm import LastfmError
from tagmend.engine.library import ScanMode
from tagmend.engine.musicbrainz import MusicBrainzError
from tagmend.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

mcp = FastMCP("tagmend")

# The failures a tool call can meet in normal use: a bad request, a locked or unreadable
# file, a ledger another process holds, a lookup service that is down. Anything else is a bug.
_ENVELOPED_ERRORS: Final = (
    ValueError,
    OSError,
    mutagen.MutagenError,  # type: ignore[attr-defined]
    sqlite3.OperationalError,
    LastfmError,
    MusicBrainzError,
)


def _error_envelope[**P](tool: Callable[P, dict[str, object]]) -> Callable[P, dict[str, object]]:
    """Wrap *tool* so an expected failure returns an error envelope instead of crossing JSON-RPC.

    ``functools.wraps`` sets ``__wrapped__``, which FastMCP's signature read follows, so the
    tool's schema is the undecorated function's.
    """

    @functools.wraps(tool)
    def enveloped(*args: P.args, **kwargs: P.kwargs) -> dict[str, object]:
        try:
            return tool(*args, **kwargs)
        except _ENVELOPED_ERRORS as exc:
            logger.warning("tool %s failed: %s: %s", tool.__name__, type(exc).__name__, exc)
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    return enveloped


@mcp.tool()
@_error_envelope
def check_health() -> dict[str, object]:
    """Verify TagMend is ready to use.

    Checks that settings load, the configured music folder is reachable and
    readable, and the SQLite ledger opens. Returns ``{"ok": True, "ready", "checks"}`` with
    one entry per check. ``ready`` is True only when every check passes. ``ok`` reports that
    the request ran. Call this from the MCP Inspector to confirm the environment is wired up
    correctly before building or running anything else.
    """
    settings = load_settings()
    report = health.check_health(settings)
    return {"ok": True, **report.to_dict()}


@mcp.tool()
@_error_envelope
def scan_library(
    path: str | None = None,
    mode: Literal["incremental", "full", "presence"] = "incremental",
) -> dict[str, object]:
    """Scan a music folder into TagMend's snapshot database (reads files, never writes them).

    Walks the folder, records each audio file under a stable id, and stores its
    normalized tags. This is the read path: it only writes to the SQLite ledger, never
    to the music files themselves.

    Args:
        path: Folder to scan. Defaults to the configured ``music_path`` when omitted. A
            relative path resolves under ``music_path``, and a folder outside ``music_path`` is
            refused. The walk stores the configured ``music_path`` spelling plus each folder's
            on-disk name, so a path typed in another case finds the same files.
        mode: ``incremental`` re-reads tags only when a file changed or was never read;
            ``full`` re-reads every file's tags; ``presence`` only reconciles which
            files exist (added/missing/restored) without reading any tags.

    Returns:
        Per-run counts (``added``, ``updated``, ``tags_read``, ``missing_flagged``,
        ``restored``, ``errors``, ``respelled``, ...) plus ``ok``. ``respelled`` counts known
        files found under a new spelling of the same path (a case-only rename on Windows),
        which keep their id and history. ``updated`` counts files whose
        on-disk size/mtime signature changed since the last scan. ``tags_read`` counts
        files whose tags were re-read and actually differed from the stored snapshot (an
        identical re-read is an honest no-op and is not tallied). On a configuration/path
        problem, returns ``{"ok": False, "error": <message>}``.
    """
    result = library.scan_library(
        load_settings(),
        path=Path(path) if path is not None else None,
        mode=ScanMode(mode),
    )
    return {"ok": True, **result.to_dict()}


@mcp.tool()
@_error_envelope
def get_library_stats() -> dict[str, object]:
    """Report library-wide snapshot counts.

    Returns totals for tracked files, how many are present vs missing on disk, how many
    are still ``unprocessed`` (no tags read yet), a per-extension breakdown, the total
    number of stored tag values, and four per-axis workflow-state blocks — each a
    progress gauge for one resolver, drilled into with the matching ``list_files`` filter:

    * ``genre`` — ``pending`` / ``no_identity`` / ``staged`` / ``done`` / ``no_match`` /
      ``manual`` for ``resolve_genres``; drill with ``list_files(genre_status=...)``.
    * ``artist`` — ``pending`` / ``no_identity`` / ``staged`` / ``done`` / ``manual`` (no
      ``no_match`` on this axis) for ``resolve_artists``; drill with
      ``list_files(artist_status=...)``.
    * ``year`` — ``pending`` / ``no_identity`` / ``staged`` / ``done`` / ``no_match`` /
      ``manual`` for ``resolve_years``; drill with ``list_files(year_status=...)``.
    * ``mismatch`` — ``pending`` / ``legit_ignore`` / ``misfiled_deferred`` for
      ``detect_mismatches`` dispositions; drill with ``list_files(mismatch_status=...)``.
    """
    return {"ok": True, **library.get_library_stats(load_settings())}


@mcp.tool()
@_error_envelope
def stage_tags(
    file_id: int,
    tags: dict[str, list[str]],
    note: str | None = None,
) -> dict[str, object]:
    """Stage a managed-tag change for one file (the git "index"). Writes nothing to disk.

    Records *tags* for *file_id*, replacing any pending change for that file. Only managed
    tags are allowed. The closed ``tags.MANAGED_TAGS`` set holds 26 keys: ``genre``, ``artist``,
    the multi-value ``artists`` list, ``albumartist``, ``artistsort``, ``albumartistsort``,
    ``title``, ``album``, ``date``, ``originaldate``, ``tracknumber``, ``discnumber``, the six
    MusicBrainz ids (``musicbrainz_artistid``, ``musicbrainz_albumartistid``,
    ``musicbrainz_albumid``, ``musicbrainz_releasegroupid``, ``musicbrainz_trackid``,
    ``musicbrainz_releasetrackid``), ``musicbrainz_albumtype``, and the release stamp
    (``musicbrainz_albumstatus``, ``media``, ``releasecountry``, ``barcode``, ``catalognumber``,
    ``isrc``, ``asin``). Any other key is rejected. The music file is not touched and no history
    is recorded until you call ``commit_tags``.

    *tags* is merged **onto** the file's current managed tags: keys you omit are left
    alone, so staging ``{"genre": ["Synthwave"]}`` changes only the genre and preserves
    title/album/track/MusicBrainz ids. To intentionally clear a managed field, pass it
    explicitly with an empty list (e.g. ``{"genre": []}``).

    Every value is stripped of leading and trailing whitespace and NFC-normalized, and a value
    left empty is dropped (a list left empty clears the field). A value containing a NUL, CR or
    LF is rejected and nothing is staged.

    Args:
        file_id: Stable id of the file (from ``scan_library`` / the snapshot).
        tags: Managed tags to set, as name -> ordered values, e.g. ``{"genre": ["Synthwave"]}``.
            Omitted managed keys are preserved; ``{"key": []}`` deletes that key.
        note: Optional free-text note stored with the eventual revision.

    Returns:
        ``{"ok": True}`` on success, or ``{"ok": False, "error": ...}`` on a bad request.
    """
    staging.stage_tags(
        load_settings(),
        file_id=file_id,
        tags=tags,
        note=note,
    )
    return {"ok": True}


@mcp.tool()
@_error_envelope
def stage_tags_batch(
    entries: list[dict[str, object]],
    note: str | None = None,
) -> dict[str, object]:
    """Stage managed-tag changes for MANY files in one atomic, all-or-nothing call (no disk write).

    The batch counterpart of ``stage_tags`` for the mismatch-fix flow: pass one entry per file
    and every change is staged in a single transaction. If ANY entry is invalid (an unmanaged
    tag key, an unknown/missing file, or a duplicate ``file_id`` in the batch) the whole batch
    is rejected and NOTHING is staged. Each entry's ``tags`` is merged onto that file's current
    managed tags exactly like ``stage_tags``: omitted keys are preserved, and ``{"key": []}``
    deletes. Values are cleaned the same way. They are stripped and NFC-normalized, and a value
    holding a NUL, CR or LF rejects the whole batch. A subsequent
    ``commit_tags(path=<folder>)`` groups the batch into ONE revertible commit.
    ``commit_tags(path=<folder>)`` and ``diff_tags(path=<folder>)`` cover that folder AND every
    folder nested under it. Run ``diff_tags(path=<folder>)`` first and ``unstage_tags`` any
    nested change you do not want in this commit.

    ``tracknumber``/``discnumber`` are staged verbatim. Supply the full ``"n/total"`` string
    (e.g. ``"3/12"``). This tool never parses or renumbers them.

    Args:
        entries: A list of ``{"file_id": <int>, "tags": {name: [values], ...}}`` objects.
        note: Optional free-text note stored with each eventual revision.

    Returns:
        ``{"ok": True, "staged": <count>, "file_ids": [...]}`` on success, or
        ``{"ok": False, "error": ...}`` on a bad request (nothing staged).
    """
    pairs = [(entry.get("file_id"), entry.get("tags")) for entry in entries]
    staged = staging.stage_tags_batch(load_settings(), entries=pairs, note=note)
    return {"ok": True, "staged": len(staged), "file_ids": staged}


@mcp.tool()
@_error_envelope
def unstage_tags(file_id: int) -> dict[str, object]:
    """Remove a pending staged change for one file.

    Returns ``{"ok": True, "removed": <bool>}``, where ``removed`` is ``False`` when the file
    had nothing staged, or ``{"ok": False, "error": ...}`` if the file id is unknown.
    """
    removed = staging.unstage_tags(load_settings(), file_id=file_id)
    return {"ok": True, "removed": removed}


@mcp.tool()
@_error_envelope
def diff_tags(path: str | None = None) -> dict[str, object]:
    """Show staged-but-uncommitted tag changes, enriched with the current→target diff.

    This is ``git diff --staged``. ``current`` is read from the FILE, so ``diff`` is exactly
    what the commit will change on disk. A no-op stage still appears, with ``diff == {}``.

    ``stale_identity`` is the one thing to read before committing an identity fix. Staging
    merges onto the file's current tags, so a change that rewrites ``artist`` but omits
    ``musicbrainz_artistid`` keeps the OLD artist's id. The file then names one artist and
    points at another, and Picard or Navidrome will re-link it to the wrong one. Each entry is
    ``{changed, stale_field, stale_value}``: the coupled field this change leaves behind, and
    the value it keeps. It is a REPORT, never a block. Supply the matching id (or an empty
    list to clear it) and re-stage if the retained value is wrong. Coupled groups are
    artist/albumartist with their MB ids and sort names, album with its album + release-group
    ids, title with its track + release-track ids, and album or its release id with the
    release-stamp fields. A sort-name or release-stamp change alone flags nothing.

    Args:
        path: When given, only staged changes for files at this folder or nested under it
            are returned. Otherwise all staged changes are listed. Compared as a path: case
            and ``/`` versus backslash do not matter on Windows, and a relative folder resolves
            under ``music_path``. ``commit_tags(path=<folder>)`` and ``diff_tags(path=<folder>)``
            cover that folder AND every folder nested under it. Run ``diff_tags(path=<folder>)``
            first and ``unstage_tags`` any nested change you do not want in this commit.

    Returns:
        ``{"ok": True, "changes": [{file_id, folder, filename, is_missing, origin, note,
        staged_at, current, target, diff, stale_identity}, ...]}``.
    """
    changes = staging.diff_tags(
        load_settings(),
        path=Path(path) if path is not None else None,
    )
    return {"ok": True, "changes": [view.to_dict() for view in changes]}


@mcp.tool()
@_error_envelope
def commit_tags(message: str | None = None, path: str | None = None) -> dict[str, object]:
    """Apply all staged tag changes to disk as one revertible commit.

    Writes each staged file's target tags to disk and appends an append-only revision
    under a shared commit id, so the whole batch reverts as a unit. The commit's ``origin`` is
    ``auto`` only when every change it sweeps came from a resolver, and ``manual`` otherwise.
    Files that vanished from disk since staging are flagged missing, dropped, and reported under
    ``missing_files``. Any commit left ``applying`` by a prior crash is marked interrupted
    first and its leftover staged rows are swept into this commit.

    A file edited on disk after it was staged (by Picard or any other tagger) is refused with
    ``status: "changed_since_stage"`` and a ``detail``, so the commit never overwrites the
    edit. Any external change counts, even to an unmanaged tag or the audio: re-staging costs
    one call, and a lost edit costs the edit. Re-stage it (``stage_tags`` replaces the pending
    row) or unstage it. A file that fails to write (locked by a player, read-only) is
    reported with ``status: "error"`` and a ``detail``. Both keep their staged row, and the
    rest of the commit completes.

    Args:
        message: Optional commit message stored on the commit.
        path: When given, only staged changes for files at this folder or nested under it
            are committed. Otherwise all staged changes are committed. Compared as a path:
            case and ``/`` versus backslash do not matter on Windows, and a relative folder
            resolves under ``music_path``. ``commit_tags(path=<folder>)`` and
            ``diff_tags(path=<folder>)`` cover that folder AND every folder nested under it.
            Run ``diff_tags(path=<folder>)`` first and ``unstage_tags`` any nested change you
            do not want in this commit.

    Returns:
        ``{"ok": True, ...}`` with per-file ``outcomes`` (each ``{file_id, version, status,
        detail}``) and ``committed`` / ``noop`` / ``missing`` / ``changed_since_stage`` /
        ``errors`` counts, or ``{"ok": False, "error": ...}`` on a bad request.
    """
    result = staging.commit_tags(
        load_settings(),
        message=message,
        path=Path(path) if path is not None else None,
    )
    return {"ok": True, **result.to_dict()}


@mcp.tool()
@_error_envelope
def reopen_axes(commit_id: int) -> dict[str, object]:
    """Re-open the genre, artist and year axes after a manual identity fix (call it AFTER one).

    The spine is ``stage_tags_batch`` -> ``diff_tags`` -> ``commit_tags`` -> ``reopen_axes``.
    For every file the given commit changed, this deletes the ``done`` and ``no_match`` status
    rows on all three axes, so ``resolve_genres``, ``resolve_artists`` and ``resolve_years``
    re-derive them against the fixed tags. A ``manual`` row is kept: ``reset_<axis>_status`` is
    its only hand-back. Call ``reset_artist_status`` next when a fixed name should be
    re-checked by the normaliser. History is never touched.

    Call it with a ``manual`` (or ``revert``) commit id from ``commit_tags`` / ``list_commits``.
    A commit holding any auto-resolved revision is refused, whatever its own origin, since
    re-opening fresh auto work would only repeat it. A commit that changed no tags is refused
    too.

    Returns:
        ``{"ok": True, "commit_id": ..., "files": <count>, "genre": {outcomes_reopened,
        manual_kept}, "artist": {...}, "year": {...}}``, or ``{"ok": False, "error": ...}`` if
        the commit id is unknown, holds an auto-resolved revision, or changed no tags.
    """
    result = staging.reopen_axes(load_settings(), commit_id=commit_id)
    return {"ok": True, **result.to_dict()}


@mcp.tool()
@_error_envelope
def list_files(  # noqa: PLR0913 - cohesive MCP discovery filters
    path: str | None = None,
    limit: int | None = None,
    genre_status: Literal["pending", "no_identity", "no_match", "manual", "staged", "done"]
    | None = None,
    artist_status: Literal["pending", "no_identity", "no_match", "manual", "staged", "done"]
    | None = None,
    year_status: Literal["pending", "no_identity", "no_match", "manual", "staged", "done"]
    | None = None,
    mismatch_status: Literal["pending", "legit_ignore", "misfiled_deferred"] | None = None,
) -> dict[str, object]:
    """List tracked files with their current managed tags (to discover file ids).

    Each entry carries the stable ``file_id`` you pass to ``stage_tags`` / ``history_tags``
    / ``revert_tags``, plus the file's folder/filename, current managed tags (the
    ``MANAGED_TAGS`` set listed under ``stage_tags``), and its genre workflow ``genre_status``. Run
    ``scan_library`` first to populate the snapshot.

    ``genre_status="no_match"`` is the **fix-by-hand worklist**: files Last.fm had nothing
    for. Each carries ``genre_source_artist``/``genre_source_album``, the identity the lookup
    used, so a misspelled artist/album is visible next to the current tags. Fix the tags,
    rescan, and the file reads ``pending`` again on its own. ``manual`` lists the sticky human
    decisions (``set_genre_status`` or a committed hand edit of the field). A ``done`` or
    ``no_match`` status counts only while the identity and the field values it was decided on
    still match the file. ``no_identity`` lists the files an axis has nothing to look up for
    (neither ``artist`` nor ``albumartist``, and on the year axis also no ``album``).
    Whitespace-only counts as blank, and no resolver selects them. A genre, artist or year
    filter never returns a missing file.

    Args:
        path: When given, only files at this folder or nested under it are returned.
            Compared as a path: case and ``/`` versus backslash do not matter on Windows, and a
            relative folder resolves under ``music_path``.
        limit: Cap the number of files returned. With any status filter the cap counts
            matching files. Without one it is applied before reading tags.
        genre_status: Return only files in this genre workflow state
            (``pending`` | ``no_identity`` | ``no_match`` | ``manual`` | ``staged`` | ``done``).
        artist_status: Return only files in this artist workflow state
            (``pending`` | ``no_identity`` | ``no_match`` | ``manual`` | ``staged`` | ``done``).
            ``no_match`` holds the files whose names were held for review (a ``feat`` credit,
            no correction, a credit shrink, a name/id disagreement) or are multi-value.
            Combined with ``genre_status``, a file must match BOTH. The axes are independent and
            field-aware: genre keys on ``genre``, artist on the name, id and sort-name fields,
            year on ``originaldate``.
        year_status: Return only files in this year workflow state
            (``pending`` | ``no_identity`` | ``no_match`` | ``manual`` | ``staged`` | ``done``).
            Combined with the other filters, a file must match ALL. ``year_source_artist`` /
            ``year_source_album`` carry the resolved identity a stored decision was taken
            against.
        mismatch_status: Return only files with this mismatch disposition
            (``pending`` | ``legit_ignore`` | ``misfiled_deferred``). Combined with the other
            filters, a file must match ALL. ``mismatch_source_field`` / ``mismatch_source_value``
            carry the disagreeing tag a stored disposition was recorded against.

    Returns:
        ``{"ok": True, "files": [{file_id, folder, filename, ext, is_missing,
        managed_tags, genre_status, genre_source_artist, genre_source_album,
        artist_status, artist_source_artist, artist_source_albumartist, year_status,
        year_source_artist, year_source_album, mismatch_status, mismatch_source_field,
        mismatch_source_value}, ...]}``,
        or ``{"ok": False, "error": ...}`` on a bad request.
    """
    views = library.list_files(
        load_settings(),
        path=Path(path) if path is not None else None,
        limit=limit,
        genre_status=genre_status,
        artist_status=artist_status,
        year_status=year_status,
        mismatch_status=mismatch_status,
    )
    return {"ok": True, "files": [view.to_dict() for view in views]}


@mcp.tool()
@_error_envelope
def get_file(file_id: int) -> dict[str, object]:
    """Return one tracked file with its current managed tags, by stable ``file_id``.

    Returns ``{"ok": True, "file": {file_id, folder, filename, ext, is_missing,
    managed_tags, genre_status, genre_source_artist, genre_source_album, artist_status,
    artist_source_artist, artist_source_albumartist, year_status, year_source_artist,
    year_source_album, mismatch_status, mismatch_source_field, mismatch_source_value}}``,
    or ``{"ok": False, "error": ...}`` if the id is unknown.
    """
    view = library.get_file(load_settings(), file_id)
    if view is None:
        return {"ok": False, "error": f"unknown file_id={file_id}"}
    return {"ok": True, "file": view.to_dict()}


@mcp.tool()
@_error_envelope
def detect_mismatches(
    tier: Literal["high", "medium", "low"] | None = None,
    limit: int | None = None,
    group: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
    folder: str | None = None,
) -> dict[str, object]:
    """Detect files whose identity tags disagree with their folder path (read-only report).

    Flags the fingerprint of a MusicBrainz Picard release mis-match: files stamped with the
    WRONG ``albumartist`` (with an ``artist`` fallback for files that have none) while their
    folder path kept the truth — e.g. Ozzy's *Down to Earth* files tagged as *Jem*. Pure read
    over the snapshot: writes nothing, stages nothing, no network. Run ``scan_library`` first.

    Recommended workflow: start with ``group=true`` for a compact one-line-per-folder overview
    (cheap on a big library), then expand a single folder with ``folder="<exact folder path>"``
    to see its flagged rows, research the correct identity, and fix them with ``stage_tags_batch``
    → ``commit_tags(path=<folder>)`` → ``reopen_axes``. ``commit_tags(path=<folder>)`` and
    ``diff_tags(path=<folder>)`` cover that folder AND every folder nested under it. Run
    ``diff_tags(path=<folder>)`` first and ``unstage_tags`` any nested change you do not want in
    this commit. Silence a false positive or defer a misfiled file with ``set_mismatch_status``.
    Such files are dropped from the flagged rows and reported under ``suppressed`` (a
    disposition-status → count map), so nothing is hidden silently. The disposition goes stale
    (and the file re-surfaces) if its identity tag changes.

    Each file is classified by ``albumartist``-vs-path bidirectional containment into a
    confidence tier: ``high`` (path disagreement in a folder with mixed albumartists),
    ``medium`` (path disagreement in a uniformly mis-stamped folder), or ``low`` (a
    folder-consistency fallback, a non-album/singles folder, or the artist fallback).
    Various-Artists/soundtrack albumartists are excluded. A library-wide reliability guard
    suppresses the ``high``/``medium`` path tiers (emitting only the naming-agnostic ``low``
    tier) when the path likely does not encode artist — reported via ``path_signal_suppressed``
    and ``disagreement_rate``.

    Configure the ``container_folders`` setting (a semicolon-delimited list via
    ``tagmend config-set container_folders "<TopFolder>;<Other>"``) to name top-level folders
    that hold many unrelated albumartists. Files under such a folder have their path signal
    suppressed — they never flag on the ``path='<container>'`` signal (present and future) and
    are counted in the ``container_suppressed`` map — while a mixed-albumartist album folder
    INSIDE a container still surfaces as ``low`` (in-container misfile detection is preserved).

    ``flagged`` counts only files that need work. A file sitting in a mixed-albumartist folder
    while agreeing with its OWN path is review context, not a defect: it is reported in
    ``folder_context_rows`` and counted by ``folder_context``, outside ``flagged`` and the
    tier counts (so the tier counts always sum to ``flagged``). Such rows usually clear
    themselves once the folder's real mismatch is fixed. A file that disagrees, or whose path
    signal is unavailable (reliability guard, container folder, library root), stays flagged.

    Args:
        tier: Return only rows/groups in this tier (``high`` | ``medium`` | ``low``). The
            tier filters rows first, and the grouped view is built from the filtered rows, so a
            group appears only when it holds a file of that tier and its ``flagged``,
            ``file_ids`` and tier counts describe only those files. The
            ``high``/``medium``/``low``/``flagged``/``folder_context`` counts still describe
            the whole library, and context rows are never returned under a tier filter.
        limit: Cap the number of rows returned (or groups, with ``group=true``); counts
            unaffected.
        group: Return one compact group per folder instead of flat rows (``rows`` is then
            empty; each group carries ``folder``, ``path_artist``, ``file_count``, ``flagged``,
            ``folder_context``, ``tag_values``, ``tiers``, ``fields``, ``file_ids``,
            ``suppressed`` — the per-folder ``file_ids`` list holds flagged files only).
        folder: Return the flat rows of exactly this folder, never a subfolder. Takes
            precedence over ``group``. Compared as a path: case and ``/`` versus backslash do
            not matter on Windows, and a relative folder resolves under ``music_path``.

    Returns:
        ``{"ok": True, rows, folder_context_rows, groups, total_files, flagged, high, medium,
        low, folder_context, disagreement_rate, path_signal_suppressed, suppressed,
        container_suppressed, summary}``
        — ``container_suppressed`` is a top-folder → file-count map (files whose path signal a
        ``container_folders`` entry suppressed); each row is
        ``{file_id, folder, filename, field, tag_value, path_artist, tier, reason}`` — or
        ``{"ok": False, "error": ...}`` (e.g. no music path configured).
    """
    report = mismatch.detect_mismatches(
        load_settings(),
        tier=tier,
        limit=limit,
        group=group,
        folder=folder,
    )
    return {"ok": True, **report.to_dict()}


@mcp.tool()
@_error_envelope
def detect_track_conflicts(
    tier: Literal["high", "medium", "low"] | None = None,
    limit: int | None = None,
    group: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
    folder: str | None = None,
) -> dict[str, object]:
    """Find files that share a ``(disc, track)`` slot with a sibling in the same folder.

    The intra-folder half of tag coherence, and the sibling of ``detect_mismatches`` (which
    compares tags against the folder PATH). A folder holding one album describes ONE tracklist,
    so no two files in it may claim the same track number. When two do, one is wrong: a bulk
    tagger stamped a whole folder with one track's numbers, a duplicate was never renumbered,
    or two takes matched the same MusicBrainz recording. Pure read over the snapshot: writes
    nothing, stages nothing, no network. Run ``scan_library`` first.

    Recommended workflow: start with ``group=true`` for one line per folder, then expand a
    single folder with ``folder="<exact folder path>"`` to see its rows, research the correct
    tracklist, and fix with ``stage_tags_batch`` -> ``diff_tags`` -> ``commit_tags(path=<folder>)``
    -> ``reopen_axes``. Read ``diff_tags``' ``stale_identity`` before committing.
    ``commit_tags(path=<folder>)`` and ``diff_tags(path=<folder>)`` cover that folder AND every
    folder nested under it. Run ``diff_tags(path=<folder>)`` first and ``unstage_tags`` any
    nested change you do not want in this commit.

    Tiers, matching the shapes a real library contains:

    * ``high`` — the colliding files carry DIFFERENT titles. Two distinct songs cannot both be
      track 7, so the numbering is wrong.
    * ``medium`` — same title, same container: a genuine duplicate track stamp.
    * ``low`` — same title, different container (an ``.mp3`` and a ``.flac`` of one song),
      usually a deliberate duplicate encode rather than a defect.

    A folder holding more than one album, or named ``Singles``/``Remixes``/``Featured``/etc.,
    legitimately repeats track numbers — every single is track 1. Those rows are reported under
    ``folder_context`` instead, outside ``flagged`` and outside the tier counts, so ``flagged``
    keeps meaning "files that are wrong". The guard is derived from the folder's own ``album``
    values, so it needs no configuration.

    Track TOTALS are deliberately not reported: a folder of 10 files whose tags say ``/12`` is
    either missing two tracks or carrying a wrong total, and only a MusicBrainz release
    tracklist can tell which.

    Args:
        tier: Keep only rows in this tier (``high`` | ``medium`` | ``low``).
        limit: Cap the rows returned, or the groups with ``group=true``.
        group: Return one compact line per folder instead of flat rows.
        folder: Expand exactly this folder's rows, never a subfolder. Takes precedence over
            ``group``. Compared as a path: case and ``/`` versus backslash do not matter on
            Windows, and a relative folder resolves under ``music_path``.

    Returns:
        ``{"ok": True, rows, total_files, flagged, high, medium, low, folder_context,
        folder_context_rows, groups, summary}`` — each row is ``{file_id, folder, filename,
        disc, track, title, tier, reason, peers}`` where ``peers`` names the other file ids in
        that slot, and each group is ``{folder, file_count, flagged, folder_context, slots,
        tiers, file_ids}`` — or ``{"ok": False, "error": ...}``.
    """
    report = track_conflicts.detect_track_conflicts(
        load_settings(),
        tier=tier,
        limit=limit,
        group=group,
        folder=folder,
    )
    return {"ok": True, **report.to_dict()}


@mcp.tool()
@_error_envelope
def detect_disagreements(  # noqa: PLR0913 - one parameter per scope/view knob, cohesive
    tier: Literal["high", "medium", "low"] | None = None,
    path: str | None = None,
    folder: str | None = None,
    file_ids: list[int] | None = None,
    release_limit: int | None = None,
    limit: int | None = None,
    group: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
) -> dict[str, object]:
    """Find files whose tags contradict the MusicBrainz release their album id names.

    The third comparison in the coherence family. ``detect_mismatches`` compares a file's tags
    against the folder PATH, ``detect_album_conflicts`` and ``detect_track_conflicts`` compare
    a file against its folder SIBLINGS, and this compares a file against an EXTERNAL
    authority: the release its own ``musicbrainz_albumid`` names. That id makes it a direct
    lookup with nothing to guess, which is what lets it say what a tag SHOULD be rather than
    only that something is wrong.

    Inside the release, a file finds its own track by ``musicbrainz_releasetrackid`` (which
    names a track on this release) or, failing that, ``musicbrainz_trackid`` (the recording).
    Position is deliberately never used to match: a file whose numbering is wrong is exactly
    what this reports, so matching on it would hide the defect.

    Release-level fields (``album``, ``albumartist``, ``date``, ``releasecountry``,
    ``musicbrainz_albumstatus``) are checked even for a file carrying no track id.
    Track-level fields (``title``, ``artist``, ``tracknumber``, ``discnumber``) need a matched
    track and are skipped without one. ``artist`` is track-level because a credit belongs to a
    track: a guest track carries its own. No ``discnumber`` is proposed for a single-medium
    release, since Picard routinely omits it there.

    **A blank field is a fill, not a disagreement.** ``flagged`` counts the files with at least
    one field where the file says one thing and the release says another, and
    ``flagged_fields`` counts those fields. Fields the file simply lacks are collected under
    ``fill_rows``/``fills``, outside ``flagged`` and outside the tier counts.

    Tiers: ``high`` when the file's release-track id is not on the release at all (a broken
    identity), ``medium`` for a field that decides how a library groups, names or orders the
    file, ``low`` for release provenance. Each file counts once, in the tier of its most
    severe row, so the tier counts sum to ``flagged``. The ``tier`` filter selects rows by
    each row's own tier.

    Each distinct release is fetched once and cached, paced at MusicBrainz's requested one
    request per second. ``release_limit`` caps the releases fetched this call (default 200,
    about three minutes) and the rest are reported under ``releases_remaining``/``more``.
    ``path`` and ``file_ids`` scope the run: the counts describe the run, and no release outside
    it is fetched. ``folder`` narrows the view of a scoped run and wins over ``group``, so a bare
    ``folder`` with neither ``path`` nor ``file_ids`` is refused rather than starting a
    library-wide fetch. Reads the snapshot, so run ``scan_library`` first. Writes no tags and
    stages nothing.

    Recommended workflow: ``group=true`` for one line per folder, then ``path="<folder>"`` to
    expand one folder (nested disc folders included), then fix with ``stage_tags_batch`` ->
    ``diff_tags`` -> ``commit_tags(path=<folder>)`` -> ``reopen_axes``.
    ``commit_tags(path=<folder>)`` and ``diff_tags(path=<folder>)`` cover that folder AND every
    folder nested under it. Run ``diff_tags(path=<folder>)`` first and ``unstage_tags`` any
    nested change you do not want in this commit.

    Args:
        tier: Keep only rows in this tier (``high`` | ``medium`` | ``low``). The tier filters
            rows first, and the grouped view is built from the filtered rows, so a group appears
            only when it holds a file of that tier and its ``flagged``, ``file_ids`` and tier
            counts describe only those files. The report-level counts still describe the run.
        path: Scope the run to this folder and every folder under it. Compared as a path: case
            and ``/`` versus backslash do not matter on Windows, and a relative path resolves
            under ``music_path``.
        folder: Narrow a scoped run's view to exactly this folder, never a subfolder. Takes
            precedence over ``group``. Needs ``path`` or ``file_ids``. Compared as a path like
            ``path``.
        file_ids: Scope the run to these specific file ids.
        release_limit: Max distinct releases to FETCH this call.
        limit: Cap the rows returned (or groups, with ``group=true``) without changing any
            count.
        group: Return one compact line per folder instead of flat rows.

    Returns:
        ``{"ok": True, rows, fill_rows, total_files, flagged, flagged_fields, high, medium, low,
        fills, releases_attempted, releases_checked, releases_remaining, more,
        skipped_no_release_mbid, unknown_releases, unmatched_tracks, errors, error_items, groups,
        summary}``, where ``error_items`` is ``{key, message}`` keyed by the release id. Each
        row is ``{file_id, folder, filename, release_mbid, release_title, field, have, want,
        tier, reason}``. Each group is ``{folder, file_count, flagged,
        folder_context, tiers, file_ids, flagged_fields, fills, fields, releases}``, where
        ``file_ids`` names the flagged files only and ``releases`` lists ``{release_mbid,
        release_title, file_count}``. On failure, ``{"ok": False, "error": ...}``.
    """
    report = disagreements.detect_disagreements(
        load_settings(),
        tier=tier,
        path=Path(path) if path is not None else None,
        folder=folder,
        file_ids=file_ids,
        release_limit=release_limit,
        limit=limit,
        group=group,
    )
    return {"ok": True, **report.to_dict()}


@mcp.tool()
@_error_envelope
def detect_album_conflicts(
    tier: Literal["high", "medium", "low"] | None = None,
    limit: int | None = None,
    group: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
    folder: str | None = None,
) -> dict[str, object]:
    """Find files whose album identity differs from their folder siblings'.

    The release-level sibling of ``detect_track_conflicts`` (which compares a file's track slot
    against its folder siblings) and of ``detect_mismatches`` (which compares tags against the
    folder PATH). A folder holding one album describes ONE release, so when its files disagree
    about which release that is, every music server presents the one album as several. Pure
    read over the snapshot: writes nothing, stages nothing, no network. Run ``scan_library``
    first.

    A file's album identity is ``musicbrainz_albumid`` when it carries one, and otherwise the
    display album artist, the album title and the release date (``date``, else a raw Vorbis
    ``year``). The display album artist is ``albumartist``, falling back to ``Various
    Artists`` when the ``compilation`` flag is set, then to ``artist``, then to an
    unknown-artist placeholder. Casing, typographic character choice and whitespace runs are
    cosmetic and never split a folder. Punctuation is NOT
    cosmetic: ``The Crow: City of Angels`` and ``The Crow- City Of Angels`` are two albums
    downstream, which is exactly the kind of split this finds.

    Only the MINORITY is flagged: the files whose identity differs from the one most of the
    folder shares. That is the fix direction, so the flagged ids go straight to
    ``stage_tags_batch``, and a group's ``file_ids`` names those flagged files only.
    ``majority_identity`` on every row and group names what to normalize toward.

    Tiers:

    * ``high``: the file's ``musicbrainz_albumid`` differs from its folder's, or it has none
      while its siblings do. An id is an explicit claim about which release this is, and a
      server keyed on it separates the two however identical the album strings look.
    * ``medium``: no ids involved, and the album artist, album title or release date
      disagrees.
    * ``low``: the album titles differ only by a ``(disc N: …)`` suffix on one release title.
      That is deliberate Picard output for a titled multi-disc medium. It still shows as
      several albums, so you may still want to normalize it.

    One shape gets its own case. When every file in a folder agrees on the album title, none
    carries an ``albumartist``, and no track artist holds half the folder, it is a compilation
    missing its album artist. Every file falls back to its own artist, so the one album shows
    as one card per track. Every file is flagged, because every file needs the same fix.

    A folder named ``Singles``/``Remixes``/``Featured``/etc. holds several releases by design.
    Its rows are reported under ``folder_context`` instead, outside ``flagged`` and outside the
    tier counts, so ``flagged`` keeps meaning "files that are wrong". Every filter applies to
    those rows too, and ``groups`` are returned only with ``group=true`` and no ``folder``.

    Recommended workflow: start with ``group=true`` for one line per folder, then expand a
    single folder with ``folder="<exact folder path>"``, then fix with ``stage_tags_batch`` ->
    ``diff_tags`` -> ``commit_tags(path=<folder>)`` -> ``reopen_axes``. Read ``diff_tags``'
    ``stale_identity`` before committing. ``commit_tags(path=<folder>)`` and
    ``diff_tags(path=<folder>)`` cover that folder AND every folder nested under it. Run
    ``diff_tags(path=<folder>)`` first and ``unstage_tags`` any nested change you do not want in
    this commit.

    Args:
        tier: Keep only rows in this tier (``high`` | ``medium`` | ``low``). The tier filters
            rows first, and the grouped view is built from the filtered rows, so a group appears
            only when it holds a file of that tier and its ``flagged``, ``file_ids`` and tier
            counts describe only those files. The report-level counts still describe the whole
            library.
        limit: Cap the rows returned, or the groups with ``group=true``. Counts are
            unaffected.
        group: Return one compact line per folder instead of flat rows.
        folder: Expand exactly this folder's rows, never a subfolder. Takes precedence over
            ``group``. Compared as a path: case and ``/`` versus backslash do not matter on
            Windows, and a relative folder resolves under ``music_path``.

    Returns:
        ``{"ok": True, rows, total_files, flagged, high, medium, low, folder_context,
        folder_context_rows, groups, summary}``. Each row is ``{file_id, folder, filename,
        album, albumartist, release_mbid, date, identity, majority_identity, tier, reason}``.
        Each group is ``{folder, file_count, flagged, folder_context, tiers, file_ids,
        identities, majority_identity, majority_files}``, sorted by folder, where
        ``file_count`` counts every present file in the folder and ``file_ids`` names the
        flagged files only. On failure, ``{"ok": False, "error": ...}``.
    """
    report = album_conflicts.detect_album_conflicts(
        load_settings(),
        tier=tier,
        limit=limit,
        group=group,
        folder=folder,
    )
    return {"ok": True, **report.to_dict()}


@mcp.tool()
@_error_envelope
def detect_album_gaps(
    limit: int | None = None,
    folder: str | None = None,
    use_musicbrainz: bool = True,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
) -> dict[str, object]:
    """Find blank-``album`` files, grouped by folder, with grounded fill proposals.

    The blank-``album`` companion to ``detect_mismatches``: 92-ish files carry no ``album`` at
    all, so ``resolve_years`` skips them and nothing else can even list them. This groups every
    file whose ``album`` is blank (across ALL tag ordinals) by its folder and, per folder,
    proposes a fill from one of three grounded sources, or leaves it blank when there is no
    defensible ground. Writes nothing to tags/status/staging. Only the recording source touches
    the ledger (its lookup cache). Run ``scan_library`` first.

    Binding guarantee: a proposal is emitted ONLY for a file whose ``album`` is blank on every
    ordinal, so acting on the report can never overwrite a real album value.

    The three sources:

    * ``sibling``: the blank files' folder mates share a single non-blank ``album`` value
      (unanimous). Confidence is ``green`` (safe to bulk-stage) only when there are >=2
      witnesses AND the value is neither a known genre nor a placeholder. Otherwise it is
      ``confirm`` with a reason: ``genre_like`` (a genre string in the album field, e.g.
      "Reggaeton"), ``placeholder`` (a folder label like "Unreleased") or ``n1_weak`` (a lone
      witness). Mixed sibling values (no unanimity) yield NO proposal, so the folder stays
      blank.
    * ``folder_parse``: a folder with NO sibling values at all whose leaf name parses as
      ``Artist - Year - Album`` / ``Artist - Album``. Proposed (always ``confirm``) only when
      the folder's own filenames self-corroborate the parsed album above a fixed threshold, so a
      junk folder like "Maphra - YouTube" (0 filenames corroborate) stays blank.
    * ``mb_recording``: a folder the first two sources leave blank, per blank file carrying a
      non-blank artist AND title. A cached, paced MusicBrainz ``(artist, title)`` recording
      search maps to the recording's release-group title. ALWAYS ``confidence: "review"``
      (never green) with ``reason: "mb_recording"``, so a human must confirm every one.
      ``use_musicbrainz=False`` skips this source for a network-free, local-only run. The
      client is built lazily only when such candidates exist, and results are cached so re-runs
      are network-free.

    A transient MusicBrainz error is counted in ``errors`` and itemized in ``error_items``
    (``{key, message}``), never folded into ``stays_blank``. A folder whose recording lookups
    all failed has source ``lookup_error``. Re-run to retry it.

    Recommended fix flow (the human is the diff-gate for every value): start here, expand one
    folder with ``folder="<exact folder path>"``, then per source feed the proposals'
    ``{file_id, proposed}`` + ``note`` to ``stage_tags_batch`` (one call per source keeps the
    ``note`` accurate) → review ``diff_tags(path=<folder>)`` → ``commit_tags(path=<folder>)`` →
    ``reopen_axes(commit_id)`` to re-open the filled files' genre, artist and year outcomes.
    ``commit_tags(path=<folder>)`` and ``diff_tags(path=<folder>)`` cover that folder AND every
    folder nested under it. Run ``diff_tags(path=<folder>)`` first and ``unstage_tags`` any
    nested change you do not want in this commit.

    Args:
        limit: Cap the number of folder groups returned. The ``total_files``/``green``/
            ``confirm``/``review``/``stays_blank``/``errors`` counts still describe the whole
            library.
        folder: Return only the group for exactly this folder, never a subfolder. Compared as
            a path: case and ``/`` versus backslash do not matter on Windows, and a relative
            folder resolves under ``music_path``.
        use_musicbrainz: When False, skip the ``mb_recording`` review source entirely (the
            sibling and folder-parse sources only, no network). Default True.

    Returns:
        ``{"ok": True, groups, total_files, total_blank, green, confirm, review, stays_blank,
        errors, error_items, summary}``, where ``green + confirm + review + stays_blank +
        errors == total_blank``. Each group is ``{folder, blank_count, file_count, file_ids,
        sibling_histogram, source, proposals, errors}``, where ``file_ids`` names the folder's
        blank-album files. Each proposal is ``{file_id, filename, proposed, confidence, reason,
        note}``. On failure, ``{"ok": False, "error": ...}`` (e.g. a corrupt genre vocabulary).
    """
    report = album_gaps.detect_album_gaps(
        load_settings(),
        limit=limit,
        folder=folder,
        use_musicbrainz=use_musicbrainz,
    )
    return {"ok": True, **report.to_dict()}


@mcp.tool()
@_error_envelope
def history_tags(file_id: int) -> dict[str, object]:
    """Show the append-only tag-revision log for one file, oldest (version 0) first.

    Each revision carries its ``version``, ``origin`` (scan|auto|manual|revert), the
    ``commit_id`` that grouped it, the full ``managed_tags`` snapshot at that version, and
    the ``diff`` from the prior version. Use a ``version`` here with ``revert_tags``.

    Returns ``{"ok": True, "history": [{version, created_at, origin, reverted_to_version,
    commit_id, managed_tags, diff, note}, ...]}`` (empty if the file has no history), or
    ``{"ok": False, "error": ...}`` if the file id is unknown.
    """
    revisions = versioning.history_tags(load_settings(), file_id)
    return {"ok": True, "history": [r.to_dict() for r in revisions]}


@mcp.tool()
@_error_envelope
def revert_tags(
    file_id: int,
    version: int,
    note: str | None = None,
    dry_run: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
) -> dict[str, object]:
    """Restore a file's managed tags to a prior ``version`` (append-only, revertible).

    Writes the target revision's tags back to disk and appends a *new* ``revert`` revision
    under its own single-file ``origin='revert'`` commit. Nothing is destroyed, you can
    revert a revert, and the revert shows in ``list_commits`` (undoable via
    ``revert_commit``). Get valid versions from ``history_tags``. Refused while the file
    has a staged change. Commit or unstage it first.

    Args:
        file_id: The file to restore.
        version: The revision to restore it to (from ``history_tags``).
        note: Optional message stored on the revert commit and its revision.
        dry_run: When true, report the status the revert would have without touching the
            file or the ledger. Every refusal of the real call still applies.

    Returns:
        ``{"ok": True, "file_id", "target_version", "new_version", "commit_id", "status",
        "dry_run"}``, where ``status`` is ``"reverted"`` (tags moved on disk) or ``"noop"``
        (the file already held the target state). A dry run returns ``null`` for
        ``commit_id`` and ``new_version``. Returns ``{"ok": False, "error": ...}`` if the file
        or version is unknown, the file is missing on disk, or the file has a pending staged
        change.
    """
    result = versioning.revert_tags(load_settings(), file_id, version, note=note, dry_run=dry_run)
    return {"ok": True, **result.to_dict()}


@mcp.tool()
@_error_envelope
def revert_commit(
    commit_id: int,
    note: str | None = None,
    dry_run: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
) -> dict[str, object]:
    """Undo an entire commit as a unit: every file it changed goes back to its pre-commit tags.

    The group counterpart of ``revert_tags``: all reverts land under ONE new
    ``origin='revert'`` commit whose ``reverted_from`` records the undone commit, so the
    rollback is itself a tracked, revertible commit. History stays append-only — nothing
    after the commit is ever lost. Find commit ids with ``list_commits``.

    Safety rules: files changed again by a LATER commit are skipped and reported
    (``skipped_later_changes`` — revert those per-file with ``revert_tags`` if really
    wanted); the staging area must be empty (commit or unstage pending work first);
    missing files are reported, not fatal. Use ``dry_run=true`` to preview the exact
    per-file plan without touching anything.

    Args:
        commit_id: The commit to undo (from ``list_commits``).
        note: Optional message stored on the new revert commit and its revisions.
        dry_run: When true, classify and report only — no disk or ledger changes
            (``commit_id`` in the result is ``null``; ``status='reverted'`` means
            "would be reverted", ``status='noop'`` means "would change nothing").

    Returns:
        ``{"ok": True, "commit_id": ..., "reverted_from": ..., "dry_run": ...,
        "reverted"/"noop"/"skipped"/"missing"/"errors": counts, "outcomes": [...]}`` with
        one outcome per file (``noop`` = the file already held its pre-commit state, so
        the audited revert revision was appended but nothing on disk moved). Returns
        ``{"ok": False, "error": ...}`` if the commit id is unknown, the commit is still
        ``applying``, or the staging area is not empty.
    """
    result = versioning.revert_commit(
        load_settings(),
        commit_id,
        note=note,
        dry_run=dry_run,
    )
    return {"ok": True, **result.to_dict()}


@mcp.tool()
@_error_envelope
def list_commits(limit: int | None = None) -> dict[str, object]:
    """List commits newest first (the revertible units that group tag changes).

    Returns ``{"ok": True, "commits": [{commit_id, created_at, origin, message, reverted_from,
    status}, ...]}``. ``status`` is ``applied`` (clean), ``applying`` (in flight), or
    ``interrupted`` (a crashed run whose leftovers were swept into a later commit).
    """
    rows = commits.list_commits(load_settings(), limit=limit)
    return {"ok": True, "commits": [c.to_dict() for c in rows]}


@mcp.tool()
@_error_envelope
def get_commit(commit_id: int) -> dict[str, object]:
    """Return one commit by id.

    Returns ``{"ok": True, "commit": {commit_id, created_at, origin, message, reverted_from,
    status}}``, or ``{"ok": False, "error": ...}`` if the id is unknown.
    """
    commit = commits.get_commit(load_settings(), commit_id)
    if commit is None:
        return {"ok": False, "error": f"unknown commit_id={commit_id}"}
    return {"ok": True, "commit": commit.to_dict()}


@mcp.tool()
@_error_envelope
def resolve_genres(
    value: str | None = None,
    album: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
) -> dict[str, object]:
    """Look up Last.fm genres for in-scope ``pending`` files and stage the result (no disk write).

    For each selected file this looks up its artist (``albumartist`` when present, else
    ``artist``) and optionally album on Last.fm, classifies the community tags against the
    controlled genre vocabulary, and settles the file. Resolved genres equal to the current
    ones record ``done``. Differing ones are staged as an ``auto`` change replacing ONLY
    ``genre`` (other managed tags are preserved) and record ``done``. No usable genre records
    ``no_match``. Review with ``diff_tags`` and apply with ``commit_tags``. ``revert_commit``
    undoes the whole commit and ``revert_tags`` undoes one file.

    Each file's status on this axis is ``pending``, ``staged``, ``done``, ``no_match``,
    ``manual`` or ``no_identity``. A ``done`` or ``no_match`` counts only while the identity and
    the field values it was decided on still match the file, so a revert, a rescan after an
    outside edit or an identity fix re-opens the file on its own.

    Only ``pending`` files are selected, so repeated ``limit``-capped calls terminate. A real
    run is refused while anything is staged, since staging would replace that pending change.
    A transient Last.fm error writes nothing, leaves that artist's files ``pending``, and is
    counted in ``errors`` and itemized in ``error_items`` (``{key, message}``, keyed by the
    looked-up artist), so a re-run retries it.

    Args:
        value: Limit to files whose ``artist`` or ``albumartist`` tag equals this value.
        album: Narrow a ``value`` scope to files whose ``album`` equals this value. Requires
            ``value``.
        file_ids: Limit to these specific file ids (overrides ``value``/``album``). An unknown
            id is refused.
        limit: Max files to settle this call (default ``genre_stage_limit``). Call again
            while ``more`` is true.
        dry_run: Preview the would-settle and would-stage counts without staging or recording
            anything. It reads the lookup cache and fetches on a cache miss.

    Returns:
        ``{"ok": True, settled, staged_files, no_match, pending_remaining, more, errors,
        error_items, no_match_artists, summary}``, or ``{"ok": False, "error": ...}`` (e.g.
        pending changes, a negative ``limit``, or no API key configured). ``settled`` counts
        the selected files that left ``pending``. ``pending_remaining`` recounts the present
        ``pending`` files in scope. ``more`` is ``settled > 0 and pending_remaining > 0`` and
        is false on a dry run.
    """
    result = genres.resolve_genres(
        load_settings(),
        value=value,
        album=album,
        file_ids=file_ids,
        limit=limit,
        dry_run=dry_run,
    )
    return {"ok": True, **result.to_dict()}


@mcp.tool()
@_error_envelope
def resolve_artists(
    value: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
) -> dict[str, object]:
    """Normalize artist names against MusicBrainz, then Last.fm, and stage the result.

    Two tiers, tried in that order, so the strongest evidence decides first.

    **The MusicBrainz name tier** handles every value whose files already carry a
    ``musicbrainz_artistid`` (or ``musicbrainz_albumartistid`` for ``albumartist``, or the
    ``musicbrainz_artistid`` entry at an ``artists`` element's own index while the two lists
    have equal length). That id is the file's own claim about who the artist is, so this is a
    direct lookup with no search and no candidate ranking. The canonical name and the artist's
    registered aliases then settle the value: a spelling differing only in casing, typography
    or a dash-vs-space word break is the same name (staged, ``source: musicbrainz``); a name
    MusicBrainz records as an alias of this artist is merged onto the canonical one (staged,
    ``source: musicbrainz_alias``). MusicBrainz casing IS trusted here, unlike Last.fm's.

    **The Last.fm correction tier** sees only what is left: values carrying no MBID anywhere,
    and values whose MBID MusicBrainz does not know. Its gate is unchanged and stricter,
    because Last.fm has no id to anchor it (``source: lastfm``).

    The selection is the first ``limit`` ``pending`` files in scope, and every name value on
    them is resolved: ``artist``, ``albumartist`` and each element of the multi-value
    ``artists`` list a library server builds its artist entities from. Where a value resolves,
    the canonical name cascade-stages across every in-scope file carrying it (rewriting
    ``artist``, ``albumartist`` and each equal ``artists`` element, exact-match only) plus that
    field's OWN id field, as an ``auto`` change replacing ONLY those fields (every other managed
    tag, incl. ``genre``, is preserved). An ``artists`` element is rewritten in place, keeping
    the list's order and length, with its aligned ``musicbrainz_artistid`` entry. A ``manual``
    file and a file whose ``artist`` or ``albumartist`` holds several values are never staged
    on. Review with ``diff_tags`` and apply with ``commit_tags``.
    ``revert_commit``/``revert_tags`` undo it.

    Each file's status on this axis is ``pending``, ``staged``, ``done``, ``no_match``,
    ``manual`` or ``no_identity``. A ``done`` or ``no_match`` counts only while the identity and
    the field values it was decided on still match the file, so a revert, a rescan after an
    outside edit or an identity fix re-opens the file on its own.

    Each selected file then records one outcome. A multi-value file, a ``feat`` credit or a
    held value records ``no_match``, so it stays on the review list. A transient lookup error
    records nothing, so the file stays ``pending`` and a re-run retries it. Anything else
    records ``done``. A cascade carrier outside the selection gets no row.

    It deliberately **skips** (and reports) values in the ``feat``/``ft``/``featuring``
    family, compilation sentinels (``various artists``/``various``/``va``), and empty
    values. Values already exactly canonical stage nothing but are counted under
    ``already_canonical``. Values with no Last.fm correction are reported under
    ``no_correction``. A correction to a MusicBrainz special-purpose placeholder
    (``[unknown]``, ``[no artist]``, …) is treated as no correction. A transient lookup error
    is counted in ``errors`` and itemized in ``error_items`` (``{key, message}``, keyed by the
    value).

    Three classes are **held**: reported so you can act on them, never staged.
    ``shrinks_credit_values`` are names whose canonical form is contained in the current
    value (``Skrillex & The Doors`` → ``Skrillex``) — a real multi-artist credit, held even
    when it carries an MBID. ``needs_review_values`` are Last.fm corrections MusicBrainz does
    not corroborate (no MBID), the "what Last.fm found that MusicBrainz did not" list.
    ``name_id_disagreement_values`` are the forensic case: the file names one artist while
    its own MBID names another, and the name is neither a credit nor any alias MusicBrainz
    records — each entry carries ``from``/``to``/``mbid``/``reason``. A value the library
    pairs with more than one MBID lands there too, and neither tier touches it.

    Args:
        value: Limit to files whose ``artist`` or ``albumartist`` tag equals this value.
        file_ids: Limit to these specific file ids (overrides ``value``). An unknown id is
            refused.
        limit: Max files to settle this call (every ``pending`` file in scope when omitted).
            Call again while ``more`` is true.
        dry_run: Preview the ``value → canonical`` mappings and the would-settle and
            would-stage counts without writing anything. Lookups still run. A cached answer
            costs nothing and a cache miss makes a live request. A dry run skips the
            empty-staging precondition.

    Returns:
        ``{"ok": True, settled, staged_files, corrected_values, skipped_multi_artist,
        skipped_sentinel, no_correction, already_canonical, shrinks_credit, needs_review,
        name_id_disagreement, errors, pending_remaining, more, mappings (each with
        ``from``/``to``/``mbid``/``source``), multi_artist_files, no_correction_values,
        already_canonical_values, shrinks_credit_values, needs_review_values,
        name_id_disagreement_values, error_items, summary}``, or ``{"ok": False, "error":
        ...}`` (e.g. pending changes, or no API key configured). ``settled`` counts the
        selected files that left ``pending`` and ``staged_files`` every staged file, cascade
        included. ``pending_remaining`` recounts the present ``pending`` files in scope.
        ``more`` is ``settled > 0 and pending_remaining > 0`` and is false on a dry run.
    """
    result = artists.resolve_artists(
        load_settings(),
        value=value,
        file_ids=file_ids,
        limit=limit,
        dry_run=dry_run,
    )
    return {"ok": True, **result.to_dict()}


@mcp.tool()
@_error_envelope
def list_artists(limit: int | None = None) -> dict[str, object]:
    """List distinct ``artist`` tag values with file counts.

    Use a value to scope ``resolve_genres`` or ``resolve_artists`` by ``artist``.

    Returns ``{"ok": True, "artists": [{artist, file_count}, ...]}`` in artist-value order.
    Run ``scan_library`` first to populate the snapshot.

    Args:
        limit: Cap the number of artists returned (applied after the value ordering).
            Keeps the payload context-cheap on a large library.
    """
    rows = library.list_artists(load_settings(), limit=limit)
    return {"ok": True, "artists": [row.to_dict() for row in rows]}


@mcp.tool()
@_error_envelope
def set_genre_status(
    status: Literal["manual"],
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Record a deliberate human decision on the genre axis (``manual``) for in-scope files.

    ``resolve_genres`` never selects a ``manual`` file. The row is sticky: an outside edit
    does not clear it, and ``reset_genre_status`` is its only hand-back. Committing a hand edit
    of ``genre`` records ``manual`` too. With neither ``file_ids`` nor ``value`` the call changes
    nothing and returns ``affected: 0``.

    Args:
        status: ``manual``, the one state a human sets on this axis.
        file_ids: Limit to these file ids. An unknown id is refused.
        value: Limit to files whose ``artist`` or ``albumartist`` tag equals this value (used
            when ``file_ids`` is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}``.
    """
    affected = genres.set_genre_status(
        load_settings(),
        file_ids=file_ids,
        value=value,
        status=status,
    )
    return {"ok": True, "affected": affected}


@mcp.tool()
@_error_envelope
def reset_genre_status(
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Clear any genre status row for in-scope files, returning them to ``pending``.

    Removes ``done``, ``no_match`` and ``manual`` alike, so ``resolve_genres`` reconsiders the files
    on its next run. This is the only hand-back of a ``manual`` row. With neither ``file_ids``
    nor ``value`` the call changes nothing and returns ``affected: 0``.

    Args:
        file_ids: Limit to these file ids. An unknown id is refused.
        value: Limit to files whose ``artist`` or ``albumartist`` tag equals this value (used
            when ``file_ids`` is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}`` if a file id
        is unknown.
    """
    affected = genres.reset_genre_status(
        load_settings(),
        file_ids=file_ids,
        value=value,
    )
    return {"ok": True, "affected": affected}


@mcp.tool()
@_error_envelope
def set_artist_status(
    status: Literal["manual"],
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Record a deliberate human decision on the artist axis (``manual``) for in-scope files.

    ``resolve_artists`` never selects a ``manual`` file, and no cascade stages on it. The row is
    sticky: an outside edit does not clear it, and ``reset_artist_status`` is its only
    hand-back. Committing a hand edit of any artist name, id or sort-name field records
    ``manual`` too. With neither ``file_ids`` nor ``value`` the call changes nothing and returns
    ``affected: 0``.

    Args:
        status: ``manual``, the one state a human sets on this axis.
        file_ids: Limit to these file ids. An unknown id is refused.
        value: Limit to files whose ``artist`` or ``albumartist`` tag equals this value (used
            when ``file_ids`` is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}``.
    """
    affected = artists.set_artist_status(
        load_settings(),
        file_ids=file_ids,
        value=value,
        status=status,
    )
    return {"ok": True, "affected": affected}


@mcp.tool()
@_error_envelope
def reset_artist_status(
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Clear any artist status row for in-scope files, returning them to ``pending``.

    Removes ``done``, ``no_match`` and ``manual`` alike, so ``resolve_artists`` reconsiders the
    files on its next run. This is the only hand-back of a ``manual`` row. With neither
    ``file_ids`` nor ``value`` the call changes nothing and returns ``affected: 0``.

    Args:
        file_ids: Limit to these file ids. An unknown id is refused.
        value: Limit to files whose ``artist`` or ``albumartist`` tag equals this value (used
            when ``file_ids`` is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}`` if a file id
        is unknown.
    """
    affected = artists.reset_artist_status(
        load_settings(),
        file_ids=file_ids,
        value=value,
    )
    return {"ok": True, "affected": affected}


@mcp.tool()
@_error_envelope
def set_mismatch_status(
    status: Literal["legit_ignore", "misfiled_deferred", "pending"],
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Silence a mismatch false positive or defer a misfiled file (or clear with ``pending``).

    For files ``detect_mismatches`` flags, record a sticky disposition so the detector stops
    surfacing them: ``legit_ignore`` for a false positive (a legit remix/guest/alias), or
    ``misfiled_deferred`` for a genuinely misfiled file you want to handle later. Both snapshot
    the file's current disagreeing tag, so the disposition goes stale — and the file
    re-surfaces — if that tag later changes. ``pending`` removes any disposition (re-queue). An
    accepted fix needs NO disposition: once you correct the tag so it agrees with the path, the
    detector stops flagging it on its own.

    Scope is ``file_ids`` when given, else every file carrying ``value`` as its ``artist`` OR
    ``albumartist`` tag (so silencing ``"Jem"`` catches it on either field).

    Args:
        status: ``legit_ignore`` | ``misfiled_deferred`` to disposition, ``pending`` to clear.
        file_ids: Limit to these file ids.
        value: Limit to files carrying this value as ``artist`` or ``albumartist`` (used when
            ``file_ids`` is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}``.
    """
    affected = mismatch.set_mismatch_status(
        load_settings(),
        file_ids=file_ids,
        value=value,
        status=status,
    )
    return {"ok": True, "affected": affected}


@mcp.tool()
@_error_envelope
def reset_mismatch_status(
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Clear any mismatch disposition for in-scope files, returning them to ``pending``.

    Removes the ``legit_ignore``/``misfiled_deferred`` disposition so ``detect_mismatches`` will
    surface the files again.

    Args:
        file_ids: Limit to these file ids.
        value: Limit to files carrying this value as ``artist`` or ``albumartist`` (used when
            ``file_ids`` is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}`` if a file id
        is unknown.
    """
    affected = mismatch.reset_mismatch_status(
        load_settings(),
        file_ids=file_ids,
        value=value,
    )
    return {"ok": True, "affected": affected}


@mcp.tool()
@_error_envelope
def resolve_years(
    value: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
) -> dict[str, object]:
    """Blank-fill the original release year (``originaldate``) from MusicBrainz (writes no disk).

    For each selected file with a blank ``originaldate`` this looks up its album group
    ``(albumartist-else-artist, album)`` on MusicBrainz (a release group's
    ``first-release-date``, e.g. *Paranoid* = 1970, distinct from the reissue ``date``) and
    stages it into ``originaldate`` as an ``auto`` change replacing ONLY that field (every other
    managed tag preserved), recording ``done``. A group MusicBrainz has no usable Album release
    group for records ``no_match``. A selected file that already carries ``originaldate``
    records ``done`` with no lookup: an existing value is never overwritten, and ``date`` is
    never touched. Review with ``diff_tags`` and apply with ``commit_tags``.
    ``revert_commit``/``revert_tags`` undo it.

    Each file's status on this axis is ``pending``, ``staged``, ``done``, ``no_match``,
    ``manual`` or ``no_identity``. A ``done`` or ``no_match`` counts only while the identity and
    the field values it was decided on still match the file, so a revert, a rescan after an
    outside edit or an identity fix re-opens the file on its own.

    Only ``pending`` files are selected, so repeated ``limit``-capped calls terminate. A
    transient MusicBrainz error writes nothing, leaves that group's files ``pending``, and is
    counted in ``errors`` and itemized in ``error_items`` (``{key, message}``).

    Args:
        value: Limit to files whose ``album`` tag equals this value.
        file_ids: Limit to these specific file ids (overrides ``value``). An unknown id is
            refused.
        limit: Max files to settle this call (default ``year_stage_limit``). Call again while
            ``more`` is true.
        dry_run: Preview the album → original-year mappings and the would-settle and
            would-stage counts without writing anything. Lookups still run. A cached answer
            costs nothing and a cache miss makes a live request. A dry run skips the
            empty-staging precondition.

    Returns:
        ``{"ok": True, settled, staged_files, no_match, pending_remaining, more, mappings,
        errors, error_items, summary}``, or ``{"ok": False, "error": ...}`` (e.g. pending
        changes). ``settled`` counts the selected files that left ``pending``.
        ``pending_remaining`` recounts the present ``pending`` files in scope. ``more`` is
        ``settled > 0 and pending_remaining > 0`` and is false on a dry run.
    """
    result = years.resolve_years(
        load_settings(),
        value=value,
        file_ids=file_ids,
        limit=limit,
        dry_run=dry_run,
    )
    return {"ok": True, **result.to_dict()}


@mcp.tool()
@_error_envelope
def list_albums(
    year_status: Literal["pending", "no_identity", "no_match", "manual", "staged", "done"]
    | None = None,
    actionable: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
    limit: int | None = None,
) -> dict[str, object]:
    """List distinct album groups with file counts + status (to scope ``resolve_years``).

    Groups present files by ``(albumartist-else-artist, album)`` and reports each group's file
    count, derived year workflow status, and ``blank_originaldate`` — the count of the group's
    files whose ``originaldate`` is empty. A group with ``blank_originaldate > 0`` is
    actionable for ``resolve_years`` (it has years to fill); ``year_status: "pending"``
    alone does NOT mean actionable, since every file may already carry ``originaldate``.
    Pass ``actionable=True`` to get only those groups instead of paging every group.
    Returns ``{"ok": True, "albums": [{artist, album, file_count, year_status,
    blank_originaldate}, ...]}`` in ``(artist, album)`` order. Run ``scan_library`` first to
    populate the snapshot.

    Args:
        year_status: Keep only groups in this derived year workflow state (``pending`` |
            ``no_identity`` | ``no_match`` | ``manual`` | ``staged`` | ``done``), applied
            before ``limit``.
        actionable: Keep only the actionable groups, those with ``blank_originaldate > 0``
            that ``resolve_years`` can actually fill. Composes with ``year_status`` and is
            applied before ``limit``.
        limit: Cap the number of groups returned (applied after ordering + filtering).
            Keeps the payload context-cheap on a large library.
    """
    rows = library.list_albums(
        load_settings(),
        year_status=year_status,
        actionable=actionable,
        limit=limit,
    )
    return {"ok": True, "albums": [row.to_dict() for row in rows]}


@mcp.tool()
@_error_envelope
def set_year_status(
    status: Literal["manual"],
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Record a deliberate human decision on the year axis (``manual``) for in-scope files.

    ``resolve_years`` never selects a ``manual`` file. The row is sticky: an outside edit does
    not clear it, and ``reset_year_status`` is its only hand-back. Committing a hand edit of
    ``originaldate`` records ``manual`` too. With neither ``file_ids`` nor ``value`` the call
    changes nothing and returns ``affected: 0``.

    Args:
        status: ``manual``, the one state a human sets on this axis.
        file_ids: Limit to these file ids. An unknown id is refused.
        value: Limit to files whose ``album`` tag equals this value (used when ``file_ids``
            is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}``.
    """
    affected = years.set_year_status(
        load_settings(),
        file_ids=file_ids,
        value=value,
        status=status,
    )
    return {"ok": True, "affected": affected}


@mcp.tool()
@_error_envelope
def reset_year_status(
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Clear any year status row for in-scope files, returning them to ``pending``.

    Removes ``done``, ``no_match`` and ``manual`` alike, so ``resolve_years`` reconsiders the files
    on its next run. This is the only hand-back of a ``manual`` row. With neither ``file_ids``
    nor ``value`` the call changes nothing and returns ``affected: 0``.

    Args:
        file_ids: Limit to these file ids. An unknown id is refused.
        value: Limit to files whose ``album`` tag equals this value (used when ``file_ids``
            is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}`` if a file id
        is unknown.
    """
    affected = years.reset_year_status(
        load_settings(),
        file_ids=file_ids,
        value=value,
    )
    return {"ok": True, "affected": affected}


def _maybe_launch_config_ui() -> None:
    """Auto-launch the config UI when settings are incomplete (best-effort, never fatal).

    Skipped when ``TAGMEND_NO_CONFIG_UI`` is set or when both core settings are present. A
    launch failure is logged to stderr and swallowed so it can never break ``mcp.run()``;
    stdout stays reserved for the JSON-RPC channel.
    """
    if os.environ.get("TAGMEND_NO_CONFIG_UI"):
        return
    if not configui.decide_launch(load_settings()):
        return
    try:
        url = configui.launch_background()
    except Exception:
        logger.exception("could not launch the config UI")
        return
    logger.warning("settings incomplete; configure TagMend at %s", url)


def run() -> None:
    """Run the MCP server over stdio (blocking)."""
    logger.info("starting TagMend MCP server (stdio)")
    _maybe_launch_config_ui()
    mcp.run()
