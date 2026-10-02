"""Detect files whose track stamp collides with a sibling's (read-only report).

The intra-folder half of tag coherence: a folder that holds one album should describe ONE
tracklist, so no two files in it may claim the same ``(discnumber, tracknumber)`` slot. When
two do, one of them is wrong — a bulk tagger stamped a whole folder with one track's numbers,
a duplicate file was never renumbered, or two takes were matched to the same MusicBrainz
recording. Measured on the 11,196-file library: 54 folders, 294 files.

Pure read over the snapshot mirror — writes nothing, stages nothing, no network. The
comparison is a file's track fields against its **folder siblings'** (the ``conflict`` finding
noun), which is what distinguishes this from ``detect_mismatches`` (tags against the folder
PATH) and ``detect_album_gaps`` (a tag against absence).

Three tiers, matching the shapes the library actually contains:

* ``high`` — the colliding files carry DIFFERENT titles. Two distinct songs cannot both be
  track 7; the numbering is wrong.
* ``medium`` — same title, same container. A genuine duplicate stamp: the folder holds the
  same track twice under one number.
* ``low`` — same title, different container (an ``.mp3`` and a ``.flac`` of one song). Usually
  a deliberate duplicate encode rather than a tagging defect, so it is reported last.

A folder holding more than one album, or named as a non-album folder (``Singles``,
``Remixes``…), legitimately repeats track numbers — every single is track 1. Those rows go to
``folder_context`` instead: visible for review, outside ``flagged`` and outside the tier
counts, so the headline number keeps meaning "files that are wrong". The guard is derived from
the folder's own ``album`` values rather than configured, so it needs no setup.

Track TOTALS are deliberately not reported here. A folder of 10 files whose tags say ``/12``
is either missing two tracks or carrying a wrong total, and nothing in the snapshot can tell
which — that needs the MusicBrainz release tracklist.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

from tagmend.engine import db, path_keys, schema, store
from tagmend.engine.detector_core import (
    TIER_RANK,
    FieldDict,
    Tier,
    group_by_folder,
    is_non_album_folder,
    parse_position,
    validate_tier,
)
from tagmend.engine.text_keys import alnum_script_key, loose_key
from tagmend.engine.validation import check_limit
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings

logger = get_logger(__name__)

# The scalar fields the detector reads per file (ordinal-0 value of each).
_DETECT_FIELDS: Final = ("tracknumber", "discnumber", "title", "album")

# The disc a file belongs to when it carries no discnumber: single-disc releases routinely
# omit it, and treating those files as disc-less would make every one of them collide.
_DEFAULT_DISC: Final = "1"


# Per-tier reason strings (named so they stay stable across the row + tests).
_REASON_HIGH: Final = "different titles share this track slot; the numbering is wrong"
_REASON_MEDIUM: Final = "duplicate track stamp: same title and container share this track slot"
_REASON_LOW: Final = "same title in another container shares this track slot (duplicate encode)"
_REASON_MULTI_ALBUM: Final = (
    "folder holds more than one album, so a repeated track slot is expected"
)
_REASON_NON_ALBUM: Final = (
    "non-album folder (singles/remixes), so a repeated track slot is expected"
)


# --- inputs / intermediate analysis --------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FileInput:
    """One tracked file reduced to the fields the detector reads (cleaned scalars)."""

    file_id: int
    folder: str
    filename: str
    ext: str
    tracknumber: str | None = None
    discnumber: str | None = None
    title: str | None = None
    album: str | None = None

    @property
    def slot(self) -> tuple[str, int] | None:
        """Return this file's ``(disc, track)`` slot, or ``None`` when it has no track number."""
        return _slot(self.tracknumber, self.discnumber)


# --- public result types -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackConflictRow(FieldDict):
    """One flagged file: the slot it shares, who it shares it with, and why that is wrong."""

    file_id: int
    folder: str
    filename: str
    disc: str
    track: int
    title: str | None
    tier: str  # Tier value
    reason: str
    peers: list[int]


@dataclass(frozen=True, slots=True)
class TrackConflictGroup(FieldDict):
    """One folder's conflicts, compact enough to scan a whole library at a glance."""

    folder: str
    file_count: int
    flagged: int
    folder_context: int
    slots: dict[str, int]
    tiers: dict[str, int]
    file_ids: list[int]


@dataclass(frozen=True, slots=True)
class TrackConflictsReport(FieldDict):
    """Immutable summary of one :func:`detect_track_conflicts` run, JSON-ready for the tool.

    The ``high``/``medium``/``low``/``flagged`` counts describe the whole library and are
    unaffected by a ``tier``/``limit``/``folder`` narrowing, so a filtered view still shows the
    full picture of what remains actionable. The tier counts always sum to ``flagged``;
    ``folder_context`` is outside both.
    """

    rows: list[TrackConflictRow]
    total_files: int
    flagged: int
    high: int
    medium: int
    low: int
    folder_context: int = 0
    folder_context_rows: list[TrackConflictRow] = field(default_factory=list)
    groups: list[TrackConflictGroup] = field(default_factory=list)
    # Last, so the payload ends with it. Keyword-only keeps it required after the defaults.
    summary: str = field(kw_only=True)


# --- pure classifier -----------------------------------------------------------------


def _slot(tracknumber: str | None, discnumber: str | None) -> tuple[str, int] | None:
    """Return the ``(disc, track)`` slot for a pair of raw tag values, or ``None``."""
    track = parse_position(tracknumber)
    if track is None:
        return None
    disc = parse_position(discnumber)
    return (str(disc) if disc is not None else _DEFAULT_DISC, track)


def _is_context_folder(files: list[_FileInput]) -> str | None:
    """Return the reason this folder legitimately repeats track slots, or ``None``.

    A compilation/singles folder holds several releases, so two files sharing track 1 says
    nothing. The album-value test is intrinsic (no configuration); the leaf-name test catches
    the same folders when their files carry no album tag at all.
    """
    albums = {f.album for f in files if f.album}
    if len(albums) > 1:
        return _REASON_MULTI_ALBUM
    if is_non_album_folder(files[0].folder):
        return _REASON_NON_ALBUM
    return None


def _tier_for(peers: list[_FileInput]) -> tuple[Tier, str]:
    """Classify one slot's colliding files by how their titles and containers compare."""
    titles = {alnum_script_key(f.title) or loose_key(f.title) for f in peers if f.title}
    if len(titles) > 1:
        return Tier.HIGH, _REASON_HIGH
    if len({f.ext.lower() for f in peers}) > 1:
        return Tier.LOW, _REASON_LOW
    return Tier.MEDIUM, _REASON_MEDIUM


def _rows_for_slot(
    peers: list[_FileInput],
    tier: Tier,
    reason: str,
) -> list[TrackConflictRow]:
    """Build one row per file sharing a slot, each naming the others as its peers."""
    ids = [f.file_id for f in peers]
    return [
        TrackConflictRow(
            file_id=f.file_id,
            folder=f.folder,
            filename=f.filename,
            disc=slot[0],
            track=slot[1],
            title=f.title,
            tier=tier.value,
            reason=reason,
            peers=[i for i in ids if i != f.file_id],
        )
        for f in peers
        if (slot := f.slot) is not None
    ]


def _classify(files: list[_FileInput]) -> TrackConflictsReport:
    """Classify every folder's colliding track slots into flagged rows + context rows."""
    # Input: bucket files by folder, preserving discovery order within each.
    by_folder = group_by_folder(files)

    # Process: per folder, find every slot two or more files claim.
    flagged: list[TrackConflictRow] = []
    context: list[TrackConflictRow] = []
    for folder_files in by_folder.values():
        slots: dict[tuple[str, int], list[_FileInput]] = defaultdict(list)
        for f in folder_files:
            slot = f.slot
            if slot is not None:
                slots[slot].append(f)
        collisions = {s: peers for s, peers in slots.items() if len(peers) > 1}
        if not collisions:
            continue
        context_reason = _is_context_folder(folder_files)
        for peers in collisions.values():
            if context_reason is not None:
                context.extend(_rows_for_slot(peers, Tier.LOW, context_reason))
                continue
            tier, reason = _tier_for(peers)
            flagged.extend(_rows_for_slot(peers, tier, reason))

    # Output: deterministic order (tier rank, then file id) and the library-wide counts.
    flagged.sort(key=lambda r: (TIER_RANK[Tier(r.tier)], r.file_id))
    context.sort(key=lambda r: r.file_id)
    tiers = {t: sum(1 for r in flagged if r.tier == t.value) for t in Tier}
    return TrackConflictsReport(
        rows=flagged,
        total_files=len(files),
        flagged=len(flagged),
        high=tiers[Tier.HIGH],
        medium=tiers[Tier.MEDIUM],
        low=tiers[Tier.LOW],
        folder_context=len(context),
        folder_context_rows=context,
        summary=_summarize(
            total_files=len(files),
            flagged=len(flagged),
            high=tiers[Tier.HIGH],
            medium=tiers[Tier.MEDIUM],
            low=tiers[Tier.LOW],
            context=len(context),
        ),
    )


def _summarize(  # noqa: PLR0913 - one keyword per reported count, cohesive by design
    *,
    total_files: int,
    flagged: int,
    high: int,
    medium: int,
    low: int,
    context: int,
) -> str:
    """Build a short, plain human summary of the run."""
    note = (
        f", {context} review-context row(s) in multi-album or non-album folders" if context else ""
    )
    return (
        f"Flagged {flagged} of {total_files} file(s) sharing a track slot with a sibling: "
        f"{high} high, {medium} medium, {low} low{note}."
    )


# --- narrowing -----------------------------------------------------------------------


def _build_groups(
    rows: list[TrackConflictRow],
    context_rows: list[TrackConflictRow],
    file_counts: dict[str, int],
) -> list[TrackConflictGroup]:
    """Fold rows into one compact line per folder, flagged and context counted apart."""
    by_folder = group_by_folder(rows)
    context_by_folder = group_by_folder(context_rows)
    groups: list[TrackConflictGroup] = []
    for folder in sorted(by_folder.keys() | context_by_folder.keys()):
        folder_rows = by_folder.get(folder, [])
        slots: dict[str, int] = {}
        tiers: dict[str, int] = {}
        # Only FLAGGED rows feed the histograms and file_ids, so a fix flow driven off a group
        # can never pick up a review-context file.
        for row in folder_rows:
            key = f"{row.disc}-{row.track}"
            slots[key] = slots.get(key, 0) + 1
            tiers[row.tier] = tiers.get(row.tier, 0) + 1
        groups.append(
            TrackConflictGroup(
                folder=folder,
                file_count=file_counts.get(folder, 0),
                flagged=len(folder_rows),
                folder_context=len(context_by_folder.get(folder, [])),
                slots=slots,
                tiers=tiers,
                file_ids=sorted(row.file_id for row in folder_rows),
            ),
        )
    return groups


def _narrow(  # noqa: PLR0913 - the view knobs the public entry forwards, one each
    report: TrackConflictsReport,
    file_counts: dict[str, int],
    *,
    tier: str | None,
    limit: int | None,
    group: bool,
    folder_key: str | None,
) -> TrackConflictsReport:
    """Apply the tier/folder/limit view without touching the library-wide counts."""
    rows = report.rows if tier is None else [r for r in report.rows if r.tier == tier]
    context_rows = report.folder_context_rows if tier is None else []
    if folder_key is not None:
        # Key equality, never a prefix: a sibling or a subfolder must not be swept in.
        rows = [r for r in rows if path_keys.path_key(r.folder) == folder_key]
        context_rows = [r for r in context_rows if path_keys.path_key(r.folder) == folder_key]
        return replace(
            report,
            rows=rows[:limit] if limit is not None else rows,
            folder_context_rows=context_rows[:limit] if limit is not None else context_rows,
            groups=[],
        )
    if group:
        groups = _build_groups(rows, context_rows, file_counts)
        return replace(
            report,
            rows=[],
            folder_context_rows=[],
            groups=groups[:limit] if limit is not None else groups,
        )
    return replace(
        report,
        rows=rows[:limit] if limit is not None else rows,
        folder_context_rows=context_rows[:limit] if limit is not None else context_rows,
        groups=[],
    )


# --- public entry --------------------------------------------------------------------


def _load_inputs(connection: sqlite3.Connection) -> list[_FileInput]:
    """Read every present file's detect fields out of the snapshot mirror."""
    tag_values = store.load_tag_values(connection, _DETECT_FIELDS)
    inputs: list[_FileInput] = []
    for row in store.list_files(connection):
        if row.is_missing:
            continue
        values = tag_values.get(row.id, {})
        inputs.append(
            _FileInput(
                file_id=row.id,
                folder=row.folder,
                filename=row.filename,
                ext=row.ext,
                tracknumber=values.get("tracknumber"),
                discnumber=values.get("discnumber"),
                title=values.get("title"),
                album=values.get("album"),
            ),
        )
    return inputs


def detect_track_conflicts(
    settings: Settings,
    *,
    tier: str | None = None,
    limit: int | None = None,
    group: bool = False,
    folder: str | None = None,
) -> TrackConflictsReport:
    """Report files that share a ``(disc, track)`` slot with a sibling in the same folder.

    Read-only over the snapshot: run ``scan_library`` first. *folder* is compared as a path
    (:func:`tagmend.engine.path_keys.folder_arg_key`). Raises :class:`ValueError` for an unknown
    *tier*, a negative *limit* or a *folder* outside ``music_path``.
    """
    check_limit(limit)
    validate_tier(tier)
    folder_key = None if folder is None else path_keys.folder_arg_key(settings, folder)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        files = _load_inputs(connection)
    finally:
        connection.close()

    report = _classify(files)
    file_counts: dict[str, int] = defaultdict(int)
    for f in files:
        file_counts[f.folder] += 1
    logger.info(
        "track conflicts: flagged=%s context=%s of %s file(s)",
        report.flagged,
        report.folder_context,
        report.total_files,
    )
    return _narrow(
        report,
        dict(file_counts),
        tier=tier,
        limit=limit,
        group=group,
        folder_key=folder_key,
    )
