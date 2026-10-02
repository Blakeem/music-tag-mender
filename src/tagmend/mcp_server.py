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
    genres,
    health,
    library,
    mismatch,
    path_deviations,
    paths,
    release_disagreements,
    songs,
    staging,
    track_conflicts,
    versioning,
    year_disagreements,
    years,
)
from tagmend.engine.acoustid import AcoustidError, FpcalcUnavailableError
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
    AcoustidError,
    FpcalcUnavailableError,
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
    number of stored tag values, and five per-axis workflow-state blocks. Each block is a
    progress gauge for one resolver, drilled into with the matching ``list_files`` filter.

    * ``genre``: ``pending`` / ``no_identity`` / ``staged`` / ``done`` / ``no_match`` /
      ``manual`` for ``resolve_genres``. Drill with ``list_files(genre_status=...)``.
    * ``artist``: ``pending`` / ``no_identity`` / ``staged`` / ``done`` / ``no_match`` /
      ``manual`` for ``resolve_artists``. Drill with ``list_files(artist_status=...)``.
    * ``year``: ``pending`` / ``no_identity`` / ``staged`` / ``done`` / ``no_match`` /
      ``manual`` for ``resolve_years``. Drill with ``list_files(year_status=...)``.
    * ``song``: ``pending`` / ``staged`` / ``done`` / ``manual`` for ``resolve_songs``. Drill
      with ``list_files(song_status=...)``.
    * ``mismatch``: ``pending`` / ``legit_ignore`` / ``misfiled_deferred``, the path decision
      each present file reads (see ``detect_mismatches``). The counts sum to ``present``. Drill
      with ``list_files(mismatch_status=...)``.
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
def list_files(  # noqa: PLR0913 - cohesive MCP discovery filters
    path: str | None = None,
    limit: int | None = None,
    genre_status: Literal["pending", "no_identity", "no_match", "manual", "staged", "done"]
    | None = None,
    artist_status: Literal["pending", "no_identity", "no_match", "manual", "staged", "done"]
    | None = None,
    year_status: Literal["pending", "no_identity", "no_match", "manual", "staged", "done"]
    | None = None,
    song_status: Literal["pending", "manual", "staged", "done"] | None = None,
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
        song_status: Return only files in this song workflow state (``pending`` | ``manual``
            | ``staged`` | ``done``). Combined with the other filters, a file must match ALL.
            ``song_source_album_mbid`` / ``song_source_release_track_mbid`` carry the release
            ids a stored decision was taken against.
        mismatch_status: Return only present files reading this path decision
            (``pending`` | ``legit_ignore`` | ``misfiled_deferred``). A decided file that flags
            again reads ``pending``. Combined with the other filters, a file must match ALL.
            ``mismatch_source_value`` carries the snapshot of the decision the file reads:
            ``covers``, ``tags``, ``path_version`` and, on a keep, ``folder_key``.

    Returns:
        ``{"ok": True, "files": [{file_id, folder, filename, ext, is_missing,
        managed_tags, genre_status, genre_source_artist, genre_source_album,
        artist_status, artist_source_artist, artist_source_albumartist, year_status,
        year_source_artist, year_source_album, song_status, song_source_album_mbid,
        song_source_release_track_mbid, mismatch_status, mismatch_source_value}, ...]}``,
        or ``{"ok": False, "error": ...}`` on a bad request.
    """
    views = library.list_files(
        load_settings(),
        path=Path(path) if path is not None else None,
        limit=limit,
        genre_status=genre_status,
        artist_status=artist_status,
        year_status=year_status,
        song_status=song_status,
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
    year_source_album, song_status, song_source_album_mbid, song_source_release_track_mbid,
    mismatch_status, mismatch_source_value}}``,
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
    comparison: Literal[
        "top_folder_artist",
        "release_folder_album",
        "release_folder_year",
        "disc_folder_number",
        "filename_track",
        "filename_title",
    ]
    | None = None,
) -> dict[str, object]:
    """Detect files whose path, at any level, disagrees with their own tags (read-only report).

    Each file's path is read as named levels: the top folder (wrapper decoration such as
    ``[Discography]`` stripped), the release folder, a disc subfolder (``Cd1``, ``[DISC.02]``,
    ``1``, ``Bonus CD``, ``01-04 Songs``) and the filename. Six comparisons run over them:

    * ``top_folder_artist``: the top folder against ``albumartist``, else ``artist``. Skipped
      under a ``container_folders`` top folder and for a root album.
    * ``release_folder_album``: the release folder, without its years, artist and ``[...]``
      tags, against ``album``.
    * ``release_folder_year``: the release folder's year tokens against ``date`` and
      ``originaldate``. Either year agrees.
    * ``disc_folder_number``: a numbered disc subfolder against ``discnumber``.
    * ``filename_track``: the filename's number against ``tracknumber``. ``101`` and ``1-01``
      read as disc 1, track 1.
    * ``filename_title``: the filename's title text against ``title``.

    Formatting never flags: case, punctuation, diacritics, ``&`` versus ``and``, number words,
    roman numerals, ``Vol.`` versus ``Volume``, a leading article, zero padding and the year's
    position are all tolerated. A blank tag never flags. Pure read over the snapshot: writes
    nothing, stages nothing, no network. Run ``scan_library`` first.

    A file under a curated subfolder (``Singles``, ``Remixes``, ``Live``, ...) or a nested
    middle folder is an exception. It is listed in ``exception_rows`` and counted by
    ``exceptions_undecided``, outside ``flagged``. A curated file skips the album and year
    comparisons. An exception file that also differs is in ``rows`` as well.

    The path decisions (``set_mismatch_status``) gate the path tools. ``gate_open`` is true when
    ``flagged`` and ``exceptions_undecided`` are both 0. Workflow:

    1. Finish the tag phases first, ``resolve_songs`` included, so the filename comparisons
       judge verified titles and track numbers.
    2. Set ``container_folders`` for a top folder that holds several artists
       (``Soundtracks``).
    3. Call ``detect_mismatches(group=true)``. For each group, either fix the tags
       (``stage_tags_batch`` -> ``diff_tags`` -> ``commit_tags``) and re-read the group, or
       call ``set_mismatch_status(file_ids=<file_ids>, status="misfiled_deferred",
       covers=<comparisons + exception>)``, or call ``set_mismatch_status(file_ids=<file_ids +
       unflagged_ids>, status="legit_ignore", covers=<same>)``.
    4. Stop when the header reads ``gate_open: true``.

    A decision silences the names it covers while it binds the file. A covered tag edit, a
    committed move and a name outside its covers each make the file flag again. Its row then
    carries ``was`` (the decision) and ``changed`` (``uncovered``, ``moved`` or ``tags``).
    ``suppressed`` counts the files per decision in force, and ``stale`` the decisions no longer
    in force.

    Tiers: a ``top_folder_artist`` difference is ``high`` in a group with mixed albumartists,
    ``medium`` in a uniform group, and ``low`` in a one-file or curated group, for the
    ``artist`` fallback, or while ``path_signal_unreliable`` is true (more than 30 percent of
    files differ from their top folder). A number difference is ``high``. A name difference is
    ``low`` for a near spelling, ``high`` when no word is shared, else ``medium``. A file's tier
    is its most severe unsilenced difference.

    Args:
        tier: Return only files of this tier. Exception rows are dropped. In the grouped view,
            groups are built from the remaining files.
        limit: Cap the rows (or groups, with ``group=true``). Counts unaffected.
        group: Return one group per release folder instead of flat rows. A file under a disc
            subfolder joins its release folder's group. Each group carries ``folder``,
            ``file_count``, ``flagged``, ``tier``, ``comparisons`` (``{name: {files, tag,
            path}}``, one example pair each), ``mb_stamped`` (every flagged file carries
            ``musicbrainz_albumid``), ``exception``, ``suppressed``, ``file_ids`` (flagged and
            exception files) and ``unflagged_ids`` (the present files that flag nothing). A
            file whose every flag a decision silences is in neither. ``comparisons`` counts
            silenced names too, since a new decision's ``covers`` must name every flag.
        folder: Return the flat rows of the group this folder keys, or of this folder. Takes
            precedence over ``group``. Compared as a path: case and ``/`` versus backslash do
            not matter on Windows, and a relative folder resolves under ``music_path``.
        comparison: Return only files carrying this difference unsilenced. Each row still lists
            every difference of its file, silenced ones included. Exception rows are dropped.

    Returns:
        ``{"ok": True, rows, exception_rows, groups, total_files, flagged, group_count,
        by_comparison, high, medium, low, exceptions_undecided, disagreement_rate,
        path_signal_unreliable, gate_open, suppressed, stale, container_suppressed, summary}``.
        Each row is ``{file_id, folder, filename, tier, differences: [{comparison, tag_value,
        path_value, silenced}], exception, was, changed}``. The counts describe the entire
        library. Or ``{"ok": False, "error": ...}`` (no music path configured, an unknown tier
        or comparison).
    """
    report = mismatch.detect_mismatches(
        load_settings(),
        tier=tier,
        limit=limit,
        group=group,
        folder=folder,
        comparison=comparison,
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
    tracklist, and fix with ``stage_tags_batch`` -> ``diff_tags`` -> ``commit_tags(path=<folder>)``.
    Read ``diff_tags``' ``stale_identity`` before committing.
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
def detect_release_disagreements(  # noqa: PLR0913 - one parameter per scope/view knob, cohesive
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

    ``albumartist`` and ``artist`` are not compared by name when the credit names one artist and
    the file's own id field (``musicbrainz_albumartistid``, ``musicbrainz_artistid``) holds
    exactly that id. ``resolve_artists`` owns the spelling of a single identified artist.

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
    ``diff_tags`` -> ``commit_tags(path=<folder>)``.
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
    report = release_disagreements.detect_release_disagreements(
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
def detect_year_disagreements(
    tier: Literal["high", "medium", "low"] | None = None,
    limit: int | None = None,
    group: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
    folder: str | None = None,
    release_limit: int | None = None,
) -> dict[str, object]:
    """Find files whose years contradict the first release of their MusicBrainz release group.

    The year sibling of ``detect_release_disagreements``, which compares a file against the
    release its own ``musicbrainz_albumid`` names. This compares a file's ``originaldate`` (the
    original first-release date) and ``date`` (this release's date) against the first-release
    year of the release group its ``(albumartist-else-artist, album)`` resolves to. That is the
    lookup ``resolve_years`` makes, sharing its cache, so an album ``resolve_years`` already
    looked up costs no request.

    Tiers, on the year alone:

    * ``high``: the file's ``originaldate`` year differs from the first-release year.
    * ``medium``: the file's ``date`` year is EARLIER than the first-release year. A later
      ``date`` is a reissue and is not a finding.

    No row is ``low``. A blank ``originaldate`` is not a row, since ``resolve_years`` fills it.
    Each file counts once, in the tier of its most severe row, so the tier counts sum to
    ``flagged``. A folder holding several albums is grouped once per album, since each has its
    own release group. A folder named ``Singles``/``Remixes``/``Featured``/etc. holds several
    releases by design, so its rows are reported under ``folder_context`` instead, outside
    ``flagged`` and outside the tier counts.

    Review-only: it stages nothing. A match by album name can land on the wrong release group
    (a re-release, a soundtrack, a compilation), so confirm each correction before staging it.
    Reads the snapshot, so run ``scan_library`` first. The only ledger writes are lookup cache
    rows.

    Each uncached album is looked up once, paced at MusicBrainz's requested one request per
    second. ``release_limit`` caps those network lookups this call (default 200, about three
    minutes). A cached album never counts toward it, so re-running reaches the albums the last
    call left under ``release_groups_remaining``/``more``.

    Recommended workflow: start with ``group=true`` for one line per album per folder, then
    expand a single folder with ``folder="<exact folder path>"``, confirm the release group,
    then fix with ``stage_tags_batch`` -> ``diff_tags`` -> ``commit_tags(path=<folder>)``.
    ``commit_tags(path=<folder>)`` and ``diff_tags(path=<folder>)`` cover that folder AND every
    folder nested under it. Run ``diff_tags(path=<folder>)`` first and ``unstage_tags`` any
    nested change you do not want in this commit.

    Args:
        tier: Keep only rows in this tier (``high`` | ``medium`` | ``low``). The tier filters
            rows first, and the grouped view is built from the filtered rows. The report-level
            counts still describe the whole library.
        limit: Cap the rows returned, or the groups with ``group=true``. Counts are
            unaffected.
        group: Return one compact line per album per folder instead of flat rows.
        folder: Expand exactly this folder's rows, never a subfolder. Takes precedence over
            ``group``. Compared as a path: case and ``/`` versus backslash do not matter on
            Windows, and a relative folder resolves under ``music_path``.
        release_limit: Max uncached release groups to look up over the network this call.

    Returns:
        ``{"ok": True, rows, total_files, flagged, flagged_fields, high, medium, low,
        folder_context, folder_context_rows, release_groups_checked, release_groups_remaining,
        more, unknown_release_groups, skipped_no_identity, errors, error_items, groups,
        summary}``, where ``error_items`` is ``{key, message}`` keyed by ``"<artist> -
        <album>"``. Each row is ``{file_id, folder, filename, artist, album, field, have,
        first_release_year, release_group_mbid, release_group_title, tier, reason}``. Each
        group is ``{folder, artist, album, first_release_year, release_group_mbid,
        release_group_title, file_count, flagged, folder_context, tiers, file_ids, fields}``,
        where ``file_ids`` names the flagged files only. On failure,
        ``{"ok": False, "error": ...}``.
    """
    report = year_disagreements.detect_year_disagreements(
        load_settings(),
        tier=tier,
        limit=limit,
        group=group,
        folder=folder,
        release_limit=release_limit,
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
    ``diff_tags`` -> ``commit_tags(path=<folder>)``. Read ``diff_tags``'
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
    all failed has source ``lookup_error``. Re-run to retry it. A blank file whose format the
    tag writer refuses (WAV, AIFF, WMA, raw AAC) gets no proposal and is counted in
    ``unwritable``. A folder holding only such blank files has source ``unwritable``.

    Recommended fix flow (the human is the diff-gate for every value): start here, expand one
    folder with ``folder="<exact folder path>"``, then per source feed the proposals'
    ``{file_id, proposed}`` + ``note`` to ``stage_tags_batch`` (one call per source keeps the
    ``note`` accurate) → review ``diff_tags(path=<folder>)`` → ``commit_tags(path=<folder>)``.
    ``commit_tags(path=<folder>)`` and ``diff_tags(path=<folder>)`` cover that folder AND every
    folder nested under it. Run ``diff_tags(path=<folder>)`` first and ``unstage_tags`` any
    nested change you do not want in this commit.

    Args:
        limit: Cap the number of folder groups returned. The ``total_files``/``green``/
            ``confirm``/``review``/``stays_blank``/``errors``/``unwritable`` counts still
            describe the whole library.
        folder: Return only the group for exactly this folder, never a subfolder. Compared as
            a path: case and ``/`` versus backslash do not matter on Windows, and a relative
            folder resolves under ``music_path``.
        use_musicbrainz: When False, skip the ``mb_recording`` review source entirely (the
            sibling and folder-parse sources only, no network). Default True.

    Returns:
        ``{"ok": True, groups, total_files, total_blank, green, confirm, review, stays_blank,
        errors, error_items, unwritable, summary}``, where ``green + confirm + review +
        stays_blank + errors + unwritable == total_blank``. Each group is ``{folder,
        blank_count, file_count, file_ids, sibling_histogram, source, proposals, errors,
        unwritable}``, where ``file_ids`` names the folder's
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
    ``commit_id`` that grouped it, the full ``managed_tags`` snapshot at that version, the
    ``diff`` from the prior version, and the ``managed_set`` that governed the snapshot. A
    ``scan`` row is version 0 or a re-baseline that observed fields a newer managed set added.
    Use a ``version`` here with ``revert_tags``.

    Returns ``{"ok": True, "history": [{version, created_at, origin, reverted_to_version,
    commit_id, managed_tags, diff, note, managed_set}, ...]}`` (empty if the file has no
    history), or ``{"ok": False, "error": ...}`` if the file id is unknown.
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
    path: str | None = None,
) -> dict[str, object]:
    """Undo an entire commit as a unit. Every file it changed goes back to its pre-commit state.

    Works on both logs. A tag commit restores each file's pre-commit tags. A path commit
    (from ``commit_paths``) moves each file and each sidecar (cover art, cue sheet, log) back
    to the path it left, and a revert of that revert moves them forward again. ``get_commit``
    names the logs a commit changed.

    The group counterpart of ``revert_tags`` and ``revert_paths``. All reverts land under ONE
    new ``origin='revert'`` commit whose ``reverted_from`` records the undone commit, so the
    rollback is itself a tracked, revertible commit. History stays append-only, so nothing
    after the commit is ever lost. Find commit ids with ``list_commits``.

    Safety rules. A file changed again by a LATER commit, or edited outside TagMend, is skipped
    and reported as ``skipped_later_changes``. Revert it per file with ``revert_tags`` or
    ``revert_paths`` if that is really wanted. The staging area must be empty, tag and path
    rows alike, so commit or unstage pending work first, and run ``commit_paths`` to finish an
    interrupted path revert. Missing files are reported, not fatal. A path whose old location
    is taken now is reported as an ``error``. Use ``dry_run=true`` to preview the exact
    per-file plan without touching anything.

    Args:
        commit_id: The commit to undo (from ``list_commits``).
        note: Optional message stored on the new revert commit and its revisions.
        dry_run: When true, classify and report only, with no disk or ledger change.
            ``commit_id`` in the result is ``null``, ``status='reverted'`` means "would be
            reverted", and ``status='noop'`` means "would change nothing".
        path: A path commit only. Reverts just the files and sidecars sitting at this folder
            or under it now, so one album folder, or one sidecar's own path, reverts alone.
            Compared as a path, like ``unstage_paths``.

    Returns:
        ``{"ok": True, "commit_id": ..., "reverted_from": ..., "dry_run": ...,
        "reverted"/"noop"/"skipped"/"missing"/"errors": counts, "outcomes": [...]}`` with
        one outcome per file (``noop`` = the file already held its pre-commit state, so
        the audited revert revision was appended but nothing on disk moved). A path commit's
        result adds ``"sidecars_reverted"`` and ``"sidecars": [{from_path, to_path, status,
        detail}, ...]``, one per sidecar it moved. A sidecar a later move left or reached is
        ``skipped_later_changes``. Returns ``{"ok": False, "error": ...}`` if the commit id is
        unknown, the commit is still ``applying``, the commit holds no change in any log,
        ``path`` is given for a tag commit, or the staging area is not empty.
    """
    result = versioning.revert_commit(
        load_settings(),
        commit_id,
        note=note,
        dry_run=dry_run,
        path=path,
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
    """Return one commit by id, and the revision logs that hold its changes.

    Returns ``{"ok": True, "commit": {commit_id, created_at, origin, message, reverted_from,
    status}, "logs": {"tag_revisions": <count>, "path_revisions": <count>, "sidecar_moves":
    <count>}}``, where ``logs`` names only the logs holding rows of this commit (a tag commit,
    or a path commit with its moved files and sidecars), or ``{"ok": False, "error": ...}`` if
    the id is unknown.
    """
    settings = load_settings()
    commit = commits.get_commit(settings, commit_id)
    if commit is None:
        return {"ok": False, "error": f"unknown commit_id={commit_id}"}
    return {
        "ok": True,
        "commit": commit.to_dict(),
        "logs": versioning.commit_logs(settings, commit_id),
    }


@mcp.tool()
@_error_envelope
def detect_path_deviations(
    pattern: str | None = None,
    container_folders: list[str] | None = None,
    folder: str | None = None,
    group: bool = True,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
    limit: int | None = 50,
) -> dict[str, object]:
    """Report files whose path differs from the path the naming pattern renders from their tags.

    Read-only, and allowed while the ``detect_mismatches`` gate is closed. Order: set
    ``container_folders`` (with ``set_naming_pattern``) before the first ``set_mismatch_status``
    decision, since both tools read that list, then bring ``detect_mismatches`` to
    ``gate_open: true``, then pick a pattern here and stage with ``stage_paths``.

    Pattern grammar: components separated by ``/``, the last the filename (the extension is
    appended). ``{a|b}`` renders the first non-empty name, ``{tracknumber:02}`` zero-pads.
    ``[...]`` renders only when every field inside is non-empty. A field outside ``[...]`` is
    required, and a file where it is blank holds ``missing_<name>``. Names: any managed tag,
    plus ``year`` (originaldate, else date), ``disc`` (the disc number on a multi-disc album
    only) and ``container`` (the configured container folder the file sits under).

    Header: ``counts`` per status (``at_target``, ``case_only`` = only a folder's case differs,
    never staged, ``will_move``, ``held``, ``kept_staged`` = a manual or revert move is
    staged), ``held`` per reason, ``fit`` per pattern component (exact, case, differs), the
    top ``shapes`` of the current top folder, leaf folder and filename written with
    ``{field}`` placeholders, ``container_candidates`` (top folders that mostly hold other
    album artists: a container, or an artist spelling ``detect_mismatches`` owns), the
    ``gate`` and the ``volume_refusal``. Hold reasons: ``missing_<name>``, ``missing_year``
    (the folder names a year the tags lack), ``missing_disc`` (a disc subfolder with a blank
    discnumber), ``album_split``, ``unit_member`` (a folder moves as a unit), ``track_conflict``,
    ``merge``, ``duplicate_render``, ``landed_move``, ``staged_tag``, ``missing``,
    ``too_long``, ``occupied``, ``shared_target`` and ``cue_reference``.

    Args:
        pattern: A candidate pattern to preview. Never saved. Omit for the saved pattern.
        container_folders: A candidate container folder list to preview. Never saved.
        folder: One exact folder to list file by file, every status included. Compared as a
            path. Wins over ``group``.
        group: True (default) for one group per source folder holding a deviating file, with
            its file count, destinations, largest ``kind`` (move, rename, case), held counts
            and one example pair. False for one row per deviating file.
        limit: Cap on groups or rows (default 50). Counts always cover the entire library.

    Returns:
        ``{"ok": True, "pattern", "persisted", "default_pattern", "container_folders",
        "total_files", "counts", "held", "fit", "shapes", "container_candidates", "gate",
        "volume_refusal", "group_count", "groups", "rows"}``.
    """
    report = path_deviations.detect_path_deviations(
        load_settings(),
        pattern=pattern,
        container_folders=container_folders,
        folder=folder,
        group=group,
        limit=limit,
    )
    return {"ok": True, **report.to_dict()}


@mcp.tool()
@_error_envelope
def set_naming_pattern(
    pattern: str | None = None,
    container_folders: list[str] | None = None,
) -> dict[str, object]:
    """Save the naming pattern, the container folder list, or both, to settings.json.

    Set ``container_folders`` first, before the first ``set_mismatch_status`` decision, since
    ``detect_mismatches`` reads the same list: a container is a top folder that collects
    releases (``Soundtracks``) rather than naming an artist. Then preview patterns with
    ``detect_path_deviations(pattern=...)`` and save the winner here. Leave ``pattern`` out
    when the default fits. The grammar is in ``detect_path_deviations``.

    Refused while any path move is staged: run ``commit_paths``, or
    ``unstage_paths(path=...)``, first. Also refused, saving nothing, for an empty pattern, an
    unknown name, an unbalanced brace or bracket, a ``/`` inside ``[...]`` and an invalid
    folder name.

    Args:
        pattern: The pattern to save. ``None`` leaves the saved pattern unchanged.
        container_folders: Top folder names to save. ``[]`` clears the list. ``None`` leaves
            it unchanged.

    Returns:
        ``{"ok": True, "pattern", "default_pattern", "container_folders", "settings_path"}``.
    """
    saved = paths.set_naming_pattern(
        load_settings(),
        pattern=pattern,
        container_folders=container_folders,
    )
    return {"ok": True, **saved.to_dict()}


@mcp.tool()
@_error_envelope
def stage_paths(
    path: str | None = None,
    dry_run: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
    note: str | None = None,
) -> dict[str, object]:
    """Stage the move of every file under a folder to the path its tags render (no disk write).

    Renders the saved naming pattern for each file, holds every file ``detect_path_deviations``
    holds, and stages the rest as ``auto`` moves. A folder moves as a unit or not at all. The call
    replaces its own ``auto`` moves in scope and never touches a ``manual`` or ``revert`` move.
    A move that already landed on disk holds ``landed_move``: run ``commit_paths``. Nothing
    moves until ``commit_paths``.

    A folder whose every file moves to one other folder takes its sidecars along: every
    non-audio file under it (cover art, hidden thumbnails, cue sheets, rip logs, a ``Scans``
    folder), with names and relative structure unchanged. A sidecar whose target is taken
    stays in place, and of two folders claiming one target the one with the lower file id wins.

    A ``legit_ignore`` decision keeps the file's folder, and only its filename renders. No
    decision keeps a filename, so a filename rename you reverted is rendered again while the
    old filename still agrees with the tags. Change the pattern, or leave that folder out of
    ``path``, to keep it.

    Refused while ``detect_mismatches`` does not read ``gate_open: true`` and on a volume that
    ignores case under a case-keeping OS, except with ``dry_run``.

    Args:
        path: Only files sitting at this folder or under it. Compared as a path, like
            ``unstage_paths``. Omit for the entire library.
        dry_run: When true, report what would stage and change nothing.
        note: Optional free-text note stored with each eventual path revision.

    Returns:
        ``{"ok": True, "dry_run", "pattern", "matched", "staged", "folders", "kinds",
        "at_target", "case_only", "kept_staged", "unstaged", "held_count", "held",
        "held_files": [{file_id, from_path, to_path, status, kind, reasons}, ...],
        "staged_targets_under_path", "sidecars_staged", "sidecars_held": [{from_path, to_path,
        detail}, ...]}``. ``held_files`` lists the first 50. ``staged_targets_under_path`` is
        set only when ``path`` matched no file.
    """
    result = paths.stage_paths(load_settings(), path=path, dry_run=dry_run, note=note)
    return {"ok": True, **result.to_dict()}


@mcp.tool()
@_error_envelope
def stage_paths_batch(
    entries: list[dict[str, object]],
    note: str | None = None,
) -> dict[str, object]:
    """Stage explicit file moves for MANY files in one atomic, all-or-nothing call (no disk write).

    Each entry names a file and the path it should move to. ``to_path`` is relative to
    ``music_path`` (or absolute under it) and keeps the file's extension. A folder in
    ``to_path`` that already exists under another casing keeps its on-disk spelling. A
    filename casing change is a rename. Nothing moves until ``commit_paths``.

    The whole call is refused, and nothing is staged, while ``detect_mismatches`` does not read
    ``gate_open: true``, on a volume that ignores case under a case-keeping OS, and when any
    entry is held. The error lists every held entry with its reason: ``occupied`` (another
    tracked file or an entry on disk holds the target), ``shared_target`` (another staged move
    or entry targets it), ``too_long`` (a part over 255 UTF-16 units or a full path over 259
    characters), ``cue_reference`` (a cue sheet or playlist in the source folder names the
    file), ``staged_tag`` (run ``commit_tags`` or ``unstage_tags`` first), ``invalid_path`` (a
    forbidden character, edge whitespace, a trailing dot or space, a reserved device name, a
    changed extension, or the file's own path), ``unknown_file``, ``missing`` and
    ``landed_move`` (the file's staged move already landed on disk, run ``commit_paths``).

    Staging a file again replaces its row. A file whose move already landed on disk takes only
    its staged ``to_path`` again, in the same spelling, which confirms the file found there. A
    call made only of such confirmations passes while the gate is closed. Staging captures each
    file's first location as path version 0. Tag staging and path staging exclude each other
    per file. A folder whose every file the call moves to one other folder takes its sidecars
    along, as in ``stage_paths``. ``sidecars_staged`` counts the sidecar moves staged.
    ``sidecars_held`` lists each sidecar left in place, with its ``from_path``, ``to_path`` and
    ``detail`` (its target is taken or too long), so the target can be cleared before the commit.

    Args:
        entries: A list of ``{"file_id": <int>, "to_path": <str>}`` objects.
        note: Optional free-text note stored with each eventual path revision.

    Returns:
        ``{"ok": True, "staged": <count>, "file_ids": [...], "sidecars_staged": <count>,
        "sidecars_held": [...]}``, or ``{"ok": False, "error": ...}`` naming every held entry
        (nothing staged).
    """
    pairs = [(entry.get("file_id"), entry.get("to_path")) for entry in entries]
    result = paths.stage_paths_batch(load_settings(), entries=pairs, note=note)
    return {"ok": True, **result.to_dict()}


@mcp.tool()
@_error_envelope
def unstage_paths(file_id: int | None = None, path: str | None = None) -> dict[str, object]:
    """Drop staged moves for one file, or for every file and sidecar under a folder. Moves nothing.

    Pass exactly one of ``file_id`` and ``path``. ``path`` matches where a file sits now,
    which is its source until its move commits, and a sidecar by its staged source, so the
    path of one sidecar drops that sidecar alone. It is compared as a path: case and ``/``
    versus backslash do not matter on Windows, and a relative folder resolves under
    ``music_path``. A folder that loses a staged file no longer moves whole, so its sidecar
    moves are dropped too. The call is refused whole, naming them, when any matched file or
    sidecar already sits at its staged target on disk (a crash after the move): run
    ``commit_paths`` to finish those moves.

    Returns ``{"ok": True, "removed": <count>, "sidecars_removed": <count>}``, or
    ``{"ok": False, "error": ...}``.
    """
    removed = paths.unstage_paths(load_settings(), file_id=file_id, path=path)
    return {"ok": True, **removed.to_dict()}


@mcp.tool()
@_error_envelope
def diff_paths(path: str | None = None) -> dict[str, object]:
    """Show the staged moves, each with where it is on disk now. Read-only.

    ``state`` is one of ``at_source`` (ready to move), ``landed`` (the move reached disk
    before a crash, ``commit_paths`` records it), ``landed_changed`` (as ``landed``, but the
    file changed since it was staged: stage the same ``to_path`` again to confirm it),
    ``half_link`` (a cut POSIX move, ``commit_paths`` finishes it), ``target_taken`` (another
    file sits at the target: stage another ``to_path`` or unstage) and ``gone`` (neither path
    is on disk: ``commit_paths`` flags the file missing). ``stale`` marks an ``auto`` move whose
    file the saved naming settings now render elsewhere. The commit still applies the staged
    target, so run ``stage_paths`` again to follow the new render.

    ``sidecars`` lists each staged sidecar move with the same ``state`` values. A sidecar row
    stays staged after its album's audio commits when its target was taken (``target_taken``:
    move that file away and run ``commit_paths``, or ``unstage_paths(path=<from_path>)``) or it
    changed after landing (``landed_changed``: run ``stage_paths`` on its album folder). Such a
    row blocks ``revert_commit``, ``revert_paths`` and every resolver until it clears.

    Args:
        path: When given, only moves of files and sidecars sitting at this folder or under it.
            Compared as a path, like ``unstage_paths``.

    Returns:
        ``{"ok": True, "changes": [{file_id, from_path, to_path, origin, note, staged_at,
        state, stale}, ...], "sidecars": [{from_path, to_path, origin, note, staged_at,
        state}, ...]}``, every path relative to ``music_path``.
    """
    settings = load_settings()
    changes = paths.diff_paths(settings, path=path)
    sidecars = paths.diff_sidecars(settings, path=path)
    return {
        "ok": True,
        "changes": [view.to_dict() for view in changes],
        "sidecars": [view.to_dict() for view in sidecars],
    }


@mcp.tool()
@_error_envelope
def commit_paths(path: str | None = None, message: str | None = None) -> dict[str, object]:
    """Move every staged file on disk as one revertible commit, keeping each file's id.

    Before anything moves, each staged file must pass ``detect_mismatches`` at its current
    path: a file that flags with no decision covering it refuses the whole call. Fix its tags,
    record a decision with ``set_mismatch_status``, or drop it with ``unstage_paths``. Each
    move never overwrites a file, appends a ``path_revisions`` row, and repoints the file's
    row. Then each staged sidecar whose folder's audio has all moved follows it, logged in
    ``sidecar_moves``. A sidecar whose folder still holds a staged file waits for the next
    ``commit_paths``. Folders a move empties are removed up to ``music_path``, and a folder
    holding any file, a hidden one included, stays. A commit left ``applying`` by a crash is
    marked interrupted and its leftover rows, a moved file's or sidecar's included, are swept
    into this commit.

    Args:
        path: When given, only moves of files sitting at this folder or under it. Compared as a
            path, like ``unstage_paths``.
        message: Optional commit message. Without one, the message records the naming pattern.

    Returns:
        ``{"ok": True, "commit_id", "committed", "noop", "missing", "changed_since_stage",
        "errors", "problems": [{file_id, status, to_path, detail}, ...], "sidecars_moved",
        "sidecars_waiting", "sidecars_held": [<path>, ...], "folders_pruned",
        "sidecar_problems": [{from_path, to_path, status, detail}, ...]}``. ``commit_id`` is
        ``null`` when nothing was staged. ``sidecars_held`` lists the non-audio files left in
        a folder whose audio all moved, because their target was taken, the folder's files
        went to different folders, or they sit in the release folder above disc folders the
        commit emptied of audio, which no move carries. Each problem keeps its row, except a
        ``missing`` one, and its ``detail`` names the next step.
    """
    result = paths.commit_paths(load_settings(), path=path, message=message)
    return {"ok": True, **result.to_dict()}


@mcp.tool()
@_error_envelope
def history_paths(file_id: int) -> dict[str, object]:
    """Show every location one file has had, oldest (version 0) first.

    Version 0 is the location the file had when it was first staged for a move. Each later
    version carries its ``origin`` (manual|auto|revert), its ``commit_id``, ``from_path`` and
    ``to_path`` relative to ``music_path``, and on a revert ``reverted_to_version``, the version
    whose location it restored. Use a ``version`` here with ``revert_paths``.

    Returns ``{"ok": True, "history": [...]}`` (empty for a file never staged for a move), or
    ``{"ok": False, "error": ...}`` if the file id is unknown.
    """
    revisions = paths.history_paths(load_settings(), file_id)
    return {"ok": True, "history": [revision.to_dict() for revision in revisions]}


@mcp.tool()
@_error_envelope
def revert_paths(
    file_id: int,
    version: int,
    note: str | None = None,
    dry_run: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
) -> dict[str, object]:
    """Move one file back to the location of a prior path ``version``, as its own commit.

    The move lands under a single-file ``origin='revert'`` commit, so it shows in
    ``list_commits`` and can itself be undone with ``revert_commit``. Refused while any tag or
    path change is staged (run ``commit_paths`` to finish an interrupted revert), for a file
    missing from disk or moved outside TagMend, for an unknown version, and when the old
    location is taken. The ``detect_mismatches`` gate does not apply, and the restored path may
    flag again. Get versions from ``history_paths``.

    Args:
        file_id: The file to move.
        version: The path version whose ``to_path`` the file returns to.
        note: Optional message stored on the revert commit and its path revision.
        dry_run: When true, apply every refusal and change nothing.

    Returns:
        ``{"ok": True, "file_id", "target_version", "new_version", "commit_id", "to_path",
        "status", "detail", "dry_run"}``. ``status`` is ``reverted``, or why the move did not
        finish (``missing``, ``changed_since_stage``, ``error``) with a ``detail``.
    """
    result = paths.revert_paths(load_settings(), file_id, version, note=note, dry_run=dry_run)
    return {"ok": True, **result.to_dict()}


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
    ``no_match``. A genre the overlay's ``deny:`` list denies for the lookup artist, or for every
    artist, is dropped before the ``genre_max_count`` cap. Review with ``diff_tags`` and apply
    with ``commit_tags``. ``revert_commit`` undoes the whole commit and ``revert_tags`` undoes
    one file.

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
    status: Literal["legit_ignore", "misfiled_deferred"],
    covers: list[
        Literal[
            "top_folder_artist",
            "release_folder_album",
            "release_folder_year",
            "disc_folder_number",
            "filename_track",
            "filename_title",
            "curated",
            "nested",
        ]
    ],
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Record a path decision for one ``detect_mismatches`` group, so the path tools may proceed.

    * ``legit_ignore``: the file's current folder stays. The path tools keep the folder and
      render only the filename from the tags. It may also be set on a file that flags nothing,
      a durable keep of its folder.
    * ``misfiled_deferred``: the tags are right. The path tools render every level of the path
      from the tags.

    No status keeps a filename. Covering a filename difference means the tag is right and the
    filename will follow it. To keep a filename's wording, write it into the tag.

    Finish the tag phases (``resolve_songs`` included) and set ``container_folders`` first.
    Then read ``detect_mismatches(group=true)``. Per group, fix the tags, or call
    ``set_mismatch_status(file_ids=<file_ids>, status="misfiled_deferred", covers=<comparisons +
    exception>)``, or ``set_mismatch_status(file_ids=<file_ids + unflagged_ids>,
    status="legit_ignore", covers=<same>)``. Stop when ``detect_mismatches`` reads
    ``gate_open: true``.

    The entire call is refused, nothing written, when a scoped file flags a name outside
    ``covers`` (the error lists each ``(file_id, name)``), when the scope spans more than one
    release-folder group, when a keep would leave a present group member without a keep, when
    ``misfiled_deferred`` names a file that flags nothing, when a file holds a staged path
    change (run ``unstage_paths``), and for an unknown or missing id.

    Each row covers the names its file flags now and snapshots their tags, the file's path
    version and, on a keep, its folder. The file flags again when a covered tag changes, when a
    committed move changes its path, or when it flags a name outside its covers.

    Args:
        status: ``legit_ignore`` (keep the folder) or ``misfiled_deferred`` (render every level).
        covers: The comparisons and class decided: the group's ``comparisons`` keys plus its
            ``exception``. Pass ``[]`` to keep the folder of files that flag nothing.
        file_ids: The files decided, from one group.
        value: Instead of ``file_ids``, every present file carrying this ``artist`` or
            ``albumartist`` whose top folder differs. Takes only
            ``status="misfiled_deferred"`` with ``covers=["top_folder_artist"]``.

    Returns:
        ``{"ok": True, status, affected, skipped_unflagged, covers: {name: files}, files:
        [{file_id, folder: "kept" | "rendered", filename: "rendered"}], note}``, or
        ``{"ok": False, "error": ...}``.
    """
    result = mismatch.set_mismatch_status(
        load_settings(),
        status=status,
        covers=covers,
        file_ids=file_ids,
        value=value,
    )
    return {"ok": True, **result.to_dict()}


@mcp.tool()
@_error_envelope
def reset_mismatch_status(
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Delete the path decision of in-scope files, so ``detect_mismatches`` reads them afresh.

    A delete authorises nothing, so no covers or group rule applies. Refused for an unknown or
    missing id and for a file holding a staged path change (run ``unstage_paths``).

    Args:
        file_ids: Limit to these file ids.
        value: Limit to files carrying this value as ``artist`` or ``albumartist`` (used when
            ``file_ids`` is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}``.
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
    """Blank-fill the original release date (``originaldate``) from MusicBrainz (writes no disk).

    For each selected file with a blank ``originaldate`` this looks up its album group
    ``(albumartist-else-artist, album)`` on MusicBrainz (a release group's
    ``first-release-date``, e.g. *Paranoid* = 1970-09-18, distinct from the reissue ``date``) and
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
    counted in ``errors`` and itemized in ``error_items`` (``{key, message}``). A file staging
    refuses stays ``pending`` and is itemized under the key ``file_id=<id>``.

    Args:
        value: Limit to files whose ``album`` tag equals this value.
        file_ids: Limit to these specific file ids (overrides ``value``). An unknown id is
            refused.
        limit: Max files to settle this call (default ``year_stage_limit``). Call again while
            ``more`` is true.
        dry_run: Preview the album → original-date mappings and the would-settle and
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


@mcp.tool()
@_error_envelope
def resolve_songs(  # noqa: PLR0913 - cohesive scope, release path and run knobs
    folder: str | None = None,
    file_ids: list[int] | None = None,
    release_mbid: str | None = None,
    assignments: list[dict[str, object]] | None = None,
    limit: int | None = None,
    dry_run: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
) -> dict[str, object]:
    """Settle ``title``, ``tracknumber`` and ``discnumber`` by each file's audio (no disk write).

    Each file is fingerprinted with fpcalc and looked up on AcoustID, and both answers are
    cached. Every file of a folder holding a ``pending`` file votes, and the folder settles in
    one of three ways. A folder whose files all carry ``musicbrainz_albumid`` is checked
    against those releases. A folder without ids converges on the one Official release most of
    its files share, and its blank song fields are staged as an ``auto`` fill. A non-blank
    value is never rewritten. A folder whose audio is not on the release it is tagged with
    stages nothing and comes back in ``rebind_folders`` with ranked candidate releases.

    The first five candidates of a ``rebind_folders`` entry are placed as the manual release
    path would place the folder. Each carries ``placed``, the count of the folder's files that
    land on exactly one of its tracks, and ``unassigned``, a count per ``unassigned`` reason.
    Within each status tier those candidates come first, fewest unassigned first. A candidate
    with ``unassigned`` empty is one ``resolve_songs(folder=F, release_mbid=R)`` call away from
    a stamp. A candidate carries neither key when it sits past the first five, MusicBrainz does
    not hold it, or a MusicBrainz error stopped the fetches at it or before it.

    A file that agrees with its release records ``done``. A fill records ``done`` once staged.
    A disagreement, a slot two files claim, a file its release does not hold and a folder that
    does not converge are held (``held_values``, with ``have``/``want`` on a disagreement) and
    stay ``pending``. A file whose recordings fail the recording gate is held as
    ``no_contribution`` with the gate's reason. An empty AcoustID answer (``lookup_empty``) and a
    transient error (``error_items``, naming the fpcalc exit code, the HTTP status or the
    timeout) store nothing either. An empty answer is asked again after 7 days.

    ``review_values`` rows are proposals and are never staged. A held gated file with a blank
    title gets one with the audio's recording title (``field: "title"``). A gated file with no
    ``artist`` and no ``albumartist`` whose recordings credit one artist gets one with that
    credit (``field: "artist"``, ``proposal`` and ``musicbrainz_artistid``), whatever its song
    outcome. ``review_files`` counts the files holding a row. A real call records a verified or
    filled file ``done`` in the same pass that reports its artist row, so no later call reports
    that row again. Keep the artist rows from the dry run.

    Workflow: run ``dry_run=True`` over the whole library first, repeating while ``more`` is
    true, then ``limit=0`` for the free whole-library tally. Review it, then make the real
    calls and apply each with ``diff_tags`` and ``commit_tags``. For each ``rebind_folders``
    entry pick a candidate R, preferring one with ``unassigned`` empty, and call
    ``resolve_songs(folder=F, release_mbid=R, dry_run=True)``, then the real call,
    ``diff_tags`` and ``commit_tags(path=F)``.

    With ``release_mbid`` the call takes the manual release path. Every file in scope must sit
    on exactly one track of that release, or nothing is staged and the files come back in
    ``unassigned`` with a reason. Otherwise the whole release stamp (names, sort names,
    ``artists``, ids, dates, numbers, release, release-group, label and ``isrc`` fields) is
    staged as one ``manual`` batch. A field MusicBrainz leaves blank keeps the file's value
    when the file already names that release (for ``isrc``, that recording), and a rebind
    clears it. ``originaldate`` takes the release group's first-release date, but on the
    file's own release only to fill a blank value or refine a bare year or year-month of it.
    A real run fetches the release fresh, so the stamp carries the track ids MusicBrainz lists
    now. A dry run reads the cache.

    ``assignments`` names the track for a file the audio cannot place, such as an
    ``ambiguous_slot`` or ``slot_collision`` row in ``unassigned``. Take each track id from the
    ``release_track_mbid`` values of the dry run's ``release`` block. An assigned file takes that
    track in place of its audio's tracks and gets the same stamp as every other file. The whole
    call is refused, naming the file, when the track is not on the release, the file is not in
    the call's scope, a file is listed twice, another file is assigned that track or its audio
    sits on it, or the file's fingerprint duration differs from the track's length by more than
    10 seconds. A file with no stored fingerprint skips only the length check. Each dry-run
    ``mappings`` row carries ``placed_by``: ``operator`` for an assigned file, ``audio`` for a
    file its audio placed.

    Args:
        folder: Limit to the files directly in this folder. Compared as a path, and a
            relative folder resolves under ``music_path``.
        file_ids: Limit to these file ids (overrides ``folder``). An unknown id is refused.
        release_mbid: Apply this MusicBrainz release to the scope (``folder`` or ``file_ids``
            required).
        assignments: A list of ``{"file_id": <int>, "release_track_mbid": <str>}`` objects.
            Accepted only with ``release_mbid``.
        limit: Max cold folders this call (default ``song_stage_limit``, 150). A cold folder
            needs fpcalc or an AcoustID request. Warm folders always run, so ``limit=0`` costs
            no request and no fpcalc run.
        dry_run: Write the caches and nothing else. Per-file ``mappings`` appear when
            ``folder`` or ``file_ids`` scopes the call. A dry run skips the empty-staging
            precondition.

    Returns:
        ``{"ok": True, settled, staged_files, errors, error_items, pending_remaining,
        cold_folders_remaining, more, verified_files, held_disagreement, held_slot_collision,
        held_release_mismatch, held_unconverged, held_no_contribution, review_files,
        lookup_empty, skipped_manual, mappings, rebind_folders, held_values, review_values,
        summary}`` plus ``release`` and
        ``unassigned`` on the manual release path, or ``{"ok": False, "error": ...}`` (pending
        changes, no AcoustID key, fpcalc missing). ``more`` is ``cold_folders_remaining > 0``:
        a held file stays ``pending`` by design, so only a cold folder is new work.
    """
    pairs = (
        None
        if assignments is None
        else [(entry.get("file_id"), entry.get("release_track_mbid")) for entry in assignments]
    )
    result = songs.resolve_songs(
        load_settings(),
        folder=folder,
        file_ids=file_ids,
        release_mbid=release_mbid,
        assignments=pairs,
        limit=limit,
        dry_run=dry_run,
    )
    return {"ok": True, **result.to_dict()}


@mcp.tool()
@_error_envelope
def set_song_status(
    status: Literal["manual"],
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Record a deliberate human decision on the song axis (``manual``) for in-scope files.

    ``resolve_songs`` gives a ``manual`` file no outcome, though the file still votes for its
    folder. The row is sticky: an outside edit does not clear it, and ``reset_song_status`` is
    its only hand-back. Committing a hand edit of ``title``, ``tracknumber`` or ``discnumber``
    records ``manual`` too. With neither ``file_ids`` nor ``value`` the call changes nothing and
    returns ``affected: 0``.

    Args:
        status: ``manual``, the one state a human sets on this axis.
        file_ids: Limit to these file ids. An unknown id is refused.
        value: Limit to files whose ``album`` tag equals this value (used when ``file_ids``
            is omitted). The song axis has no lookup name field, so a whole album is the unit.

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}``.
    """
    affected = songs.set_song_status(
        load_settings(),
        file_ids=file_ids,
        value=value,
        status=status,
    )
    return {"ok": True, "affected": affected}


@mcp.tool()
@_error_envelope
def reset_song_status(
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Clear any song status row for in-scope files, returning them to ``pending``.

    Removes ``done`` and ``manual`` alike, so ``resolve_songs`` reconsiders the files on its
    next run. This is the only hand-back of a ``manual`` row. With neither ``file_ids`` nor
    ``value`` the call changes nothing and returns ``affected: 0``.

    Args:
        file_ids: Limit to these file ids. An unknown id is refused.
        value: Limit to files whose ``album`` tag equals this value (used when ``file_ids``
            is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}`` if a file id
        is unknown.
    """
    affected = songs.reset_song_status(
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
