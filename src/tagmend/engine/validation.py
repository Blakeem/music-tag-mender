"""Argument checks shared by the engine entry points."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence
    from pathlib import Path

    from tagmend.config import Settings

_PAIR_WIDTH: Final = 2


def check_limit(limit: int | None, *, name: str = "limit") -> None:
    """Raise :class:`ValueError` for a negative *limit*. ``None`` and ``0`` pass.

    SQL reads ``LIMIT -1`` as no limit while a Python slice reads ``[:-1]`` as all but the
    last, so a negative cap would mean two different things depending on the code path.
    """
    if limit is not None and limit < 0:
        message = f"{name} must be >= 0, got {limit}"
        raise ValueError(message)


def require_choice(name: str, value: str | None, allowed: Collection[str]) -> None:
    """Raise :class:`ValueError` when *value* is not one of *allowed*. ``None`` passes."""
    if value is not None and value not in allowed:
        message = f"unknown {name}: {value!r} (expected one of {', '.join(sorted(allowed))})"
        raise ValueError(message)


def require_music_path(settings: Settings) -> Path:
    """Return ``music_path``, or raise :class:`ValueError` when it is not configured."""
    if settings.music_path is None:
        message = "music_path not configured. Run `tagmend config-set music_path <dir>`"
        raise ValueError(message)
    return settings.music_path


def validate_file_pairs[T](
    entries: Sequence[object],
    *,
    value_name: str,
    check_value: Callable[[object], T],
) -> list[tuple[int, T]]:
    """Narrow *entries* to ``(file_id, value)`` pairs, each file once, or raise :class:`ValueError`.

    *entries* is untyped because an engine caller can hand over the MCP-shaped dict, whose two
    keys would destructure as a pair. *check_value* returns the narrowed value or raises
    :class:`ValueError` with the reason. Every message names the entry index.
    """
    validated: list[tuple[int, T]] = []
    seen: set[int] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, tuple) or len(entry) != _PAIR_WIDTH:
            shape = f"{len(entry)} items" if isinstance(entry, tuple) else type(entry).__name__
            message = f"entry {index}: expected a (file_id, {value_name}) pair, got {shape}"
            raise ValueError(message)
        file_id, value = entry
        if not isinstance(file_id, int) or isinstance(file_id, bool):
            message = f"entry {index}: file_id must be an integer, got {type(file_id).__name__}"
            raise ValueError(message)  # noqa: TRY004 - batch rejections are uniformly ValueError
        try:
            checked = check_value(value)
        except ValueError as exc:
            message = f"entry {index} (file_id={file_id}): {exc}"
            raise ValueError(message) from exc
        if file_id in seen:
            message = f"entry {index}: duplicate file_id={file_id} in batch"
            raise ValueError(message)
        seen.add(file_id)
        validated.append((file_id, checked))
    return validated
