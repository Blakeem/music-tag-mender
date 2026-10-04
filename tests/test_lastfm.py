"""Unit tests for the Last.fm client (``engine/lastfm.py``).

All network traffic is faked with :class:`httpx.MockTransport`; the cache lives in the
in-memory ``db_conn`` fixture (real schema). The clock/sleep are injected so pacing is
asserted without any real waiting.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import httpx
import pytest

from tagmend.engine import lastfm
from tagmend.engine.lastfm import (
    ArtistCorrection,
    LastfmClient,
    LastfmError,
    LastfmKeyError,
    Tag,
    _request_key,
)
from tagmend.engine.store import LastfmCorrectionRow, get_cached_correction, get_cached_tags

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence


def _toptags(*tags: tuple[str, int]) -> dict[str, object]:
    """Build a found ``toptags`` body with the given (name, count) tags, in order."""
    return {"toptags": {"tag": [{"name": name, "count": count} for name, count in tags]}}


def _handler(
    responses: Sequence[httpx.Response | Exception],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[int]]:
    """Return a MockTransport handler serving *responses* in order, plus a call counter.

    The returned ``calls`` list grows by one entry per network request, so tests can
    assert exactly how many times the transport was hit. An exception entry is raised,
    which is how a transport failure reaches the client.
    """
    calls: list[int] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        index = len(calls)
        calls.append(index)
        served = responses[index]
        if isinstance(served, Exception):
            raise served
        return served

    return handle, calls


def _json_response(body: Mapping[str, object], status_code: int = 200) -> httpx.Response:
    """A JSON httpx.Response with the given body/status."""
    return httpx.Response(status_code, json=dict(body))


def _client(  # noqa: PLR0913 - mirrors the client's keyword-only injection seams
    db_conn: sqlite3.Connection,
    responses: Sequence[httpx.Response | Exception],
    *,
    rate_per_sec: float = 0.0,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    max_attempts: int | None = None,
) -> tuple[LastfmClient, list[int]]:
    """Build a LastfmClient wired to a MockTransport serving *responses*, pacing off by default.

    Without an injected *sleep* a retry backoff sleeps through a no-op, never in real time.
    """
    handle, calls = _handler(responses)
    transport = httpx.MockTransport(handle)
    kwargs: dict[str, object] = {
        "rate_per_sec": rate_per_sec,
        "transport": transport,
        "sleep": sleep if sleep is not None else (lambda _seconds: None),
    }
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    if max_attempts is not None:
        kwargs["max_attempts"] = max_attempts
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


# --- artist.getCorrection ------------------------------------------------------------


def _correction(name: str, mbid: str | None = None) -> dict[str, object]:
    """Build a found ``corrections`` body with one correction's name (+ optional mbid)."""
    artist: dict[str, object] = {"name": name}
    if mbid is not None:
        artist["mbid"] = mbid
    return {"corrections": {"correction": {"artist": artist}}}


def test_artist_correction_parses_name_and_mbid_and_caches(db_conn: sqlite3.Connection) -> None:
    body = _correction("Miami Nights 1984", "abc-123")
    client, calls = _client(db_conn, [_json_response(body)])
    with client:
        first = client.artist_correction("Miami Nights '84")
        assert first == ArtistCorrection(name="Miami Nights 1984", mbid="abc-123")
        # Second call is served from cache — the transport is NOT hit again.
        second = client.artist_correction("Miami Nights '84")
    assert second == first
    assert len(calls) == 1


def test_positive_correction_is_cached_with_typed_columns(db_conn: sqlite3.Connection) -> None:
    client, _calls = _client(db_conn, [_json_response(_correction("Daft Punk", "mbid-1"))])
    with client:
        client.artist_correction("daft punk")

    key = _request_key("artist.getcorrection", {"artist": "daft punk"})
    row = db_conn.execute(
        "SELECT found, name, mbid FROM lastfm_correction_cache WHERE request_key = ?",
        (key,),
    ).fetchone()
    assert row == (1, "Daft Punk", "mbid-1")
    assert get_cached_tags(db_conn, key) is None


def test_artist_correction_without_mbid(db_conn: sqlite3.Connection) -> None:
    body = _correction("Canonical Name")  # no mbid key
    client, _calls = _client(db_conn, [_json_response(body)])
    with client:
        result = client.artist_correction("canonical name")
    assert result == ArtistCorrection(name="Canonical Name", mbid=None)


def test_artist_correction_equal_to_input_is_a_found_result(db_conn: sqlite3.Connection) -> None:
    body = _correction("Daft Punk")
    client, _calls = _client(db_conn, [_json_response(body)])
    with client:
        result = client.artist_correction("Daft Punk")
    assert result == ArtistCorrection(name="Daft Punk", mbid=None)


def test_artist_correction_error_6_is_none_and_negative_cached(db_conn: sqlite3.Connection) -> None:
    body = {"error": 6, "message": "The artist you supplied could not be found"}
    client, calls = _client(db_conn, [_json_response(body)])
    with client:
        first = client.artist_correction("No Such Artist")
        assert first is None
        second = client.artist_correction("No Such Artist")
    assert second is None
    assert len(calls) == 1  # negative-cached: no second network hit
    key = _request_key("artist.getcorrection", {"artist": "No Such Artist"})
    assert get_cached_correction(db_conn, key) == (False, LastfmCorrectionRow(None, None))


def test_artist_correction_empty_corrections_is_none_and_negative_cached(
    db_conn: sqlite3.Connection,
) -> None:
    # Last.fm returns an empty-string ``corrections`` on HTTP 200 when there's no match.
    body: dict[str, object] = {"corrections": ""}
    client, calls = _client(db_conn, [_json_response(body)])
    with client:
        first = client.artist_correction("Plain")
        assert first is None
        second = client.artist_correction("Plain")
    assert second is None
    assert len(calls) == 1
    key = _request_key("artist.getcorrection", {"artist": "Plain"})
    assert get_cached_correction(db_conn, key) == (False, LastfmCorrectionRow(None, None))


def test_artist_correction_absent_corrections_is_none(db_conn: sqlite3.Connection) -> None:
    body: dict[str, object] = {}  # no ``corrections`` key at all
    client, _calls = _client(db_conn, [_json_response(body)])
    with client:
        result = client.artist_correction("Missing")
    assert result is None


def test_artist_correction_non_six_error_raises_and_does_not_cache(
    db_conn: sqlite3.Connection,
) -> None:
    body = {"error": 10, "message": "Invalid API key"}
    client, _calls = _client(db_conn, [_json_response(body)])
    with client, pytest.raises(LastfmError):
        client.artist_correction("Anybody")
    key = _request_key("artist.getcorrection", {"artist": "Anybody"})
    assert get_cached_correction(db_conn, key) is None


def test_artist_correction_http_500_raises_and_does_not_cache(db_conn: sqlite3.Connection) -> None:
    # A 5xx is retried, so every attempt must fail for the lookup to raise.
    client, _calls = _client(db_conn, [_json_response({}, status_code=500)] * 3)
    with client, pytest.raises(LastfmError):
        client.artist_correction("Anybody")
    key = _request_key("artist.getcorrection", {"artist": "Anybody"})
    assert get_cached_correction(db_conn, key) is None


# --- a Last.fm error is raised and nothing is cached ---------------------------------


def test_non_six_error_raises_and_does_not_cache(db_conn: sqlite3.Connection) -> None:
    body = {"error": 10, "message": "Invalid API key"}
    client, _calls = _client(db_conn, [_json_response(body)])
    with client, pytest.raises(LastfmError):
        client.artist_top_tags("Anybody")
    # Nothing cached → a follow-up call would retry.
    key = _request_key("artist.gettoptags", {"artist": "Anybody"})
    assert get_cached_tags(db_conn, key) is None


def test_http_500_raises_and_does_not_cache(db_conn: sqlite3.Connection) -> None:
    # A 5xx is retried, so every attempt must fail for the lookup to raise.
    client, calls = _client(db_conn, [_json_response({}, status_code=500)] * 3)
    with client, pytest.raises(LastfmError):
        client.artist_top_tags("Anybody")
    assert len(calls) == 3
    key = _request_key("artist.gettoptags", {"artist": "Anybody"})
    assert get_cached_tags(db_conn, key) is None


@pytest.mark.parametrize(
    ("status", "code"),
    [(403, 10), (403, 26), (200, 10)],
    ids=["invalid-key-403", "suspended-key-403", "invalid-key-200"],
)
def test_a_rejected_key_raises_lastfm_key_error_and_caches_nothing(
    db_conn: sqlite3.Connection,
    status: int,
    code: int,
) -> None:
    body = {"error": code, "message": "Invalid API key - You must be granted a valid key"}
    client, calls = _client(db_conn, [_json_response(body, status_code=status)])
    with client, pytest.raises(LastfmKeyError, match="lastfm_api_key"):
        client.artist_top_tags("Anybody")
    assert len(calls) == 1
    key = _request_key("artist.gettoptags", {"artist": "Anybody"})
    assert get_cached_tags(db_conn, key) is None


def test_a_4xx_error_body_is_named_in_the_error_and_never_cached(
    db_conn: sqlite3.Connection,
) -> None:
    # A keyless request answers HTTP 400 with error 6, a missing parameter, not a missing entity.
    body = {"error": 6, "message": "Invalid parameters - Your request is missing a parameter"}
    client, calls = _client(db_conn, [_json_response(body, status_code=400)])
    with client, pytest.raises(LastfmError) as raised:
        client.artist_correction("Anybody")
    assert type(raised.value) is LastfmError
    assert str(raised.value) == (
        "Last.fm HTTP 400 for artist.getcorrection: error 6: "
        "Invalid parameters - Your request is missing a parameter"
    )
    assert len(calls) == 1
    key = _request_key("artist.getcorrection", {"artist": "Anybody"})
    assert get_cached_correction(db_conn, key) is None


def test_a_4xx_non_json_body_raises_the_bare_status(db_conn: sqlite3.Connection) -> None:
    client, _calls = _client(db_conn, [httpx.Response(400, content=b"<html>")])
    with client, pytest.raises(LastfmError) as raised:
        client.artist_top_tags("Anybody")
    assert str(raised.value) == "Last.fm HTTP 400 for artist.gettoptags"
    key = _request_key("artist.gettoptags", {"artist": "Anybody"})
    assert get_cached_tags(db_conn, key) is None


def test_non_json_body_raises_lastfm_error_and_caches_nothing(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [httpx.Response(200, content=b"<html>")])
    with client, pytest.raises(LastfmError, match="non-JSON"):
        client.artist_top_tags("Anybody")
    assert len(calls) == 1
    key = _request_key("artist.gettoptags", {"artist": "Anybody"})
    assert get_cached_tags(db_conn, key) is None


def test_json_body_that_is_not_an_object_raises_lastfm_error(db_conn: sqlite3.Connection) -> None:
    client, _calls = _client(db_conn, [httpx.Response(200, json=["not", "an", "object"])])
    with client, pytest.raises(LastfmError, match="not an object"):
        client.artist_correction("Anybody")


def test_malformed_tag_entry_raises_lastfm_error(db_conn: sqlite3.Connection) -> None:
    body = {"toptags": {"tag": [{"name": "rock"}]}}  # no ``count``
    client, _calls = _client(db_conn, [_json_response(body)])
    with client, pytest.raises(LastfmError, match="malformed tag entry"):
        client.artist_top_tags("Anybody")
    key = _request_key("artist.gettoptags", {"artist": "Anybody"})
    assert get_cached_tags(db_conn, key) is None


# --- transient failures are retried with a doubling backoff ---------------------------


def test_transport_error_is_retried_then_succeeds(db_conn: sqlite3.Connection) -> None:
    sleeps: list[float] = []
    client, calls = _client(
        db_conn,
        [
            httpx.ConnectError("connection dropped"),
            httpx.ConnectError("connection dropped"),
            _json_response(_toptags(("rock", 100))),
        ],
        sleep=sleeps.append,
    )
    with client:
        result = client.artist_top_tags("Anybody")
    assert result == [Tag("rock", 100)]
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_transport_error_exhausts_into_lastfm_error(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(
        db_conn,
        [httpx.ConnectError("connection dropped")] * 3,
    )
    with client, pytest.raises(LastfmError) as raised:
        client.artist_top_tags("Anybody")
    assert isinstance(raised.value.__cause__, httpx.ConnectError)
    assert "api_key" not in str(raised.value)  # the request URL carries the key
    assert len(calls) == 3
    key = _request_key("artist.gettoptags", {"artist": "Anybody"})
    assert get_cached_tags(db_conn, key) is None


def test_error_29_is_retried(db_conn: sqlite3.Connection) -> None:
    sleeps: list[float] = []
    client, calls = _client(
        db_conn,
        [
            _json_response({"error": 29, "message": "Rate limit exceeded"}),
            _json_response(_toptags(("rock", 100))),
        ],
        sleep=sleeps.append,
    )
    with client:
        result = client.artist_top_tags("Anybody")
    assert result == [Tag("rock", 100)]
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_http_503_is_retried(db_conn: sqlite3.Connection) -> None:
    sleeps: list[float] = []
    client, calls = _client(
        db_conn,
        [_json_response({}, status_code=503), _json_response(_correction("Bjork Official"))],
        sleep=sleeps.append,
    )
    with client:
        result = client.artist_correction("Bjork")
    assert result == ArtistCorrection(name="Bjork Official", mbid=None)
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_max_attempts_one_does_not_sleep(db_conn: sqlite3.Connection) -> None:
    sleeps: list[float] = []
    client, calls = _client(
        db_conn,
        [_json_response({}, status_code=503)],
        sleep=sleeps.append,
        max_attempts=1,
    )
    with client, pytest.raises(LastfmError, match="1 attempt"):
        client.artist_top_tags("Anybody")
    assert len(calls) == 1
    assert sleeps == []


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


def test_distinct_artist_names_get_different_keys() -> None:
    first = _request_key("artist.gettoptags", {"artist": "Ours"})
    second = _request_key("artist.gettoptags", {"artist": "Theirs"})
    assert first != second


def test_request_key_version_one_matches_legacy_bytes() -> None:
    # Version 1 keeps the key bytes every row cached before parse versions existed was stored
    # under, so those rows stay reachable.
    legacy = hashlib.sha1(b"artist.gettoptags\x00artist=X").hexdigest()  # noqa: S324 - cache key

    assert _request_key("artist.gettoptags", {"artist": "X"}) == legacy


def test_parse_version_bump_refetches(
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _toptags(("rock", 100))
    client, calls = _client(db_conn, [_json_response(body), _json_response(body)])
    with client:
        client.artist_top_tags("Band")
        monkeypatch.setitem(lastfm._PARSE_VERSIONS, "artist.gettoptags", 2)
        client.artist_top_tags("Band")

    assert len(calls) == 2


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
