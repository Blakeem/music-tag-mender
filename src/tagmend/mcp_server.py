"""FastMCP server — a thin wrapper exposing the engine over MCP (stdio).

Contains no business logic: each tool marshals arguments, calls into
:mod:`tagmend.engine`, and returns a JSON-serializable result. Launch it with
``tagmend mcp`` (or point an MCP client / the MCP Inspector at that command).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from tagmend import configui
from tagmend.config import load_settings
from tagmend.engine import (
    albums,
    artists,
    commits,
    genres,
    library,
    mismatch,
    staging,
    versioning,
)
from tagmend.engine.doctor import run_health_check
from tagmend.engine.library import ScanMode
from tagmend.log import get_logger

logger = get_logger(__name__)

mcp = FastMCP("tagmend")


@mcp.tool()
def health_check() -> dict[str, object]:
    """Verify TagMend is ready to use.

    Checks that settings load, the configured music folder is reachable and
    readable, and the SQLite ledger opens. Returns an overall ``ok`` flag plus one
    entry per check. Call this from the MCP Inspector to confirm the environment is
    wired up correctly before building or running anything else.
    """
    settings = load_settings()
    report = run_health_check(settings)
    return report.to_dict()


@mcp.tool()
def scan_library(
    path: str | None = None,
    mode: Literal["incremental", "full", "presence"] = "incremental",
) -> dict[str, object]:
    """Scan a music folder into TagMend's snapshot database (reads files, never writes them).

    Walks the folder, records each audio file under a stable id, and stores its
    normalized tags. This is the read path: it only writes to the SQLite ledger, never
    to the music files themselves.

    Args:
        path: Folder to scan. Defaults to the configured ``music_path`` when omitted.
        mode: ``incremental`` re-reads tags only when a file changed or was never read;
            ``full`` re-reads every file's tags; ``presence`` only reconciles which
            files exist (added/missing/restored) without reading any tags.

    Returns:
        Per-run counts (``added``, ``updated``, ``tags_read``, ``missing_flagged``,
        ``restored``, ``errors``, ...) plus ``ok``. On a configuration/path problem,
        returns ``{"ok": False, "error": <message>}``.
    """
    settings = load_settings()
    try:
        result = library.scan_library(
            settings,
            path=Path(path) if path is not None else None,
            mode=ScanMode(mode),
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result.to_dict()}


@mcp.tool()
def library_stats() -> dict[str, object]:
    """Report library-wide snapshot counts.

    Returns totals for tracked files, how many are present vs missing on disk, how many
    are still ``unprocessed`` (no tags read yet), a per-extension breakdown, the total
    number of stored tag values, and a ``genre`` block counting every file's genre
    workflow state (``pending`` / ``staged`` / ``done`` / ``no_match`` / ``manual``) —
    the progress gauge for ``stage_genres``. Drill into a non-zero ``no_match`` or
    ``manual`` count with ``list_files(genre_status=...)``.
    """
    return {"ok": True, **library.library_stats(load_settings())}


@mcp.tool()
def stage_tags(
    file_id: int,
    tags: dict[str, list[str]],
    note: str | None = None,
) -> dict[str, object]:
    """Stage a managed-tag change for one file (the git "index"). Writes nothing to disk.

    Records *tags* for *file_id*, replacing any pending change for that file. Only managed
    tags are allowed — the closed ``tags.MANAGED_TAGS`` set (the genre/artist names plus
    the full title/album/date/track/disc + MusicBrainz-id identity "stamp"); any other key
    is rejected. The music file is not touched and no history is recorded until you call
    ``commit_tags``.

    *tags* is merged **onto** the file's current managed tags: keys you omit are left
    alone, so staging ``{"genre": ["Synthwave"]}`` changes only the genre and preserves
    title/album/track/MusicBrainz ids. To intentionally clear a managed field, pass it
    explicitly with an empty list (e.g. ``{"genre": []}``).

    Args:
        file_id: Stable id of the file (from ``scan_library`` / the snapshot).
        tags: Managed tags to set, as name -> ordered values, e.g. ``{"genre": ["Synthwave"]}``.
            Omitted managed keys are preserved; ``{"key": []}`` deletes that key.
        note: Optional free-text note stored with the eventual revision.

    Returns:
        ``{"ok": True}`` on success, or ``{"ok": False, "error": ...}`` on a bad request.
    """
    try:
        staging.stage_tags(
            load_settings(),
            file_id=file_id,
            managed_tags=tags,
            note=note,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


@mcp.tool()
def unstage_tags(file_id: int) -> dict[str, object]:
    """Remove a pending staged change for one file.

    Returns ``{"ok": True, "removed": <bool>}`` — ``removed`` is ``False`` when the file
    had nothing staged.
    """
    removed = staging.unstage_tags(load_settings(), file_id=file_id)
    return {"ok": True, "removed": removed}


@mcp.tool()
def diff_tags(path: str | None = None) -> dict[str, object]:
    """Show staged-but-uncommitted tag changes, enriched with the current→target diff.

    This is ``git diff --staged`` (staged-vs-snapshot), not working-tree: ``current`` is
    the last committed/scanned snapshot and may lag the file on disk after an interrupted
    commit. A no-op stage still appears, with ``diff == {}``.

    Args:
        path: When given, only staged changes for files at this folder or nested under it
            are returned; otherwise all staged changes are listed.

    Returns:
        ``{"ok": True, "changes": [{file_id, folder, filename, is_missing, origin, note,
        staged_at, current, target, diff}, ...]}``.
    """
    changes = staging.diff_tags(
        load_settings(),
        root=Path(path) if path is not None else None,
    )
    return {"ok": True, "changes": [view.to_dict() for view in changes]}


@mcp.tool()
def commit_tags(message: str | None = None, path: str | None = None) -> dict[str, object]:
    """Apply all staged tag changes to disk as one revertible commit.

    Writes each staged file's target tags to disk and appends an append-only revision
    under a shared commit id, so the whole batch reverts as a unit. Files that vanished
    from disk since staging are flagged missing, dropped, and reported under
    ``missing_files`` — the commit still completes for the rest. Any commit left
    ``applying`` by a prior crash is marked interrupted first and its leftover staged rows
    are swept into this commit.

    Args:
        message: Optional commit message stored on the commit.
        path: When given, only staged changes for files at this folder or nested under it
            are committed; otherwise all staged changes are committed.

    Returns:
        ``{"ok": True, ...}`` with per-file ``outcomes`` and ``committed`` / ``noop`` /
        ``missing`` counts, or ``{"ok": False, "error": ...}`` on a bad request.
    """
    try:
        result = staging.commit_tags(
            load_settings(),
            message=message,
            root=Path(path) if path is not None else None,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result.to_dict()}


@mcp.tool()
def list_files(
    path: str | None = None,
    limit: int | None = None,
    genre_status: Literal["pending", "no_match", "manual", "staged", "done"] | None = None,
    artist_status: Literal["pending", "manual", "staged", "done"] | None = None,
    album_status: Literal["pending", "no_match", "manual", "staged", "done"] | None = None,
) -> dict[str, object]:
    """List tracked files with their current managed tags (to discover file ids).

    Each entry carries the stable ``file_id`` you pass to ``stage_tags`` / ``history_tags``
    / ``revert_tags``, plus the file's folder/filename, current managed tags (the closed
    ``tags.MANAGED_TAGS`` set — genre/artist names + the title/album/date/track/disc +
    MusicBrainz-id identity "stamp"), and its genre workflow ``genre_status``. Run
    ``scan_library`` first to populate the snapshot.

    ``genre_status="no_match"`` is the **fix-by-hand worklist**: files Last.fm had nothing
    for. Each carries ``genre_source_artist``/``genre_source_album`` — the identity the
    lookup used — so a misspelled artist/album is visible next to the current tags (fix
    the tags, rescan, and ``stage_genres`` retries automatically). ``manual`` lists the
    sticky exclusions (``set_genre_status``). A stored status is reported even if the
    file's identity changed since (compare the source fields to spot staleness).
    ``pending`` includes files ``stage_genres`` cannot process for lack of an artist tag.

    Args:
        path: When given, only files at this folder or nested under it are returned.
        limit: Cap the number of files returned. With ``genre_status`` the cap counts
            *matching* files; without it, it is applied before reading tags.
        genre_status: Return only files in this genre workflow state
            (``pending`` | ``no_match`` | ``manual`` | ``staged`` | ``done``).
        artist_status: Return only files in this artist workflow state
            (``pending`` | ``manual`` | ``staged`` | ``done``). Combined with
            ``genre_status``, a file must match BOTH. The axes are independent:
            ``staged``/``done`` are field-aware (genre keys on ``genre``; artist on
            ``artist``/``albumartist``; album on ``originaldate``).
        album_status: Return only files in this album workflow state
            (``pending`` | ``no_match`` | ``manual`` | ``staged`` | ``done``). Combined with
            the other filters, a file must match ALL. ``album_source_artist`` /
            ``album_source_album`` carry the resolved identity a stored decision was taken
            against.

    Returns:
        ``{"ok": True, "files": [{file_id, folder, filename, ext, is_missing,
        managed_tags, genre_status, genre_source_artist, genre_source_album,
        artist_status, artist_source_artist, artist_source_albumartist, album_status,
        album_source_artist, album_source_album}, ...]}``,
        or ``{"ok": False, "error": ...}`` on a bad request.
    """
    try:
        views = library.list_files(
            load_settings(),
            root=Path(path) if path is not None else None,
            limit=limit,
            genre_status=genre_status,
            artist_status=artist_status,
            album_status=album_status,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "files": [view.to_dict() for view in views]}


@mcp.tool()
def get_file(file_id: int) -> dict[str, object]:
    """Return one tracked file with its current managed tags, by stable ``file_id``.

    Returns ``{"ok": True, "file": {file_id, folder, filename, ext, is_missing,
    managed_tags, genre_status, genre_source_artist, genre_source_album, artist_status,
    artist_source_artist, artist_source_albumartist}}``, or
    ``{"ok": False, "error": ...}`` if the id is unknown.
    """
    view = library.get_file_view(load_settings(), file_id)
    if view is None:
        return {"ok": False, "error": f"unknown file_id={file_id}"}
    return {"ok": True, "file": view.to_dict()}


@mcp.tool()
def detect_mismatches(
    tier: Literal["high", "medium", "low"] | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    """Detect files whose identity tags disagree with their folder path (read-only report).

    Flags the fingerprint of a MusicBrainz Picard release mis-match: files stamped with the
    WRONG ``albumartist`` (with an ``artist`` fallback for files that have none) while their
    folder path kept the truth — e.g. Ozzy's *Down to Earth* files tagged as *Jem*. Pure read
    over the snapshot: writes nothing, stages nothing, no network. Run ``scan_library`` first.

    Each file is classified by ``albumartist``-vs-path bidirectional containment into a
    confidence tier: ``high`` (path disagreement in a folder with mixed albumartists),
    ``medium`` (path disagreement in a uniformly mis-stamped folder), or ``low`` (a
    folder-consistency fallback, a non-album/singles folder, or the artist fallback).
    Various-Artists/soundtrack albumartists are excluded. A library-wide reliability guard
    suppresses the ``high``/``medium`` path tiers (emitting only the naming-agnostic ``low``
    tier) when the path likely does not encode artist — reported via ``path_signal_suppressed``
    and ``disagreement_rate``.

    Args:
        tier: Return only rows in this tier (``high`` | ``medium`` | ``low``). The
            ``high``/``medium``/``low``/``flagged`` counts still describe the whole library.
        limit: Cap the number of rows returned (the counts are unaffected).

    Returns:
        ``{"ok": True, rows, total_files, flagged, high, medium, low, disagreement_rate,
        path_signal_suppressed, summary}`` — each row is ``{file_id, folder, filename, field,
        tag_value, path_artist, tier, reason}`` — or ``{"ok": False, "error": ...}`` (e.g. no
        music path configured).
    """
    try:
        report = mismatch.detect_mismatches(load_settings(), tier=tier, limit=limit)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **report.to_dict()}


@mcp.tool()
def history_tags(file_id: int) -> dict[str, object]:
    """Show the append-only tag-revision log for one file, oldest (version 0) first.

    Each revision carries its ``version``, ``origin`` (scan|auto|manual|revert), the
    ``commit_id`` that grouped it, the full ``managed_tags`` snapshot at that version, and
    the ``diff`` from the prior version. Use a ``version`` here with ``revert_tags``.

    Returns ``{"ok": True, "history": [{version, created_at, origin, reverted_from,
    commit_id, managed_tags, diff, note}, ...]}`` (empty if the file has no history).
    """
    revisions = versioning.history_for(load_settings(), file_id)
    return {
        "ok": True,
        "history": [
            {
                "version": r.version,
                "created_at": r.created_at,
                "origin": r.origin,
                "reverted_from": r.reverted_from,
                "commit_id": r.commit_id,
                "managed_tags": r.managed_tags,
                "diff": r.diff,
                "note": r.note,
            }
            for r in revisions
        ],
    }


@mcp.tool()
def revert_tags(file_id: int, version: int, note: str | None = None) -> dict[str, object]:
    """Restore a file's managed tags to a prior ``version`` (append-only, revertible).

    Writes the target revision's tags back to disk and appends a *new* ``revert`` revision
    under its own single-file ``origin='revert'`` commit — nothing is destroyed, you can
    revert a revert, and the revert shows in ``list_commits`` (undoable via
    ``revert_commit``). Get valid versions from ``history_tags``. Refused while the file
    has a staged change — commit or unstage it first.

    Returns ``{"ok": True, "new_version": int, "commit_id": int, ...}``, or
    ``{"ok": False, "error": ...}`` if the file or version is unknown, the file is
    missing on disk, or the file has a pending staged change.
    """
    try:
        result = versioning.revert(load_settings(), file_id, version, note=note)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result.to_dict()}


@mcp.tool()
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
            "would be reverted").

    Returns:
        ``{"ok": True, "commit_id": ..., "reverted_from": ..., "dry_run": ...,
        "reverted"/"skipped"/"missing"/"errors": counts, "outcomes": [...]}`` with one
        outcome per file, or ``{"ok": False, "error": ...}`` if the commit id is
        unknown, the commit is still ``applying``, or the staging area is not empty.
    """
    try:
        result = versioning.revert_commit(
            load_settings(),
            commit_id,
            note=note,
            dry_run=dry_run,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result.to_dict()}


@mcp.tool()
def list_commits(limit: int | None = None) -> dict[str, object]:
    """List commits newest first (the revertible units that group tag changes).

    Returns ``{"ok": True, "commits": [{id, created_at, origin, message, reverted_from,
    status}, ...]}``. ``status`` is ``applied`` (clean), ``applying`` (in flight), or
    ``interrupted`` (a crashed run whose leftovers were swept into a later commit).
    """
    rows = commits.list_commits_for(load_settings(), limit=limit)
    return {"ok": True, "commits": [_commit_to_dict(c) for c in rows]}


@mcp.tool()
def get_commit(commit_id: int) -> dict[str, object]:
    """Return one commit by id.

    Returns ``{"ok": True, "commit": {id, created_at, origin, message, reverted_from,
    status}}``, or ``{"ok": False, "error": ...}`` if the id is unknown.
    """
    commit = commits.get_commit_for(load_settings(), commit_id)
    if commit is None:
        return {"ok": False, "error": f"unknown commit_id={commit_id}"}
    return {"ok": True, "commit": _commit_to_dict(commit)}


def _commit_to_dict(commit: commits.Commit) -> dict[str, object]:
    """JSON-serializable form of a commit row."""
    return {
        "id": commit.id,
        "created_at": commit.created_at,
        "origin": commit.origin,
        "message": commit.message,
        "reverted_from": commit.reverted_from,
        "status": commit.status,
    }


@mcp.tool()
def stage_genres(
    artist: str | None = None,
    album: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    """Look up Last.fm genres for in-scope files and stage the result (writes nothing to disk).

    For each in-scope file this looks up its artist (``albumartist`` when present, else
    ``artist``) and optionally album on Last.fm, classifies the community tags against the
    controlled genre vocabulary, and stages the resolved genres as an ``auto`` change
    (replacing ONLY ``genre`` — other managed tags are preserved). Review with
    ``diff_tags`` and apply with ``commit_tags``; ``revert_tags`` undoes it.

    It deliberately **skips** files that are already done (already staged, or already have
    a committed ``auto`` genre revision), files marked ``no_match`` (unless the artist or
    album tag has changed since), files marked ``manual``, and files with no artist tag at
    all. Files whose artist isn't on Last.fm (or yield no usable genre) are recorded
    ``no_match`` and not re-tried until their tags change. A transient Last.fm error leaves
    that artist pending and is reported under ``errors`` so a re-run retries it.

    Args:
        artist: Limit to files whose ``artist`` tag equals this value.
        album: Narrow an ``artist`` scope to one album.
        file_ids: Limit to these specific file ids (overrides ``artist``/``album``).
        limit: Max files to process this call (default ``genre_stage_limit``). Remaining
            candidates are reported via ``pending_remaining`` / ``more`` — call again to
            continue.

    Returns:
        ``{"ok": True, processed, staged, no_match, skipped{done,no_match,manual,no_artist},
        pending_remaining, more, errors, no_match_artists, summary}``, or
        ``{"ok": False, "error": ...}`` (e.g. no API key configured).
    """
    try:
        result = genres.stage_genres(
            load_settings(),
            artist=artist,
            album=album,
            file_ids=file_ids,
            limit=limit,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result.to_dict()}


@mcp.tool()
def resolve_artists(
    artist: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
) -> dict[str, object]:
    """Normalize artist names via Last.fm getCorrection and stage the result (writes no disk).

    For each distinct ``artist``/``albumartist`` value in scope this looks up the canonical
    name on Last.fm and, where it differs, cascade-stages the corrected name across every
    file carrying that value (rewriting ``artist`` and/or ``albumartist``, exact-match only)
    plus the correction's ``musicbrainz_artistid`` — as an ``auto`` change replacing ONLY
    those fields (every other managed tag, incl. ``genre``, is preserved). Review with
    ``diff_tags`` and apply with ``commit_tags``; ``revert_commit``/``revert_tags`` undo it.

    It deliberately **skips** (and reports) values in the ``feat``/``ft``/``featuring``
    family, compilation sentinels (``various artists``/``various``/``va``), and empty
    values; and any file whose ``artist``/``albumartist`` is multi-value. Values already
    canonical, or with no Last.fm correction, stage nothing (the latter reported under
    ``no_correction``). A transient Last.fm error leaves that value pending under
    ``error_values`` so a re-run retries it.

    Args:
        artist: Limit to files whose ``artist`` tag equals this value.
        file_ids: Limit to these specific file ids (overrides ``artist``).
        limit: Max distinct values to process this call. Remaining values are reported via
            ``pending_remaining`` / ``more`` — call again to continue.
        dry_run: Preview the ``value → canonical`` mappings + would-stage count without
            staging anything (works from cache, no precondition).

    Returns:
        ``{"ok": True, processed, staged_files, corrected_values, skipped_multi_artist,
        skipped_sentinel, no_correction, errors, pending_remaining, more, mappings,
        multi_artist_files, no_correction_values, error_values, summary}``, or
        ``{"ok": False, "error": ...}`` (e.g. pending changes, or no API key configured).
    """
    try:
        result = artists.resolve_artists(
            load_settings(),
            artist=artist,
            file_ids=file_ids,
            limit=limit,
            dry_run=dry_run,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result.to_dict()}


@mcp.tool()
def list_artists() -> dict[str, object]:
    """List distinct ``artist`` tag values with file counts (to scope ``stage_genres``).

    Returns ``{"ok": True, "artists": [{artist, file_count}, ...]}`` in artist-value order.
    Run ``scan_library`` first to populate the snapshot.
    """
    rows = genres.list_artists(load_settings())
    return {"ok": True, "artists": [row.to_dict() for row in rows]}


@mcp.tool()
def set_genre_status(
    status: Literal["manual", "pending"],
    file_ids: list[int] | None = None,
    artist: str | None = None,
) -> dict[str, object]:
    """Exclude files from genre tagging (``manual``) or re-queue them (``pending``).

    ``manual`` marks the in-scope files as a deliberate human/LLM choice: ``stage_genres``
    skips them until you reset. ``pending`` removes any status row, re-queuing them. You
    may exclude or re-include files, but cannot set engine-owned outcomes (e.g.
    ``no_match`` — that is decided by the Last.fm lookup).

    Args:
        status: ``manual`` to exclude, ``pending`` to re-queue.
        file_ids: Limit to these file ids.
        artist: Limit to files whose ``artist`` tag equals this value (used when
            ``file_ids`` is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}``.
    """
    try:
        affected = genres.set_genre_status(
            load_settings(),
            file_ids=file_ids,
            artist=artist,
            status=status,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "affected": affected}


@mcp.tool()
def reset_genre_status(
    file_ids: list[int] | None = None,
    artist: str | None = None,
) -> dict[str, object]:
    """Clear any genre status row for in-scope files, returning them to ``pending``.

    Removes both ``no_match`` and ``manual`` decisions so ``stage_genres`` will reconsider
    the files on its next run.

    Args:
        file_ids: Limit to these file ids.
        artist: Limit to files whose ``artist`` tag equals this value (used when
            ``file_ids`` is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``.
    """
    affected = genres.reset_genre_status(
        load_settings(),
        file_ids=file_ids,
        artist=artist,
    )
    return {"ok": True, "affected": affected}


@mcp.tool()
def set_artist_status(
    status: Literal["manual", "pending"],
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Exclude files from artist-name normalization (``manual``) or re-queue them (``pending``).

    ``manual`` marks the in-scope files as a deliberate human/LLM choice: ``resolve_artists``
    always skips them (sticky — no staleness re-check) until you reset. ``pending`` removes
    any status row, re-queuing them.

    Scope is ``file_ids`` when given, else every file carrying ``value`` as its ``artist``
    OR ``albumartist`` tag (so excluding ``"Miami Nights 84"`` catches it on either field).

    Args:
        status: ``manual`` to exclude, ``pending`` to re-queue.
        file_ids: Limit to these file ids.
        value: Limit to files carrying this value as ``artist`` or ``albumartist`` (used
            when ``file_ids`` is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}``.
    """
    try:
        affected = artists.set_artist_status(
            load_settings(),
            file_ids=file_ids,
            value=value,
            status=status,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "affected": affected}


@mcp.tool()
def reset_artist_status(
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Clear any artist status row for in-scope files, returning them to ``pending``.

    Removes the ``manual`` exclusion so ``resolve_artists`` will reconsider the files on
    its next run.

    Args:
        file_ids: Limit to these file ids.
        value: Limit to files carrying this value as ``artist`` or ``albumartist`` (used
            when ``file_ids`` is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``.
    """
    affected = artists.reset_artist_status(
        load_settings(),
        file_ids=file_ids,
        value=value,
    )
    return {"ok": True, "affected": affected}


@mcp.tool()
def resolve_albums(
    album: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = False,  # noqa: FBT001, FBT002 - MCP tool surface, not a Python API
) -> dict[str, object]:
    """Blank-fill the original release year (``originaldate``) from MusicBrainz (writes no disk).

    For each in-scope album group ``(albumartist-else-artist, album)`` this looks up the
    original first-release year on MusicBrainz (a release group's ``first-release-date``,
    e.g. *Paranoid* = 1970 — distinct from the reissue ``date``) and stages it into
    ``originaldate`` on every group file whose ``originaldate`` is currently **blank**, as an
    ``auto`` change replacing ONLY that field (every other managed tag preserved). It never
    overwrites an existing ``originaldate`` and never touches ``date``. Review with
    ``diff_tags`` and apply with ``commit_tags``; ``revert_commit``/``revert_tags`` undo it.

    It deliberately **skips** files that already have an ``originaldate``
    (``skipped_present``), files with no ``album`` (``skipped_no_album``), files with no
    artist (``skipped_no_artist``), and ``manual`` exclusions (``skipped_manual``). A group
    MusicBrainz has no usable Album release group for is recorded ``no_match`` (re-opened if
    the artist or album changes). A transient MusicBrainz error leaves that group pending.

    Args:
        album: Limit to files whose ``album`` tag equals this value.
        file_ids: Limit to these specific file ids (overrides ``album``).
        limit: Max album groups to process this call (default ``album_stage_limit``).
            Remaining groups are reported via ``pending_remaining`` / ``more``.
        dry_run: Preview the album → original-year mappings + would-stage count without
            staging anything (works from cache, no precondition).

    Returns:
        ``{"ok": True, processed, staged_files, no_match, skipped_present, skipped_no_album,
        skipped_no_artist, skipped_manual, pending_remaining, more, mappings, summary}``, or
        ``{"ok": False, "error": ...}`` (e.g. pending changes).
    """
    try:
        result = albums.resolve_albums(
            load_settings(),
            album=album,
            file_ids=file_ids,
            limit=limit,
            dry_run=dry_run,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result.to_dict()}


@mcp.tool()
def list_albums() -> dict[str, object]:
    """List distinct album groups with file counts + status (to scope ``resolve_albums``).

    Groups files by ``(albumartist-else-artist, album)`` and reports each group's file count
    and derived album workflow status. Returns ``{"ok": True, "albums": [{artist, album,
    file_count, album_status}, ...]}`` in ``(artist, album)`` order. Run ``scan_library``
    first to populate the snapshot.
    """
    rows = albums.list_albums(load_settings())
    return {"ok": True, "albums": [row.to_dict() for row in rows]}


@mcp.tool()
def set_album_status(
    status: Literal["manual", "pending"],
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Exclude files from album-year fill (``manual``) or re-queue them (``pending``).

    ``manual`` marks the in-scope files as a deliberate human/LLM choice: ``resolve_albums``
    skips them until you reset. ``pending`` removes any status row, re-queuing them.

    Scope is ``file_ids`` when given, else every file carrying ``value`` as its ``album``
    tag.

    Args:
        status: ``manual`` to exclude, ``pending`` to re-queue.
        file_ids: Limit to these file ids.
        value: Limit to files whose ``album`` tag equals this value (used when ``file_ids``
            is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``, or ``{"ok": False, "error": ...}``.
    """
    try:
        affected = albums.set_album_status(
            load_settings(),
            file_ids=file_ids,
            value=value,
            status=status,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "affected": affected}


@mcp.tool()
def reset_album_status(
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> dict[str, object]:
    """Clear any album status row for in-scope files, returning them to ``pending``.

    Removes both ``no_match`` and ``manual`` decisions so ``resolve_albums`` will reconsider
    the files on its next run.

    Args:
        file_ids: Limit to these file ids.
        value: Limit to files whose ``album`` tag equals this value (used when ``file_ids``
            is omitted).

    Returns:
        ``{"ok": True, "affected": <count>}``.
    """
    affected = albums.reset_album_status(
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
