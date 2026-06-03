"""Opt-in file/folder reorganization with an append-only path history (M6).

Disabled by default (``organize.enabled = false``). Renames files and renames/moves
folders into a consistent scheme, recording every move in ``path_revisions`` (keyed
by a stable ``file_id``) so any rename/move is individually revertible — separately
from tag history.

Planned public API::

    plan_organize(root: Path, *, template, folder_template) -> MovePlan   # dry-run
    commit_organize(plan: MovePlan) -> None                               # atomic
    revert_move(file_id: str, version: int) -> int
    move_history(file_id: str) -> list[PathRevision]

Not yet implemented — see PLAN.md §18.
"""

from __future__ import annotations
