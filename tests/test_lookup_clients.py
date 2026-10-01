"""Tests for the injected-or-owned lookup client seam and the clients' settings builders."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Self

import pytest

from tagmend.engine import lookup_clients
from tagmend.engine.lastfm import LastfmClient
from tagmend.engine.musicbrainz import MusicBrainzClient

if TYPE_CHECKING:
    import sqlite3
    from types import TracebackType

    from tagmend.config import Settings


class _RecordingContext:
    """A fake owned client that records whether it was entered and exited."""

    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    def __enter__(self) -> Self:
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.exited = True


def test_injected_client_is_yielded_without_building() -> None:
    injected = _RecordingContext()
    built: list[_RecordingContext] = []

    def build() -> _RecordingContext:
        built.append(_RecordingContext())
        return built[-1]

    with lookup_clients.injected_or_owned(injected, build) as source:
        assert source is injected

    assert built == []
    assert injected.entered is False
    assert injected.exited is False


def test_owned_client_is_entered_and_exited() -> None:
    owned = _RecordingContext()

    with lookup_clients.injected_or_owned(None, lambda: owned) as source:
        assert source is owned
        assert owned.entered is True
        assert owned.exited is False

    assert owned.exited is True


def test_lastfm_from_settings_requires_an_api_key(
    engine_settings: Settings,
    db_conn: sqlite3.Connection,
) -> None:
    with pytest.raises(ValueError, match=r"no Last\.fm API key configured"):
        LastfmClient.from_settings(engine_settings, db_conn)


def test_lastfm_from_settings_builds_with_a_key(
    engine_settings: Settings,
    db_conn: sqlite3.Connection,
) -> None:
    settings = dataclasses.replace(engine_settings, lastfm_api_key="key")

    assert isinstance(LastfmClient.from_settings(settings, db_conn), LastfmClient)


def test_musicbrainz_from_settings_builds(
    engine_settings: Settings,
    db_conn: sqlite3.Connection,
) -> None:
    assert isinstance(MusicBrainzClient.from_settings(engine_settings, db_conn), MusicBrainzClient)
