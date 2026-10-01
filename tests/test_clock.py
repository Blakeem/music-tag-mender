"""Tests for the engine clock (:mod:`tagmend.engine.clock`)."""

from __future__ import annotations

from datetime import datetime, timedelta

from tagmend.engine import clock


def test_utc_now_is_iso_8601_utc() -> None:
    parsed = datetime.fromisoformat(clock.utc_now())
    assert parsed.utcoffset() == timedelta(0)


def test_utc_now_orders_as_a_string() -> None:
    first = clock.utc_now()
    second = clock.utc_now()
    assert first <= second
