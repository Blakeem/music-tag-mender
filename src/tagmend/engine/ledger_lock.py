"""The ledger-wide mutation lock, which admits one mutating TagMend call at a time per ledger.

Two processes changing one ledger race each other: a second commit would mark a live one
``interrupted`` and walk its staged rows. The lock is an OS lock on ``<db_path>.lock``. The OS
frees it when the holding process dies, so no lease or liveness state is stored.
"""

from __future__ import annotations

import functools
import inspect
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from tagmend.log import get_logger

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from tagmend.config import Settings

logger = get_logger(__name__)

_LOCK_SUFFIX: Final = ".lock"
_LOCK_FILE_MODE: Final = 0o644


class LedgerBusyError(ValueError):
    """Another process, or another thread of this one, holds the ledger's mutation lock."""

    def __init__(self, db_path: Path) -> None:
        """Name the busy ledger at *db_path* and tell the caller to retry."""
        super().__init__(
            f"the ledger {db_path} is busy: another TagMend process or call is changing it. "
            "Retry when it finishes",
        )


@dataclass(slots=True)
class _Hold:
    """This process's hold on one lock file: the owning thread, its nesting depth and the fd."""

    owner: int
    depth: int
    fd: int


_HOLDS: Final[dict[Path, _Hold]] = {}
_HOLDS_GUARD: Final = threading.Lock()


def lock_path(db_path: Path) -> Path:
    """Return the lock file of the ledger at *db_path*. It sits beside the ledger."""
    resolved = db_path.resolve()
    return resolved.with_name(resolved.name + _LOCK_SUFFIX)


def _os_lock(path: Path) -> int | None:
    """Take the OS lock on *path* without waiting and return its fd, or ``None`` when held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, _LOCK_FILE_MODE)
    try:
        if sys.platform == "win32":
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (PermissionError, BlockingIOError):
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        raise
    return fd


def _os_unlock(fd: int) -> None:
    """Drop the OS lock held through *fd* and close it."""
    try:
        if sys.platform == "win32":
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _acquire(db_path: Path) -> Path:
    """Take or re-enter the mutation lock of *db_path* for this thread and return its lock file."""
    path = lock_path(db_path)
    thread_id = threading.get_ident()
    with _HOLDS_GUARD:
        hold = _HOLDS.get(path)
        if hold is not None and hold.owner == thread_id:
            hold.depth += 1
            return path
        fd = None if hold is not None else _os_lock(path)
        if fd is None:
            raise LedgerBusyError(db_path)
        _HOLDS[path] = _Hold(owner=thread_id, depth=1, fd=fd)
    logger.debug("took the mutation lock %s", path)
    return path


def _release(path: Path) -> None:
    """Leave one level of this thread's hold on *path*, dropping the OS lock at the outermost."""
    with _HOLDS_GUARD:
        hold = _HOLDS[path]
        hold.depth -= 1
        if hold.depth > 0:
            return
        del _HOLDS[path]
        _os_unlock(hold.fd)
    logger.debug("released the mutation lock %s", path)


@contextmanager
def mutation_lock(settings: Settings) -> Iterator[None]:
    """Hold the mutation lock of the ledger *settings* names for the block.

    It is re-entrant within one thread, so a mutating entry point that calls another never
    refuses itself. Raises :class:`LedgerBusyError` at once when another process or thread holds
    it.
    """
    path = _acquire(settings.db_path)
    try:
        yield
    finally:
        _release(path)


def held_elsewhere(db_path: Path) -> bool:
    """Whether another process, or another thread of this one, holds the lock of *db_path*.

    The probe takes and drops the OS lock, so it never waits.
    """
    path = lock_path(db_path)
    with _HOLDS_GUARD:
        hold = _HOLDS.get(path)
        if hold is not None:
            return hold.owner != threading.get_ident()
        fd = _os_lock(path)
        if fd is None:
            return True
        _os_unlock(fd)
    return False


def mutating[**P, R](entry: Callable[P, R]) -> Callable[P, R]:
    """Hold :func:`mutation_lock` for the whole run of the mutating engine entry point *entry*.

    *entry* takes ``settings``, which names the ledger. A ``dry_run=True`` call changes nothing,
    so it runs unlocked.
    """
    signature = inspect.signature(entry)
    if "settings" not in signature.parameters:
        message = f"{entry.__qualname__} takes no settings, so it names no ledger to lock"
        raise TypeError(message)

    @functools.wraps(entry)
    def locked(*args: P.args, **kwargs: P.kwargs) -> R:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        if bound.arguments.get("dry_run"):
            return entry(*args, **kwargs)
        with mutation_lock(bound.arguments["settings"]):
            return entry(*args, **kwargs)

    return locked
