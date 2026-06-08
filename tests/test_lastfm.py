"""Unit tests for the Last.fm client (``engine/lastfm.py``).

All network traffic is faked with :class:`httpx.MockTransport`; the cache lives in the
in-memory ``db_conn`` fixture (real schema). The clock/sleep are injected so pacing is
asserted without any real waiting.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from tagmend.engine.lastfm import LastfmClient, LastfmError, Tag, _request_key
from tagmend.engine.store import get_cached_tags

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping


def _toptags(*tags: tuple[str, int]) -> dict[str, object]:
    """Build a found ``toptags`` body with the given (name, count) tags, in order."""
    return {"toptags": {"tag": [{"name": name, "count": count} for name, count in tags]}}


def _handler(
    responses: list[httpx.Response],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[int]]:
    """Return a MockTransport handler serving *responses* in order, plus a call counter.

    The returned ``calls`` list grows by one entry per network request, so tests can
    assert exactly how many times the transport was hit.
    """
    calls: list[int] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        index = len(calls)
        calls.append(index)
        return responses[index]

    return handle, calls


def _json_response(body: Mapping[str, object], status_code: int = 200) -> httpx.Response:
    """A JSON httpx.Response with the given body/status."""
    return httpx.Response(status_code, json=dict(body))


def _client(
    db_conn: sqlite3.Connection,
    responses: list[httpx.Response],
    *,
    rate_per_sec: float = 0.0,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[LastfmClient, list[int]]:
    """Build a LastfmClient wired to a MockTransport serving *responses*; pacing off by default."""
    handle, calls = _handler(responses)
    transport = httpx.MockTransport(handle)
    kwargs: dict[str, object] = {"rate_per_sec": rate_per_sec, "transport": transport}
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    if sleep is not None:
        kwargs["sleep"] = sleep
    client = LastfmClient("key", db_conn, **kwargs)  # type: ignore[arg-type]
    return client, calls


# --- found / parsing -----------------------------------------------------------------


def test_artist_found_parses_in_order_and_caches(db_conn: sqlite3.Connection) -> None:
    body = _toptags(("electronic", 100), ("house", 63), ("techno", 26))
    client, calls = _client(db_conn, [_json_response(body)])
    with client:
        first = client.artist_top_tags("Daft Punk")
        assert first == [
            Tag("electronic", 100),
            Tag("house", 63),
            Tag("techno", 26),
        ]
        # Second call must be served from cache — the transport is NOT hit again.
        second = client.artist_top_tags("Daft Punk")
    assert second == first
    assert len(calls) == 1


def test_error_6_negative_caches_and_returns_none(db_conn: sqlite3.Connection) -> None:
    body = {"error": 6, "message": "The artist you supplied could not be found"}
    client, calls = _client(db_conn, [_json_response(body)])
    with client:
        first = client.artist_top_tags("No Such Artist")
        assert first is None
        # Negative-cached: second call returns None with no network request.
        second = client.artist_top_tags("No Such Artist")
    assert second is None
    assert len(calls) == 1
    key = _request_key("artist.gettoptags", {"artist": "No Such Artist"})
    assert get_cached_tags(db_conn, key) == (False, [])


def test_found_but_empty_returns_empty_list_distinct_from_none(db_conn: sqlite3.Connection) -> None:
    # `tag` absent → found with no tags → [], which must differ from the error-6 None path.
    body: dict[str, object] = {"toptags": {}}
    client, _calls = _client(db_conn, [_json_response(body)])
    with client:
        result = client.artist_top_tags("Obscure")
    assert result == []
    assert result is not None


def test_single_tag_dict_normalized_to_one_element_list(db_conn: sqlite3.Connection) -> None:
    # Last.fm returns a single dict (not a list) when there's exactly one tag.
    body = {"toptags": {"tag": {"name": "rock", "count": 100}}}
    client, _calls = _client(db_conn, [_json_response(body)])
    with client:
        result = client.artist_top_tags("OneTag")
    assert result == [Tag("rock", 100)]


def test_album_top_tags_found(db_conn: sqlite3.Connection) -> None:
    body = _toptags(("electronic", 87), ("disco", 43), ("funk", 32))
    client, calls = _client(db_conn, [_json_response(body)])
    with client:
        result = client.album_top_tags("Daft Punk", "Random Access Memories")
    assert result == [Tag("electronic", 87), Tag("disco", 43), Tag("funk", 32)]
    assert len(calls) == 1


# --- transient errors are raised and NOT cached --------------------------------------


def test_non_six_error_raises_and_does_not_cache(db_conn: sqlite3.Connection) -> None:
    body = {"error": 10, "message": "Invalid API key"}
    client, _calls = _client(db_conn, [_json_response(body)])
    with client, pytest.raises(LastfmError):
        client.artist_top_tags("Anybody")
    # Nothing cached → a follow-up call would retry.
    key = _request_key("artist.gettoptags", {"artist": "Anybody"})
    assert get_cached_tags(db_conn, key) is None


def test_http_500_raises_and_does_not_cache(db_conn: sqlite3.Connection) -> None:
    client, _calls = _client(db_conn, [_json_response({}, status_code=500)])
    with client, pytest.raises(LastfmError):
        client.artist_top_tags("Anybody")
    key = _request_key("artist.gettoptags", {"artist": "Anybody"})
    assert get_cached_tags(db_conn, key) is None


# --- argument validation -------------------------------------------------------------


def test_artist_top_tags_requires_exactly_one_identity(db_conn: sqlite3.Connection) -> None:
    client, _calls = _client(db_conn, [])
    with client:
        with pytest.raises(ValueError, match="exactly one"):
            client.artist_top_tags()
        with pytest.raises(ValueError, match="exactly one"):
            client.artist_top_tags("name", mbid="abc")


def test_artist_by_mbid_uses_mbid_param(db_conn: sqlite3.Connection) -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _json_response(_toptags(("rock", 100)))

    client = LastfmClient("key", db_conn, rate_per_sec=0.0, transport=httpx.MockTransport(handle))
    with client:
        result = client.artist_top_tags(mbid="mbid-123")
    assert result == [Tag("rock", 100)]
    assert captured[0].url.params["mbid"] == "mbid-123"
    assert "artist" not in captured[0].url.params


# --- pacing --------------------------------------------------------------------------


def test_pacing_sleeps_the_remaining_interval_between_network_calls(
    db_conn: sqlite3.Connection,
) -> None:
    # Fake clock: first request at t=10.0, then (after the sleep) the second request reads
    # the clock again. We advance the clock by 0.2s between the two _monotonic() reads so
    # the gate must sleep ~0.8s to honor a 1 req/sec rate.
    ticks = iter([10.0, 10.2, 99.0])
    sleeps: list[float] = []

    client, _calls = _client(
        db_conn,
        [_json_response(_toptags(("a", 1))), _json_response(_toptags(("b", 1)))],
        rate_per_sec=1.0,
        monotonic=lambda: next(ticks),
        sleep=sleeps.append,
    )
    with client:
        client.artist_top_tags("First")
        client.artist_top_tags("Second")

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.8)


def test_cache_hit_triggers_no_sleep(db_conn: sqlite3.Connection) -> None:
    sleeps: list[float] = []
    ticks = iter([0.0, 0.0, 0.0])

    client, calls = _client(
        db_conn,
        [_json_response(_toptags(("a", 1)))],
        rate_per_sec=1.0,
        monotonic=lambda: next(ticks),
        sleep=sleeps.append,
    )
    with client:
        client.artist_top_tags("Same")  # network: first request, no prior → no sleep
        client.artist_top_tags("Same")  # cache hit → no network, no sleep

    assert len(calls) == 1
    assert sleeps == []


# --- request-key stability -----------------------------------------------------------


def test_request_key_is_order_independent() -> None:
    key_a = _request_key("album.gettoptags", {"artist": "X", "album": "Y"})
    key_b = _request_key("album.gettoptags", {"album": "Y", "artist": "X"})
    assert key_a == key_b


def test_name_and_mbid_queries_get_different_keys() -> None:
    by_name = _request_key("artist.gettoptags", {"artist": "Ours"})
    by_mbid = _request_key("artist.gettoptags", {"mbid": "some-mbid"})
    assert by_name != by_mbid


def test_method_distinguishes_keys() -> None:
    artist = _request_key("artist.gettoptags", {"artist": "X"})
    album = _request_key("album.gettoptags", {"artist": "X"})
    assert artist != album


# --- cache survives a fresh client (persistence) -------------------------------------


def test_cache_is_shared_across_client_instances_on_same_conn(db_conn: sqlite3.Connection) -> None:
    body = _toptags(("rock", 100))
    first_client, first_calls = _client(db_conn, [_json_response(body)])
    with first_client:
        first_client.artist_top_tags("Band")
    # A brand-new client on the same connection should hit the cache, not the network.
    second_client, second_calls = _client(db_conn, [])
    with second_client:
        result = second_client.artist_top_tags("Band")
    assert result == [Tag("rock", 100)]
    assert len(first_calls) == 1
    assert len(second_calls) == 0


def test_stored_cache_payload_is_name_weight_pairs(db_conn: sqlite3.Connection) -> None:
    body = _toptags(("electronic", 100), ("house", 63))
    client, _calls = _client(db_conn, [_json_response(body)])
    with client:
        client.artist_top_tags("Daft Punk")
    row = db_conn.execute(
        "SELECT found, tags FROM lastfm_cache WHERE request_key = ?",
        (_request_key("artist.gettoptags", {"artist": "Daft Punk"}),),
    ).fetchone()
    assert row[0] == 1
    assert json.loads(row[1]) == [["electronic", 100], ["house", 63]]
