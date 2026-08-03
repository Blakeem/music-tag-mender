"""Tests for the health check / readiness probe.

Every ``check_health`` call injects BOTH network transports (MockTransport / the
no-key path): the ``musicbrainz`` check has no key gate, so an un-injected call would
make a live release-group round-trip. No test here touches the real network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from tagmend.config import Settings
from tagmend.engine import commits
from tagmend.engine.db import connect
from tagmend.engine.health import HealthReport, check_health
from tagmend.engine.schema import apply_schema

if TYPE_CHECKING:
    from pathlib import Path

_LASTFM_KEY = "test-key-0123456789abcdef"

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
) -> Settings:
    return Settings(
        music_path=music_path,
        lastfm_api_key=lastfm_api_key,
        db_path=tmp_path / "ledger.sqlite3",
    )


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
    )


def test_passes_for_valid_library(temp_library: Path, tmp_path: Path) -> None:
    report = _run_ok(_settings(temp_library, tmp_path, lastfm_api_key=_LASTFM_KEY))
    assert report.ok
    assert {c.name for c in report.checks} == {
        "music_path",
        "database",
        "commits",
        "lastfm",
        "musicbrainz",
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

    assert report.ok  # informational only — never flips overall readiness
    commits_check = next(c for c in report.checks if c.name == "commits")
    assert "interrupted" in commits_check.detail
    assert commits_check.ok


def test_to_dict_shape(temp_library: Path, tmp_path: Path) -> None:
    data = _run_ok(_settings(temp_library, tmp_path, lastfm_api_key=_LASTFM_KEY)).to_dict()
    assert data["ok"] is True
    assert isinstance(data["checks"], list)


def test_fails_when_music_path_unset(tmp_path: Path) -> None:
    report = _run_ok(_settings(None, tmp_path))
    assert not report.ok


def test_fails_when_music_path_missing(tmp_path: Path) -> None:
    report = _run_ok(_settings(tmp_path / "does-not-exist", tmp_path))
    assert not report.ok


def test_database_check_creates_ledger(temp_library: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "ledger.sqlite3"
    settings = Settings(music_path=temp_library, lastfm_api_key=_LASTFM_KEY, db_path=db_path)
    report = _run_ok(settings)
    assert report.ok
    assert db_path.exists()


# --- lastfm / musicbrainz network checks ---------------------------------------------


def test_lastfm_no_key_fails_without_http(temp_library: Path, tmp_path: Path) -> None:
    lastfm_transport, calls = _fixed_transport(httpx.Response(200, json=_LASTFM_OK_BODY))
    report = check_health(
        _settings(temp_library, tmp_path, lastfm_api_key=None),
        lastfm_transport=lastfm_transport,
        musicbrainz_transport=_ok_mb(),
    )
    lastfm_check = next(c for c in report.checks if c.name == "lastfm")
    assert not lastfm_check.ok
    assert "not configured" in lastfm_check.detail
    assert calls == []  # a missing key attempts no HTTP at all
    assert not report.ok  # a failed lastfm check flips overall readiness


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
    )
    lastfm_check = next(c for c in report.checks if c.name == "lastfm")
    assert not lastfm_check.ok
    assert "unreachable" in lastfm_check.detail
    assert len(calls) == 1  # the round-trip was attempted
    # A failed network check still yields a full five-check report (no exception escapes).
    assert len(report.checks) == 5
    assert not report.ok


def test_musicbrainz_http_error_fails_gracefully(temp_library: Path, tmp_path: Path) -> None:
    error_transport, calls = _fixed_transport(httpx.Response(503, json={}))
    report = check_health(
        _settings(temp_library, tmp_path, lastfm_api_key=_LASTFM_KEY),
        lastfm_transport=_ok_lastfm(),
        musicbrainz_transport=error_transport,
    )
    mb_check = next(c for c in report.checks if c.name == "musicbrainz")
    assert not mb_check.ok
    assert "unreachable" in mb_check.detail
    assert len(calls) == 1
    assert not report.ok


def test_musicbrainz_network_down_fails_gracefully(temp_library: Path, tmp_path: Path) -> None:
    mb_transport, calls = _raising_transport(httpx.ConnectError("network down"))
    report = check_health(
        _settings(temp_library, tmp_path, lastfm_api_key=_LASTFM_KEY),
        lastfm_transport=_ok_lastfm(),
        musicbrainz_transport=mb_transport,
    )
    mb_check = next(c for c in report.checks if c.name == "musicbrainz")
    assert not mb_check.ok
    assert "unreachable" in mb_check.detail
    assert len(calls) == 1
    assert not report.ok
