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

Paths is the *second* :class:`tagmend.engine.commits.RevisionDomain`, mirroring
``staging.TagDomain``. The sketch below validates that the shared commit seam fits a move
domain; it is a design note, not shipped code. Only the per-domain pieces differ from
tags — the crash-safe ``commits.run_commit`` loop is reused unchanged::

    # class PathDomain:  # FUTURE (M6) — implements commits.RevisionDomain
    #     name = "paths"
    #     list_staged_file_ids       -> store.list_staged_paths (path_revisions_staged)
    #     resolve_path               -> the current (FROM) folder/filename
    #     apply_to_disk:
    #         staged = store.get_staged_path(conn, file_id)      # to_path
    #         ensure_path_baseline(conn, file_id, from_path=current, now)  # v0 at STAGE time
    #         move_file(from_path, staged.to_path)               # DISK ACTION diverges
    #         store.update_file_location(conn, file_id, staged.to_path)    # snapshot refresh
    #         append_path_revision(conn, file_id, from_path=current,
    #                              to_path=staged.to_path, commit_id=commit_id, note=...)
    #         store.delete_staged_path(conn, file_id)
    #     post_commit_file           -> prune the now-empty source dir (organize.prune_empty_dirs)
    #     plan_order                 -> topological move order + collision resolution

THREE seam questions parked for the M6 paths build (do NOT assume these are solved by the
tags seam — they are paths-only and unresolved here):

1. **Intra-batch move ordering / clobber.** Moves ``A->B`` and ``B->C`` in one commit must
   run in dependency order or ``A->B`` clobbers the file ``B->C`` was about to move.
   ``plan_order`` is the hook for a topological sort; confirm it is expressive enough (it
   returns only an order, not a per-step plan).

2. **Collision policy (PLAN.md §15, OPEN).** When two files resolve to the same destination
   (e.g. a folder rename collapsing two artist spellings), the policy — refuse / suffix /
   merge — is undecided. ``plan_order`` may need to *filter* colliding files and report
   them, i.e. a richer per-file decision than "an order"; revisit the seam if so rather than
   branching inside ``run_commit``.

3. **Folder-rename atomicity.** ``run_commit`` commits per file, so a multi-file folder
   rename is not atomic — a crash mid-folder leaves half the album moved (the rest re-sweep
   on the next commit). Tags have no equivalent. Decide whether per-file granularity is
   acceptable for moves or whether folder moves need a coarser transaction boundary.
"""

from __future__ import annotations
