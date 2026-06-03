"""Smoke tests for the CLI wiring (Typer)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from conftest import make_track
from tagmend import __version__
from tagmend.cli import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_N = 2


def _populate(music_dir: Path, count: int) -> None:
    for index in range(count):
        make_track(music_dir / f"track{index:02d}.mp3", {"genre": ["Synthwave"]})


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_config_path() -> None:
    result = runner.invoke(app, ["config-path"])
    assert result.exit_code == 0
    assert "settings.json" in result.stdout


def test_doctor_ok_with_music_path_override(temp_library: Path) -> None:
    result = runner.invoke(app, ["doctor", "--music-path", str(temp_library)])
    assert result.exit_code == 0
    assert "All checks passed" in result.stdout


def test_doctor_fails_for_missing_path(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--music-path", str(tmp_path / "nope")])
    assert result.exit_code == 1


def test_scan_with_explicit_path(music_dir: Path) -> None:
    _populate(music_dir, _N)

    result = runner.invoke(app, ["scan", str(music_dir)])

    assert result.exit_code == 0
    assert "added:" in result.stdout
    assert str(_N) in result.stdout


def test_scan_then_stats_share_ledger(music_dir: Path) -> None:
    # Both invocations run under the same isolated config/data dirs (one tmp_path),
    # so the scan's ledger is the one stats reads back.
    _populate(music_dir, _N)
    scan_result = runner.invoke(app, ["scan", str(music_dir)])
    assert scan_result.exit_code == 0

    stats_result = runner.invoke(app, ["stats"])
    assert stats_result.exit_code == 0
    assert "total files:" in stats_result.stdout
    assert str(_N) in stats_result.stdout


def test_scan_presence_mode_reads_no_tags(music_dir: Path) -> None:
    _populate(music_dir, _N)

    result = runner.invoke(app, ["scan", str(music_dir), "--mode", "presence"])

    assert result.exit_code == 0
    assert "tags read:" in result.stdout
    assert "0" in result.stdout


def test_scan_without_configured_music_path() -> None:
    # The isolate fixture clears config, so no music_path is set and no path arg given.
    result = runner.invoke(app, ["scan"])

    assert result.exit_code == 1
    assert "music_path" in result.stdout
