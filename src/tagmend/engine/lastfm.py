"""Last.fm client: artist/album top tags, with persistent caching and pacing (M2).

Free API key only; ``ws.audioscrobbler.com/2.0/``. This module sources the *genre tags*
the classifier later filters against the controlled vocabulary (see
``docs/genre-tagging-spec.md`` §2). Two endpoints are used:

* ``artist.getTopTags`` — ranked community tags for an artist (by name **or** MBID).
* ``album.getTopTags``  — ranked community tags for one album (by artist + album).

Each response's parsed ``(name, weight)`` list is cached persistently in ``lastfm_cache``
so every unique entity is queried at most once ever and re-runs are free. The negative
result (genuinely absent — Last.fm ``error 6``) is cached too, distinct from a found
result that simply has no tags. Transient/auth failures (HTTP non-2xx, any other error
code) raise :class:`LastfmError` and are **never** cached, so a re-run retries them.

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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol, Self, cast

import httpx

from tagmend.engine.store import get_cached_tags, put_cached_tags
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from types import TracebackType

logger = get_logger(__name__)

_API_URL: Final = "https://ws.audioscrobbler.com/2.0/"

# Last.fm's "not found" error code. The body is ``{"error": 6, "message": ...}``; this is
# the one error we negative-cache (the entity genuinely is not on Last.fm). Every other
# code (e.g. ``10`` invalid key) is transient/auth and must stay retryable.
_ERROR_NOT_FOUND: Final = 6


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
    """Cached, paced Last.fm top-tags client (implements :class:`TagSource`).

    Owns one :class:`httpx.Client` for its lifetime via the context-manager protocol;
    use it as ``with LastfmClient(...) as client:``. The cache connection is supplied by
    the caller (the orchestrator owns it) and is committed eagerly after each network
    fetch so an error later in a batch never loses prior cache work.
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
    ) -> None:
        """Configure the client; injectables default to the real httpx transport + clock."""
        self._api_key = api_key
        self._conn = conn
        self._rate_per_sec = rate_per_sec
        self._transport = transport
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._client: httpx.Client | None = None

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

        cached = get_cached_tags(self._conn, request_key)
        if cached is not None:
            found, pairs = cached
            if not found:
                return None
            return _decode_correction(pairs)

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

        tags = _parse_top_tags(body)

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
                self._store(request_key, found=False, tags=[])
                return None
            message = f"Last.fm error {error}: {body.get('message', 'unknown')}"
            raise LastfmError(message)

        correction = _parse_correction(body)
        if correction is None:
            self._store(request_key, found=False, tags=[])
            return None

        # Output: positive-cache the canonical name + MBID as two weight-0 pairs.
        self._store(
            request_key,
            found=True,
            tags=[(correction.name, 0), (correction.mbid or "", 0)],
        )
        return correction

    def _request(self, method: str, identity: dict[str, str]) -> dict[str, object]:
        """Pace, then perform one Last.fm GET, returning the decoded JSON object.

        Raises :class:`LastfmError` on an HTTP non-2xx status (transient/auth).
        """
        if self._client is None:  # pragma: no cover - guard against misuse outside `with`
            message = "LastfmClient must be used as a context manager"
            raise RuntimeError(message)

        params = {"method": method, "api_key": self._api_key, "format": "json", **identity}
        self._pace()
        logger.debug("last.fm request method=%s identity=%s", method, identity)
        response = self._client.get(_API_URL, params=params)
        if response.is_error:
            message = f"Last.fm HTTP {response.status_code} for {method}"
            raise LastfmError(message)
        return cast("dict[str, object]", response.json())

    def _store(self, request_key: str, *, found: bool, tags: list[tuple[str, int]]) -> None:
        """Cache a parsed result and commit immediately (so a later error can't lose it)."""
        put_cached_tags(
            self._conn,
            request_key=request_key,
            found=found,
            tags=tags,
            now=_utc_now(),
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
    """Return a stable ``sha1`` over *method* + the entity-identifying params.

    ``api_key``/``format`` are excluded (they do not identify the entity), and the
    identity params are sorted so insertion order never changes the key. A name-based and
    an mbid-based artist query therefore get distinct keys.
    """
    parts = [method]
    parts.extend(f"{key}={value}" for key, value in sorted(identity.items()))
    payload = "\x00".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324 - cache key, not security


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


def _decode_correction(pairs: list[tuple[str, int]]) -> ArtistCorrection:
    """Rebuild an :class:`ArtistCorrection` from its cached ``[(name,0),(mbid,0)]`` pairs."""
    name = pairs[0][0]
    mbid = pairs[1][0] if len(pairs) > 1 else ""
    return ArtistCorrection(name=name, mbid=mbid or None)


def _utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string (the engine's timestamp form)."""
    return datetime.now(UTC).isoformat()
