"""Read/write the narrow managed tag set via mutagen (M1 read, M3 write).

Managed tags are deliberately narrow so snapshots stay small and reverts can never
damage unrelated metadata: ``GENRE``, ``ARTIST`` (cautious — preserves "feat."),
``ALBUMARTIST``, ``MUSICBRAINZ_ARTISTID``. Titles, track numbers, and album art are
never touched.

Planned public API::

    read_managed_tags(path: Path) -> ManagedTags
    write_managed_tags(path: Path, tags: ManagedTags) -> None   # atomic temp+rename

Not yet implemented — see PLAN.md §7 and §11.
"""

from __future__ import annotations
