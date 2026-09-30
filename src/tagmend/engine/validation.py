"""Argument checks shared by the engine entry points."""

from __future__ import annotations


def check_limit(limit: int | None, *, name: str = "limit") -> None:
    """Raise :class:`ValueError` for a negative *limit*. ``None`` and ``0`` pass.

    SQL reads ``LIMIT -1`` as no limit while a Python slice reads ``[:-1]`` as all but the
    last, so a negative cap would mean two different things depending on the code path.
    """
    if limit is not None and limit < 0:
        message = f"{name} must be >= 0, got {limit}"
        raise ValueError(message)
