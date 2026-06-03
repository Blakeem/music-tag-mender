"""Smoke tests for the CLI wiring (Typer)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from tagmend import __version__
from tagmend.cli import app

runner = CliRunner()


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
