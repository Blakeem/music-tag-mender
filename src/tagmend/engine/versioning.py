"""Append-only tag-revision history + revert (M3).

Version 0 is captured at first scan, before any write — the permanent safety
baseline. Every write appends a new revision (full managed-tag snapshot + diff);
``revert`` reads a target revision, writes it back, and appends a new
``origin='revert'`` row. History is never mutated or deleted.

Planned public API::

    capture_baseline(path: Path, tags: ManagedTags) -> None     # version 0
    append_revision(path: Path, tags: ManagedTags, *, origin, note) -> int
    revert(path: Path, target_version: int) -> int
    history(path: Path) -> list[Revision]

Not yet implemented — see PLAN.md §7 (versioning/undo semantics).
"""

from __future__ import annotations
