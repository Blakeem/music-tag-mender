"""Tests for the health check / readiness probe."""

from __future__ import annotations

from pathlib import Path

from tagmend.config import Settings
from tagmend.engine.doctor import run_health_check


def _settings(music_path: Path | None, tmp_path: Path) -> Settings:
    return Settings(
        music_path=music_path,
        lastfm_api_key=None,
        db_path=tmp_path / "ledger.sqlite3",
    )


def test_passes_for_valid_library(temp_library: Path, tmp_path: Path) -> None:
    report = run_health_check(_settings(temp_library, tmp_path))
    assert report.ok
    assert {c.name for c in report.checks} == {"music_path", "database"}


def test_to_dict_shape(temp_library: Path, tmp_path: Path) -> None:
    data = run_health_check(_settings(temp_library, tmp_path)).to_dict()
    assert data["ok"] is True
    assert isinstance(data["checks"], list)


def test_fails_when_music_path_unset(tmp_path: Path) -> None:
    report = run_health_check(_settings(None, tmp_path))
    assert not report.ok


def test_fails_when_music_path_missing(tmp_path: Path) -> None:
    report = run_health_check(_settings(tmp_path / "does-not-exist", tmp_path))
    assert not report.ok


def test_database_check_creates_ledger(temp_library: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "ledger.sqlite3"
    settings = Settings(music_path=temp_library, lastfm_api_key=None, db_path=db_path)
    report = run_health_check(settings)
    assert report.ok
    assert db_path.exists()
