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
    # M2 genre/Last.fm settings fall back to their built-in defaults.
    assert settings.genre_min_weight == 2
    assert settings.genre_max_count == 4
    assert settings.genre_use_album_tags is True
    assert settings.lastfm_rate_per_sec == 1.0
    assert settings.genre_stage_limit == 300


def test_genre_settings_parse_valid_values() -> None:
    config.set_setting("genre_min_weight", "5")
    config.set_setting("genre_max_count", "3")
    config.set_setting("genre_use_album_tags", "false")
    config.set_setting("lastfm_rate_per_sec", "2.5")
    config.set_setting("genre_stage_limit", "50")

    settings = config.load_settings()
    assert settings.genre_min_weight == 5
    assert settings.genre_max_count == 3
    assert settings.genre_use_album_tags is False
    assert settings.lastfm_rate_per_sec == 2.5
    assert settings.genre_stage_limit == 50


def test_malformed_int_falls_back_to_default() -> None:
    config.set_setting("genre_min_weight", "not-a-number")
    config.set_setting("genre_stage_limit", "abc")
    config.set_setting("lastfm_rate_per_sec", "fast")

    settings = config.load_settings()
    assert settings.genre_min_weight == 2
    assert settings.genre_stage_limit == 300
    assert settings.lastfm_rate_per_sec == 1.0


@pytest.mark.parametrize("token", ["", "0", "none", "NULL", "None"])
def test_genre_max_count_none_sentinel(token: str) -> None:
    config.set_setting("genre_max_count", token)
    assert config.load_settings().genre_max_count is None


def test_genre_max_count_malformed_falls_back_to_default() -> None:
    config.set_setting("genre_max_count", "lots")
    assert config.load_settings().genre_max_count == 4


@pytest.mark.parametrize("token", ["false", "0", "no", "off", "OFF", "No"])
def test_genre_use_album_tags_false_tokens(token: str) -> None:
    config.set_setting("genre_use_album_tags", token)
    assert config.load_settings().genre_use_album_tags is False


@pytest.mark.parametrize("token", ["true", "1", "yes", "on", "anything"])
def test_genre_use_album_tags_truthy_tokens(token: str) -> None:
    config.set_setting("genre_use_album_tags", token)
    assert config.load_settings().genre_use_album_tags is True


def test_env_overrides_genre_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    config.set_setting("genre_min_weight", "5")
    monkeypatch.setenv("TAGMEND_GENRE_MIN_WEIGHT", "9")
    assert config.load_settings().genre_min_weight == 9


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
