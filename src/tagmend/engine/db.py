"""SQLite ledger access.

Opens the ledger in WAL mode with foreign keys on and creates its parent folder. It creates
no tables, because :func:`tagmend.engine.schema.apply_schema` owns every table.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, SupportsInt, cast

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


def as_int(value: object) -> int:
    """Coerce a SQLite scalar to ``int`` for strict typing. ``None`` raises :class:`TypeError`."""
    return int(cast("SupportsInt", value))
