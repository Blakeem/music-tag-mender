"""Integration tests for the MCP server tools (:mod:`tagmend.mcp_server`).

``@mcp.tool()`` returns the original function, so the tool callables are invoked
directly; the real MCP roundtrip path is exercised via ``mcp.call_tool`` / ``list_tools``.
All tools call ``load_settings()``, which the autouse isolate fixture redirects to the
temp config/data dirs, so every scan/stats lands in the same isolated ledger.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from mcp.types import TextContent

from conftest import make_track
from tagmend import mcp_server

if TYPE_CHECKING:
    from pathlib import Path

_N = 3


def _populate(music_dir: Path, count: int) -> None:
    for index in range(count):
        make_track(music_dir / f"track{index:02d}.mp3", {"genre": ["Synthwave"]})


def test_direct_scan_and_stats(music_dir: Path) -> None:
    _populate(music_dir, _N)

    scan_payload = mcp_server.scan_library(path=str(music_dir))
    assert scan_payload["ok"] is True
    assert scan_payload["added"] == _N

    stats_payload = mcp_server.library_stats()
    assert stats_payload["ok"] is True
    assert stats_payload["present"] == _N


def test_direct_scan_path_missing_returns_error(tmp_path: Path) -> None:
    payload = mcp_server.scan_library(path=str(tmp_path / "does-not-exist"))
    assert payload["ok"] is False
    assert "error" in payload


def test_direct_scan_without_config_returns_error() -> None:
    # No music_path configured (isolate fixture cleared config) and no path arg.
    payload = mcp_server.scan_library()
    assert payload["ok"] is False
    assert "error" in payload


def test_real_call_tool_roundtrip(music_dir: Path) -> None:
    _populate(music_dir, _N)

    raw = asyncio.run(
        mcp_server.mcp.call_tool("scan_library", {"path": str(music_dir)}),
    )
    # Be defensive: a future mcp may return (content, structured) instead of plain
    # content; unwrap the tuple before indexing the content blocks.
    if isinstance(raw, tuple):
        raw = raw[0]
    assert isinstance(raw, Sequence)
    first = raw[0]
    assert isinstance(first, TextContent)
    payload = json.loads(first.text)

    assert payload["ok"] is True
    assert payload["added"] == _N


def test_list_tools_exposes_expected_tools_and_schema() -> None:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert {"health_check", "scan_library", "library_stats"} <= names

    scan_tool = next(tool for tool in tools if tool.name == "scan_library")
    mode_schema = scan_tool.inputSchema["properties"]["mode"]
    assert mode_schema["enum"] == ["incremental", "full", "presence"]
