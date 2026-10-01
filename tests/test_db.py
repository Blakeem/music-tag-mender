"""Tests for the ledger connection (:mod:`tagmend.engine.db`)."""

from __future__ import annotations

from pathlib import Path

from tagmend.engine import db


def test_connect_waits_a_minute_for_another_writer(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "ledger.sqlite3")
    try:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
    finally:
        connection.close()
