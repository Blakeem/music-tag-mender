"""Cover Art Archive client: the front image of a release or a release group, cached and paced.

:meth:`CoverArtClient.front_image` reads the listing at
``https://coverartarchive.org/<kind>/<mbid>/`` and returns its first approved front.
:meth:`CoverArtClient.fetch_image` downloads one image. CAA answers both with a redirect to
archive.org, so every request follows redirects.

Each listing answer is cached in ``coverart_cache`` under a request hash carrying its own version
token. A found listing never expires. A not-found answer (a 404, a 400 for an id that is no UUID,
or a listing with no approved front) is served for 7 days only, because CAA gains art over time.
Any other failure raises :class:`CoverArtError` and is never cached. Images are not cached. The
User-Agent names the project and never a person.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal, Protocol, Self, cast

from tagmend.config import PROJECT_URL, build_user_agent
from tagmend.engine import clock
from tagmend.engine.lookup_clients import (
    PacedHttp,
    decode_object,
    retry_throttle_or_server_error,
)
from tagmend.engine.store import get_cached_coverart, put_cached_coverart
from tagmend.engine.validation import require_choice
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping
    from types import TracebackType

    import httpx

    from tagmend.config import Settings

logger = get_logger(__name__)

type CoverArtKind = Literal["release", "release-group"]

_API_URL: Final = "https://coverartarchive.org"
_SOURCE: Final = "Cover Art Archive"
_KINDS: Final = ("release", "release-group")
# CAA documents no rate limit, so the client keeps to the pace MusicBrainz asks for.
_RATE_PER_SEC: Final = 1.0
_USER_AGENT: Final = build_user_agent(PROJECT_URL)
# Bump when the fields `_front_from_entry` extracts change, so a cached answer is re-fetched.
_LISTING_VERSION: Final = "1"
_NOT_FOUND_TTL: Final = timedelta(days=7)
_HTTP_OK: Final = 200
_HTTP_NOT_FOUND: Final = 404
# CAA answers 400 only when the id is no UUID, so that answer is as final as a 404.
_HTTP_BAD_REQUEST: Final = 400


class CoverArtError(RuntimeError):
    """A Cover Art Archive request failed. The failure is never cached, so a re-run retries it."""


@dataclass(frozen=True, slots=True)
class CoverArtFront:
    """The approved front image of one CAA release or release group, with its thumbnail URLs."""

    kind: CoverArtKind
    mbid: str
    image: str
    thumbnail_1200: str | None
    thumbnail_500: str | None
    thumbnail_large: str | None


class CoverArtSource(Protocol):
    """The CAA lookups :func:`tagmend.engine.covers.stage_covers` uses, so a test can fake them."""

    def front_image(self, kind: CoverArtKind, mbid: str) -> CoverArtFront | None:
        """Return the first approved front CAA holds for *mbid*, or ``None``."""

    def has_cached_listing(self, kind: CoverArtKind, mbid: str) -> bool:
        """Return whether :meth:`front_image` answers *kind*/*mbid* without a request."""

    def fetch_image(self, url: str) -> bytes:
        """Return the body of the image at *url*."""


class CoverArtClient:
    """Cached, paced Cover Art Archive client. Use it as ``with CoverArtClient(conn) as client:``.

    The cache connection is supplied by the caller and committed after each cache write, so a
    later failure in a sweep never loses an earlier answer.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Configure the client. The injectables default to the real transport and clock."""
        self._conn = conn
        self._http = PacedHttp(
            rate_per_sec=_RATE_PER_SEC,
            transport=transport,
            monotonic=monotonic,
            sleep=sleep,
            headers={"User-Agent": _USER_AGENT},
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,  # noqa: ARG003 - every lookup client shares one builder signature
        conn: sqlite3.Connection,
    ) -> Self:
        """Build a client for *conn*. CAA takes no key and no configured rate."""
        return cls(conn)

    def __enter__(self) -> Self:
        """Open the underlying :class:`httpx.Client` with the project User-Agent."""
        self._http.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the underlying :class:`httpx.Client`."""
        self._http.close()

    def front_image(self, kind: CoverArtKind, mbid: str) -> CoverArtFront | None:
        """Return the first approved front CAA holds for *mbid*, or ``None`` when it holds none.

        *kind* is ``release`` or ``release-group``. The cache answers first, else one paced
        request. A 404, a 400 or a listing with no approved front is cached as not-found. Any other
        failure raises :class:`CoverArtError` and caches nothing.
        """
        hit, front = self._cached(kind, mbid)
        if hit:
            return front

        return self._fetch_and_cache(kind, mbid, _request_key(kind, mbid))

    def has_cached_listing(self, kind: CoverArtKind, mbid: str) -> bool:
        """Return whether :meth:`front_image` answers *kind*/*mbid* from the cache, found or not."""
        hit, _ = self._cached(kind, mbid)
        return hit

    def _cached(self, kind: CoverArtKind, mbid: str) -> tuple[bool, CoverArtFront | None]:
        """Return ``(hit, front)`` from the cache for the *kind* listing of *mbid*."""
        require_choice("kind", kind, _KINDS)
        row = get_cached_coverart(self._conn, _request_key(kind, mbid))
        return _read_cached(kind, mbid, row, clock.utc_now())

    def fetch_image(self, url: str) -> bytes:
        """Return the body of the image at *url*, following redirects.

        Raises :class:`CoverArtError` on any final status but 200, or when the retries run out.
        """
        response = self._send(url, "image download")
        if response.status_code != _HTTP_OK:
            message = f"{_SOURCE} HTTP {response.status_code} for image {url}"
            raise CoverArtError(message)
        return response.content

    def _fetch_and_cache(
        self, kind: CoverArtKind, mbid: str, request_key: str
    ) -> CoverArtFront | None:
        """Fetch one listing over the network, then cache its answer and commit it."""
        body = self._request_listing(kind, mbid)

        front = None if body is None else _parse_listing(kind, mbid, body)

        payload = None if front is None else _front_to_payload(front)
        put_cached_coverart(
            self._conn,
            request_key=request_key,
            found=front is not None,
            payload=payload,
            now=clock.utc_now(),
        )
        # Committing each write keeps earlier cache work when a later lookup fails.
        self._conn.commit()
        return front

    def _request_listing(self, kind: CoverArtKind, mbid: str) -> dict[str, object] | None:
        """GET the listing of *mbid*, or ``None`` when CAA holds no art for it, a 404 or a 400."""
        what = f"{kind} {mbid} listing"
        logger.debug("coverart listing request kind=%s mbid=%r", kind, mbid)
        response = self._send(f"{_API_URL}/{kind}/{mbid}/", what)
        if response.status_code == _HTTP_BAD_REQUEST:
            logger.info("coverart: CAA rejects %s id %r as no UUID", kind, mbid)
            return None
        if response.status_code == _HTTP_NOT_FOUND:
            return None
        if response.status_code != _HTTP_OK:
            message = f"{_SOURCE} HTTP {response.status_code} for {what}"
            raise CoverArtError(message)
        return decode_object(response, CoverArtError, source=_SOURCE, what=what)

    def _send(self, url: str, what: str) -> httpx.Response:
        """Pace, then GET *url* following redirects, under the retry verdict."""
        return self._http.send(
            # httpx follows no redirect by default, and CAA redirects every answer to archive.org.
            lambda client: client.get(url, follow_redirects=True),
            verdict=retry_throttle_or_server_error,
            error=lambda failure, attempts: CoverArtError(
                f"{_SOURCE} {failure} after {attempts} attempt(s) for {what}",
            ),
            label=f"coverart {what}",
        )


def _request_key(kind: CoverArtKind, mbid: str) -> str:
    """Return a stable ``sha1`` over the kind, the listing-parse rules and the MBID."""
    payload = "\x00".join([kind, f"listing_version={_LISTING_VERSION}", f"mbid={mbid}"])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324 - cache key, not security


def _read_cached(
    kind: CoverArtKind,
    mbid: str,
    row: tuple[bool, str | None, str] | None,
    now: str,
) -> tuple[bool, CoverArtFront | None]:
    """Return ``(hit, front)`` for one cache row.

    An expired not-found row is a miss. An unreadable found row is a miss too, so the next
    fetch overwrites it.
    """
    if row is None:
        return False, None
    found, payload, fetched_at = row
    if not found:
        age = datetime.fromisoformat(now) - datetime.fromisoformat(fetched_at)
        return age < _NOT_FOUND_TTL, None

    front = None if payload is None else _front_from_payload(kind, mbid, payload)
    if front is None:
        logger.warning("unreadable coverart_cache row for %s %s, treating it as a miss", kind, mbid)
    return front is not None, front


def _parse_listing(
    kind: CoverArtKind, mbid: str, body: Mapping[str, object]
) -> CoverArtFront | None:
    """Return the first image of a listing that is an approved front with an image URL."""
    images = body.get("images")
    if not isinstance(images, list):
        return None
    for entry in images:
        if not isinstance(entry, dict):
            continue
        fields = cast("dict[str, object]", entry)
        if fields.get("front") is not True or fields.get("approved") is not True:
            continue
        front = _front_from_entry(kind, mbid, fields)
        if front is not None:
            return front
    return None


def _front_from_entry(
    kind: CoverArtKind, mbid: str, entry: Mapping[str, object]
) -> CoverArtFront | None:
    """Build a front from one CAA image entry, or ``None`` when it carries no image URL."""
    image = _url(entry.get("image"))
    if image is None:
        return None
    raw_thumbnails = entry.get("thumbnails")
    thumbnails = (
        cast("dict[str, object]", raw_thumbnails) if isinstance(raw_thumbnails, dict) else {}
    )
    return CoverArtFront(
        kind=kind,
        mbid=mbid,
        image=image,
        thumbnail_1200=_url(thumbnails.get("1200")),
        thumbnail_500=_url(thumbnails.get("500")),
        thumbnail_large=_url(thumbnails.get("large")),
    )


def _url(value: object) -> str | None:
    """Return *value* when it is a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None


def _front_to_payload(front: CoverArtFront) -> str:
    """Serialize *front* for the cache payload, shaped as a CAA image entry."""
    entry = {
        "image": front.image,
        "thumbnails": {
            "1200": front.thumbnail_1200,
            "500": front.thumbnail_500,
            "large": front.thumbnail_large,
        },
    }
    return json.dumps(entry, separators=(",", ":"))


def _front_from_payload(kind: CoverArtKind, mbid: str, payload: str) -> CoverArtFront | None:
    """Rebuild a cached front, or ``None`` when the payload is unreadable."""
    try:
        entry: object = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    return _front_from_entry(kind, mbid, cast("dict[str, object]", entry))
