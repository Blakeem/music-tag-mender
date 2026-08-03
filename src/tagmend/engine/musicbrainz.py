"""MusicBrainz client: an album's original (first) release year, cached and paced.

The album-axis authority (mirrors :mod:`tagmend.engine.lastfm`'s shape). Last.fm cannot
supply an album's year and has no album correction; MusicBrainz can — a *release-group*'s
``first-release-date`` is the original year (e.g. *Paranoid* = 1970), distinct from a
reissue *release* ``date`` (the edition year). One endpoint is used:

* ``/ws/2/release-group/`` (Lucene query ``artist:"…" AND releasegroup:"…"``) — ranked
  candidate release groups; we keep only ``primary-type == "Album"`` groups with no
  ``Live``/``Compilation`` secondary type and pick the highest-scoring one.
* ``/ws/2/recording/`` (Lucene query ``artist:"…" AND recording:"…"``) — a review-only
  ``(artist, title)`` → album lookup feeding ``detect_album_gaps``' recording tier. Ranked
  candidate recordings; we keep only the highest-scoring recording that has a release whose
  release group is a usable Album (same ``primary-type``/secondary-type gate) and return that
  release group's title (+ recording MBID + release-group id).

Each release-group lookup's parsed result is cached persistently in ``musicbrainz_cache`` and
each recording lookup's in ``musicbrainz_recording_cache`` so every unique album/recording is
queried at most once. A no-match (no usable Album release group) is negative-cached too.
Transient/HTTP failures raise :class:`MusicBrainzError` and are **never** cached, so a re-run
retries them (the caller leaves the group pending, like the genre path).

A small in-process rate limiter paces *network* requests (never cache hits) to
``rate_per_sec`` (MusicBrainz asks for ~1/s) and every request carries a mandatory,
descriptive ``User-Agent``. The httpx transport, clock, and sleep are injectable so the
whole thing is unit-testable with :class:`httpx.MockTransport` and a fake clock.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol, Self, cast

import httpx

from tagmend.engine.store import (
    get_cached_mb_album,
    get_cached_mb_recording,
    put_cached_mb_album,
    put_cached_mb_recording,
)
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from types import TracebackType

logger = get_logger(__name__)

_API_URL: Final = "https://musicbrainz.org/ws/2/release-group/"
_RECORDING_API_URL: Final = "https://musicbrainz.org/ws/2/recording/"

# Secondary release-group types that disqualify a candidate (not an original studio album).
_EXCLUDED_SECONDARY_TYPES: Final = frozenset({"Live", "Compilation"})

# A bare four-digit year prefix length (MusicBrainz dates are ``YYYY`` / ``YYYY-MM`` / full).
_YEAR_PREFIX_LEN: Final = 4


@dataclass(frozen=True, slots=True)
class MBAlbum:
    """A MusicBrainz release group's original-year resolution for the album axis."""

    album_title: str
    original_date: str
    release_group_id: str
    release_mbid: str | None


@dataclass(frozen=True, slots=True)
class MBRecording:
    """A MusicBrainz recording's resolved album for the review-only album-gaps tier.

    ``album_title`` is the release group's title (the proposed ``album`` fill);
    ``release_group_id`` and ``recording_mbid`` are carried for provenance/audit.
    """

    album_title: str
    release_group_id: str
    recording_mbid: str | None


class MusicBrainzError(RuntimeError):
    """A MusicBrainz lookup failed transiently (HTTP non-2xx).

    These are deliberately **not** cached so a re-run retries them.
    """


class MBAlbumSource(Protocol):
    """The album lookup the orchestrator depends on (so it can use a fake in tests).

    Returns an :class:`MBAlbum` when a usable Album release group is found, or ``None`` when
    nothing usable exists (genuinely no first-release year for the album axis to fill).
    """

    def album_first_release(self, artist: str, album: str) -> MBAlbum | None:
        """Return the album's original first-release resolution, or ``None`` if none usable."""


class MBRecordingSource(Protocol):
    """The recording lookup the album-gaps detector depends on (so it can use a fake in tests).

    Deliberately separate from :class:`MBAlbumSource`: an ``album_first_release``-only fake
    stays a valid ``MBAlbumSource`` without gaining a ``recording_search`` obligation. Returns
    an :class:`MBRecording` when a usable Album release group is found for the recording, or
    ``None`` when nothing usable exists.
    """

    def recording_search(self, artist: str, title: str) -> MBRecording | None:
        """Return the recording's resolved album, or ``None`` if none usable."""


class MusicBrainzClient:
    """Cached, paced MusicBrainz client for the album + recording endpoints.

    Implements both :class:`MBAlbumSource` (release-group year lookups) and
    :class:`MBRecordingSource` (``(artist, title)`` → album, the album-gaps review tier).

    Owns one :class:`httpx.Client` for its lifetime via the context-manager protocol; use it
    as ``with MusicBrainzClient(...) as client:``. The cache connection is supplied by the
    caller (the orchestrator owns it) and committed eagerly after each network fetch so an
    error later in a batch never loses prior cache work.
    """

    def __init__(  # noqa: PLR0913 - cohesive keyword-only injection seams for testing
        self,
        user_agent: str,
        conn: sqlite3.Connection,
        *,
        rate_per_sec: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Configure the client; injectables default to the real httpx transport + clock."""
        self._user_agent = user_agent
        self._conn = conn
        self._rate_per_sec = rate_per_sec
        self._transport = transport
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._client: httpx.Client | None = None

    # --- context manager: own one httpx.Client for the client's lifetime -------------

    def __enter__(self) -> Self:
        """Open the underlying :class:`httpx.Client` with the mandatory User-Agent."""
        self._client = httpx.Client(
            transport=self._transport,
            timeout=30.0,
            headers={"User-Agent": self._user_agent},
        )
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

    def album_first_release(self, artist: str, album: str) -> MBAlbum | None:
        """Return *album* by *artist*'s original first-release resolution, or ``None``.

        Cache first (positive or negative), else one paced network query. Raises
        :class:`MusicBrainzError` on a transient failure (caches nothing).
        """
        request_key = _request_key(artist, album)

        cached = get_cached_mb_album(self._conn, request_key)
        if cached is not None:
            found, row = cached
            if not found or row.original_date is None or row.album_title is None:
                return None
            return MBAlbum(
                album_title=row.album_title,
                original_date=row.original_date,
                release_group_id=row.release_group_id or "",
                release_mbid=row.release_mbid,
            )

        return self._fetch_and_cache(artist, album, request_key)

    def recording_search(self, artist: str, title: str) -> MBRecording | None:
        """Return the recording *title* by *artist*'s resolved album, or ``None``.

        Cache first (positive or negative) in ``musicbrainz_recording_cache``, else one paced
        network query against the recording endpoint. Raises :class:`MusicBrainzError` on a
        transient failure (caches nothing). Review-only: the caller never auto-stages the result.
        """
        request_key = _recording_request_key(artist, title)

        cached = get_cached_mb_recording(self._conn, request_key)
        if cached is not None:
            found, row = cached
            if not found or row.album_title is None:
                return None
            return MBRecording(
                album_title=row.album_title,
                release_group_id=row.release_group_id or "",
                recording_mbid=row.recording_mbid,
            )

        return self._fetch_and_cache_recording(artist, title, request_key)

    # --- internals -------------------------------------------------------------------

    def _fetch_and_cache(self, artist: str, album: str, request_key: str) -> MBAlbum | None:
        """Fetch one release-group query over the network (paced), then cache eagerly."""
        # Input: one paced network request.
        body = self._request(artist, album)

        # Process: pick the best usable Album release group (or None).
        resolved = _select_album(body)

        # Output: cache the result eagerly, then return it.
        if resolved is None:
            self._store_negative(request_key)
            return None
        self._store_positive(request_key, resolved)
        return resolved

    def _request(self, artist: str, album: str) -> dict[str, object]:
        """Pace, then perform one MusicBrainz GET, returning the decoded JSON object.

        Raises :class:`MusicBrainzError` on an HTTP non-2xx status (transient).
        """
        if self._client is None:  # pragma: no cover - guard against misuse outside `with`
            message = "MusicBrainzClient must be used as a context manager"
            raise RuntimeError(message)

        query = f'artist:"{_escape(artist)}" AND releasegroup:"{_escape(album)}"'
        params = {"query": query, "fmt": "json"}
        self._pace()
        logger.debug("musicbrainz request artist=%r album=%r", artist, album)
        response = self._client.get(_API_URL, params=params)
        if response.is_error:
            message = f"MusicBrainz HTTP {response.status_code} for release-group query"
            raise MusicBrainzError(message)
        return cast("dict[str, object]", response.json())

    def _store_negative(self, request_key: str) -> None:
        """Negative-cache a no-match and commit immediately."""
        put_cached_mb_album(
            self._conn,
            request_key=request_key,
            found=False,
            album_title=None,
            original_date=None,
            release_mbid=None,
            release_group_id=None,
            now=_utc_now(),
        )
        self._conn.commit()

    def _store_positive(self, request_key: str, album: MBAlbum) -> None:
        """Positive-cache a resolved album and commit immediately."""
        put_cached_mb_album(
            self._conn,
            request_key=request_key,
            found=True,
            album_title=album.album_title,
            original_date=album.original_date,
            release_mbid=album.release_mbid,
            release_group_id=album.release_group_id,
            now=_utc_now(),
        )
        self._conn.commit()

    def _fetch_and_cache_recording(
        self,
        artist: str,
        title: str,
        request_key: str,
    ) -> MBRecording | None:
        """Fetch one recording query over the network (paced), then cache eagerly."""
        # Input: one paced network request.
        body = self._request_recording(artist, title)

        # Process: pick the best recording whose release group is a usable Album (or None).
        resolved = _select_recording(body)

        # Output: cache the result eagerly, then return it.
        if resolved is None:
            self._store_negative_recording(request_key)
            return None
        self._store_positive_recording(request_key, resolved)
        return resolved

    def _request_recording(self, artist: str, title: str) -> dict[str, object]:
        """Pace, then perform one MusicBrainz recording GET, returning the decoded JSON.

        Raises :class:`MusicBrainzError` on an HTTP non-2xx status (transient).
        """
        if self._client is None:  # pragma: no cover - guard against misuse outside `with`
            message = "MusicBrainzClient must be used as a context manager"
            raise RuntimeError(message)

        query = f'artist:"{_escape(artist)}" AND recording:"{_escape(title)}"'
        params = {"query": query, "fmt": "json"}
        self._pace()
        logger.debug("musicbrainz recording request artist=%r title=%r", artist, title)
        response = self._client.get(_RECORDING_API_URL, params=params)
        if response.is_error:
            message = f"MusicBrainz HTTP {response.status_code} for recording query"
            raise MusicBrainzError(message)
        return cast("dict[str, object]", response.json())

    def _store_negative_recording(self, request_key: str) -> None:
        """Negative-cache a recording no-match and commit immediately."""
        put_cached_mb_recording(
            self._conn,
            request_key=request_key,
            found=False,
            album_title=None,
            release_group_id=None,
            recording_mbid=None,
            now=_utc_now(),
        )
        self._conn.commit()

    def _store_positive_recording(self, request_key: str, recording: MBRecording) -> None:
        """Positive-cache a resolved recording and commit immediately."""
        put_cached_mb_recording(
            self._conn,
            request_key=request_key,
            found=True,
            album_title=recording.album_title,
            release_group_id=recording.release_group_id,
            recording_mbid=recording.recording_mbid,
            now=_utc_now(),
        )
        self._conn.commit()

    def _pace(self) -> None:
        """Sleep just enough so consecutive network requests honor ``rate_per_sec``.

        Uses the injected ``monotonic``/``sleep`` so tests assert pacing without waiting.
        ``rate_per_sec <= 0`` disables pacing.
        """
        if self._rate_per_sec > 0 and self._last_request_at is not None:
            interval = 1.0 / self._rate_per_sec
            elapsed = self._monotonic() - self._last_request_at
            remaining = interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._monotonic()


# --- module helpers ------------------------------------------------------------------


def _request_key(artist: str, album: str) -> str:
    """Return a stable ``sha1`` over the artist+album identifying the lookup."""
    payload = "\x00".join(["release-group", f"artist={artist}", f"album={album}"])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324 - cache key, not security


def _escape(value: str) -> str:
    """Escape Lucene special characters that would break the quoted query phrase."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _select_album(body: dict[str, object]) -> MBAlbum | None:
    """Pick the best usable Album release group from a release-group query response.

    Keeps only ``primary-type == "Album"`` groups with no ``Live``/``Compilation``
    secondary type and a non-empty ``first-release-date``; returns the highest-scoring one
    (``None`` when nothing usable). The year is normalized to the four-digit prefix.
    """
    raw_groups = body.get("release-groups")
    if not isinstance(raw_groups, list):
        return None

    best: MBAlbum | None = None
    best_score = -1
    for entry in raw_groups:
        candidate = _candidate(entry)
        if candidate is None:
            continue
        score = candidate[0]
        if score > best_score:
            best_score = score
            best = candidate[1]
    return best


def _candidate(entry: object) -> tuple[int, MBAlbum] | None:
    """Return ``(score, MBAlbum)`` for a usable Album release group, or ``None``."""
    if not isinstance(entry, dict):
        return None
    if entry.get("primary-type") != "Album":
        return None

    secondary = entry.get("secondary-types")
    if isinstance(secondary, list) and any(s in _EXCLUDED_SECONDARY_TYPES for s in secondary):
        return None

    raw_date = entry.get("first-release-date")
    if not isinstance(raw_date, str) or not raw_date:
        return None
    prefix = raw_date[:_YEAR_PREFIX_LEN]
    original_date = prefix if len(raw_date) >= _YEAR_PREFIX_LEN and prefix.isdigit() else raw_date

    title = entry.get("title")
    if not isinstance(title, str) or not title:
        return None

    raw_score = entry.get("score")
    score = raw_score if isinstance(raw_score, int) else 0

    rgid = entry.get("id")
    release_group_id = rgid if isinstance(rgid, str) else ""
    return (
        score,
        MBAlbum(
            album_title=title,
            original_date=original_date,
            release_group_id=release_group_id,
            release_mbid=_first_release_mbid(entry),
        ),
    )


def _recording_request_key(artist: str, title: str) -> str:
    """Return a stable ``sha1`` over the artist+title identifying the recording lookup."""
    payload = "\x00".join(["recording", f"artist={artist}", f"title={title}"])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324 - cache key, not security


def _select_recording(body: dict[str, object]) -> MBRecording | None:
    """Pick the best recording whose release group is a usable Album from a search response.

    Keeps only recordings that have a release whose release group is ``primary-type ==
    "Album"`` with no ``Live``/``Compilation`` secondary type; returns the highest-scoring
    one's release-group title (+ ids), or ``None`` when nothing usable exists.
    """
    raw_recordings = body.get("recordings")
    if not isinstance(raw_recordings, list):
        return None

    best: MBRecording | None = None
    best_score = -1
    for entry in raw_recordings:
        candidate = _recording_candidate(entry)
        if candidate is None:
            continue
        score = candidate[0]
        if score > best_score:
            best_score = score
            best = candidate[1]
    return best


def _recording_candidate(entry: object) -> tuple[int, MBRecording] | None:
    """Return ``(score, MBRecording)`` for a recording with a usable Album release, or ``None``."""
    if not isinstance(entry, dict):
        return None

    release_group = _usable_release_group(entry)
    if release_group is None:
        return None
    title, release_group_id = release_group

    raw_score = entry.get("score")
    score = raw_score if isinstance(raw_score, int) else 0

    rec_id = entry.get("id")
    recording_mbid = rec_id if isinstance(rec_id, str) and rec_id else None
    return (
        score,
        MBRecording(
            album_title=title,
            release_group_id=release_group_id,
            recording_mbid=recording_mbid,
        ),
    )


def _usable_release_group(entry: dict[str, object]) -> tuple[str, str] | None:
    """Return ``(title, release_group_id)`` for the first usable Album release, or ``None``.

    Scans the recording's ``releases``; a release's ``release-group`` qualifies when its
    ``primary-type == "Album"``, it carries no ``Live``/``Compilation`` secondary type, and it
    has a non-empty title (mirroring :func:`_candidate`'s Album gate).
    """
    releases = entry.get("releases")
    if not isinstance(releases, list):
        return None
    for release in releases:
        if not isinstance(release, dict):
            continue
        group = release.get("release-group")
        if not isinstance(group, dict):
            continue
        if group.get("primary-type") != "Album":
            continue
        secondary = group.get("secondary-types")
        if isinstance(secondary, list) and any(s in _EXCLUDED_SECONDARY_TYPES for s in secondary):
            continue
        title = group.get("title")
        if not isinstance(title, str) or not title:
            continue
        rgid = group.get("id")
        release_group_id = rgid if isinstance(rgid, str) else ""
        return (title, release_group_id)
    return None


def _first_release_mbid(entry: dict[str, object]) -> str | None:
    """Return the first release's MBID from a release group's ``releases`` list, if any."""
    releases = entry.get("releases")
    if not isinstance(releases, list) or not releases:
        return None
    first = releases[0]
    if isinstance(first, dict):
        rid = first.get("id")
        if isinstance(rid, str) and rid:
            return rid
    return None


def _utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string (the engine's timestamp form)."""
    return datetime.now(UTC).isoformat()
