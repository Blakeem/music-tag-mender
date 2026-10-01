"""Tests for the health check / readiness probe.

Every ``check_health`` call injects every network transport (MockTransport / the no-key
path) and a fake fpcalc runner: the ``musicbrainz`` check has no key gate, so an un-injected
call would make a live release-group round-trip, and an un-injected fpcalc check would run a
real binary. No test here touches the real network or spawns fpcalc.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import httpx
import pytest

from tagmend.config import Settings
from tagmend.engine import acoustid, commits
from tagmend.engine.db import connect
from tagmend.engine.health import HealthReport, check_health
from tagmend.engine.schema import SCHEMA_VERSION, apply_schema

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_LASTFM_KEY = "test-key-0123456789abcdef"
_ACOUSTID_KEY = "acoustid-key-0123456789"
_FPCALC = "fake-fpcalc"
_FPCALC_VERSION = "fpcalc version 1.5.1"

# Canned success bodies for each authority's single read endpoint.
_LASTFM_OK_BODY: dict[str, object] = {
    "corrections": {"correction": {"artist": {"name": "Radiohead", "mbid": "abc"}}},
}
_MB_OK_BODY: dict[str, object] = {
    "release-groups": [
        {
            "id": "rg-1",
            "title": "Paranoid",
            "primary-type": "Album",
            "first-release-date": "1970-09-18",
            "score": 100,
        },
    ],
}


def _settings(
    music_path: Path | None,
    tmp_path: Path,
    *,
    lastfm_api_key: str | None = None,
    acoustid_api_key: str | None = _ACOUSTID_KEY,
    fpcalc_path: str | None = _FPCALC,
) -> Settings:
    return Settings(
        music_path=music_path,
        lastfm_api_key=lastfm_api_key,
        db_path=tmp_path / "ledger.sqlite3",
        acoustid_api_key=acoustid_api_key,
        fpcalc_path=fpcalc_path,
    )


def _ok_fpcalc(argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
    """A fake fpcalc runner that answers ``-version`` like a real install."""
    assert list(argv) == [_FPCALC, "-version"]
    assert timeout == 5.0
    return 0, f"{_FPCALC_VERSION}\n", ""


def _acoustid_error(code: int, message: str) -> httpx.Response:
    return httpx.Response(
        400,
        json={"status": "error", "error": {"code": code, "message": message}},
    )


def _ok_acoustid() -> httpx.MockTransport:
    transport, _ = _fixed_transport(_acoustid_error(3, "invalid fingerprint"))
    return transport


def _fixed_transport(response: httpx.Response) -> tuple[httpx.MockTransport, list[int]]:
    """A MockTransport serving *response* for every request, plus a per-request counter."""
    calls: list[int] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        calls.append(len(calls))
        return response

    return httpx.MockTransport(handle), calls


def _raising_transport(exc: Exception) -> tuple[httpx.MockTransport, list[int]]:
    """A MockTransport whose every request raises *exc*, plus a per-request counter."""
    calls: list[int] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        calls.append(len(calls))
        raise exc

    return httpx.MockTransport(handle), calls


def _ok_lastfm() -> httpx.MockTransport:
    transport, _ = _fixed_transport(httpx.Response(200, json=_LASTFM_OK_BODY))
    return transport


def _ok_mb() -> httpx.MockTransport:
    transport, _ = _fixed_transport(httpx.Response(200, json=_MB_OK_BODY))
    return transport


def _run_ok(settings: Settings) -> HealthReport:
    """Run the health check with success transports for both network authorities."""
    return check_health(
        settings,
        lastfm_transport=_ok_lastfm(),
        musicbrainz_transport=_ok_mb(),
        acoustid_transport=_ok_acoustid(),
        fpcalc_runner=_ok_fpcalc,
    )


def test_passes_for_valid_library(temp_library: Path, tmp_path: Path) -> None:
    report = _run_ok(_settings(temp_library, tmp_path, lastfm_api_key=_LASTFM_KEY))
    assert report.ready
    assert {c.name for c in report.checks} == {
        "music_path",
        "database",
        "commits",
        "paths",
        "lastfm",
        "musicbrainz",
        "fpcalc",
        "acoustid",
    }


def test_interrupted_commit_is_reported_but_not_a_failure(
    temp_library: Path,
    tmp_path: Path,
) -> None:
    settings = _settings(temp_library, tmp_path, lastfm_api_key=_LASTFM_KEY)
    # Leave a commit stuck in 'applying' (a crash remnant).
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        commits.create_commit(conn, origin="manual", message=None, now="2026-06-02T00:00:00+00:00")
        conn.commit()
    finally:
        conn.close()

    report = _run_ok(settings)

    assert report.ready  # informational only, it never flips overall readiness
    commits_check = next(c for c in report.checks if c.name == "commits")
    assert "interrupted" in commits_check.detail
    assert commits_check.ok


def test_to_dict_shape(temp_library: Path, tmp_path: Path) -> None:
    data = _run_ok(_settings(temp_library, tmp_path, lastfm_api_key=_LASTFM_KEY)).to_dict()
    assert data["ready"] is True
    assert "ok" not in data
    assert isinstance(data["checks"], list)


def test_fails_when_music_path_unset(tmp_path: Path) -> None:
    report = _run_ok(_settings(None, tmp_path))
    assert not report.ready


def test_fails_when_music_path_missing(tmp_path: Path) -> None:
    report = _run_ok(_settings(tmp_path / "does-not-exist", tmp_path))
    assert not report.ready


def test_database_check_creates_ledger(temp_library: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "ledger.sqlite3"
    settings = Settings(
        music_path=temp_library,
        lastfm_api_key=_LASTFM_KEY,
        db_path=db_path,
        acoustid_api_key=_ACOUSTID_KEY,
        fpcalc_path=_FPCALC,
    )
    report = _run_ok(settings)
    assert report.ready
    assert db_path.exists()


def test_a_refused_ledger_is_reported_not_raised(temp_library: Path, tmp_path: Path) -> None:
    settings = _settings(temp_library, tmp_path, lastfm_api_key=_LASTFM_KEY)
    conn = connect(settings.db_path)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()

    report = _run_ok(settings)

    database = next(c for c in report.checks if c.name == "database")
    assert not database.ok
    assert "refused" in database.detail
    assert "newer than this tagmend" in database.detail
    assert not report.ready


# --- lastfm / musicbrainz network checks ---------------------------------------------


def test_lastfm_no_key_fails_without_http(temp_library: Path, tmp_path: Path) -> None:
    lastfm_transport, calls = _fixed_transport(httpx.Response(200, json=_LASTFM_OK_BODY))
    report = check_health(
        _settings(temp_library, tmp_path, lastfm_api_key=None),
        lastfm_transport=lastfm_transport,
        musicbrainz_transport=_ok_mb(),
        acoustid_transport=_ok_acoustid(),
        fpcalc_runner=_ok_fpcalc,
    )
    lastfm_check = next(c for c in report.checks if c.name == "lastfm")
    assert not lastfm_check.ok
    assert "not configured" in lastfm_check.detail
    assert calls == []  # a missing key attempts no HTTP at all
    assert not report.ready  # a failed lastfm check flips overall readiness


def test_network_checks_pass_on_success(temp_library: Path, tmp_path: Path) -> None:
    report = _run_ok(_settings(temp_library, tmp_path, lastfm_api_key=_LASTFM_KEY))
    lastfm_check = next(c for c in report.checks if c.name == "lastfm")
    mb_check = next(c for c in report.checks if c.name == "musicbrainz")
    assert lastfm_check.ok
    assert lastfm_check.detail == "reachable"
    assert mb_check.ok
    assert mb_check.detail == "reachable"


def test_lastfm_http_error_fails_gracefully(temp_library: Path, tmp_path: Path) -> None:
    error_transport, calls = _fixed_transport(httpx.Response(500, json={}))
    report = check_health(
        _settings(temp_library, tmp_path, lastfm_api_key=_LASTFM_KEY),
        lastfm_transport=error_transport,
        musicbrainz_transport=_ok_mb(),
        acoustid_transport=_ok_acoustid(),
        fpcalc_runner=_ok_fpcalc,
    )
    lastfm_check = next(c for c in report.checks if c.name == "lastfm")
    assert not lastfm_check.ok
    assert "unreachable" in lastfm_check.detail
    assert len(calls) == 1  # the round-trip was attempted
    # A failed network check still yields a full eight-check report (no exception escapes).
    assert len(report.checks) == 8
    assert not report.ready


def test_musicbrainz_http_error_fails_gracefully(temp_library: Path, tmp_path: Path) -> None:
    error_transport, calls = _fixed_transport(httpx.Response(503, json={}))
    report = check_health(
        _settings(temp_library, tmp_path, lastfm_api_key=_LASTFM_KEY),
        lastfm_transport=_ok_lastfm(),
        musicbrainz_transport=error_transport,
        acoustid_transport=_ok_acoustid(),
        fpcalc_runner=_ok_fpcalc,
    )
    mb_check = next(c for c in report.checks if c.name == "musicbrainz")
    assert not mb_check.ok
    assert "unreachable" in mb_check.detail
    assert len(calls) == 1
    assert not report.ready


def test_musicbrainz_network_down_fails_gracefully(temp_library: Path, tmp_path: Path) -> None:
    mb_transport, calls = _raising_transport(httpx.ConnectError("network down"))
    report = check_health(
        _settings(temp_library, tmp_path, lastfm_api_key=_LASTFM_KEY),
        lastfm_transport=_ok_lastfm(),
        musicbrainz_transport=mb_transport,
        acoustid_transport=_ok_acoustid(),
        fpcalc_runner=_ok_fpcalc,
    )
    mb_check = next(c for c in report.checks if c.name == "musicbrainz")
    assert not mb_check.ok
    assert "unreachable" in mb_check.detail
    assert len(calls) == 1
    assert not report.ready


# --- fpcalc / acoustid checks ----------------------------------------------------------


def _check(report: HealthReport, name: str) -> tuple[bool, str]:
    found = next(c for c in report.checks if c.name == name)
    return found.ok, found.detail


def _run_with(
    settings: Settings,
    *,
    fpcalc_runner: acoustid.FpcalcRunner = _ok_fpcalc,
    acoustid_transport: httpx.MockTransport | None = None,
) -> HealthReport:
    return check_health(
        settings,
        lastfm_transport=_ok_lastfm(),
        musicbrainz_transport=_ok_mb(),
        acoustid_transport=acoustid_transport or _ok_acoustid(),
        fpcalc_runner=fpcalc_runner,
    )


def test_fpcalc_check_reports_the_version(temp_library: Path, tmp_path: Path) -> None:
    ok, detail = _check(_run_ok(_settings(temp_library, tmp_path)), "fpcalc")
    assert ok
    assert detail == _FPCALC_VERSION


def test_fpcalc_check_fails_with_an_install_hint_when_nothing_resolves(
    temp_library: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def runner(argv: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        calls.append(list(argv))
        return 0, "", ""

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    report = _run_with(_settings(temp_library, tmp_path, fpcalc_path=None), fpcalc_runner=runner)

    ok, detail = _check(report, "fpcalc")
    assert not ok
    assert "acoustid.org/chromaprint" in detail
    assert "fpcalc_path" in detail
    assert calls == []
    assert not report.ready


def test_fpcalc_check_uses_fpcalc_on_path_when_no_path_is_set(
    temp_library: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: _FPCALC)
    report = _run_ok(_settings(temp_library, tmp_path, fpcalc_path=None))
    assert _check(report, "fpcalc") == (True, _FPCALC_VERSION)


@pytest.mark.parametrize(
    "failure",
    [OSError("not found"), subprocess.TimeoutExpired([_FPCALC], 5.0)],
)
def test_fpcalc_check_fails_when_fpcalc_cannot_run(
    temp_library: Path,
    tmp_path: Path,
    failure: Exception,
) -> None:
    def runner(_argv: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        raise failure

    report = _run_with(_settings(temp_library, tmp_path), fpcalc_runner=runner)
    ok, detail = _check(report, "fpcalc")
    assert not ok
    assert "acoustid.org/chromaprint" in detail


def test_fpcalc_check_fails_on_a_nonzero_exit(temp_library: Path, tmp_path: Path) -> None:
    def runner(_argv: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        return 2, "", "broken"

    report = _run_with(_settings(temp_library, tmp_path), fpcalc_runner=runner)
    ok, detail = _check(report, "fpcalc")
    assert not ok
    assert "exited 2" in detail


def test_acoustid_check_without_a_key_fails_without_http(
    temp_library: Path,
    tmp_path: Path,
) -> None:
    transport, calls = _fixed_transport(_acoustid_error(3, "invalid fingerprint"))
    report = _run_with(
        _settings(temp_library, tmp_path, acoustid_api_key=None),
        acoustid_transport=transport,
    )
    ok, detail = _check(report, "acoustid")
    assert not ok
    assert "acoustid_api_key" in detail
    assert calls == []
    assert not report.ready


def test_acoustid_check_passes_when_the_probe_reports_an_invalid_fingerprint(
    temp_library: Path,
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _acoustid_error(3, "invalid fingerprint")

    report = _run_with(
        _settings(temp_library, tmp_path, lastfm_api_key=_LASTFM_KEY),
        acoustid_transport=httpx.MockTransport(handle),
    )
    assert _check(report, "acoustid") == (True, "key accepted")
    assert len(seen) == 1
    assert report.ready


def test_acoustid_check_fails_when_the_key_is_rejected(temp_library: Path, tmp_path: Path) -> None:
    transport, _ = _fixed_transport(_acoustid_error(4, "invalid API key"))
    report = _run_with(_settings(temp_library, tmp_path), acoustid_transport=transport)
    ok, detail = _check(report, "acoustid")
    assert not ok
    assert "acoustid_api_key" in detail
    assert _ACOUSTID_KEY not in detail


def test_acoustid_check_only_warns_when_acoustid_is_unreachable(
    temp_library: Path,
    tmp_path: Path,
) -> None:
    transport, calls = _raising_transport(httpx.ConnectError("network down"))
    report = _run_with(_settings(temp_library, tmp_path), acoustid_transport=transport)
    ok, detail = _check(report, "acoustid")
    assert ok
    assert detail.startswith("warning:")
    assert len(calls) == 1  # one attempt, no retry


def test_acoustid_check_makes_one_attempt_on_a_server_error(
    temp_library: Path,
    tmp_path: Path,
) -> None:
    transport, calls = _fixed_transport(httpx.Response(503, text="busy"))
    report = _run_with(_settings(temp_library, tmp_path), acoustid_transport=transport)
    ok, detail = _check(report, "acoustid")
    assert ok
    assert "warning" in detail
    assert len(calls) == 1
