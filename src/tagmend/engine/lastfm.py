"""Last.fm client: artist/album top tags, with persistent caching and pacing (M2).

Free API key only; ``ws.audioscrobbler.com/2.0/``. This module sources the *genre tags*
the classifier later filters against the controlled vocabulary (see
``docs/genre-tagging-spec.md`` §2). Three endpoints are used:

* ``artist.getTopTags`` — ranked community tags for an artist (by name **or** MBID).
* ``album.getTopTags``  — ranked community tags for one album (by artist + album).
* ``artist.getCorrection``: the canonical artist name plus MBID, feeding ``resolve_artists``'
  Last.fm tier.

Each response's parsed ``(name, weight)`` list is cached persistently in ``lastfm_cache``
so every unique entity is queried at most once ever and re-runs are free. Each
``artist.getCorrection`` answer is cached the same way in ``lastfm_correction_cache``. The
negative result (genuinely absent, Last.fm ``error 6``) is cached too, distinct from a found
result that simply has no tags. Transient/auth failures (HTTP non-2xx, any other error
code) raise :class:`LastfmError` and are **never** cached, so a re-run retries them.
A transport error, an HTTP 429 or 5xx, or a temporary Last.fm error code (11, 16, 29) is
retried first, with a doubling backoff, up to ``max_attempts`` times.

A small in-process rate limiter paces *network* requests (never cache hits) to
``rate_per_sec``. The httpx transport, clock, and sleep are injectable so the whole thing
is unit-testable with :class:`httpx.MockTransport` and a fake clock — no real network and
no real waiting. The ``monotonic``/``sleep`` gate state lives on the instance, so reuse
one client across a batch to keep the pacing honest within a run.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, Self, cast

import httpx

from tagmend.engine import clock
from tagmend.engine.store import (
    get_cached_correction,
    get_cached_tags,
    put_cached_correction,
    put_cached_tags,
)
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping
    from types import TracebackType

    from tagmend.config import Settings

logger = get_logger(__name__)

_API_URL: Final = "https://ws.audioscrobbler.com/2.0/"

# Last.fm's "not found" error code. The body is ``{"error": 6, "message": ...}``; this is
# the one error we negative-cache (the entity genuinely is not on Last.fm). Every other
# code (e.g. ``10`` invalid key) is transient/auth and must stay retryable.
_ERROR_NOT_FOUND: Final = 6

# Last.fm documents these codes as temporary: 11 service offline, 16 temporary error, 29 rate
# limit exceeded. Each is retried like a dropped connection rather than failing the lookup.
_TEMPORARY_ERROR_CODES: Final = frozenset({11, 16, 29})
_HTTP_TOO_MANY_REQUESTS: Final = 429
_HTTP_SERVER_ERROR: Final = 500
_RETRY_ATTEMPTS: Final = 3
_RETRY_BACKOFF_SECONDS: Final = 1.0

# A cache hit never re-parses, so bump a method's entry when its parse changes and its cached
# rows are re-fetched instead of replayed. Version 1 keeps the original key bytes.
_PARSE_VERSIONS: Final[Mapping[str, int]] = {
    "artist.gettoptags": 1,
    "album.gettoptags": 1,
    "artist.getcorrection": 1,
}


@dataclass(frozen=True, slots=True)
class Tag:
    """One Last.fm community tag: its name and normalized 0-100 weight (``count``)."""

    name: str
    weight: int


@dataclass(frozen=True, slots=True)
class ArtistCorrection:
    """A canonical artist name from ``artist.getCorrection``, with its MBID when known."""

    name: str
    mbid: str | None


class LastfmError(RuntimeError):
    """A Last.fm lookup failed transiently (HTTP non-2xx, or a non-"not found" error).

    These are deliberately **not** cached so a re-run retries them.
    """


class TagSource(Protocol):
    """The genre-tag lookups the orchestrator depends on (so it can use a fake in tests).

    Each method returns ``list[Tag]`` when the entity is found (possibly empty when found
    with no tags) and ``None`` when the entity is genuinely not on Last.fm.
    """

    def artist_top_tags(
        self,
        name: str | None = None,
        *,
        mbid: str | None = None,
    ) -> list[Tag] | None:
        """Return the artist's top tags, or ``None`` if the artist is not found."""

    def album_top_tags(self, artist: str, album: str) -> list[Tag] | None:
        """Return the album's top tags, or ``None`` if the album is not found."""


class CorrectionSource(Protocol):
    """The artist-name correction lookup the orchestrator depends on (fakeable in tests).

    Returns an :class:`ArtistCorrection` (the canonical name + optional MBID) when Last.fm
    has a correction — possibly equal to the input — and ``None`` when the artist has no
    correction (genuinely absent / ``error 6`` / no ``corrections`` object).
    """

    def artist_correction(self, name: str) -> ArtistCorrection | None:
        """Return the canonical artist name for *name*, or ``None`` if uncorrectable."""


class LastfmClient:
    """Cached, paced Last.fm client (implements :class:`TagSource` and :class:`CorrectionSource`).

    Owns one :class:`httpx.Client` for its lifetime via the context-manager protocol;
    use it as ``with LastfmClient(...) as client:``. The cache connection is supplied by
    the caller (the orchestrator owns it) and is committed eagerly after each network
    fetch so an error later in a batch never loses prior cache work. Each request is tried
    up to ``max_attempts`` times while the failure is transient, and a failure that outlasts
    them raises :class:`LastfmError`.
    """

    def __init__(  # noqa: PLR0913 - cohesive keyword-only injection seams for testing
        self,
        api_key: str,
        conn: sqlite3.Connection,
        *,
        rate_per_sec: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = _RETRY_ATTEMPTS,
    ) -> None:
        """Configure the client; injectables default to the real httpx transport + clock."""
        self._api_key = api_key
        self._conn = conn
        self._rate_per_sec = rate_per_sec
        self._transport = transport
        self._monotonic = monotonic
        self._sleep = sleep
        self._max_attempts = max(1, max_attempts)
        self._last_request_at: float | None = None
        self._client: httpx.Client | None = None

    @classmethod
    def from_settings(cls, settings: Settings, conn: sqlite3.Connection) -> Self:
        """Build a client with the configured API key and request rate.

        Raises :class:`ValueError` when no API key is configured.
        """
        if not settings.lastfm_api_key:
            message = (
                "no Last.fm API key configured. Run `tagmend config-set lastfm_api_key <key>`."
            )
            raise ValueError(message)
        return cls(settings.lastfm_api_key, conn, rate_per_sec=settings.lastfm_rate_per_sec)

    # --- context manager: own one httpx.Client for the client's lifetime -------------

    def __enter__(self) -> Self:
        """Open the underlying :class:`httpx.Client`."""
        self._client = httpx.Client(transport=self._transport, timeout=30.0)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the underlying :class:`httpx.Client`."""
        if self._client is not None:
            self._client.close()
            self._client = None

    # --- public API ------------------------------------------------------------------

    def artist_top_tags(
        self,
        name: str | None = None,
        *,
        mbid: str | None = None,
    ) -> list[Tag] | None:
        """Return an artist's top tags by *name* **or** *mbid* (exactly one required).

        Returns ``list[Tag]`` when found (possibly empty), ``None`` when the artist is
        genuinely not on Last.fm. Raises :class:`ValueError` if neither or both of
        *name*/*mbid* are given, and :class:`LastfmError` on a transient failure.
        """
        if (name is None) == (mbid is None):
            message = "artist_top_tags requires exactly one of name or mbid"
            raise ValueError(message)
        identity = {"mbid": mbid} if mbid is not None else {"artist": cast("str", name)}
        return self._top_tags("artist.gettoptags", identity)

    def album_top_tags(self, artist: str, album: str) -> list[Tag] | None:
        """Return an album's top tags by *artist* + *album*.

        Returns ``list[Tag]`` when found (possibly empty), ``None`` when the album is
        genuinely not on Last.fm. Raises :class:`LastfmError` on a transient failure.
        """
        return self._top_tags("album.gettoptags", {"artist": artist, "album": album})

    def artist_correction(self, name: str) -> ArtistCorrection | None:
        """Return the canonical name (+ MBID) for *name*, or ``None`` if uncorrectable.

        Caches like :meth:`artist_top_tags`: a found correction is positive-cached and a
        no-correction (``error 6`` or an absent/empty ``corrections`` object on an HTTP 200)
        is negative-cached, both so re-runs are free. Any other error code / HTTP non-2xx
        raises :class:`LastfmError` and caches nothing (retryable). A correction equal to
        the input is a valid found result.
        """
        request_key = _request_key("artist.getcorrection", {"artist": name})

        cached = get_cached_correction(self._conn, request_key)
        if cached is not None:
            found, row = cached
            if not found or row.name is None:
                return None
            return ArtistCorrection(name=row.name, mbid=row.mbid)

        return self._fetch_and_cache_correction(name, request_key)

    # --- internals -------------------------------------------------------------------

    def _top_tags(self, method: str, identity: dict[str, str]) -> list[Tag] | None:
        """Resolve *method* for the entity in *identity*: cache first, else fetch + cache."""
        request_key = _request_key(method, identity)

        cached = get_cached_tags(self._conn, request_key)
        if cached is not None:
            found, pairs = cached
            if not found:
                return None
            return [Tag(name=name, weight=weight) for name, weight in pairs]

        return self._fetch_and_cache(method, identity, request_key)

    def _fetch_and_cache(
        self,
        method: str,
        identity: dict[str, str],
        request_key: str,
    ) -> list[Tag] | None:
        """Fetch *method* over the network (paced), then cache the parsed result eagerly.

        Last.fm ``error 6`` (not found) is negative-cached and returns ``None``; any
        other error or HTTP non-2xx raises :class:`LastfmError` and caches nothing.
        """
        # Input: one paced network request.
        body = self._request(method, identity)

        # Process: distinguish "not found" (cacheable) from transient errors (not).
        error = body.get("error")
        if error is not None:
            if error == _ERROR_NOT_FOUND:
                self._store(request_key, found=False, tags=[])
                return None
            message = f"Last.fm error {error}: {body.get('message', 'unknown')}"
            raise LastfmError(message)

        try:
            tags = _parse_top_tags(body)
        except (KeyError, TypeError, ValueError) as exc:
            message = f"Last.fm returned a malformed tag entry for {method}"
            raise LastfmError(message) from exc

        # Output: cache the found result eagerly, then return it.
        self._store(request_key, found=True, tags=[(t.name, t.weight) for t in tags])
        return tags

    def _fetch_and_cache_correction(
        self,
        name: str,
        request_key: str,
    ) -> ArtistCorrection | None:
        """Fetch ``artist.getcorrection`` over the network (paced), then cache eagerly.

        ``error 6`` *or* an absent/empty ``corrections`` object (HTTP 200, no error) is a
        no-correction → negative-cached → ``None``. Any other error / HTTP non-2xx raises
        :class:`LastfmError` and caches nothing.
        """
        # Input: one paced network request.
        body = self._request("artist.getcorrection", {"artist": name})

        # Process: distinguish "no correction" (cacheable) from transient errors (not).
        error = body.get("error")
        if error is not None:
            if error == _ERROR_NOT_FOUND:
                self._store_correction(request_key, None)
                return None
            message = f"Last.fm error {error}: {body.get('message', 'unknown')}"
            raise LastfmError(message)

        correction = _parse_correction(body)

        # Output: cache the answer (a missing correction negative-cached), then return it.
        self._store_correction(request_key, correction)
        return correction

    def _request(self, method: str, identity: dict[str, str]) -> dict[str, object]:
        """Pace, then GET one Last.fm method, returning the decoded JSON object.

        A transport error, an HTTP 429 or 5xx, or a temporary Last.fm error code is retried
        with a doubling backoff. Any other answer returns at once. A failure that outlasts
        every attempt, any other HTTP non-2xx and a body that is not a JSON object each raise
        :class:`LastfmError`. No message carries the request URL, since it holds the API key.
        """
        if self._client is None:  # pragma: no cover - guard against misuse outside `with`
            message = "LastfmClient must be used as a context manager"
            raise RuntimeError(message)

        params = {"method": method, "api_key": self._api_key, "format": "json", **identity}
        delay = _RETRY_BACKOFF_SECONDS
        failure = "no attempt made"
        cause: httpx.HTTPError | None = None

        logger.debug("last.fm request method=%s identity=%s", method, identity)
        for attempt in range(self._max_attempts):
            self._pace()
            try:
                response = self._client.get(_API_URL, params=params)
            except httpx.HTTPError as exc:
                cause = exc
                failure = f"transport error ({type(exc).__name__})"
            else:
                cause = None
                body, failure = _classify_response(response, method)
                if body is not None:
                    return body
            if attempt < self._max_attempts - 1:
                logger.debug("last.fm %s for %s, backing off %ss", failure, method, delay)
                self._sleep(delay)
                delay *= 2

        message = f"Last.fm {method} failed after {self._max_attempts} attempt(s): {failure}"
        raise LastfmError(message) from cause

    def _store(self, request_key: str, *, found: bool, tags: list[tuple[str, int]]) -> None:
        """Cache a parsed result and commit immediately (so a later error can't lose it)."""
        put_cached_tags(
            self._conn,
            request_key=request_key,
            found=found,
            tags=tags,
            now=clock.utc_now(),
        )
        self._conn.commit()

    def _store_correction(self, request_key: str, correction: ArtistCorrection | None) -> None:
        """Cache a parsed correction (``None`` = no correction) and commit immediately."""
        put_cached_correction(
            self._conn,
            request_key=request_key,
            found=correction is not None,
            name=None if correction is None else correction.name,
            mbid=None if correction is None else correction.mbid,
            now=clock.utc_now(),
        )
        self._conn.commit()

    def _pace(self) -> None:
        """Sleep just enough so consecutive network requests honor ``rate_per_sec``.

        Uses the injected ``monotonic``/``sleep`` so tests assert pacing without waiting.
        ``rate_per_sec <= 0`` disables pacing. Records each request's time on the
        instance, so reusing one client across a batch keeps the gate honest.
        """
        if self._rate_per_sec > 0 and self._last_request_at is not None:
            interval = 1.0 / self._rate_per_sec
            elapsed = self._monotonic() - self._last_request_at
            remaining = interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._monotonic()


# --- module helpers ------------------------------------------------------------------


def _request_key(method: str, identity: dict[str, str]) -> str:
    """Return a stable ``sha1`` over *method* + the entity-identifying params + parse version.

    ``api_key``/``format`` are excluded (they do not identify the entity), and the
    identity params are sorted so insertion order never changes the key. A name-based and
    an mbid-based artist query therefore get distinct keys. A method missing from
    :data:`_PARSE_VERSIONS` raises :class:`KeyError`.
    """
    parse_version = _PARSE_VERSIONS[method]
    parts = [method]
    parts.extend(f"{key}={value}" for key, value in sorted(identity.items()))
    if parse_version > 1:
        parts.append(f"parse={parse_version}")
    payload = "\x00".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324 - cache key, not security


def _classify_response(
    response: httpx.Response,
    method: str,
) -> tuple[dict[str, object] | None, str]:
    """Return ``(body, "")`` for a final answer or ``(None, reason)`` for a temporary one.

    Raises :class:`LastfmError` for a permanent failure: an HTTP non-2xx other than 429 or 5xx,
    or a body that is not a JSON object.
    """
    status = response.status_code
    if status == _HTTP_TOO_MANY_REQUESTS or status >= _HTTP_SERVER_ERROR:
        return None, f"HTTP {status}"
    if response.is_error:
        message = f"Last.fm HTTP {status} for {method}"
        raise LastfmError(message)

    try:
        body = response.json()
    except ValueError as exc:
        message = f"Last.fm returned a non-JSON body for {method}"
        raise LastfmError(message) from exc
    if not isinstance(body, dict):
        message = f"Last.fm returned a JSON {type(body).__name__}, not an object, for {method}"
        raise LastfmError(message)

    error = body.get("error")
    if error in _TEMPORARY_ERROR_CODES:
        return None, f"error {error}"
    return cast("dict[str, object]", body), ""


def _parse_top_tags(body: dict[str, object]) -> list[Tag]:
    """Parse ``toptags.tag`` into an ordered ``list[Tag]``.

    ``tag`` may be a list, a single dict (one tag), or absent (→ ``[]``, i.e. found with
    no tags). Each entry's ``count`` is the normalized 0-100 weight.
    """
    toptags = body.get("toptags")
    if not isinstance(toptags, dict):
        return []
    raw = toptags.get("tag")
    if raw is None:
        return []
    entries = raw if isinstance(raw, list) else [raw]
    tags: list[Tag] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        tags.append(Tag(name=str(entry["name"]), weight=int(entry["count"])))
    return tags


def _parse_correction(body: dict[str, object]) -> ArtistCorrection | None:
    """Parse ``corrections.correction.artist.{name,mbid}`` into an :class:`ArtistCorrection`.

    Every intermediate key is guarded: ``corrections`` may be an empty string or absent (a
    no-correction → ``None``), and ``correction``/``artist`` may be missing or non-dict.
    A missing/empty ``name`` is treated as no correction; ``mbid`` is optional.
    """
    corrections = body.get("corrections")
    if not isinstance(corrections, dict):
        return None
    correction = corrections.get("correction")
    if not isinstance(correction, dict):
        return None
    artist = correction.get("artist")
    if not isinstance(artist, dict):
        return None
    raw_name = artist.get("name")
    if not isinstance(raw_name, str) or not raw_name:
        return None
    raw_mbid = artist.get("mbid")
    mbid = raw_mbid if isinstance(raw_mbid, str) and raw_mbid else None
    return ArtistCorrection(name=raw_name, mbid=mbid)
