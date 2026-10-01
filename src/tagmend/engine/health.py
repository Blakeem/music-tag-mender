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

from tagmend.engine import commits, db, paths, scan, schema
from tagmend.engine.acoustid import (
    AcoustidClient,
    AcoustidError,
    AcoustidKeyError,
    Fingerprint,
    Fingerprinter,
    FpcalcRunner,
    FpcalcUnavailableError,
    run_fpcalc,
)
from tagmend.engine.lastfm import LastfmClient, LastfmError
from tagmend.engine.musicbrainz import MusicBrainzClient, MusicBrainzError
from tagmend.log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

logger = get_logger(__name__)

# Fixed, uncontroversial entities for one live round-trip per authority. The result is
# discarded.
_LASTFM_PING_ARTIST: Final = "Radiohead"
_MB_PING_ARTIST: Final = "Black Sabbath"
_MB_PING_ALBUM: Final = "Paranoid"
# A fingerprint AcoustID cannot decode. It answers "invalid fingerprint" (code 3) only after
# accepting the key, so the probe tests the key without looking anything up.
_ACOUSTID_PROBE: Final = Fingerprint(fingerprint="AQAB", duration=1)
_FPCALC_HINT: Final = (
    "install fpcalc from https://acoustid.org/chromaprint and put it on PATH, or set fpcalc_path"
)


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
    def ready(self) -> bool:
        """``True`` only if every check passed."""
        return all(check.ok for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form, suitable for returning from an MCP tool."""
        return {
            "ready": self.ready,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


def check_health(
    settings: Settings,
    *,
    lastfm_transport: httpx.BaseTransport | None = None,
    musicbrainz_transport: httpx.BaseTransport | None = None,
    acoustid_transport: httpx.BaseTransport | None = None,
    fpcalc_runner: FpcalcRunner = run_fpcalc,
) -> HealthReport:
    """Run every readiness check against *settings* and return the aggregate report.

    The three ``*_transport`` kwargs and *fpcalc_runner* are test-only injection seams
    (default: the real httpx transport and subprocess). Production callers (CLI, MCP) never
    pass them, so every authority is pinged live and fpcalc really runs.
    """
    checks = [
        _check_music_path(settings.music_path),
        _check_database(settings.db_path),
        _check_interrupted_commits(settings.db_path),
        _check_path_staging(settings),
        _check_lastfm(settings, transport=lastfm_transport),
        _check_musicbrainz(settings, transport=musicbrainz_transport),
        _check_fpcalc(settings, runner=fpcalc_runner),
        _check_acoustid(settings, transport=acoustid_transport),
    ]
    report = HealthReport(checks=checks)
    logger.info("health check complete: ready=%s", report.ready)
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
    """Confirm the SQLite ledger opens and accepts this tagmend's schema.

    Applying the schema is the check, because every other tool refuses a ledger it rejects.
    """
    name = "database"
    try:
        connection = db.connect(db_path)
        try:
            schema.apply_schema(connection)
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        return Check(name=name, ok=False, detail=f"cannot open ledger at {db_path}: {exc}")
    except RuntimeError as exc:
        return Check(name=name, ok=False, detail=f"ledger at {db_path} refused: {exc}")
    return Check(name=name, ok=True, detail=f"ledger OK at {db_path}")


def _check_interrupted_commits(db_path: Path) -> Check:
    """Report any commit left ``applying`` by a crash. Informational, it never fails the report.

    A lingering ``applying`` commit is recovered by running ``commit_tags`` or ``commit_paths``
    again (the resume-free model), so this is a hint, not a readiness blocker. It always reports
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
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        return Check(name=name, ok=True, detail=f"(could not check interrupted runs: {exc})")

    if not interrupted:
        return Check(name=name, ok=True, detail="no interrupted runs")
    ids = ", ".join(str(c.id) for c in interrupted)
    return Check(
        name=name,
        ok=True,
        detail=(
            f"{len(interrupted)} interrupted run(s) ({ids}). Run commit_tags or commit_paths to "
            "recover"
        ),
    )


def _check_path_staging(settings: Settings) -> Check:
    """Report the staged moves and sidecar moves, those at their target or gone, and the volume.

    Informational, it never fails the report: the tag tools work on any volume, and a landed
    move is finished by running ``commit_paths``. A staged sidecar row blocks every revert and
    resolver, so it is counted even after its album's audio has committed.
    """
    name = "paths"
    try:
        report = paths.staging_report(settings)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        return Check(name=name, ok=True, detail=f"(could not check staged moves: {exc})")

    parts = [f"{report.staged} staged move(s)", f"{report.sidecars} staged sidecar move(s)"]
    if report.landed:
        parts.append(
            f"file_id(s) {list(report.landed)} already sit at their target. Run commit_paths",
        )
    if report.sidecars_landed:
        parts.append(
            f"sidecar(s) {list(report.sidecars_landed)} already sit at their target. "
            "Run commit_paths",
        )
    if report.gone:
        parts.append(f"file_id(s) {list(report.gone)} are at neither their source nor target")
    parts.append(report.volume_refusal or "the volume check passes")
    return Check(name=name, ok=True, detail=". ".join(parts))


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
        with LastfmClient(
            settings.lastfm_api_key,
            conn,
            transport=transport,
            # A readiness probe answers on the first attempt, like the MusicBrainz check.
            max_attempts=1,
        ) as client:
            client.artist_correction(_LASTFM_PING_ARTIST)
    except LastfmError as exc:
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


def _check_fpcalc(settings: Settings, *, runner: FpcalcRunner = run_fpcalc) -> Check:
    """Confirm fpcalc resolves and runs (``fpcalc -version``), reporting its version string."""
    name = "fpcalc"
    try:
        version = Fingerprinter.from_settings(settings, runner=runner).version()
    except FpcalcUnavailableError as exc:
        return Check(name=name, ok=False, detail=f"{exc}. {_FPCALC_HINT}")
    return Check(name=name, ok=True, detail=version)


def _check_acoustid(
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Check:
    """Confirm AcoustID accepts the configured key (one probe lookup, one attempt).

    A missing key fails with no HTTP attempted. An unreachable AcoustID says nothing about the
    key, so it passes with a warning rather than failing readiness on a network blip.
    """
    name = "acoustid"

    if not settings.acoustid_api_key:
        return Check(
            name=name,
            ok=False,
            detail="not configured: set acoustid_api_key (needed by the song axis)",
        )

    try:
        with AcoustidClient(
            settings.acoustid_api_key,
            transport=transport,
            max_attempts=1,
        ) as client:
            client.lookup(_ACOUSTID_PROBE)
    except AcoustidKeyError as exc:
        return Check(name=name, ok=False, detail=f"key rejected: {exc}")
    except AcoustidError as exc:
        return Check(name=name, ok=True, detail=f"warning: key not verified, {exc}")

    return Check(name=name, ok=True, detail="key accepted")
