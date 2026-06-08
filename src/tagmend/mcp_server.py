"""FastMCP server — a thin wrapper exposing the engine over MCP (stdio).

Contains no business logic: each tool marshals arguments, calls into
:mod:`tagmend.engine`, and returns a JSON-serializable result. Launch it with
``tagmend mcp`` (or point an MCP client / the MCP Inspector at that command).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from tagmend.config import load_settings
from tagmend.engine import commits, genres, library, staging, versioning
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
    are still ``unprocessed`` (no tags read yet), a per-extension breakdown, and the
    total number of stored tag values. Useful for gauging scan progress before running
    later resolve/commit steps.
    """
    return {"ok": True, **library.library_stats(load_settings())}


@mcp.tool()
def stage_tags(
    file_id: int,
    tags: dict[str, list[str]],
    note: str | None = None,
) -> dict[str, object]:
    """Stage a managed-tag change for one file (the git "index"). Writes nothing to disk.

    Records *tags* as the desired target for *file_id*, replacing any pending change for
    that file. Only managed tags are allowed (``genre``, ``artist``, ``albumartist``,
    ``musicbrainz_artistid``). The music file is not touched and no history is recorded
    until you call ``commit_tags``.

    Args:
        file_id: Stable id of the file (from ``scan_library`` / the snapshot).
        tags: Target managed tags as name -> ordered values, e.g. ``{"genre": ["Synthwave"]}``.
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
def list_files(path: str | None = None, limit: int | None = None) -> dict[str, object]:
    """List tracked files with their current managed tags (to discover file ids).

    Each entry carries the stable ``file_id`` you pass to ``stage_tags`` / ``history_tags``
    / ``revert_tags``, plus the file's folder/filename and current managed tags
    (``genre``, ``artist``, ``albumartist``, ``musicbrainz_artistid``). Run ``scan_library``
    first to populate the snapshot.

    Args:
        path: When given, only files at this folder or nested under it are returned.
        limit: Cap the number of files returned (applied before reading tags).

    Returns:
        ``{"ok": True, "files": [{file_id, folder, filename, ext, is_missing,
        managed_tags}, ...]}``.
    """
    views = library.list_files(
        load_settings(),
        root=Path(path) if path is not None else None,
        limit=limit,
    )
    return {"ok": True, "files": [view.to_dict() for view in views]}


@mcp.tool()
def get_file(file_id: int) -> dict[str, object]:
    """Return one tracked file with its current managed tags, by stable ``file_id``.

    Returns ``{"ok": True, "file": {file_id, folder, filename, ext, is_missing,
    managed_tags}}``, or ``{"ok": False, "error": ...}`` if the id is unknown.
    """
    view = library.get_file_view(load_settings(), file_id)
    if view is None:
        return {"ok": False, "error": f"unknown file_id={file_id}"}
    return {"ok": True, "file": view.to_dict()}


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
    — nothing is destroyed, and you can revert a revert. Get valid versions from
    ``history_tags``.

    Returns ``{"ok": True, "new_version": int}``, or ``{"ok": False, "error": ...}`` if the
    file or version is unknown or the file is missing on disk.
    """
    try:
        new_version = versioning.revert(load_settings(), file_id, version, note=note)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "new_version": new_version}


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


def run() -> None:
    """Run the MCP server over stdio (blocking)."""
    logger.info("starting TagMend MCP server (stdio)")
    mcp.run()
