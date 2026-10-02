"""Unit tests for the Cover Art Archive client (``engine/coverart.py``).

All traffic is faked with :class:`httpx.MockTransport`, the cache lives in the in-memory
``db_conn`` fixture and the wall clock is patched, so no test touches the network or waits.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from tagmend.config import PROJECT_URL, build_user_agent
from tagmend.engine import clock
from tagmend.engine.coverart import CoverArtClient, CoverArtError, CoverArtFront

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings

_MBID = "11111111-2222-3333-4444-555555555555"
_LISTING_URL = f"https://coverartarchive.org/release/{_MBID}/"
_ARCHIVE_URL = f"https://archive.org/download/mbid-{_MBID}/index.json"
_IMAGE_URL = f"http://coverartarchive.org/release/{_MBID}/1.jpg"
_START = datetime(2026, 10, 1, tzinfo=UTC)


def _entry(*, front: bool = True, approved: bool = True, image_id: int = 1) -> dict[str, object]:
    """Build one image entry as a CAA listing shapes it."""
    base = f"http://coverartarchive.org/release/{_MBID}/{image_id}"
    return {
        "approved": approved,
        "back": not front,
        "front": front,
        "id": image_id,
        "image": f"{base}.jpg",
        "thumbnails": {
            "1200": f"{base}-1200.jpg",
            "250": f"{base}-250.jpg",
            "500": f"{base}-500.jpg",
            "large": f"{base}-500.jpg",
            "small": f"{base}-250.jpg",
        },
        "types": ["Front"] if front else ["Back"],
    }


def _listing(*entries: dict[str, object]) -> dict[str, object]:
    return {"images": list(entries), "release": f"https://musicbrainz.org/release/{_MBID}"}


def _redirect(location: str = _ARCHIVE_URL) -> httpx.Response:
    return httpx.Response(307, headers={"Location": location})


_EXPECTED_FRONT = CoverArtFront(
    kind="release",
    mbid=_MBID,
    image=_IMAGE_URL,
    thumbnail_1200=f"http://coverartarchive.org/release/{_MBID}/1-1200.jpg",
    thumbnail_500=f"http://coverartarchive.org/release/{_MBID}/1-500.jpg",
    thumbnail_large=f"http://coverartarchive.org/release/{_MBID}/1-500.jpg",
)


class _Clock:
    """A settable wall clock that stands in for :func:`tagmend.engine.clock.utc_now`."""

    def __init__(self) -> None:
        self.now = _START

    def utc_now(self) -> str:
        return self.now.isoformat()


@pytest.fixture
def wall_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(clock, "utc_now", fake.utc_now)
    return fake


def _client(
    db_conn: sqlite3.Connection,
    responses: list[httpx.Response | Exception],
) -> tuple[CoverArtClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        outcome = responses[len(seen) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client = CoverArtClient(
        db_conn,
        transport=httpx.MockTransport(handle),
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )
    return client, seen


def _cache_rows(db_conn: sqlite3.Connection) -> list[tuple[int, str | None, str]]:
    return db_conn.execute("SELECT found, payload, fetched_at FROM coverart_cache").fetchall()


# --- listing lookup ----------------------------------------------------------------------


def test_front_image_follows_the_redirect_and_returns_the_approved_front(
    db_conn: sqlite3.Connection,
    wall_clock: _Clock,
) -> None:
    listing = _listing(_entry(front=False, image_id=9), _entry())
    client, seen = _client(db_conn, [_redirect(), httpx.Response(200, json=listing)])

    with client:
        front = client.front_image("release", _MBID)

    assert front == _EXPECTED_FRONT
    assert [str(request.url) for request in seen] == [_LISTING_URL, _ARCHIVE_URL]


def test_front_image_requests_the_release_group_listing(
    db_conn: sqlite3.Connection,
    wall_clock: _Clock,
) -> None:
    client, seen = _client(db_conn, [httpx.Response(200, json=_listing(_entry()))])

    with client:
        front = client.front_image("release-group", _MBID)

    assert front is not None
    assert front.kind == "release-group"
    assert str(seen[0].url) == f"https://coverartarchive.org/release-group/{_MBID}/"


def test_an_unapproved_front_is_no_front_and_is_cached_as_not_found(
    db_conn: sqlite3.Connection,
    wall_clock: _Clock,
) -> None:
    listing = _listing(_entry(approved=False), _entry(front=False, image_id=2))
    client, seen = _client(db_conn, [_redirect(), httpx.Response(200, json=listing)])

    with client:
        assert client.front_image("release", _MBID) is None
        assert client.front_image("release", _MBID) is None

    assert len(seen) == 2
    assert _cache_rows(db_conn) == [(0, None, _START.isoformat())]


def test_a_front_without_an_image_url_is_skipped(
    db_conn: sqlite3.Connection,
    wall_clock: _Clock,
) -> None:
    broken = _entry(image_id=7)
    broken["image"] = None
    client, _seen = _client(db_conn, [httpx.Response(200, json=_listing(broken, _entry()))])

    with client:
        assert client.front_image("release", _MBID) == _EXPECTED_FRONT


def test_a_missing_thumbnail_is_none(db_conn: sqlite3.Connection, wall_clock: _Clock) -> None:
    entry = _entry()
    entry["thumbnails"] = {"large": "http://example.org/large.jpg"}
    client, _seen = _client(db_conn, [httpx.Response(200, json=_listing(entry))])

    with client:
        front = client.front_image("release", _MBID)

    assert front is not None
    assert (front.thumbnail_1200, front.thumbnail_500) == (None, None)
    assert front.thumbnail_large == "http://example.org/large.jpg"


def test_an_unknown_kind_is_refused_before_any_request(
    db_conn: sqlite3.Connection,
    wall_clock: _Clock,
) -> None:
    client, seen = _client(db_conn, [])

    with client, pytest.raises(ValueError, match="unknown kind"):
        client.front_image("artist", _MBID)  # type: ignore[arg-type]

    assert seen == []


# --- listing cache -----------------------------------------------------------------------


def test_a_404_is_cached_as_not_found_for_seven_days(
    db_conn: sqlite3.Connection,
    wall_clock: _Clock,
) -> None:
    client, seen = _client(
        db_conn,
        [httpx.Response(404), httpx.Response(200, json=_listing(_entry()))],
    )

    with client:
        assert client.front_image("release", _MBID) is None
        assert _cache_rows(db_conn) == [(0, None, _START.isoformat())]

        wall_clock.now = _START + timedelta(days=6, hours=23)
        assert client.front_image("release", _MBID) is None
        assert len(seen) == 1

        wall_clock.now = _START + timedelta(days=7)
        assert client.front_image("release", _MBID) == _EXPECTED_FRONT
        assert len(seen) == 2


def test_a_found_listing_is_served_from_the_cache(
    db_conn: sqlite3.Connection,
    wall_clock: _Clock,
) -> None:
    client, seen = _client(db_conn, [_redirect(), httpx.Response(200, json=_listing(_entry()))])

    with client:
        first = client.front_image("release", _MBID)
        wall_clock.now = _START + timedelta(days=365)
        second = client.front_image("release", _MBID)

    assert first == second == _EXPECTED_FRONT
    assert len(seen) == 2  # the redirect and the listing, both from the first call


def test_the_cache_is_keyed_by_kind(db_conn: sqlite3.Connection, wall_clock: _Clock) -> None:
    client, seen = _client(
        db_conn,
        [httpx.Response(200, json=_listing(_entry())), httpx.Response(404)],
    )

    with client:
        assert client.front_image("release", _MBID) is not None
        assert client.front_image("release-group", _MBID) is None

    assert len(seen) == 2


def test_an_unreadable_found_row_is_a_miss(db_conn: sqlite3.Connection, wall_clock: _Clock) -> None:
    client, seen = _client(
        db_conn,
        [
            httpx.Response(200, json=_listing(_entry())),
            httpx.Response(200, json=_listing(_entry())),
        ],
    )

    with client:
        client.front_image("release", _MBID)
        db_conn.execute("UPDATE coverart_cache SET payload = 'not json'")
        assert client.front_image("release", _MBID) == _EXPECTED_FRONT

    assert len(seen) == 2


# --- retries and failures ----------------------------------------------------------------


def test_two_server_errors_then_a_listing_return_the_front(
    db_conn: sqlite3.Connection,
    wall_clock: _Clock,
) -> None:
    client, seen = _client(
        db_conn,
        [httpx.Response(500), httpx.Response(503), httpx.Response(200, json=_listing(_entry()))],
    )

    with client:
        assert client.front_image("release", _MBID) == _EXPECTED_FRONT

    assert len(seen) == 3


def test_a_throttle_and_a_transport_error_are_retried(
    db_conn: sqlite3.Connection,
    wall_clock: _Clock,
) -> None:
    client, seen = _client(
        db_conn,
        [
            httpx.Response(429),
            httpx.ConnectError("connection reset"),
            httpx.Response(200, json=_listing(_entry())),
        ],
    )

    with client:
        assert client.front_image("release", _MBID) == _EXPECTED_FRONT

    assert len(seen) == 3


def test_three_server_errors_raise_and_cache_nothing(
    db_conn: sqlite3.Connection,
    wall_clock: _Clock,
) -> None:
    client, seen = _client(db_conn, [httpx.Response(500)] * 3)

    with client, pytest.raises(CoverArtError, match="HTTP 500 after 3 attempt"):
        client.front_image("release", _MBID)

    assert len(seen) == 3
    assert _cache_rows(db_conn) == []


def test_another_error_status_raises_at_once_and_caches_nothing(
    db_conn: sqlite3.Connection,
    wall_clock: _Clock,
) -> None:
    client, seen = _client(db_conn, [httpx.Response(400, text="invalid UUID")])

    with client, pytest.raises(CoverArtError, match="HTTP 400"):
        client.front_image("release", _MBID)

    assert len(seen) == 1
    assert _cache_rows(db_conn) == []


def test_a_non_json_listing_raises_and_caches_nothing(
    db_conn: sqlite3.Connection,
    wall_clock: _Clock,
) -> None:
    client, _seen = _client(db_conn, [httpx.Response(200, text="<html>proxy error</html>")])

    with client, pytest.raises(CoverArtError, match="non-JSON"):
        client.front_image("release", _MBID)

    assert _cache_rows(db_conn) == []


def test_every_request_carries_the_project_user_agent(
    db_conn: sqlite3.Connection,
    wall_clock: _Clock,
) -> None:
    client, seen = _client(
        db_conn,
        [
            _redirect(),
            httpx.Response(200, json=_listing(_entry())),
            _redirect("https://ia800.us.archive.org/1.jpg"),
            httpx.Response(200, content=b"jpeg"),
        ],
    )

    with client:
        client.front_image("release", _MBID)
        client.fetch_image(_IMAGE_URL)

    assert len(seen) == 4
    assert {request.headers["User-Agent"] for request in seen} == {build_user_agent(PROJECT_URL)}


# --- image download ----------------------------------------------------------------------


def test_fetch_image_returns_the_body_after_a_redirect(db_conn: sqlite3.Connection) -> None:
    target = "https://ia800.us.archive.org/1.jpg"
    client, seen = _client(db_conn, [_redirect(target), httpx.Response(200, content=b"\xff\xd8")])

    with client:
        body = client.fetch_image(_IMAGE_URL)

    assert body == b"\xff\xd8"
    assert [str(request.url) for request in seen] == [_IMAGE_URL, target]
    assert _cache_rows(db_conn) == []


def test_fetch_image_raises_on_a_404(db_conn: sqlite3.Connection) -> None:
    client, _seen = _client(db_conn, [httpx.Response(404)])

    with client, pytest.raises(CoverArtError, match="HTTP 404"):
        client.fetch_image(_IMAGE_URL)


def test_fetch_image_retries_a_server_error(db_conn: sqlite3.Connection) -> None:
    client, seen = _client(db_conn, [httpx.Response(502), httpx.Response(200, content=b"png")])

    with client:
        assert client.fetch_image(_IMAGE_URL) == b"png"

    assert len(seen) == 2


def test_from_settings_builds(engine_settings: Settings, db_conn: sqlite3.Connection) -> None:
    assert isinstance(CoverArtClient.from_settings(engine_settings, db_conn), CoverArtClient)
