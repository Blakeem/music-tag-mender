"""Audio identification: fpcalc fingerprints and AcoustID lookups, each with a ledger cache.

The song axis identifies a file by its audio rather than its tags. :class:`Fingerprinter` runs
Chromaprint's ``fpcalc`` on one file, :class:`AcoustidClient` asks AcoustID which MusicBrainz
recordings carry that fingerprint, and two cache helper pairs keep both answers in the ledger:

* ``fingerprint_cache`` (:func:`get_fingerprint` / :func:`put_fingerprint`) holds one fpcalc
  outcome per file, reused while the files-row signature is unchanged. A failure is stored
  too, so an undecodable file does not re-run on every call. A timeout is never stored,
  because the next run may succeed.
* ``acoustid_cache`` (:func:`get_lookup` / :func:`put_lookup`) holds one lookup per request
  hash. An empty result is served for 7 days only, because AcoustID keeps learning new
  fingerprints. An error is never stored.

The helpers never commit: the caller owns the transaction. The API key travels in a POST body
over https and never in a URL, and the User-Agent names the project and never a person, so no
request line and no log record carries the key or personal data.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import shutil
import subprocess
import sys
import time
import urllib.parse
import zlib
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self, cast

import httpx

from tagmend.config import PROJECT_URL, build_user_agent
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence
    from types import TracebackType

    from tagmend.config import Settings

logger = get_logger(__name__)

_LOOKUP_URL: Final = "https://api.acoustid.org/v2/lookup"
_META: Final = "recordings releases tracks compress sources"
# Folded into the lookup cache key, so changing what the parser extracts re-fetches every lookup.
_LOOKUP_VERSION: Final = "2"
_EMPTY_TTL: Final = timedelta(days=7)
_USER_AGENT: Final = build_user_agent(PROJECT_URL)
_FORM_HEADERS: Final = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Content-Encoding": "gzip",
}

_RETRY_ATTEMPTS: Final = 3
_RETRY_BACKOFF_SECONDS: Final = 1.0
_HTTP_TOO_MANY_REQUESTS: Final = 429
_HTTP_SERVER_ERROR: Final = 500
_ERROR_INVALID_FINGERPRINT: Final = 3
_ERROR_INVALID_API_KEY: Final = 4
_KEY_SETTING: Final = "acoustid_api_key"

_FPCALC_NAME: Final = "fpcalc"
_FPCALC_LENGTH_SECONDS: Final = 120
_FPCALC_TIMEOUT_SECONDS: Final = 60.0
_FPCALC_VERSION_TIMEOUT_SECONDS: Final = 5.0
# fpcalc 1.5 and 1.6 exit 3 on a decode warning while still printing a usable fingerprint.
_FPCALC_OK_EXITS: Final = frozenset({0, 3})
# Windows refuses a path near MAX_PATH (260) unless it carries the extended-length prefix.
_LONG_PATH_THRESHOLD: Final = 240
_LONG_PATH_PREFIX: Final = "\\\\?\\"
_LONG_UNC_PREFIX: Final = "\\\\?\\UNC\\"
_UNC_PREFIX: Final = "\\\\"
# A console-less MCP server would otherwise flash a console window for every fpcalc run.
_NO_WINDOW: Final[int] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

type FpcalcRunner = Callable[[Sequence[str], float], tuple[int, str, str]]


# --- results ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """One file's Chromaprint fingerprint and its duration in whole seconds."""

    fingerprint: str
    duration: int


@dataclass(frozen=True, slots=True)
class StoredFingerprint:
    """One ``fingerprint_cache`` row: a fingerprint, or a stored failure when it is ``None``."""

    fpcalc_exit: int
    fingerprint: Fingerprint | None
    fingerprinted_at: str


@dataclass(frozen=True, slots=True)
class AcoustidReleaseRef:
    """One slot a recording occupies on a release: the release, its medium and its track.

    ``date`` is ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD``. Every field AcoustID omitted is
    ``None``. ``release_track_count`` counts the whole release, ``medium_track_count`` one disc.
    """

    id: str
    title: str | None
    country: str | None
    date: str | None
    medium_position: int | None
    medium_track_count: int | None
    track_position: int | None
    track_id: str | None
    release_track_count: int | None
    medium_count: int | None


@dataclass(frozen=True, slots=True)
class AcoustidArtist:
    """One credited artist of a recording. ``joinphrase`` joins it to the next credited name."""

    id: str
    name: str
    joinphrase: str


@dataclass(frozen=True, slots=True)
class AcoustidRecording:
    """One MusicBrainz recording linked to a fingerprint. ``title`` is empty when unknown.

    ``artists`` is the recording's artist credit in order, empty when AcoustID gave none.
    """

    id: str
    title: str
    duration: int | None
    sources: int
    releases: tuple[AcoustidReleaseRef, ...]
    artists: tuple[AcoustidArtist, ...]


@dataclass(frozen=True, slots=True)
class AcoustidMatch:
    """One AcoustID fingerprint match and the recordings linked to it."""

    score: float
    recordings: tuple[AcoustidRecording, ...]


@dataclass(frozen=True, slots=True)
class AcoustidResult:
    """Every match AcoustID returned for one lookup. Empty means no match."""

    results: tuple[AcoustidMatch, ...] = ()


# --- errors ----------------------------------------------------------------------------


class FingerprintError(RuntimeError):
    """fpcalc failed on one file. The same file fails the same way, so callers may store it."""

    def __init__(self, exit_code: int | None, message: str) -> None:
        """Keep the fpcalc *exit_code* (``None`` when it never exited) beside *message*."""
        super().__init__(message)
        self.exit_code = exit_code


class FingerprintTimeout(FingerprintError):  # noqa: N818 - the transient case of FingerprintError
    """fpcalc ran past its timeout. Transient, so callers never store it."""


class FingerprintUnreadableError(FingerprintError):
    """fpcalc failed on a file that could not be opened. Transient, so callers never store it."""


class FpcalcUnavailableError(RuntimeError):
    """fpcalc itself cannot run. Says nothing about any one file, so callers never store it."""


class AcoustidError(RuntimeError):
    """An AcoustID lookup failed. Never cached, so a re-run retries it."""


class AcoustidKeyError(AcoustidError):
    """AcoustID rejected the configured API key."""


# --- fpcalc ----------------------------------------------------------------------------


def run_fpcalc(argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
    """Run *argv* and return its exit code, stdout and stderr.

    Raises :class:`subprocess.TimeoutExpired` past *timeout* and :class:`OSError` when the
    executable cannot start.
    """
    completed = subprocess.run(  # noqa: S603 - argv is the resolved fpcalc binary and a file path, no shell
        list(argv),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=_NO_WINDOW,
    )
    return completed.returncode, completed.stdout, completed.stderr


def resolve_fpcalc(configured: str | None) -> str | None:
    """Return the fpcalc executable: *configured* when set, else ``fpcalc`` on PATH, else None."""
    if configured and configured.strip():
        return configured.strip()
    return shutil.which(_FPCALC_NAME)


class Fingerprinter:
    """Runs ``fpcalc -json -length 120`` on one file through an injectable runner."""

    def __init__(
        self,
        executable: str,
        *,
        runner: FpcalcRunner = run_fpcalc,
        is_windows: bool = sys.platform == "win32",
    ) -> None:
        """Use *executable* through *runner*. *is_windows* decides the long-path prefix."""
        self._executable = executable
        self._runner = runner
        self._is_windows = is_windows

    @classmethod
    def from_settings(cls, settings: Settings, *, runner: FpcalcRunner = run_fpcalc) -> Self:
        """Build a fingerprinter on ``settings.fpcalc_path``, else ``fpcalc`` on PATH.

        Raises :class:`FpcalcUnavailableError` when neither names an executable.
        """
        executable = resolve_fpcalc(settings.fpcalc_path)
        if executable is None:
            message = "fpcalc is not on PATH and fpcalc_path is not set"
            raise FpcalcUnavailableError(message)
        return cls(executable, runner=runner)

    def version(self) -> str:
        """Return what ``fpcalc -version`` prints, or raise :class:`FpcalcUnavailableError`."""
        argv = [self._executable, "-version"]
        try:
            exit_code, stdout, _stderr = self._runner(argv, _FPCALC_VERSION_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            message = f"could not run {self._executable} -version: {exc}"
            raise FpcalcUnavailableError(message) from exc
        if exit_code != 0:
            message = f"{self._executable} -version exited {exit_code}"
            raise FpcalcUnavailableError(message)
        return stdout.strip() or self._executable

    def fingerprint(self, path: Path) -> Fingerprint:
        """Return *path*'s fingerprint and whole-second duration.

        Raises :class:`FingerprintError` on an exit other than 0 or 3 or on unusable output,
        :class:`FingerprintUnreadableError` on such an exit when the file cannot be opened,
        :class:`FingerprintTimeout` past 60 s, and :class:`FpcalcUnavailableError` when the
        executable cannot start.
        """
        target = _fpcalc_path_arg(path, is_windows=self._is_windows)
        argv = [self._executable, "-json", "-length", str(_FPCALC_LENGTH_SECONDS), target]
        try:
            exit_code, stdout, stderr = self._runner(argv, _FPCALC_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            message = f"fpcalc timed out after {_FPCALC_TIMEOUT_SECONDS:g}s on {path}"
            raise FingerprintTimeout(None, message) from exc
        except OSError as exc:
            message = f"could not run {self._executable}: {exc}"
            raise FpcalcUnavailableError(message) from exc

        if exit_code not in _FPCALC_OK_EXITS:
            if not _is_readable(target):
                message = (
                    f"fpcalc exited {exit_code} on {path}, which could not be opened: "
                    f"{stderr.strip()}. Rescan the library, or reconnect its drive, then run again"
                )
                raise FingerprintUnreadableError(exit_code, message)
            message = f"fpcalc exited {exit_code} on {path}: {stderr.strip()}"
            raise FingerprintError(exit_code, message)
        return _parse_fpcalc_output(exit_code, stdout, path)


def _is_readable(path: str) -> bool:
    """Return whether *path* opens for reading, which tells a missing file from a bad one.

    fpcalc exits 2 both for a file it cannot open and for undecodable data.
    """
    try:
        with Path(path).open("rb"):
            return True
    except OSError:
        return False


def _fpcalc_path_arg(path: Path, *, is_windows: bool) -> str:
    """Return *path* as fpcalc's argument, extended-length prefixed when Windows needs it."""
    text = str(path.absolute())
    if not is_windows or len(text) <= _LONG_PATH_THRESHOLD or text.startswith(_LONG_PATH_PREFIX):
        return text
    if text.startswith(_UNC_PREFIX):
        return _LONG_UNC_PREFIX + text[len(_UNC_PREFIX) :]
    return _LONG_PATH_PREFIX + text


def _parse_fpcalc_output(exit_code: int, stdout: str, path: Path) -> Fingerprint:
    """Pull the fingerprint and duration out of ``fpcalc -json`` output."""
    try:
        data: object = json.loads(stdout)
    except ValueError as exc:
        message = f"fpcalc printed no JSON for {path}"
        raise FingerprintError(exit_code, message) from exc
    fields = cast("dict[str, object]", data) if isinstance(data, dict) else {}
    fingerprint = fields.get("fingerprint")
    duration = fields.get("duration")
    if not isinstance(fingerprint, str) or not fingerprint or not _is_finite_number(duration):
        message = f"fpcalc printed no fingerprint and duration for {path}"
        raise FingerprintError(exit_code, message)
    return Fingerprint(fingerprint=fingerprint, duration=int(cast("float", duration)))


def _is_finite_number(value: object) -> bool:
    """Return whether *value* is a finite JSON number (a bool is not one)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value)


# --- AcoustID --------------------------------------------------------------------------


class AcoustidClient:
    """Paced AcoustID lookup client. Use it as ``with AcoustidClient(...) as client:``.

    Owns one :class:`httpx.Client` for its lifetime. It caches nothing itself: the caller pairs
    it with :func:`get_lookup` and :func:`put_lookup`.
    """

    def __init__(  # noqa: PLR0913 - cohesive keyword-only injection seams for testing
        self,
        api_key: str,
        *,
        rate_per_sec: float = 2.0,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = _RETRY_ATTEMPTS,
    ) -> None:
        """Configure the client. The injectables default to the real transport and clock."""
        self._api_key = api_key
        self._rate_per_sec = rate_per_sec
        self._transport = transport
        self._monotonic = monotonic
        self._sleep = sleep
        # A readiness probe wants one attempt and a fast answer. A long sweep wants the retries.
        self._max_attempts = max(1, max_attempts)
        self._last_request_at: float | None = None
        self._client: httpx.Client | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build a client with the configured API key and request rate.

        Raises :class:`ValueError` when no API key is configured.
        """
        if not settings.acoustid_api_key:
            message = (
                "no AcoustID API key configured. Run `tagmend config`, or "
                "`tagmend config-set acoustid_api_key`, which prompts for the key without "
                "echoing it."
            )
            raise ValueError(message)
        return cls(settings.acoustid_api_key, rate_per_sec=settings.acoustid_rate_per_sec)

    def __enter__(self) -> Self:
        """Open the underlying :class:`httpx.Client` with the project User-Agent."""
        self._client = httpx.Client(
            transport=self._transport,
            timeout=30.0,
            headers={"User-Agent": _USER_AGENT},
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

    def lookup(self, fp: Fingerprint) -> AcoustidResult:
        """Return every recording AcoustID links to *fp*.

        An invalid fingerprint returns an empty result, since the same fingerprint always gets
        the same answer. Raises :class:`AcoustidKeyError` when AcoustID rejects the key and
        :class:`AcoustidError` on any other failure.
        """
        logger.debug("acoustid lookup duration=%d", fp.duration)
        response = self._post_with_backoff(_encode_form(self._api_key, fp))
        body = _decode_object(response)
        return _interpret(body, response.status_code)

    def _post_with_backoff(self, content: bytes) -> httpx.Response:
        """Pace, then POST *content*, retrying transport errors, 429 and 5xx with a backoff.

        The backoff doubles between attempts. After the last one the failure is raised as
        :class:`AcoustidError`, so it is reported and never cached.
        """
        if self._client is None:  # pragma: no cover - guard against misuse outside `with`
            message = "AcoustidClient must be used as a context manager"
            raise RuntimeError(message)

        delay = _RETRY_BACKOFF_SECONDS
        failure = ""
        cause: httpx.HTTPError | None = None
        for attempt in range(self._max_attempts):
            self._pace()
            try:
                response = self._client.post(_LOOKUP_URL, content=content, headers=_FORM_HEADERS)
            except httpx.HTTPError as exc:
                failure = f"transport failure ({type(exc).__name__})"
                cause = exc
            else:
                if not _is_retryable(response.status_code):
                    return response
                failure = f"HTTP {response.status_code}"
                cause = None
            logger.debug("acoustid %s, attempt %d of %d", failure, attempt + 1, self._max_attempts)
            if attempt < self._max_attempts - 1:
                self._sleep(delay)
                delay *= 2

        message = f"AcoustID {failure} after {self._max_attempts} attempt(s)"
        raise AcoustidError(message) from cause

    def _pace(self) -> None:
        """Sleep just enough so consecutive requests honor ``rate_per_sec``."""
        if self._rate_per_sec > 0 and self._last_request_at is not None:
            interval = 1.0 / self._rate_per_sec
            remaining = interval - (self._monotonic() - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._monotonic()


def _is_retryable(status_code: int) -> bool:
    """Return whether an HTTP status is a throttle or a server fault worth retrying."""
    return status_code == _HTTP_TOO_MANY_REQUESTS or status_code >= _HTTP_SERVER_ERROR


def _encode_form(api_key: str, fp: Fingerprint) -> bytes:
    """Return the gzip-compressed form body of one lookup."""
    form = urllib.parse.urlencode(
        {
            "client": api_key,
            "format": "json",
            "duration": str(fp.duration),
            "fingerprint": fp.fingerprint,
            "meta": _META,
        },
    )
    return gzip.compress(form.encode("ascii"))


def _decode_object(response: httpx.Response) -> dict[str, object]:
    """Decode a response body as a JSON object, or raise :class:`AcoustidError`."""
    try:
        body: object = response.json()
    except ValueError as exc:
        message = f"AcoustID returned a non-JSON body (HTTP {response.status_code})"
        raise AcoustidError(message) from exc
    if not isinstance(body, dict):
        message = f"AcoustID returned a JSON {type(body).__name__}, not an object"
        raise AcoustidError(message)
    return cast("dict[str, object]", body)


def _interpret(body: Mapping[str, object], status_code: int) -> AcoustidResult:
    """Turn a decoded lookup body into a result, or raise the error it reports."""
    status = body.get("status")
    if status == "ok":
        return _parse_response(body)
    if status != "error":
        message = f"AcoustID answered HTTP {status_code} with no lookup status"
        raise AcoustidError(message)

    error = body.get("error")
    details = cast("dict[str, object]", error) if isinstance(error, dict) else {}
    code = details.get("code")
    if code == _ERROR_INVALID_FINGERPRINT:
        return AcoustidResult()
    if code == _ERROR_INVALID_API_KEY:
        message = f"AcoustID rejected the API key. Check the {_KEY_SETTING} setting."
        raise AcoustidKeyError(message)
    message = f"AcoustID error {code}: {details.get('message', '')}"
    raise AcoustidError(message)


# --- response parsing ------------------------------------------------------------------


def _parse_response(body: Mapping[str, object]) -> AcoustidResult:
    """Parse a ``status: ok`` lookup body. Missing keys are tolerated, wrong types are not."""
    return AcoustidResult(
        results=tuple(
            AcoustidMatch(
                score=_number(match, "score", "result") or 0.0,
                recordings=tuple(
                    recording
                    for entry in _objects(match, "recordings", "result")
                    if (recording := _parse_recording(entry)) is not None
                ),
            )
            for match in _objects(body, "results", "response")
        ),
    )


def _parse_recording(entry: Mapping[str, object]) -> AcoustidRecording | None:
    """Parse one recording, or ``None`` when it carries no id to act on."""
    recording_id = _text(entry, "id", "recording")
    if not recording_id:
        return None
    return AcoustidRecording(
        id=recording_id,
        title=_text(entry, "title", "recording") or "",
        duration=_integer(entry, "duration", "recording"),
        sources=_integer(entry, "sources", "recording") or 0,
        releases=tuple(
            ref
            for release in _objects(entry, "releases", "recording")
            for ref in _parse_release_refs(release)
        ),
        artists=_parse_artists(entry, "recording"),
    )


def _parse_artists(entry: Mapping[str, object], where: str) -> tuple[AcoustidArtist, ...]:
    """Parse a recording's artist credit in order. A missing field reads as empty."""
    return tuple(
        AcoustidArtist(
            id=_text(credit, "id", where) or "",
            name=_text(credit, "name", where) or "",
            joinphrase=_text(credit, "joinphrase", where) or "",
        )
        for credit in _objects(entry, "artists", where)
    )


def _parse_release_refs(release: Mapping[str, object]) -> list[AcoustidReleaseRef]:
    """Flatten one release into a ref per (medium, track) the recording occupies on it."""
    release_id = _text(release, "id", "release")
    if not release_id:
        return []
    bare = AcoustidReleaseRef(
        id=release_id,
        title=_text(release, "title", "release"),
        country=_text(release, "country", "release"),
        date=_date(release),
        medium_position=None,
        medium_track_count=None,
        track_position=None,
        track_id=None,
        release_track_count=_integer(release, "track_count", "release"),
        medium_count=_integer(release, "medium_count", "release"),
    )
    refs: list[AcoustidReleaseRef] = []
    for medium in _objects(release, "mediums", "release"):
        on_medium = replace(
            bare,
            medium_position=_integer(medium, "position", "medium"),
            medium_track_count=_integer(medium, "track_count", "medium"),
        )
        tracks = _objects(medium, "tracks", "medium")
        refs.extend(
            replace(
                on_medium,
                track_position=_integer(track, "position", "track"),
                track_id=_text(track, "id", "track"),
            )
            for track in tracks
        )
        if not tracks:
            refs.append(on_medium)
    return refs or [bare]


def _date(release: Mapping[str, object]) -> str | None:
    """Return a release's ``date`` object as ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD``."""
    raw = release.get("date")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        error = _wrong_type("release", "date", raw)
        raise error
    parts = cast("dict[str, object]", raw)
    year = _integer(parts, "year", "date")
    if year is None:
        return None
    month = _integer(parts, "month", "date")
    day = _integer(parts, "day", "date")
    text = f"{year:04d}"
    if month is not None:
        text += f"-{month:02d}"
        if day is not None:
            text += f"-{day:02d}"
    return text


def _objects(entry: Mapping[str, object], key: str, where: str) -> list[Mapping[str, object]]:
    """Return *entry*'s list of objects under *key*, empty when absent."""
    raw = entry.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _wrong_type(where, key, raw)
    items = cast("list[object]", raw)
    for item in items:
        if not isinstance(item, dict):
            raise _wrong_type(where, key, item)
    return cast("list[Mapping[str, object]]", items)


def _text(entry: Mapping[str, object], key: str, where: str) -> str | None:
    """Return *entry*'s string under *key*, ``None`` when absent."""
    raw = entry.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _wrong_type(where, key, raw)
    return raw


def _number(entry: Mapping[str, object], key: str, where: str) -> float | None:
    """Return *entry*'s finite number under *key*, ``None`` when absent."""
    raw = entry.get(key)
    if raw is None:
        return None
    if not _is_finite_number(raw):
        raise _wrong_type(where, key, raw)
    return float(cast("float", raw))


def _integer(entry: Mapping[str, object], key: str, where: str) -> int | None:
    """Return *entry*'s number under *key* as an int, ``None`` when absent."""
    number = _number(entry, key, where)
    return None if number is None else int(number)


def _wrong_type(where: str, key: str, value: object) -> AcoustidError:
    """Build the error for a field whose JSON type is not the one AcoustID documents."""
    return AcoustidError(f"AcoustID returned a {type(value).__name__} for {where} {key}")


# --- fingerprint_cache -----------------------------------------------------------------


def get_fingerprint(
    conn: sqlite3.Connection,
    file_id: int,
    size_bytes: int,
    mtime_ns: int,
) -> StoredFingerprint | None:
    """Return *file_id*'s stored fpcalc outcome when it was taken at this signature, else None."""
    row = conn.execute(
        """
        SELECT fpcalc_exit, fingerprint, duration, fingerprinted_at FROM fingerprint_cache
        WHERE file_id = ? AND size_bytes = ? AND mtime_ns = ?
        """,
        (file_id, size_bytes, mtime_ns),
    ).fetchone()
    if row is None:
        return None
    fpcalc_exit, fingerprint, duration, fingerprinted_at = row
    stored = (
        None
        if fingerprint is None or duration is None
        else Fingerprint(fingerprint=str(fingerprint), duration=int(duration))
    )
    return StoredFingerprint(
        fpcalc_exit=int(fpcalc_exit),
        fingerprint=stored,
        fingerprinted_at=str(fingerprinted_at),
    )


def put_fingerprint(  # noqa: PLR0913 - one keyword per stored column, cohesive by design
    conn: sqlite3.Connection,
    file_id: int,
    size_bytes: int,
    mtime_ns: int,
    *,
    fpcalc_exit: int,
    fingerprint: Fingerprint | None,
    now: datetime,
) -> None:
    """Store *file_id*'s fpcalc outcome at this signature, replacing any older one.

    A success carries *fingerprint* with exit 0 or 3. A stored failure passes ``None`` and
    the failing exit. A timeout has no exit code, so it cannot be stored.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO fingerprint_cache
          (file_id, size_bytes, mtime_ns, fpcalc_exit, fingerprint, duration, fingerprinted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            size_bytes,
            mtime_ns,
            fpcalc_exit,
            None if fingerprint is None else fingerprint.fingerprint,
            None if fingerprint is None else fingerprint.duration,
            now.isoformat(),
        ),
    )


# --- acoustid_cache --------------------------------------------------------------------


def get_lookup(conn: sqlite3.Connection, fp: Fingerprint, now: datetime) -> AcoustidResult | None:
    """Return the cached lookup for *fp*, or ``None`` on a miss.

    An empty result is served only while younger than 7 days. An unreadable row is a miss,
    so the next lookup overwrites it.
    """
    row = conn.execute(
        "SELECT found, payload, fetched_at FROM acoustid_cache WHERE request_key = ?",
        (_lookup_key(fp),),
    ).fetchone()
    if row is None:
        return None
    found, payload, fetched_at = row
    if not found:
        age = now - datetime.fromisoformat(str(fetched_at))
        return AcoustidResult() if age < _EMPTY_TTL else None
    try:
        return _result_from_payload(bytes(payload))
    except (zlib.error, ValueError, AcoustidError) as exc:
        logger.warning("unreadable acoustid_cache row, treating it as a miss: %s", exc)
        return None


def put_lookup(
    conn: sqlite3.Connection,
    fp: Fingerprint,
    result: AcoustidResult,
    now: datetime,
) -> None:
    """Store *result* as the lookup for *fp*, replacing any older one."""
    found = bool(result.results)
    payload = zlib.compress(_result_to_json(result).encode("utf-8")) if found else None
    conn.execute(
        """
        INSERT OR REPLACE INTO acoustid_cache (request_key, found, payload, fetched_at)
        VALUES (?, ?, ?, ?)
        """,
        (_lookup_key(fp), 1 if found else 0, payload, now.isoformat()),
    )


def _lookup_key(fp: Fingerprint) -> str:
    """Return the cache key: a hash of everything that decides what a lookup answers."""
    payload = "\x00".join([fp.fingerprint, str(fp.duration), _META, _LOOKUP_VERSION])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _result_to_json(result: AcoustidResult) -> str:
    """Serialize a parsed result for the cache payload."""
    return json.dumps(asdict(result), separators=(",", ":"))


def _result_from_payload(payload: bytes) -> AcoustidResult:
    """Rebuild a parsed result from its compressed cache payload."""
    data: object = json.loads(zlib.decompress(payload))
    if not isinstance(data, dict):
        error = _wrong_type("cache", "payload", data)
        raise error
    body = cast("dict[str, object]", data)
    return AcoustidResult(
        results=tuple(
            AcoustidMatch(
                score=_number(match, "score", "cached result") or 0.0,
                recordings=tuple(
                    _recording_from_json(entry)
                    for entry in _objects(match, "recordings", "cached result")
                ),
            )
            for match in _objects(body, "results", "cache")
        ),
    )


def _recording_from_json(entry: Mapping[str, object]) -> AcoustidRecording:
    """Rebuild one cached recording."""
    where = "cached recording"
    return AcoustidRecording(
        id=_text(entry, "id", where) or "",
        title=_text(entry, "title", where) or "",
        duration=_integer(entry, "duration", where),
        sources=_integer(entry, "sources", where) or 0,
        releases=tuple(
            AcoustidReleaseRef(
                id=_text(ref, "id", where) or "",
                title=_text(ref, "title", where),
                country=_text(ref, "country", where),
                date=_text(ref, "date", where),
                medium_position=_integer(ref, "medium_position", where),
                medium_track_count=_integer(ref, "medium_track_count", where),
                track_position=_integer(ref, "track_position", where),
                track_id=_text(ref, "track_id", where),
                release_track_count=_integer(ref, "release_track_count", where),
                medium_count=_integer(ref, "medium_count", where),
            )
            for ref in _objects(entry, "releases", where)
        ),
        artists=_parse_artists(entry, where),
    )
