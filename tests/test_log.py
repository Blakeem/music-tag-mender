"""Tests for the shared logger setup (:mod:`tagmend.log`)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from tagmend.engine.lastfm import LastfmClient
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    import pytest


def test_http_client_loggers_are_quiet() -> None:
    get_logger("x")

    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_lastfm_request_never_logs_the_api_key(
    caplog: pytest.LogCaptureFixture,
    db_conn: sqlite3.Connection,
) -> None:
    # httpx logs every request URL at INFO, and the Last.fm key rides in the query string.
    caplog.set_level(logging.DEBUG)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"toptags": {"tag": []}}),
    )

    with LastfmClient("SECRET-KEY", db_conn, rate_per_sec=0.0, transport=transport) as client:
        client.artist_top_tags("Anybody")

    assert "SECRET-KEY" not in caplog.text
