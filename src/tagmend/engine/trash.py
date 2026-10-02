"""Send a file to the OS trash, so a removal TagMend makes stays visible and restorable.

Windows deletes a file for good, and reports no error, when its volume has no Recycle Bin. A
network share, a removable drive and a UNC path have none, so :func:`send_to_trash` accepts only a
fixed local drive on Windows.
"""

from __future__ import annotations

import ctypes
import sys
from typing import TYPE_CHECKING, Final

import send2trash

from tagmend.log import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

DRIVE_FIXED: Final = 3
_DRIVE_NAMES: Final = {
    0: "unknown",
    1: "no root directory",
    2: "removable",
    4: "network",
    5: "optical",
    6: "RAM disk",
}
_UNC_PREFIX: Final = "\\\\"


class TrashUnavailableError(OSError):
    """The OS trash cannot take a file, which therefore stays where it is."""


def _drive_type(root: str) -> int:
    """Return the ``GetDriveTypeW`` code of the Windows volume whose root is *root*."""
    if sys.platform != "win32":  # pragma: no cover - only Windows probes its volumes
        message = f"the drive probe runs on Windows only, not for {root}"
        raise TrashUnavailableError(message)
    return int(ctypes.windll.kernel32.GetDriveTypeW(root))


def _windows_refusal(path: Path) -> str | None:
    """Return why Windows would delete *path* for good instead of trashing it, else ``None``."""
    if path.drive.startswith(_UNC_PREFIX):
        return f"{path} is a UNC or device path, which has no Recycle Bin"
    drive_type = _drive_type(path.drive + "\\")
    if drive_type != DRIVE_FIXED:
        name = _DRIVE_NAMES.get(drive_type, str(drive_type))
        return f"{path} sits on drive type {name}, and only a fixed drive has a Recycle Bin"
    return None


def send_to_trash(path: Path) -> None:
    """Send the file *path* to the OS trash.

    Raises :class:`TrashUnavailableError` naming the path when Windows would delete it for good
    and when the trash refuses it. The file then stays in place.
    """
    # Input
    absolute = path.absolute()
    refusal = _windows_refusal(absolute) if sys.platform == "win32" else None

    # Process
    if refusal is not None:
        raise TrashUnavailableError(refusal)
    try:
        send2trash.send2trash(absolute)
    except OSError as exc:
        message = f"the OS trash refused {absolute}: {exc}"
        raise TrashUnavailableError(message) from exc

    # Output
    logger.info("sent %s to the OS trash", absolute)
