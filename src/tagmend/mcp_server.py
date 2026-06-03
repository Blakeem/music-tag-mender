"""FastMCP server — a thin wrapper exposing the engine over MCP (stdio).

Contains no business logic: each tool marshals arguments, calls into
:mod:`tagmend.engine`, and returns a JSON-serializable result. Launch it with
``tagmend mcp`` (or point an MCP client / the MCP Inspector at that command).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from tagmend.config import load_settings
from tagmend.engine.doctor import run_health_check
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


def run() -> None:
    """Run the MCP server over stdio (blocking)."""
    logger.info("starting TagMend MCP server (stdio)")
    mcp.run()
