"""Tests for the lookup clients' shared plumbing and the clients' settings builders."""

from __future__ import annotations

import dataclasses
import itertools
import threading
import time
from typing import TYPE_CHECKING, Self

import httpx
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


def test_two_threads_never_send_closer_together_than_the_interval() -> None:
    interval = 0.5
    sends_per_thread = 5
    # Each thread keeps its own fake time, advanced only by its own sleeps, so the time a
    # request reaches the transport is the send slot its thread waited for.
    thread_clock = threading.local()
    sends: list[float] = []
    sends_lock = threading.Lock()
    start = threading.Barrier(2)

    def monotonic() -> float:
        # A real yield lets the other thread run inside the pacer's read-then-record window.
        time.sleep(0.001)
        return float(getattr(thread_clock, "now", 0.0))

    def sleep(seconds: float) -> None:
        thread_clock.now = monotonic() + seconds

    def handle(_request: httpx.Request) -> httpx.Response:
        with sends_lock:
            sends.append(monotonic())
        return httpx.Response(200, json={})

    http = lookup_clients.PacedHttp(
        rate_per_sec=1.0 / interval,
        transport=httpx.MockTransport(handle),
        monotonic=monotonic,
        sleep=sleep,
    )

    def send_several() -> None:
        start.wait()
        for _ in range(sends_per_thread):
            http.send(
                lambda client: client.get("https://lookup.invalid/"),
                verdict=lambda response: response,
                error=lambda failure, _attempts: RuntimeError(failure),
                label="test",
            )

    with http:
        threads = [threading.Thread(target=send_several) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    ordered = sorted(sends)
    gaps = [later - earlier for earlier, later in itertools.pairwise(ordered)]
    assert len(ordered) == 2 * sends_per_thread
    assert min(gaps) >= interval
