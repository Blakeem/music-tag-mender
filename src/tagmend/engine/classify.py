"""Genre resolution: vocabulary, thresholds, and auto-vs-review classification (M2).

Filters Last.fm community tags through a controlled allow-list
(``data/genre_vocabulary.yml``), applies a weight threshold to decide *auto* vs
*needs_review*, and maps surviving tags to the canonical genre string written to
files.

Planned public API::

    load_vocabulary(path: Path | None = None) -> Vocabulary
    resolve_genre(top_tags: list[Tag], vocab: Vocabulary) -> Resolution

Not yet implemented — see PLAN.md §9 and §10.
"""

from __future__ import annotations
