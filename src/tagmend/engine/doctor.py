"""Health check / readiness probe (M0).

Verifies that the environment is wired up: settings resolve, the configured music
folder is reachable and readable, and the SQLite ledger can be opened. Surfaced both
as ``tagmend doctor`` (CLI) and the ``health_check`` MCP tool, so the very first
thing we can do — before any feature exists — is confirm we're ready to build.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tagmend.engine import db, scan
from tagmend.log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Check:
    """The result of one individual readiness check."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Aggregate of all readiness checks."""

    checks: list[Check]

    @property
    def ok(self) -> bool:
        """``True`` only if every check passed."""
        return all(check.ok for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form, suitable for returning from an MCP tool."""
        return {
            "ok": self.ok,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


def run_health_check(settings: Settings) -> HealthReport:
    """Run every readiness check against *settings* and return the aggregate report."""
    checks = [
        _check_music_path(settings.music_path),
        _check_database(settings.db_path),
    ]
    report = HealthReport(checks=checks)
    logger.info("health check complete: ok=%s", report.ok)
    return report


def _check_music_path(music_path: Path | None) -> Check:
    """Confirm the music folder is configured, exists, and is a readable directory."""
    name = "music_path"

    if music_path is None:
        return Check(
            name=name,
            ok=False,
            detail="not configured — run `tagmend config-set music_path <dir>`",
        )
    if not music_path.exists():
        return Check(name=name, ok=False, detail=f"does not exist: {music_path}")
    if not music_path.is_dir():
        return Check(name=name, ok=False, detail=f"not a directory: {music_path}")

    try:
        audio_count = scan.count_audio_files(music_path)
    except OSError as exc:
        return Check(name=name, ok=False, detail=f"not readable: {music_path} ({exc})")

    return Check(
        name=name,
        ok=True,
        detail=f"{music_path} reachable, {audio_count} audio file(s) found",
    )


def _check_database(db_path: Path) -> Check:
    """Confirm the SQLite ledger can be opened and queried."""
    name = "database"
    try:
        db.check_connection(db_path)
    except (sqlite3.Error, OSError) as exc:
        return Check(name=name, ok=False, detail=f"cannot open ledger at {db_path}: {exc}")
    return Check(name=name, ok=True, detail=f"ledger OK at {db_path}")
