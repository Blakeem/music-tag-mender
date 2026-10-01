"""Unit tests for the audio-identification layer (``engine/acoustid.py``).

fpcalc never runs: every :class:`Fingerprinter` gets a fake runner. AcoustID is faked with
:class:`httpx.MockTransport`, the clock and sleep are injected, and the caches live in the
in-memory ``db_conn`` ledger. Nothing here reaches the network or spawns a process.
"""

from __future__ import annotations

import gzip
import logging
import shutil
import subprocess
import sys
import urllib.parse
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from tagmend import __version__
from tagmend.config import PROJECT_URL, Settings
from tagmend.engine import acoustid
from tagmend.engine.acoustid import (
    AcoustidArtist,
    AcoustidClient,
    AcoustidError,
    AcoustidKeyError,
    AcoustidMatch,
    AcoustidRecording,
    AcoustidReleaseRef,
    AcoustidResult,
    Fingerprint,
    Fingerprinter,
    FingerprintError,
    FingerprintTimeout,
    FingerprintUnreadableError,
    FpcalcUnavailableError,
    get_fingerprint,
    get_lookup,
    put_fingerprint,
    put_lookup,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator, Sequence

_KEY = "acoustid-secret-key-123"
_FPCALC = "fake-fpcalc"
_FP = Fingerprint(fingerprint="AQADtEmUaEkS", duration=243)
_NOW = datetime(2026, 9, 1, tzinfo=UTC)
_META = "recordings releases tracks compress sources"


# --- Fingerprinter ---------------------------------------------------------------------


class FakeRunner:
    """A fake fpcalc runner that records each call and answers with a fixed outcome."""

    def __init__(self, outcome: tuple[int, str, str] | BaseException) -> None:
        self._outcome = outcome
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
        self.calls.append((list(argv), timeout))
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


_FPCALC_JSON = '{"duration": 243.47, "fingerprint": "AQADtEmUaEkS"}'


@pytest.mark.parametrize("exit_code", [0, 3])
def test_a_usable_exit_parses_to_a_fingerprint(tmp_path: Path, exit_code: int) -> None:
    runner = FakeRunner((exit_code, _FPCALC_JSON, "decode warning"))
    track = tmp_path / "a.mp3"

    result = Fingerprinter(_FPCALC, runner=runner, is_windows=False).fingerprint(track)

    assert result == Fingerprint(fingerprint="AQADtEmUaEkS", duration=243)
    assert runner.calls == [([_FPCALC, "-json", "-length", "120", str(track.absolute())], 60.0)]


def test_another_exit_raises_a_fingerprint_error_carrying_the_code(tmp_path: Path) -> None:
    runner = FakeRunner((2, "", "ERROR: Invalid data found when processing input"))
    track = tmp_path / "a.mp3"
    track.write_bytes(b"x")

    with pytest.raises(FingerprintError) as caught:
        Fingerprinter(_FPCALC, runner=runner).fingerprint(track)

    assert caught.value.exit_code == 2
    assert not isinstance(caught.value, FingerprintTimeout)
    assert not isinstance(caught.value, FingerprintUnreadableError)


def test_a_failed_exit_on_a_file_that_cannot_be_opened_is_the_transient_subclass(
    tmp_path: Path,
) -> None:
    # fpcalc exits 2 for a missing file too. A file gone since the scan must not be stored as bad.
    runner = FakeRunner((2, "", "ERROR: Could not open the input file"))
    missing = tmp_path / "gone.mp3"

    with pytest.raises(FingerprintUnreadableError) as caught:
        Fingerprinter(_FPCALC, runner=runner).fingerprint(missing)

    assert caught.value.exit_code == 2
    assert "Rescan the library" in str(caught.value)


def test_a_timeout_raises_the_transient_subclass(tmp_path: Path) -> None:
    runner = FakeRunner(subprocess.TimeoutExpired([_FPCALC], 60.0))

    with pytest.raises(FingerprintTimeout) as caught:
        Fingerprinter(_FPCALC, runner=runner).fingerprint(tmp_path / "a.mp3")

    assert isinstance(caught.value, FingerprintError)
    assert caught.value.exit_code is None


def test_an_executable_that_cannot_start_is_not_a_file_failure(tmp_path: Path) -> None:
    runner = FakeRunner(FileNotFoundError("no such file"))

    with pytest.raises(FpcalcUnavailableError):
        Fingerprinter(_FPCALC, runner=runner).fingerprint(tmp_path / "a.mp3")


@pytest.mark.parametrize("stdout", ["not json", "[]", '{"fingerprint": "AQAD"}', '{"duration": 1}'])
def test_unusable_output_raises_a_fingerprint_error(tmp_path: Path, stdout: str) -> None:
    runner = FakeRunner((0, stdout, ""))

    with pytest.raises(FingerprintError) as caught:
        Fingerprinter(_FPCALC, runner=runner).fingerprint(tmp_path / "a.mp3")

    assert caught.value.exit_code == 0


def test_a_long_path_gets_the_extended_prefix_on_windows_only(tmp_path: Path) -> None:
    track = tmp_path / ("x" * 250 + ".mp3")
    on_windows = FakeRunner((0, _FPCALC_JSON, ""))
    elsewhere = FakeRunner((0, _FPCALC_JSON, ""))

    Fingerprinter(_FPCALC, runner=on_windows, is_windows=True).fingerprint(track)
    Fingerprinter(_FPCALC, runner=elsewhere, is_windows=False).fingerprint(track)

    assert on_windows.calls[0][0][-1] == "\\\\?\\" + str(track.absolute())
    assert elsewhere.calls[0][0][-1] == str(track.absolute())


def test_a_short_path_is_never_prefixed(tmp_path: Path) -> None:
    runner = FakeRunner((0, _FPCALC_JSON, ""))
    Fingerprinter(_FPCALC, runner=runner, is_windows=True).fingerprint(tmp_path / "a.mp3")
    assert not runner.calls[0][0][-1].startswith("\\\\?\\")


@pytest.mark.skipif(sys.platform != "win32", reason="a UNC path only parses on Windows")
def test_a_long_unc_path_gets_the_unc_form_of_the_prefix() -> None:
    track = Path("//server/share/" + "x" * 250 + ".mp3")
    runner = FakeRunner((0, _FPCALC_JSON, ""))

    Fingerprinter(_FPCALC, runner=runner, is_windows=True).fingerprint(track)

    assert runner.calls[0][0][-1] == "\\\\?\\UNC\\server\\share\\" + "x" * 250 + ".mp3"


def test_the_configured_path_wins_over_path_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "on-path")
    assert acoustid.resolve_fpcalc(" C:/tools/fpcalc.exe ") == "C:/tools/fpcalc.exe"
    assert acoustid.resolve_fpcalc("") == "on-path"
    assert acoustid.resolve_fpcalc(None) == "on-path"


def test_from_settings_refuses_when_nothing_resolves(
    engine_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(FpcalcUnavailableError, match="fpcalc_path"):
        Fingerprinter.from_settings(engine_settings)


# --- AcoustidClient --------------------------------------------------------------------


def _release_payload() -> dict[str, object]:
    """One compressed release entry holding the recording on its third track."""
    return {
        "id": "rel-1",
        "title": "In the Skin",
        "country": "US",
        "date": {"year": 1997, "month": 6, "day": 3},
        "medium_count": 2,
        "track_count": 24,
        "mediums": [
            {
                "position": 1,
                "format": "CD",
                "track_count": 12,
                "tracks": [{"id": "track-3", "position": 3}],
            },
        ],
    }


_OK_BODY: dict[str, object] = {
    "status": "ok",
    "results": [
        {
            "id": "acoustid-1",
            "score": 0.97,
            "recordings": [
                {
                    "id": "rec-1",
                    "title": "Enemy Throttle",
                    "duration": 244,
                    "sources": 12,
                    "releases": [
                        _release_payload(),
                        {"id": "rel-2", "date": {"year": 2001}},
                    ],
                    "artists": [
                        {"id": "art-1", "name": "Swingin' Utters", "joinphrase": " feat. "},
                        {"id": "art-2", "name": "Mike Ness"},
                    ],
                },
                {"id": "rec-2", "sources": 1},
            ],
        },
    ],
}

_EXPECTED_RESULT = AcoustidResult(
    results=(
        AcoustidMatch(
            score=0.97,
            recordings=(
                AcoustidRecording(
                    id="rec-1",
                    title="Enemy Throttle",
                    duration=244,
                    sources=12,
                    releases=(
                        AcoustidReleaseRef(
                            id="rel-1",
                            title="In the Skin",
                            country="US",
                            date="1997-06-03",
                            medium_position=1,
                            medium_track_count=12,
                            track_position=3,
                            track_id="track-3",
                            release_track_count=24,
                            medium_count=2,
                        ),
                        AcoustidReleaseRef(
                            id="rel-2",
                            title=None,
                            country=None,
                            date="2001",
                            medium_position=None,
                            medium_track_count=None,
                            track_position=None,
                            track_id=None,
                            release_track_count=None,
                            medium_count=None,
                        ),
                    ),
                    artists=(
                        AcoustidArtist(id="art-1", name="Swingin' Utters", joinphrase=" feat. "),
                        AcoustidArtist(id="art-2", name="Mike Ness", joinphrase=""),
                    ),
                ),
                AcoustidRecording(
                    id="rec-2",
                    title="",
                    duration=None,
                    sources=1,
                    releases=(),
                    artists=(),
                ),
            ),
        ),
    ),
)


def _error_body(code: int, message: str) -> dict[str, object]:
    return {"status": "error", "error": {"code": code, "message": message}}


def _client(
    responses: list[httpx.Response | Exception],
    *,
    rate_per_sec: float = 0.0,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[AcoustidClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        outcome = responses[len(seen) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client = AcoustidClient(
        _KEY,
        rate_per_sec=rate_per_sec,
        transport=httpx.MockTransport(handle),
        monotonic=monotonic or (lambda: 0.0),
        sleep=sleep or (lambda _seconds: None),
    )
    return client, seen


def test_the_lookup_is_a_gzip_form_post_to_the_https_endpoint() -> None:
    client, seen = _client([httpx.Response(200, json=_OK_BODY)])
    with client:
        client.lookup(_FP)

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.acoustid.org/v2/lookup"
    assert request.headers["Content-Encoding"] == "gzip"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert request.headers["User-Agent"] == f"TagMend/{__version__} ( {PROJECT_URL} )"
    form = urllib.parse.parse_qs(gzip.decompress(request.content).decode("ascii"))
    assert form == {
        "client": [_KEY],
        "format": ["json"],
        "duration": ["243"],
        "fingerprint": [_FP.fingerprint],
        "meta": [_META],
    }


def test_the_compressed_response_parses_into_the_dataclasses() -> None:
    client, _ = _client([httpx.Response(200, json=_OK_BODY)])
    with client:
        result = client.lookup(_FP)

    assert result == _EXPECTED_RESULT


def test_an_artist_credit_parses_in_order_and_a_missing_one_reads_empty() -> None:
    client, _ = _client([httpx.Response(200, json=_OK_BODY)])
    with client:
        credited, uncredited = client.lookup(_FP).results[0].recordings

    assert [(a.id, a.name, a.joinphrase) for a in credited.artists] == [
        ("art-1", "Swingin' Utters", " feat. "),
        ("art-2", "Mike Ness", ""),
    ]
    assert uncredited.artists == ()


def test_a_medium_holding_the_recording_twice_yields_one_ref_per_track() -> None:
    release = _release_payload()
    release["mediums"] = [
        {"position": 1, "track_count": 2, "tracks": [{"id": "t1", "position": 1}]},
        {"position": 2, "track_count": 2, "tracks": [{"id": "t2", "position": 2}]},
    ]
    body = {
        "status": "ok",
        "results": [{"score": 1, "recordings": [{"id": "r", "releases": [release]}]}],
    }
    client, _ = _client([httpx.Response(200, json=body)])
    with client:
        refs = client.lookup(_FP).results[0].recordings[0].releases

    assert [(r.medium_position, r.track_position, r.track_id) for r in refs] == [
        (1, 1, "t1"),
        (2, 2, "t2"),
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"status": "ok", "results": {"not": "a list"}},
        {"status": "ok", "results": [{"score": "high"}]},
        {"status": "ok", "results": [{"recordings": [{"id": "r", "title": 7}]}]},
        {
            "status": "ok",
            "results": [{"recordings": [{"id": "r", "releases": [{"id": "x", "date": "1997"}]}]}],
        },
        {"status": "ok", "results": [{"recordings": [{"id": "r", "artists": "Moby"}]}]},
        {"status": "ok", "results": [{"recordings": [{"id": "r", "artists": [{"name": 7}]}]}]},
    ],
)
def test_a_wrong_type_raises(body: dict[str, object]) -> None:
    client, _ = _client([httpx.Response(200, json=body)])
    with client, pytest.raises(AcoustidError):
        client.lookup(_FP)


def test_a_429_is_retried_after_a_backoff() -> None:
    slept: list[float] = []
    client, seen = _client(
        [httpx.Response(429, text="slow down"), httpx.Response(200, json=_OK_BODY)],
        sleep=slept.append,
    )
    with client:
        result = client.lookup(_FP)

    assert result == _EXPECTED_RESULT
    assert len(seen) == 2
    assert slept == [1.0]


def test_three_server_errors_raise_after_a_doubling_backoff() -> None:
    slept: list[float] = []
    client, seen = _client([httpx.Response(503, text="busy")] * 3, sleep=slept.append)
    with client, pytest.raises(AcoustidError, match="HTTP 503"):
        client.lookup(_FP)

    assert len(seen) == 3
    assert slept == [1.0, 2.0]


def test_a_transport_failure_that_never_clears_raises() -> None:
    client, seen = _client([httpx.ConnectError("down")] * 3)
    with client, pytest.raises(AcoustidError, match="transport failure"):
        client.lookup(_FP)

    assert len(seen) == 3


def test_an_invalid_fingerprint_is_an_empty_result() -> None:
    client, _ = _client([httpx.Response(400, json=_error_body(3, "invalid fingerprint"))])
    with client:
        assert client.lookup(_FP) == AcoustidResult()


def test_an_invalid_key_names_the_setting_and_never_the_key() -> None:
    client, seen = _client([httpx.Response(400, json=_error_body(4, "invalid API key"))])
    with client, pytest.raises(AcoustidKeyError) as caught:
        client.lookup(_FP)

    assert "acoustid_api_key" in str(caught.value)
    assert _KEY not in str(caught.value)
    assert len(seen) == 1  # a rejected key is a verdict, not a transient failure


def test_another_error_code_raises() -> None:
    client, _ = _client([httpx.Response(400, json=_error_body(2, "missing parameter"))])
    with client, pytest.raises(AcoustidError, match="missing parameter"):
        client.lookup(_FP)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>proxy</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
)
def test_a_body_that_is_not_a_json_object_raises(response: httpx.Response) -> None:
    client, _ = _client([response])
    with client, pytest.raises(AcoustidError):
        client.lookup(_FP)


def test_pacing_sleeps_between_back_to_back_lookups() -> None:
    clock = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    client, _ = _client(
        [httpx.Response(200, json=_OK_BODY), httpx.Response(200, json=_OK_BODY)],
        rate_per_sec=2.0,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    with client:
        client.lookup(_FP)
        client.lookup(_FP)

    assert slept == [0.5]


@pytest.fixture
def every_log_record(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Capture every record at DEBUG, the ``tagmend`` tree included despite its propagate=False."""
    tagmend_logger = logging.getLogger("tagmend")
    tagmend_logger.addHandler(caplog.handler)
    try:
        with (
            caplog.at_level(logging.DEBUG),
            caplog.at_level(logging.DEBUG, logger="tagmend"),
            caplog.at_level(logging.DEBUG, logger="httpx"),
        ):
            yield caplog
    finally:
        tagmend_logger.removeHandler(caplog.handler)


def test_no_log_record_carries_the_key(every_log_record: pytest.LogCaptureFixture) -> None:
    client, _ = _client(
        [
            httpx.ConnectError("down"),
            httpx.Response(429, text="slow down"),
            httpx.Response(200, json=_OK_BODY),
            httpx.Response(400, json=_error_body(4, "invalid API key")),
        ],
    )
    with client:
        client.lookup(_FP)
        with pytest.raises(AcoustidKeyError):
            client.lookup(_FP)

    assert every_log_record.records  # the capture really saw the client's logging
    for record in every_log_record.records:
        assert _KEY not in record.getMessage()


def test_from_settings_refuses_without_a_key(engine_settings: Settings) -> None:
    with pytest.raises(ValueError, match="acoustid_api_key"):
        AcoustidClient.from_settings(engine_settings)


# --- fingerprint_cache -----------------------------------------------------------------


def _file_id(conn: sqlite3.Connection, filename: str = "a.mp3") -> int:
    cursor = conn.execute(
        """
        INSERT INTO files (folder, filename, ext, first_seen_at, updated_at)
        VALUES ('/lib', ?, '.mp3', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')
        """,
        (filename,),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def test_a_fingerprint_row_is_returned_only_at_an_equal_signature(
    db_conn: sqlite3.Connection,
) -> None:
    file_id = _file_id(db_conn)
    other = _file_id(db_conn, "b.mp3")
    put_fingerprint(db_conn, file_id, 10, 20, fpcalc_exit=3, fingerprint=_FP, now=_NOW)

    stored = get_fingerprint(db_conn, file_id, 10, 20)

    assert stored is not None
    assert stored.fpcalc_exit == 3
    assert stored.fingerprint == _FP
    assert stored.fingerprinted_at == _NOW.isoformat()
    assert get_fingerprint(db_conn, file_id, 11, 20) is None
    assert get_fingerprint(db_conn, file_id, 10, 21) is None
    assert get_fingerprint(db_conn, other, 10, 20) is None


def test_a_stored_failure_round_trips_with_a_null_fingerprint(db_conn: sqlite3.Connection) -> None:
    file_id = _file_id(db_conn)
    put_fingerprint(db_conn, file_id, 10, 20, fpcalc_exit=2, fingerprint=None, now=_NOW)

    stored = get_fingerprint(db_conn, file_id, 10, 20)

    assert stored is not None
    assert stored.fpcalc_exit == 2
    assert stored.fingerprint is None
    raw = db_conn.execute(
        "SELECT fingerprint, duration FROM fingerprint_cache WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    assert raw == (None, None)


def test_a_new_signature_replaces_the_old_row(db_conn: sqlite3.Connection) -> None:
    file_id = _file_id(db_conn)
    put_fingerprint(db_conn, file_id, 10, 20, fpcalc_exit=2, fingerprint=None, now=_NOW)
    put_fingerprint(db_conn, file_id, 11, 30, fpcalc_exit=0, fingerprint=_FP, now=_NOW)

    assert get_fingerprint(db_conn, file_id, 10, 20) is None
    replaced = get_fingerprint(db_conn, file_id, 11, 30)
    assert replaced is not None
    assert replaced.fingerprint == _FP


# --- acoustid_cache --------------------------------------------------------------------


def test_a_lookup_round_trips_byte_exact_through_zlib(db_conn: sqlite3.Connection) -> None:
    put_lookup(db_conn, _FP, _EXPECTED_RESULT, _NOW)

    found, payload = db_conn.execute("SELECT found, payload FROM acoustid_cache").fetchone()

    assert found == 1
    assert zlib.decompress(payload) == acoustid._result_to_json(_EXPECTED_RESULT).encode("utf-8")
    assert get_lookup(db_conn, _FP, _NOW + timedelta(days=365)) == _EXPECTED_RESULT


def test_a_cached_lookup_keeps_its_artist_credit(db_conn: sqlite3.Connection) -> None:
    put_lookup(db_conn, _FP, _EXPECTED_RESULT, _NOW)

    cached = get_lookup(db_conn, _FP, _NOW)

    assert cached is not None
    credited, uncredited = cached.results[0].recordings
    assert credited.artists == _EXPECTED_RESULT.results[0].recordings[0].artists
    assert uncredited.artists == ()


def test_an_empty_lookup_is_served_at_six_days_and_missed_at_eight(
    db_conn: sqlite3.Connection,
) -> None:
    put_lookup(db_conn, _FP, AcoustidResult(), _NOW)

    found, payload = db_conn.execute("SELECT found, payload FROM acoustid_cache").fetchone()
    assert (found, payload) == (0, None)
    assert get_lookup(db_conn, _FP, _NOW + timedelta(days=6)) == AcoustidResult()
    assert get_lookup(db_conn, _FP, _NOW + timedelta(days=8)) is None


def test_a_lookup_for_another_fingerprint_or_duration_misses(db_conn: sqlite3.Connection) -> None:
    put_lookup(db_conn, _FP, _EXPECTED_RESULT, _NOW)

    assert get_lookup(db_conn, Fingerprint(_FP.fingerprint, 244), _NOW) is None
    assert get_lookup(db_conn, Fingerprint("AQADother", _FP.duration), _NOW) is None


@pytest.mark.parametrize(
    ("constant", "value"),
    [("_META", "recordings releases tracks sources"), ("_LOOKUP_VERSION", "next")],
)
def test_a_key_built_from_another_meta_or_version_misses(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    value: str,
) -> None:
    put_lookup(db_conn, _FP, _EXPECTED_RESULT, _NOW)

    with monkeypatch.context() as patch:
        patch.setattr(acoustid, constant, value)
        assert get_lookup(db_conn, _FP, _NOW) is None

    assert get_lookup(db_conn, _FP, _NOW) == _EXPECTED_RESULT


def test_a_row_cached_before_the_artist_credit_was_parsed_misses(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(acoustid, "_LOOKUP_VERSION", "1")
        put_lookup(db_conn, _FP, _EXPECTED_RESULT, _NOW)

    assert get_lookup(db_conn, _FP, _NOW) is None


def test_an_unreadable_payload_is_a_miss(db_conn: sqlite3.Connection) -> None:
    put_lookup(db_conn, _FP, _EXPECTED_RESULT, _NOW)
    db_conn.execute("UPDATE acoustid_cache SET payload = ?", (b"not zlib",))

    assert get_lookup(db_conn, _FP, _NOW) is None
