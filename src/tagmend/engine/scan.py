"""Library discovery: walk a root folder and yield audio files.

This is the foundation of the read path (M1). For M0 it powers the health check's
"music folder reachable" probe and gives a quick file count.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# Formats mutagen can read/write that we care about. Lowercased for comparison.
AUDIO_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".mp3",
        ".flac",
        ".m4a",
        ".aac",
        ".ogg",
        ".opus",
        ".wma",
        ".wav",
        ".aiff",
        ".aif",
    }
)


def is_audio_file(path: Path) -> bool:
    """Return ``True`` if *path* is a regular file with a known audio extension."""
    return path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS


def iter_audio_files(root: Path) -> Iterator[Path]:
    """Yield every audio file under *root*, recursively, in sorted (stable) order."""
    for path in sorted(root.rglob("*")):
        if is_audio_file(path):
            yield path


def count_audio_files(root: Path) -> int:
    """Return the number of audio files under *root*."""
    return sum(1 for _ in iter_audio_files(root))
