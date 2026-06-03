"""SQLite ledger access.

For M0 this only establishes a connection (WAL mode, foreign keys on) and ensures
the file exists — there are no tables yet. Each feature milestone adds its own schema
(see PLAN.md §7 for tags/versioning and §18 for the path/move log).
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from tagmend.log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating parent dirs as needed) the ledger in WAL mode.

    The caller owns the connection and must close it. No schema is created here.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    logger.debug("opened ledger at %s", db_path)
    return connection


def check_connection(db_path: Path) -> None:
    """Open the ledger, run a trivial query, and close it. Raises on failure."""
    connection = connect(db_path)
    try:
        connection.execute("SELECT 1").fetchone()
    finally:
        connection.close()
