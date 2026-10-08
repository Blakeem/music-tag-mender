"""The lookup clients' shared plumbing: the injected-or-owned seam, paced HTTP and JSON decoding."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self, cast

import httpx

from tagmend.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from contextlib import AbstractContextManager
    from types import TracebackType

logger = get_logger(__name__)

RETRY_ATTEMPTS: Final = 3
_RETRY_BACKOFF_SECONDS: Final = 1.0
_TIMEOUT_SECONDS: Final = 30.0
_HTTP_TOO_MANY_REQUESTS: Final = 429
_HTTP_SERVER_ERROR: Final = 500


@contextmanager
def injected_or_owned[T](
    injected: T | None,
    build: Callable[[], AbstractContextManager[T]],
) -> Iterator[T]:
    """Yield *injected* when it is given, else enter ``build()`` and yield the client it owns.

    *build* runs only when nothing was injected, so a test fake never needs real settings.
    """
    if injected is not None:
        yield injected
        return
    with build() as owned:
        yield owned


@dataclass(frozen=True, slots=True)
class Retry:
    """A temporary answer: the request is sent again after a backoff, and *reason* names it."""

    reason: str


def retry_throttle_or_server_error(response: httpx.Response) -> httpx.Response | Retry:
    """Retry a throttle (HTTP 429) or a server fault (HTTP 5xx), else hand the response back."""
    status = response.status_code
    if status == _HTTP_TOO_MANY_REQUESTS or status >= _HTTP_SERVER_ERROR:
        return Retry(f"HTTP {status}")
    return response


class PacedHttp:
    """One :class:`httpx.Client` whose requests are paced to ``rate_per_sec`` and retried.

    Use it as ``with PacedHttp(...) as http:``. ``rate_per_sec <= 0`` disables pacing. The
    transport, clock and sleep are injectable, so tests need no network and no real waiting.
    """

    def __init__(  # noqa: PLR0913 - cohesive keyword-only injection seams for testing
        self,
        *,
        rate_per_sec: float,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = RETRY_ATTEMPTS,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Configure the transport. The injectables default to the real transport and clock."""
        self._rate_per_sec = rate_per_sec
        self._transport = transport
        self._monotonic = monotonic
        self._sleep = sleep
        self._max_attempts = max(1, max_attempts)
        self._headers = dict(headers or {})
        self._slot_lock = threading.Lock()
        self._last_slot: float | None = None
        self._client: httpx.Client | None = None

    def __enter__(self) -> Self:
        """Open the underlying :class:`httpx.Client`."""
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the underlying :class:`httpx.Client`."""
        self.close()

    def open(self) -> None:
        """Open the underlying :class:`httpx.Client`, for an owner that is a context manager."""
        self._client = httpx.Client(
            transport=self._transport,
            timeout=_TIMEOUT_SECONDS,
            headers=self._headers,
        )

    def close(self) -> None:
        """Close the underlying :class:`httpx.Client` when it is open."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def send[T](
        self,
        request: Callable[[httpx.Client], httpx.Response],
        *,
        verdict: Callable[[httpx.Response], T | Retry],
        error: Callable[[str, int], Exception],
        label: str,
    ) -> T:
        """Pace, then send *request*, retrying a transport error or a :class:`Retry` verdict.

        The backoff doubles between attempts. Any other verdict, or an exception it raises, ends
        the loop at once. After the last attempt ``error(failure, attempts)`` is raised, chained
        to the last transport error. *label* names the lookup in debug logs and carries no key.
        """
        if self._client is None:  # pragma: no cover - guard against misuse outside `with`
            message = "PacedHttp must be used as a context manager"
            raise RuntimeError(message)

        client = self._client
        delay = _RETRY_BACKOFF_SECONDS
        failure = "no attempt made"
        cause: httpx.HTTPError | None = None

        for attempt in range(1, self._max_attempts + 1):
            self._pace()
            try:
                response = request(client)
            except httpx.HTTPError as exc:
                # A dropped connection or a timeout is as transient as a throttle answer, and
                # letting it escape aborts a whole sweep over one lookup.
                cause = exc
                failure = f"transport failure ({type(exc).__name__})"
            else:
                cause = None
                answer = verdict(response)
                if not isinstance(answer, Retry):
                    return answer
                failure = answer.reason
            logger.debug("%s %s, attempt %d of %d", label, failure, attempt, self._max_attempts)
            if attempt < self._max_attempts:
                self._sleep(delay)
                delay *= 2

        raise error(failure, self._max_attempts) from cause

    def _pace(self) -> None:
        """Wait for this request's send slot, one interval after the last slot reserved.

        The slot is reserved under the lock and slept for outside it, so two threads never send
        closer together than ``1 / rate_per_sec`` and neither holds the lock while it waits.
        """
        if self._rate_per_sec <= 0:
            return

        interval = 1.0 / self._rate_per_sec
        with self._slot_lock:
            now = self._monotonic()
            slot = now if self._last_slot is None else max(now, self._last_slot + interval)
            self._last_slot = slot

        if slot > now:
            self._sleep(slot - now)


def decode_object(
    response: httpx.Response,
    error_type: type[Exception],
    *,
    source: str,
    what: str,
) -> dict[str, object]:
    """Decode *response* as a JSON object, or raise *error_type* naming *source* and *what*.

    A proxy error page or a truncated response arrives as a 200 too, and must fail as one
    retryable lookup rather than as an unrelated ``ValueError`` that aborts the run.
    """
    try:
        body: object = response.json()
    except ValueError as exc:
        message = f"{source} returned a non-JSON body for {what} (HTTP {response.status_code})"
        raise error_type(message) from exc
    if not isinstance(body, dict):
        message = f"{source} returned a JSON {type(body).__name__}, not an object, for {what}"
        raise error_type(message)
    return cast("dict[str, object]", body)
