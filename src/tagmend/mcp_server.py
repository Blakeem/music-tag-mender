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
from tagmend.engine import library
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


def run() -> None:
    """Run the MCP server over stdio (blocking)."""
    logger.info("starting TagMend MCP server (stdio)")
    mcp.run()
