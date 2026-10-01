"""Use an injected lookup client, or build and own a real one."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager


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
