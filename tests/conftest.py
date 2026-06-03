"""Shared pytest fixtures.

Tests never touch the real ``music/`` library or the real OS config/data dirs.
``temp_library`` builds a throwaway tree with one dummy audio file (and a non-audio
sidecar that must be ignored); the autouse ``_isolate_config`` fixture redirects the
config/data dirs into ``tmp_path`` and clears env overrides for every test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tagmend import config


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect config/data dirs into a temp location and clear env overrides."""
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "data")
    for var in ("TAGMEND_MUSIC_PATH", "TAGMEND_LASTFM_API_KEY", "TAGMEND_DB_PATH"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def temp_library(tmp_path: Path) -> Path:
    """A throwaway music library: one audio file plus a non-audio sidecar."""
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 Track.mp3").write_bytes(b"\x00")
    (album / "cover.jpg").write_bytes(b"\x00")  # ignored: not an audio extension
    return tmp_path
