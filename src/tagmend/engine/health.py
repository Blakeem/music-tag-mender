"""Health check / readiness probe (M0).

Verifies that the environment is wired up: settings resolve, the configured music
folder is reachable and readable, and the SQLite ledger can be opened. Surfaced both
as ``tagmend check-health`` (CLI) and the ``check_health`` MCP tool, so the very first
thing we can do — before any feature exists — is confirm we're ready to build.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import httpx

from tagmend.engine import commits, db, scan, schema
from tagmend.engine.lastfm import LastfmClient, LastfmError
from tagmend.engine.musicbrainz import MusicBrainzClient, MusicBrainzError
from tagmend.log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

logger = get_logger(__name__)

# Well-known ping targets — fixed, uncontroversial entities that exercise each authority's
# only read endpoint. The result is discarded; we only care that the round-trip succeeded.
_LASTFM_PING_ARTIST: Final = "Radiohead"
_MB_PING_ARTIST: Final = "Black Sabbath"
_MB_PING_ALBUM: Final = "Paranoid"


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


def check_health(
    settings: Settings,
    *,
    lastfm_transport: httpx.BaseTransport | None = None,
    musicbrainz_transport: httpx.BaseTransport | None = None,
) -> HealthReport:
    """Run every readiness check against *settings* and return the aggregate report.

    The two ``*_transport`` kwargs are test-only injection seams (default: the real httpx
    transport); production callers (CLI, MCP) never pass them, so both authorities are
    pinged live.
    """
    checks = [
        _check_music_path(settings.music_path),
        _check_database(settings.db_path),
        _check_interrupted_commits(settings.db_path),
        _check_lastfm(settings, transport=lastfm_transport),
        _check_musicbrainz(settings, transport=musicbrainz_transport),
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


def _check_interrupted_commits(db_path: Path) -> Check:
    """Report any commit left ``applying`` by a crash. Informational — never fails the report.

    A lingering ``applying`` commit is recovered by simply running ``commit_tags`` again
    (the resume-free model), so this is a hint, not a readiness blocker — it always reports
    ``ok=True``. Real ledger problems are caught by :func:`_check_database`.
    """
    name = "commits"
    try:
        connection = db.connect(db_path)
        try:
            schema.apply_schema(connection)
            interrupted = commits.get_applying_commits(connection)
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        return Check(name=name, ok=True, detail=f"(could not check interrupted runs: {exc})")

    if not interrupted:
        return Check(name=name, ok=True, detail="no interrupted runs")
    ids = ", ".join(str(c.id) for c in interrupted)
    return Check(
        name=name,
        ok=True,
        detail=f"{len(interrupted)} interrupted run(s) ({ids}) — run commit_tags to recover",
    )


def _memory_conn() -> sqlite3.Connection:
    """Return a throwaway in-memory connection with the schema applied.

    Both API clients are unconditionally cache-first, so pinging against the real ledger
    cache would verify nothing after the first run. Building each check's client over a
    fresh, empty cache forces a genuine live round-trip every time (and never touches or
    pollutes the real ledger's API caches).
    """
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    return conn


def _check_lastfm(
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Check:
    """Confirm Last.fm is reachable with the configured key (one live ``getCorrection``).

    A missing key fails immediately with **no HTTP attempted** (genre + artist resolution
    both need it). With a key, one ``artist.getCorrection`` round-trip against an empty
    throwaway cache proves the key + network are live; any client/HTTP error is caught so
    no exception escapes.
    """
    name = "lastfm"

    if not settings.lastfm_api_key:
        return Check(
            name=name,
            ok=False,
            detail="not configured — set lastfm_api_key (needed by resolve_genres/resolve_artists)",
        )

    conn = _memory_conn()
    try:
        with LastfmClient(settings.lastfm_api_key, conn, transport=transport) as client:
            client.artist_correction(_LASTFM_PING_ARTIST)
    except (LastfmError, httpx.HTTPError) as exc:
        return Check(name=name, ok=False, detail=f"unreachable: {exc}")
    finally:
        conn.close()

    return Check(name=name, ok=True, detail="reachable")


def _check_musicbrainz(
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Check:
    """Confirm MusicBrainz is reachable (one live release-group search).

    No API key is required — just the configured ``User-Agent``. One
    ``album_first_release`` round-trip against an empty throwaway cache proves the network
    is live; any client/HTTP error is caught so no exception escapes.
    """
    name = "musicbrainz"

    conn = _memory_conn()
    try:
        with MusicBrainzClient(
            settings.musicbrainz_user_agent,
            conn,
            transport=transport,
            # A readiness probe answers on the first attempt. Retrying here would turn a
            # down service into a multi-second wait.
            max_attempts=1,
        ) as client:
            client.album_first_release(_MB_PING_ARTIST, _MB_PING_ALBUM)
    except (MusicBrainzError, httpx.HTTPError) as exc:
        return Check(name=name, ok=False, detail=f"unreachable: {exc}")
    finally:
        conn.close()

    return Check(name=name, ok=True, detail="reachable")
