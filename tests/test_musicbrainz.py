"""Unit tests for the MusicBrainz client (``engine/musicbrainz.py``).

All network traffic is faked with :class:`httpx.MockTransport`; the cache lives in the
in-memory ``db_conn`` fixture (real schema). The clock/sleep are injected so pacing is
asserted without any real waiting. These never hit the live MusicBrainz API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from tagmend.engine.musicbrainz import (
    MusicBrainzClient,
    MusicBrainzError,
    _recording_request_key,
    _request_key,
)
from tagmend.engine.store import get_cached_mb_album, get_cached_mb_recording

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping


def _group(**overrides: object) -> dict[str, object]:
    """Build one release-group entry as the MB JSON search response shapes it.

    Defaults describe a clean Album hit (*Paranoid*, 1970, score 100, one release);
    pass keyword overrides (e.g. ``primary_type="Single"``, ``secondary_types=["Live"]``,
    ``releases=None``) to vary it.
    """
    entry: dict[str, object] = {
        "id": "rg-1",
        "title": "Paranoid",
        "primary-type": "Album",
        "first-release-date": "1970-09-18",
        "score": 100,
        "releases": [{"id": "rel-1"}],
    }
    # Normalize Python-friendly override keys to the MB JSON spellings.
    rename = {
        "primary_type": "primary-type",
        "secondary_types": "secondary-types",
        "first_release_date": "first-release-date",
        "rgid": "id",
    }
    for key, value in overrides.items():
        mb_key = rename.get(key, key)
        if value is None:
            entry.pop(mb_key, None)
        else:
            entry[mb_key] = value
    return entry


def _body(*groups: dict[str, object]) -> dict[str, object]:
    """Wrap *groups* in the release-group search envelope."""
    return {"release-groups": list(groups)}


def _handler(
    responses: list[httpx.Response],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[int]]:
    calls: list[int] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        index = len(calls)
        calls.append(index)
        return responses[index]

    return handle, calls


def _json_response(body: Mapping[str, object], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=dict(body))


def _client(
    db_conn: sqlite3.Connection,
    responses: list[httpx.Response],
    *,
    rate_per_sec: float = 0.0,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[MusicBrainzClient, list[int]]:
    handle, calls = _handler(responses)
    transport = httpx.MockTransport(handle)
    kwargs: dict[str, object] = {"rate_per_sec": rate_per_sec, "transport": transport}
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    if sleep is not None:
        kwargs["sleep"] = sleep
    client = MusicBrainzClient("TagMend/test ( test@example.com )", db_conn, **kwargs)  # type: ignore[arg-type]
    return client, calls


# --- found / selection ---------------------------------------------------------------


def test_returns_album_original_year(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_body(_group()))])
    with client:
        album = client.album_first_release("Black Sabbath", "Paranoid")

    assert album is not None
    assert album.original_date == "1970"  # normalized to the four-digit year
    assert album.album_title == "Paranoid"
    assert album.release_group_id == "rg-1"
    assert album.release_mbid == "rel-1"
    assert len(calls) == 1


def test_picks_highest_scoring_album(db_conn: sqlite3.Connection) -> None:
    body = _body(
        _group(title="Low score", first_release_date="1999", score=40, rgid="rg-lo"),
        _group(title="High score", first_release_date="1970", score=100, rgid="rg-hi"),
    )
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        album = client.album_first_release("Artist", "Album")
    assert album is not None
    assert album.original_date == "1970"
    assert album.release_group_id == "rg-hi"


def test_skips_non_album_primary_type(db_conn: sqlite3.Connection) -> None:
    body = _body(_group(primary_type="Single", first_release_date="1971"))
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        assert client.album_first_release("Artist", "Album") is None


def test_skips_live_and_compilation_secondary_types(db_conn: sqlite3.Connection) -> None:
    body = _body(
        _group(title="Live one", secondary_types=["Live"], first_release_date="1975"),
        _group(title="Comp", secondary_types=["Compilation"], first_release_date="1980"),
    )
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        assert client.album_first_release("Artist", "Album") is None


def test_empty_results_is_no_match(db_conn: sqlite3.Connection) -> None:
    client, _ = _client(db_conn, [_json_response(_body())])
    with client:
        assert client.album_first_release("Nobody", "Nothing") is None


# --- caching -------------------------------------------------------------------------


def test_found_result_is_positive_cached(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_body(_group()))])
    with client:
        client.album_first_release("Black Sabbath", "Paranoid")
        # Second call is served from cache — no second network request.
        again = client.album_first_release("Black Sabbath", "Paranoid")
    assert again is not None
    assert again.original_date == "1970"
    assert len(calls) == 1

    cached = get_cached_mb_album(db_conn, _request_key("Black Sabbath", "Paranoid"))
    assert cached is not None
    assert cached[0] is True


def test_no_match_is_negative_cached(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_body())])
    with client:
        client.album_first_release("Nobody", "Nothing")
        client.album_first_release("Nobody", "Nothing")
    assert len(calls) == 1  # the negative result is cached, not re-fetched
    cached = get_cached_mb_album(db_conn, _request_key("Nobody", "Nothing"))
    assert cached is not None
    assert cached[0] is False


# --- transient errors ----------------------------------------------------------------


def test_http_error_raises_and_caches_nothing(db_conn: sqlite3.Connection) -> None:
    client, _ = _client(db_conn, [_json_response({}, status_code=503)])
    with client, pytest.raises(MusicBrainzError):
        client.album_first_release("Artist", "Album")
    # Nothing cached → a re-run would retry.
    assert get_cached_mb_album(db_conn, _request_key("Artist", "Album")) is None


# --- pacing --------------------------------------------------------------------------


def test_pacing_sleeps_between_network_requests(db_conn: sqlite3.Connection) -> None:
    clock = [0.0]
    slept: list[float] = []

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    client, _ = _client(
        db_conn,
        [_json_response(_body(_group())), _json_response(_body())],
        rate_per_sec=1.0,
        monotonic=monotonic,
        sleep=sleep,
    )
    with client:
        client.album_first_release("A", "One")
        client.album_first_release("B", "Two")

    # The second request was paced to honor 1 req/sec.
    assert slept == [1.0]


# --- recording search (the review-only album-gaps tier) ------------------------------


def _rec_group(**overrides: object) -> dict[str, object]:
    """Build one nested release-group as the recording response shapes it (Album by default)."""
    group: dict[str, object] = {"id": "rg-1", "title": "Paranoid", "primary-type": "Album"}
    rename = {"primary_type": "primary-type", "secondary_types": "secondary-types", "rgid": "id"}
    for key, value in overrides.items():
        mb_key = rename.get(key, key)
        if value is None:
            group.pop(mb_key, None)
        else:
            group[mb_key] = value
    return group


def _recording(group: dict[str, object] | None = None, **overrides: object) -> dict[str, object]:
    """Build one recording entry wrapping *group* in a single release (score 100 by default)."""
    entry: dict[str, object] = {
        "id": "rec-1",
        "title": "War Pigs",
        "score": 100,
        "releases": [{"id": "rel-1", "release-group": _rec_group() if group is None else group}],
    }
    rename = {"rec_id": "id"}
    for key, value in overrides.items():
        mb_key = rename.get(key, key)
        if value is None:
            entry.pop(mb_key, None)
        else:
            entry[mb_key] = value
    return entry


def _recording_body(*recordings: dict[str, object]) -> dict[str, object]:
    """Wrap *recordings* in the recording-search envelope."""
    return {"recordings": list(recordings)}


def test_recording_search_returns_album(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_recording_body(_recording()))])
    with client:
        recording = client.recording_search("Black Sabbath", "War Pigs")

    assert recording is not None
    assert recording.album_title == "Paranoid"
    assert recording.release_group_id == "rg-1"
    assert recording.recording_mbid == "rec-1"
    assert len(calls) == 1


def test_recording_search_picks_highest_scoring(db_conn: sqlite3.Connection) -> None:
    body = _recording_body(
        _recording(_rec_group(title="Low", rgid="rg-lo"), rec_id="rec-lo", score=40),
        _recording(_rec_group(title="High", rgid="rg-hi"), rec_id="rec-hi", score=100),
    )
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        recording = client.recording_search("Artist", "Title")
    assert recording is not None
    assert recording.album_title == "High"
    assert recording.release_group_id == "rg-hi"
    assert recording.recording_mbid == "rec-hi"


def test_recording_search_skips_non_album_release_group(db_conn: sqlite3.Connection) -> None:
    body = _recording_body(_recording(_rec_group(primary_type="Single")))
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        assert client.recording_search("Artist", "Title") is None


def test_recording_search_skips_live_and_compilation(db_conn: sqlite3.Connection) -> None:
    body = _recording_body(
        _recording(_rec_group(title="Live", secondary_types=["Live"]), rec_id="rec-live"),
        _recording(_rec_group(title="Comp", secondary_types=["Compilation"]), rec_id="rec-comp"),
    )
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        assert client.recording_search("Artist", "Title") is None


def test_recording_search_empty_is_no_match(db_conn: sqlite3.Connection) -> None:
    client, _ = _client(db_conn, [_json_response(_recording_body())])
    with client:
        assert client.recording_search("Nobody", "Nothing") is None


def test_recording_found_is_positive_cached(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_recording_body(_recording()))])
    with client:
        client.recording_search("Black Sabbath", "War Pigs")
        # Second call is served from cache — no second network request.
        again = client.recording_search("Black Sabbath", "War Pigs")
    assert again is not None
    assert again.album_title == "Paranoid"
    assert len(calls) == 1

    cached = get_cached_mb_recording(db_conn, _recording_request_key("Black Sabbath", "War Pigs"))
    assert cached is not None
    assert cached[0] is True


def test_recording_no_match_is_negative_cached(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_recording_body())])
    with client:
        client.recording_search("Nobody", "Nothing")
        client.recording_search("Nobody", "Nothing")
    assert len(calls) == 1  # the negative result is cached, not re-fetched
    cached = get_cached_mb_recording(db_conn, _recording_request_key("Nobody", "Nothing"))
    assert cached is not None
    assert cached[0] is False


def test_recording_http_error_raises_and_caches_nothing(db_conn: sqlite3.Connection) -> None:
    client, _ = _client(db_conn, [_json_response({}, status_code=503)])
    with client, pytest.raises(MusicBrainzError):
        client.recording_search("Artist", "Title")
    # Nothing cached → a re-run would retry.
    assert get_cached_mb_recording(db_conn, _recording_request_key("Artist", "Title")) is None


def test_recording_pacing_sleeps_between_network_requests(db_conn: sqlite3.Connection) -> None:
    clock = [0.0]
    slept: list[float] = []

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    client, _ = _client(
        db_conn,
        [_json_response(_recording_body(_recording())), _json_response(_recording_body())],
        rate_per_sec=1.0,
        monotonic=monotonic,
        sleep=sleep,
    )
    with client:
        client.recording_search("A", "One")
        client.recording_search("B", "Two")

    # The shared pacer honors 1 req/sec for the recording endpoint too.
    assert slept == [1.0]
