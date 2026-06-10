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
from tagmend.config import load_settings
from tagmend.engine import store
from tagmend.engine.db import connect
from tagmend.engine.tags import read_tags

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


def _enum_values(schema: object) -> list[object]:
    """Extract a JSON-Schema ``enum`` list, unwrapping an ``anyOf`` optional wrapper.

    A ``Literal[...] | None`` parameter is encoded as ``anyOf: [{enum: [...]}, {null}]``
    rather than a top-level ``enum`` key (which is how a non-optional Literal with a
    default, like ``mode``, is encoded).
    """
    assert isinstance(schema, dict)
    if "enum" in schema:
        enum = schema["enum"]
        assert isinstance(enum, list)
        return enum
    any_of = schema.get("anyOf")
    assert isinstance(any_of, list)
    for branch in any_of:
        if isinstance(branch, dict) and "enum" in branch:
            enum = branch["enum"]
            assert isinstance(enum, list)
            return enum
    message = f"no enum found in schema: {schema!r}"
    raise AssertionError(message)


def test_list_tools_exposes_expected_tools_and_schema() -> None:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert {
        "health_check",
        "scan_library",
        "library_stats",
        "list_files",
        "get_file",
        "stage_tags",
        "unstage_tags",
        "diff_tags",
        "commit_tags",
        "history_tags",
        "revert_tags",
        "revert_commit",
        "list_commits",
        "get_commit",
    } <= names
    # Retired names must be gone.
    assert names.isdisjoint({"unstage", "list_pending", "resume_commits"})

    scan_tool = next(tool for tool in tools if tool.name == "scan_library")
    mode_schema = scan_tool.inputSchema["properties"]["mode"]
    assert mode_schema["enum"] == ["incremental", "full", "presence"]

    list_tool = next(tool for tool in tools if tool.name == "list_files")
    genre_status_schema = list_tool.inputSchema["properties"]["genre_status"]
    enum_values = _enum_values(genre_status_schema)
    assert set(enum_values) == {"pending", "no_match", "manual", "staged", "done"}


def read_tags_via_disk(music_dir: Path, filename: str = "track.mp3") -> list[str]:
    """Read the genre tag straight off disk for the single track ``_scanned_track_id`` makes."""
    return read_tags(music_dir / filename).tags.get("genre", [])


def _scanned_track_id(music_dir: Path, filename: str = "track.mp3") -> int:
    """Make + scan one track via the MCP scan tool and return its stable file id."""
    track = make_track(music_dir / filename, {"genre": ["Electronic"]})
    mcp_server.scan_library(path=str(music_dir))
    conn = connect(load_settings().db_path)
    try:
        row = store.get_file(conn, str(music_dir), track.name)
        assert row is not None
        return row.id
    finally:
        conn.close()


def test_stage_diff_commit_roundtrip(music_dir: Path) -> None:
    file_id = _scanned_track_id(music_dir)

    assert mcp_server.stage_tags(file_id, {"genre": ["Synthwave"]}) == {"ok": True}

    diff = mcp_server.diff_tags()
    assert diff["ok"] is True
    changes = diff["changes"]
    assert isinstance(changes, list)
    assert len(changes) == 1
    assert changes[0]["diff"] == {"genre": {"from": ["Electronic"], "to": ["Synthwave"]}}

    committed = mcp_server.commit_tags(message="reclassify")
    assert committed["ok"] is True
    assert committed["committed"] == 1
    assert committed["missing_files"] == []
    assert "resumed" not in committed


def test_stage_tags_unmanaged_key_returns_error(music_dir: Path) -> None:
    file_id = _scanned_track_id(music_dir)

    payload = mcp_server.stage_tags(file_id, {"title": ["Nope"]})

    assert payload["ok"] is False
    assert "error" in payload


def test_unstage_tags_and_empty_commit_are_clean_noops(music_dir: Path) -> None:
    file_id = _scanned_track_id(music_dir)
    mcp_server.stage_tags(file_id, {"genre": ["Synthwave"]})

    assert mcp_server.unstage_tags(file_id) == {"ok": True, "removed": True}
    assert mcp_server.diff_tags()["changes"] == []

    # Nothing staged -> a commit is a clean no-op with no commit id.
    committed = mcp_server.commit_tags()
    assert committed["ok"] is True
    assert committed["commit_id"] is None
    assert committed["committed"] == 0


def test_list_files_and_get_file(music_dir: Path) -> None:
    file_id = _scanned_track_id(music_dir)

    listed = mcp_server.list_files()
    assert listed["ok"] is True
    files = listed["files"]
    assert isinstance(files, list)
    assert len(files) == 1
    assert files[0]["file_id"] == file_id
    assert files[0]["managed_tags"]["genre"] == ["Electronic"]
    # New genre-status fields ride along on every file dict.
    assert files[0]["genre_status"] == "pending"
    assert files[0]["genre_source_artist"] is None
    assert files[0]["genre_source_album"] is None

    got = mcp_server.get_file(file_id)
    assert got["ok"] is True
    assert got["file"]["file_id"] == file_id
    assert got["file"]["genre_status"] == "pending"

    assert mcp_server.get_file(9999)["ok"] is False


def _set_no_match(file_id: int, *, source_artist: str, source_album: str | None = None) -> None:
    """Persist a terminal ``no_match`` genre decision in the isolated ledger."""
    conn = connect(load_settings().db_path)
    try:
        store.set_genre_status(
            conn,
            file_id=file_id,
            status="no_match",
            source_artist=source_artist,
            source_album=source_album,
            now="2026-06-09T00:00:00+00:00",
        )
        conn.commit()
    finally:
        conn.close()


def test_list_files_genre_status_worklist_has_sources(music_dir: Path) -> None:
    file_id = _scanned_track_id(music_dir)
    _set_no_match(file_id, source_artist="Obscure Band", source_album="Demos")

    listed = mcp_server.list_files(genre_status="no_match")

    assert listed["ok"] is True
    files = listed["files"]
    assert isinstance(files, list)
    assert len(files) == 1
    entry = files[0]
    assert entry["file_id"] == file_id
    assert entry["genre_status"] == "no_match"
    assert entry["genre_source_artist"] == "Obscure Band"
    assert entry["genre_source_album"] == "Demos"


def test_list_files_bad_genre_status_returns_error(music_dir: Path) -> None:
    _scanned_track_id(music_dir)
    # The Literal annotation is not enforced at runtime; the engine ValueError is caught.
    payload = mcp_server.list_files(genre_status="bogus")
    assert payload["ok"] is False
    assert "error" in payload


def test_library_stats_includes_genre_block(music_dir: Path) -> None:
    file_id = _scanned_track_id(music_dir)
    _set_no_match(file_id, source_artist="Obscure Band")

    stats = mcp_server.library_stats()

    assert stats["ok"] is True
    genre = stats["genre"]
    assert isinstance(genre, dict)
    assert set(genre) == store.GENRE_WORKFLOW_STATUSES
    assert genre["no_match"] == 1


def test_history_and_revert_roundtrip(music_dir: Path) -> None:
    file_id = _scanned_track_id(music_dir)  # genre Electronic
    mcp_server.stage_tags(file_id, {"genre": ["Synthwave"]})
    mcp_server.commit_tags()

    hist = mcp_server.history_tags(file_id)
    assert hist["ok"] is True
    history = hist["history"]
    assert isinstance(history, list)
    assert [r["version"] for r in history] == [0, 1]

    reverted = mcp_server.revert_tags(file_id, 0)
    assert reverted["ok"] is True
    assert reverted["file_id"] == file_id
    assert reverted["target_version"] == 0
    assert reverted["new_version"] == 2
    # The single-file revert now lands under its own origin='revert' commit.
    assert isinstance(reverted["commit_id"], int)
    assert mcp_server.get_commit(reverted["commit_id"])["ok"] is True

    # An unknown version is an error, not a crash.
    assert mcp_server.revert_tags(file_id, 99)["ok"] is False


def test_list_commits_and_get_commit(music_dir: Path) -> None:
    file_id = _scanned_track_id(music_dir)
    mcp_server.stage_tags(file_id, {"genre": ["Synthwave"]})
    committed = mcp_server.commit_tags(message="reclassify")
    commit_id = committed["commit_id"]
    assert isinstance(commit_id, int)

    listed = mcp_server.list_commits()
    assert listed["ok"] is True
    commits_list = listed["commits"]
    assert isinstance(commits_list, list)
    assert any(c["id"] == commit_id for c in commits_list)

    got = mcp_server.get_commit(commit_id)
    assert got["ok"] is True
    assert got["commit"]["status"] == "applied"
    assert got["commit"]["message"] == "reclassify"

    assert mcp_server.get_commit(9999)["ok"] is False


def test_revert_commit_roundtrip(music_dir: Path) -> None:
    file_id = _scanned_track_id(music_dir)  # genre Electronic
    mcp_server.stage_tags(file_id, {"genre": ["Synthwave"]})
    committed = mcp_server.commit_tags()
    target = committed["commit_id"]
    assert isinstance(target, int)
    assert read_tags_via_disk(music_dir) == ["Synthwave"]

    reverted = mcp_server.revert_commit(target)
    assert reverted["ok"] is True
    assert reverted["reverted_from"] == target
    assert reverted["reverted"] == 1
    assert reverted["dry_run"] is False
    assert isinstance(reverted["commit_id"], int)
    assert reverted["commit_id"] != target
    # The whole commit was undone: the file is back to its pre-commit genre.
    assert read_tags_via_disk(music_dir) == ["Electronic"]

    # The new revert commit is itself a tracked, applied commit.
    new_commit = mcp_server.get_commit(reverted["commit_id"])
    assert new_commit["ok"] is True
    assert new_commit["commit"]["status"] == "applied"
    assert new_commit["commit"]["reverted_from"] == target


def test_revert_commit_dry_run_touches_nothing(music_dir: Path) -> None:
    file_id = _scanned_track_id(music_dir)
    mcp_server.stage_tags(file_id, {"genre": ["Synthwave"]})
    target = mcp_server.commit_tags()["commit_id"]
    assert isinstance(target, int)

    preview = mcp_server.revert_commit(target, dry_run=True)
    assert preview["ok"] is True
    assert preview["dry_run"] is True
    assert preview["commit_id"] is None
    assert preview["reverted"] == 1
    outcomes = preview["outcomes"]
    assert isinstance(outcomes, list)
    assert outcomes[0]["status"] == "reverted"
    # Disk and history are untouched by a dry run.
    assert read_tags_via_disk(music_dir) == ["Synthwave"]


def test_revert_commit_unknown_id_returns_error() -> None:
    payload = mcp_server.revert_commit(9999)
    assert payload["ok"] is False
    assert "error" in payload
