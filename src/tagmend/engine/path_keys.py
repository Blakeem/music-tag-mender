"""The one place that decides when two path strings name the same file.

A path key is a folder or file path normalized for identity: separators and ``.``/``..``
segments collapsed, and case folded where the filesystem ignores it. The ``files`` table stores
each file's key in ``files.path_key``, and every lookup, subtree scope and folder filter
compares keys rather than the display strings.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tagmend.config import Settings


def path_key(path: str | os.PathLike[str]) -> str:
    """Return the identity key of *path*."""
    # normcase lowercases on Windows, where NTFS ignores case, and is the identity on POSIX,
    # so a case-insensitive macOS volume is not covered.
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def file_path_key(folder: str, filename: str) -> str:
    """Return the identity key of the file *filename* inside *folder*."""
    return path_key(Path(folder) / filename)


def subtree_bounds(root_key: str) -> tuple[str, str]:
    """Return the half-open key range ``[low, high)`` of every path strictly under *root_key*."""
    # The separator is part of the prefix, so a sibling like ``Album 2`` falls outside ``Album``.
    low = root_key.rstrip(os.sep) + os.sep
    high = low[:-1] + chr(ord(os.sep) + 1)
    return low, high


def is_within(key: str, root_key: str) -> bool:
    """Whether *key* is *root_key* itself or lies anywhere under it."""
    low, high = subtree_bounds(root_key)
    return key == root_key or low <= key < high


def resolve_folder_arg(settings: Settings, folder: str | os.PathLike[str]) -> Path:
    """Return a user-supplied folder argument as a path, the one resolver every tool shares.

    A relative *folder* resolves under ``music_path``. Raises :class:`ValueError` for a relative
    *folder* with no ``music_path`` configured, and for a folder outside ``music_path``.
    """
    music_path = settings.music_path
    candidate = Path(folder)
    if not candidate.is_absolute():
        if music_path is None:
            message = "a relative folder needs music_path configured"
            raise ValueError(message)
        candidate = music_path / candidate

    if music_path is not None and not is_within(path_key(candidate), path_key(music_path)):
        message = f"folder {folder} is outside music_path {music_path}"
        raise ValueError(message)
    return candidate


def folder_arg_key(settings: Settings, folder: str | os.PathLike[str]) -> str:
    """Return the key of a user-supplied folder argument (see :func:`resolve_folder_arg`)."""
    return path_key(resolve_folder_arg(settings, folder))
