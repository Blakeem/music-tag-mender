"""The engine's one source of the current time."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    # Every ledger timestamp is ISO-8601 UTC and compared as a string, so one definition keeps
    # them comparable.
    return datetime.now(UTC).isoformat()
