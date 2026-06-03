"""Tests for settings loading and persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from tagmend import config

# Config/data dirs are isolated by the autouse `_isolate_config` fixture in conftest.


def test_defaults_when_no_file() -> None:
    settings = config.load_settings()
    assert settings.music_path is None
    assert settings.lastfm_api_key is None
    assert settings.db_path == config.default_db_path()


def test_set_and_load_roundtrip(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    config.set_setting("music_path", str(library))
    settings = config.load_settings()
    assert settings.music_path == library


def test_set_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="unknown setting"):
        config.set_setting("bogus", "x")


def test_env_overrides_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config.set_setting("music_path", str(tmp_path / "from_file"))
    monkeypatch.setenv("TAGMEND_MUSIC_PATH", str(tmp_path / "from_env"))
    settings = config.load_settings()
    assert settings.music_path == tmp_path / "from_env"
