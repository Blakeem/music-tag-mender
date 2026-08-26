"""Mislabeled-file detection + disposition: albumartist-vs-path disagreement, tiered.

Flags files whose ``albumartist`` (with an ``artist`` fallback) disagrees with the file's
folder path — the fingerprint of a MusicBrainz Picard release mis-match that stamped the
WRONG identity tags onto files whose filenames/paths kept the truth (e.g. Ozzy's *Down to
Earth* files tagged as *Jem*). **The detector proper stays read-only** — it writes nothing,
stages nothing, and never hits the network, a pure read over the existing
``files``/``file_tags`` snapshot. The two disposition verbs
(:func:`set_mismatch_status` / :func:`reset_mismatch_status`) are the module's ONLY writers,
and they touch only the ``file_mismatch_status`` rows (never a tag): a sticky per-file
disposition (``legit_ignore`` false-positive / ``misfiled_deferred``) that
:func:`detect_mismatches` then honours by dropping that file's flagged row while it stays
fresh (the disposition goes stale, and the file re-surfaces, once its snapshotted identity
tag changes).

The chosen design (see ``aipg/workflows/decide/runs/detect-mislabeled-tags/decision-r1.md``):

* **Primary signal** — ``albumartist`` vs the file path, **bidirectional containment** over a
  normalization ladder (:func:`fold`: casefold + strip non-alphanumerics + Unicode/ligature
  fold). A file disagrees when the folded ``albumartist`` is not contained in the folded path
  AND the folded top-level artist folder and the folded ``albumartist`` are not substrings of
  each other (the bidirectional check is what lets ``Lusine ICL`` in a ``Lusine`` folder pass).
* **Confidence tiers** keyed on per-folder distinct-``albumartist`` variance: HIGH (path
  disagreement in a folder with mixed ``albumartist`` values), MEDIUM (path disagreement in a
  uniformly mis-stamped folder), LOW (folder-consistency fallback, a non-album-guarded path
  disagreement, or the ``artist`` fallback).
* **Folder context** — a file in a mixed-``albumartist`` folder that AGREES with its own path
  is not a defect. It is carried in the report's ``folder_context_rows`` (counted by
  ``folder_context``), outside ``flagged`` and the tier tallies, so ``flagged`` only ever
  counts files that need work. The split keys on the FILE's own path agreement, never on
  which branch emitted the row: a file that disagrees, or whose path signal is nulled by the
  reliability guard / a container folder / the library root, stays flagged.
* **Reliability guard** — the library-wide path-disagreement rate; above
  :data:`RELIABILITY_FLOOR` the path likely does not encode artist, so the path tiers
  (HIGH/MEDIUM) are suppressed and only the naming-agnostic folder-consistency LOW is emitted.

Everything here is a pure classification over the snapshot; :func:`detect_mismatches` owns the
read-only connection (``connect`` → ``apply_schema`` → ``try/finally`` close, **no commit**),
mirroring :mod:`tagmend.engine.library`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tagmend.engine import axis, db, schema, store
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings

logger = get_logger(__name__)

# The two scalar fields the detector reads per file (ordinal-0 value of each).
_DETECT_FIELDS: Final = ("albumartist", "artist")

# The dispositions :func:`set_mismatch_status` may write. ``pending`` deletes the row
# (re-queue). There is no ``staged``/``done`` on this axis — an accepted fix needs no row.
_USER_MISMATCH_STATUSES: Final = frozenset({"legit_ignore", "misfiled_deferred", "pending"})


def _utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string (the engine's timestamp form)."""
    return datetime.now(UTC).isoformat()


# Library-wide path-disagreement rate above which the path signal is deemed unreliable
# (the path likely does not encode artist) and the HIGH/MEDIUM path tiers are suppressed in
# favour of the naming-agnostic folder-consistency LOW tier. The measured baseline on a
# path-encoding library is ~1.9%; this floor sits an order of magnitude above that noise
# while staying well below the near-total disagreement a non-encoding library produces.
RELIABILITY_FLOOR: Final = 0.30

# Album-artist values that are never a single real artist to compare against the path.
VA_ALBUMARTISTS: Final = frozenset(
    {"various artists", "various", "va", "soundtrack", "original soundtrack", "ost"},
)

# Leaf folder names (case-insensitive, anchored) that are not normal albums: a guest/other
# artist here is legitimate, so such a folder can never reach HIGH/MEDIUM (demoted to LOW).
NON_ALBUM_FOLDERS: Final = frozenset(
    {"singles", "featured", "remixes", "bonus", "live", "ep"},
)

# Ligature/eszett map applied after casefold (which already folds ``ß`` → ``ss`` and
# ``Æ`` → ``æ`` etc.), covering the compatibility cases NFKD does not decompose.
_LIGATURES: Final = {
    "æ": "ae",
    "ø": "o",
    "œ": "oe",
    "ł": "l",
    "þ": "th",
    "ð": "d",
}
_LIGATURE_TABLE: Final = str.maketrans(_LIGATURES)

_NON_ALNUM: Final = re.compile(r"[^a-z0-9]+")
_DISCOGRAPHY_SUFFIX: Final = re.compile(r"\s*\[discography\]\s*$", re.IGNORECASE)
# Split a possibly-multi-artist ``artist`` value on the feat./ft./featuring family and the
# ``&``/``,`` separators to recover the primary (first) artist for the fallback check.
_PRIMARY_ARTIST_SPLIT: Final = re.compile(r"\b(?:feat|ft|featuring)\b\.?|[&,]", re.IGNORECASE)


class Tier(StrEnum):
    """Confidence tier for a flagged file (most to least confident)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_TIER_RANK: Final = {Tier.HIGH: 0, Tier.MEDIUM: 1, Tier.LOW: 2}
_TIERS: Final = frozenset(t.value for t in Tier)

# Per-tier reason strings (named so they stay stable across the row + tests).
_REASON_HIGH: Final = "albumartist disagrees with the folder path; folder has mixed albumartists"
_REASON_MEDIUM: Final = (
    "albumartist disagrees with the folder path; folder is uniformly mis-stamped"
)
_REASON_GUARDED: Final = (
    "albumartist disagrees with the folder path in a non-album (singles/1-file) folder"
)
_REASON_VARIANT: Final = "folder has mixed albumartists but this file agrees with the path"
_REASON_SUPPRESSED: Final = (
    "albumartist disagrees with the folder path; library-wide path signal unreliable"
)
_REASON_NO_SIGNAL: Final = "folder has mixed albumartists and this file has no path signal"
_REASON_ARTIST: Final = "artist disagrees with the folder path (no albumartist tag)"


def fold(s: str) -> str:
    """Return the detector's fold-key for *s*: casefold + Unicode/ligature fold + strip.

    Casefold, translate the residual ligatures NFKD leaves intact (``æ`` → ``ae`` …),
    NFKD-decompose and drop combining marks (diacritics), then strip everything outside
    ``[a-z0-9]``. Deliberately a **superset** of :func:`tagmend.engine.classify.fold` (the
    genre fold-key, which is casefold + strip only): the detector additionally needs the
    Unicode/ligature folding so ``Leæther Strip`` == ``Leaether Strip`` and ``Dååth`` ==
    ``Daath``. A match/compare key only — never written to disk.
    """
    translated = s.casefold().translate(_LIGATURE_TABLE)
    decomposed = unicodedata.normalize("NFKD", translated)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_ALNUM.sub("", without_marks)


def _top_artist(folder: str, music_path: Path) -> str | None:
    """Return the top-level artist folder for *folder* under *music_path*, or ``None``.

    Derives ``Path(folder).relative_to(music_path).parts[0]`` (a generalized root, not a
    hard-coded ``music`` path literal) and strips a trailing ``[Discography]`` suffix. Returns
    ``None`` when *folder* is not under *music_path* (the ``relative_to`` ``ValueError``) or
    IS *music_path* itself — a file at the library root yields ``PurePath('.')`` whose
    ``.parts`` is empty. A ``None`` top-artist means *no path signal*: the file can never be
    HIGH/MEDIUM via the path.
    """
    try:
        relative = Path(folder).relative_to(music_path)
    except ValueError:
        return None
    parts = relative.parts
    if not parts:
        return None
    return _DISCOGRAPHY_SUFFIX.sub("", parts[0]).strip() or None


def _primary_artist(value: str) -> str:
    """Return the primary (first) artist from a possibly-multi-artist ``artist`` value.

    Splits on the ``feat.``/``ft.``/``featuring`` credit markers and the ``&``/``,``
    separators and returns the first non-empty segment. The fallback identity when a file
    has no ``albumartist``; structurally cannot reintroduce the remix-album FP class (that
    class only exists where ``albumartist`` is present and differs).
    """
    for segment in _PRIMARY_ARTIST_SPLIT.split(value):
        candidate = segment.strip()
        if candidate:
            return candidate
    return value.strip()


def _is_va(albumartist: str) -> bool:
    """Return whether *albumartist* is a Various-Artists/soundtrack value (never compared)."""
    return albumartist.strip().casefold() in VA_ALBUMARTISTS


def _disagrees(value: str, path: str, top_artist: str | None) -> bool:
    """Return whether *value* disagrees with the file *path* (bidirectional containment).

    Disagreement requires BOTH: the folded *value* is not a substring of the folded *path*,
    AND the folded *top_artist* folder and the folded *value* are not substrings of each
    other. A ``None`` *top_artist* means there is no path signal, so nothing disagrees.
    """
    if top_artist is None:
        return False
    folded_value = fold(value)
    if not folded_value:
        return False
    if folded_value in fold(path):
        return False
    folded_top = fold(top_artist)
    return not (folded_top and (folded_top in folded_value or folded_value in folded_top))


# --- inputs / intermediate analysis --------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FileInput:
    """One tracked file reduced to the fields the detector reads (cleaned scalars)."""

    file_id: int
    folder: str
    filename: str
    albumartist: str | None
    artist: str | None

    @property
    def path(self) -> str:
        """The full file path (folder + filename); only ever compared via :func:`fold`."""
        return str(Path(self.folder) / self.filename)


@dataclass(frozen=True, slots=True)
class _Analysis:
    """A file plus its precomputed path signals, shared by the guard and classifier."""

    file: _FileInput
    top_artist: str | None
    albumartist_disagrees: bool  # only meaningful when albumartist present & non-VA
    # Raw top-folder name when this file sits under a configured container folder (its path
    # signal was suppressed); ``None`` otherwise. Drives the reliability filter + count map.
    container_folder: str | None = None


@dataclass(frozen=True, slots=True)
class _FolderStats:
    """Per-folder aggregates driving the tier and the non-album guard."""

    distinct_albumartists: dict[str, int]  # folder -> count of distinct non-empty values
    file_count: dict[str, int]  # folder -> total tracked files

    def is_variant(self, folder: str) -> bool:
        """Whether *folder* carries more than one distinct ``albumartist`` value."""
        return self.distinct_albumartists.get(folder, 0) > 1

    def is_non_album(self, folder: str) -> bool:
        """Whether *folder* is a 1-file leaf or an anchored non-album name (Singles, …)."""
        if self.file_count.get(folder, 0) <= 1:
            return True
        return Path(folder).name.casefold() in NON_ALBUM_FOLDERS


# --- public result types -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MismatchRow:
    """One flagged file: the tag that disagrees with its path, its tier, and why."""

    file_id: int
    folder: str
    filename: str
    field: str  # "albumartist" | "artist"
    tag_value: str
    path_artist: str | None
    tier: str  # Tier value
    reason: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "file_id": self.file_id,
            "folder": self.folder,
            "filename": self.filename,
            "field": self.field,
            "tag_value": self.tag_value,
            "path_artist": self.path_artist,
            "tier": self.tier,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class MismatchGroup:
    """One folder's flagged files collapsed into a compact group (the ``group=True`` view)."""

    folder: str
    path_artist: str | None
    file_count: int  # tracked files in the folder (flagged or not)
    flagged: int  # flagged (post-filter) files in the folder
    folder_context: int  # agrees-with-path review-context files in the folder (not defects)
    tag_values: dict[str, int]  # disagreeing tag value -> count over flagged rows
    tiers: dict[str, int]  # tier -> count over flagged rows
    fields: list[str]  # the disagreeing field names present (sorted)
    file_ids: list[int]  # every flagged file id in the folder, sorted
    suppressed: dict[str, int]  # disposition status -> count silenced in this folder

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "folder": self.folder,
            "path_artist": self.path_artist,
            "file_count": self.file_count,
            "flagged": self.flagged,
            "folder_context": self.folder_context,
            "tag_values": self.tag_values,
            "tiers": self.tiers,
            "fields": self.fields,
            "file_ids": self.file_ids,
            "suppressed": self.suppressed,
        }


@dataclass(frozen=True, slots=True)
class MismatchReport:
    """Immutable summary of one :func:`detect_mismatches` run, JSON-ready for the MCP tool.

    ``rows`` is the (tier-filtered, capped) worklist; the ``high``/``medium``/``low``/
    ``flagged`` counts describe the whole library MINUS files silenced by a fresh disposition
    (so a filtered view still shows the full picture of what remains actionable), and the tier
    counts always sum to ``flagged``. ``folder_context_rows`` (counted by ``folder_context``)
    holds the review-context files — a mixed-albumartist folder's siblings that agree with
    their own path — kept OUT of ``flagged`` and the tier counts because they are not defects;
    a ``tier`` filter drops them from the payload entirely. ``suppressed``
    is a disposition-status → count map of the flagged rows a fresh disposition silenced
    (``{}`` when none), so silencing is always visible. ``container_suppressed`` is the separate
    top-folder → file-count map of files whose path signal a configured ``container_folders``
    entry suppressed (``{}`` when none) — distinct from the per-disposition ``suppressed`` map.
    ``groups`` is populated only in the ``group=True`` view (``rows`` is then empty);
    ``suppressed_by_folder`` is internal plumbing for that view and is not serialized.
    """

    rows: list[MismatchRow]
    total_files: int
    flagged: int
    high: int
    medium: int
    low: int
    disagreement_rate: float
    path_signal_suppressed: bool
    summary: str
    suppressed: dict[str, int] = field(default_factory=dict)
    container_suppressed: dict[str, int] = field(default_factory=dict)
    folder_context: int = 0
    folder_context_rows: list[MismatchRow] = field(default_factory=list)
    groups: list[MismatchGroup] = field(default_factory=list)
    suppressed_by_folder: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "rows": [row.to_dict() for row in self.rows],
            "folder_context_rows": [row.to_dict() for row in self.folder_context_rows],
            "groups": [group.to_dict() for group in self.groups],
            "total_files": self.total_files,
            "flagged": self.flagged,
            "high": self.high,
            "medium": self.medium,
            "low": self.low,
            "folder_context": self.folder_context,
            # Round at the serialization edge only; the engine float keeps full precision
            # for the RELIABILITY_FLOOR comparison in _reliability.
            "disagreement_rate": round(self.disagreement_rate, 4),
            "path_signal_suppressed": self.path_signal_suppressed,
            "suppressed": self.suppressed,
            "container_suppressed": self.container_suppressed,
            "summary": self.summary,
        }


# --- pure classifier -----------------------------------------------------------------


def _folder_stats(files: list[_FileInput]) -> _FolderStats:
    """Aggregate per-folder distinct-``albumartist`` counts and file counts."""
    distinct: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for f in files:
        counts[f.folder] = counts.get(f.folder, 0) + 1
        if f.albumartist is not None:
            distinct.setdefault(f.folder, set()).add(f.albumartist)
    return _FolderStats(
        distinct_albumartists={folder: len(values) for folder, values in distinct.items()},
        file_count=counts,
    )


def _analyze(
    f: _FileInput,
    music_path: Path,
    *,
    container_folders: frozenset[str] = frozenset(),
) -> _Analysis:
    """Precompute a file's top-artist folder and its ``albumartist`` path-disagreement.

    When the (fold-keyed) top-level folder is a configured container (``container_folders``,
    pre-folded), the path signal is suppressed: ``top_artist`` is treated as ``None`` (so no
    HIGH/MEDIUM path tier or artist fallback can fire) and the raw folder name is recorded in
    ``container_folder`` for the reliability filter and the visible count map.
    """
    top = _top_artist(f.folder, music_path)
    container = top if top is not None and fold(top) in container_folders else None
    if container is not None:
        top = None
    disagrees = (
        f.albumartist is not None
        and not _is_va(f.albumartist)
        and _disagrees(f.albumartist, f.path, top)
    )
    return _Analysis(
        file=f,
        top_artist=top,
        albumartist_disagrees=disagrees,
        container_folder=container,
    )


def _reliability(analyses: list[_Analysis]) -> tuple[float, bool]:
    """Return ``(disagreement_rate, suppressed)`` over non-VA files that have an albumartist.

    Container-suppressed files are excluded from the sample exactly like VA files: their path
    signal is nulled, so leaving them in the denominator would dilute the rate with non-
    disagreers rather than measuring how well the path encodes artist.
    """
    considered = [
        a
        for a in analyses
        if a.file.albumartist is not None
        and not _is_va(a.file.albumartist)
        and a.container_folder is None
    ]
    if not considered:
        return 0.0, False
    disagreeing = sum(1 for a in considered if a.albumartist_disagrees)
    rate = disagreeing / len(considered)
    return rate, rate > RELIABILITY_FLOOR


def _row(a: _Analysis, *, field: str, tag_value: str, tier: Tier, reason: str) -> MismatchRow:
    """Build a :class:`MismatchRow` from an analysis and the decided tier/reason."""
    return MismatchRow(
        file_id=a.file.file_id,
        folder=a.file.folder,
        filename=a.file.filename,
        field=field,
        tag_value=tag_value,
        path_artist=a.top_artist,
        tier=tier.value,
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class _Classified:
    """One emitted row plus whether it is review context (agrees with its path) or a defect."""

    row: MismatchRow
    context: bool


def _variant_fallback(a: _Analysis, albumartist: str) -> _Classified:
    """Classify a mixed-albumartist-folder file the tiered path branch did not claim.

    Naming-agnostic fallback, so it survives the reliability guard. The context/defect split
    keys on the FILE's own path agreement, never on which branch reached here: only a file
    with a live path signal that it agrees with is context. A file that disagrees (the guard
    merely denied it a path tier) or one whose path signal is nulled (container folder or
    library root, so agreement is unknown) stays flagged, each with its own reason.
    """
    context = a.top_artist is not None and not a.albumartist_disagrees
    if context:
        reason = _REASON_VARIANT
    elif a.albumartist_disagrees:
        reason = _REASON_SUPPRESSED
    else:
        reason = _REASON_NO_SIGNAL
    row = _row(a, field="albumartist", tag_value=albumartist, tier=Tier.LOW, reason=reason)
    return _Classified(row=row, context=context)


def _classify_albumartist(
    a: _Analysis,
    albumartist: str,
    stats: _FolderStats,
    *,
    suppressed: bool,
) -> _Classified | None:
    """Classify a file that carries a (non-VA) ``albumartist`` into a tier, or ``None``."""
    disagree = a.albumartist_disagrees
    variant = stats.is_variant(a.file.folder)
    guarded = stats.is_non_album(a.file.folder)

    tier: Tier | None = None
    reason = ""
    if disagree and not suppressed:
        if guarded:
            tier, reason = Tier.LOW, _REASON_GUARDED
        elif variant:
            tier, reason = Tier.HIGH, _REASON_HIGH
        else:
            tier, reason = Tier.MEDIUM, _REASON_MEDIUM
    if tier is None:
        return _variant_fallback(a, albumartist) if variant else None
    row = _row(a, field="albumartist", tag_value=albumartist, tier=tier, reason=reason)
    return _Classified(row=row, context=False)


def _classify_artist_fallback(
    a: _Analysis,
    artist: str,
    *,
    suppressed: bool,
) -> _Classified | None:
    """Classify a file that has no ``albumartist`` via its primary ``artist`` (LOW), or ``None``.

    A path-based check, so it is suppressed by the reliability guard alongside HIGH/MEDIUM.
    """
    if suppressed:
        return None
    primary = _primary_artist(artist)
    if not _disagrees(primary, a.file.path, a.top_artist):
        return None
    row = _row(a, field="artist", tag_value=artist, tier=Tier.LOW, reason=_REASON_ARTIST)
    return _Classified(row=row, context=False)


def _classify_file(
    a: _Analysis,
    stats: _FolderStats,
    *,
    suppressed: bool,
) -> _Classified | None:
    """Route one analyzed file to the albumartist classifier or the artist fallback."""
    f = a.file
    if f.albumartist is not None:
        if _is_va(f.albumartist):
            return None
        return _classify_albumartist(a, f.albumartist, stats, suppressed=suppressed)
    if f.artist is not None:
        return _classify_artist_fallback(a, f.artist, suppressed=suppressed)
    return None


def _disposition_blocks(disposition: store.MismatchStatusRow, f: _FileInput) -> bool:
    """Whether *f*'s stored disposition is still fresh (its snapshotted tag is unchanged).

    Delegates to :data:`tagmend.engine.axis.MISMATCH_AXIS`'s predicate, so this skip path and
    the user-facing :func:`tagmend.engine.store.derived_mismatch_status` share one rule. The
    identity is built from the SAME cleaned first values the detector already loaded, so no
    per-file query is needed.
    """
    return axis.MISMATCH_AXIS.decision_blocks(
        axis.StatusRow(
            status=disposition.status,
            source_primary=disposition.source_field,
            source_secondary=disposition.source_value,
        ),
        axis.Identity(primary=f.albumartist, secondary=f.artist),
    )


def _apply_skip_filter(
    classified: list[_Classified],
    files_by_id: dict[int, _FileInput],
    dispositions: dict[int, store.MismatchStatusRow],
) -> tuple[list[_Classified], dict[str, int], dict[str, dict[str, int]]]:
    """Drop every emitted row whose file has a FRESH disposition; tally what was silenced.

    Returns ``(kept, suppressed_by_status, suppressed_by_folder)``. A stale disposition (its
    snapshotted tag has since changed) does not silence, so the row re-surfaces. Context rows
    are filtered alongside flagged ones, so a user disposition still applies to them.
    """
    kept: list[_Classified] = []
    suppressed: dict[str, int] = {}
    by_folder: dict[str, dict[str, int]] = {}
    for item in classified:
        row = item.row
        disposition = dispositions.get(row.file_id)
        if disposition is not None and _disposition_blocks(disposition, files_by_id[row.file_id]):
            suppressed[disposition.status] = suppressed.get(disposition.status, 0) + 1
            folder_counts = by_folder.setdefault(row.folder, {})
            folder_counts[disposition.status] = folder_counts.get(disposition.status, 0) + 1
            continue
        kept.append(item)
    return kept, suppressed, by_folder


def _classify(
    files: list[_FileInput],
    music_path: Path,
    *,
    dispositions: dict[int, store.MismatchStatusRow] | None = None,
    container_folders: frozenset[str] = frozenset(),
) -> MismatchReport:
    """Classify constructed file inputs into a full :class:`MismatchReport` (pure core).

    Assumes each input's ``albumartist``/``artist`` is already cleaned (``None`` or a
    non-empty string). Produces every flagged row over ALL files (the folder stats +
    reliability guard are computed over every file), then applies the disposition skip-filter
    so a fresh ``legit_ignore``/``misfiled_deferred`` silences its file's row (reported in the
    report's ``suppressed`` map). *container_folders* is the pre-folded set of top-level folder
    names whose path signal is suppressed (counted in the report's ``container_suppressed`` map).
    tier/limit/group narrowing is applied later by :func:`detect_mismatches`.
    """
    stats = _folder_stats(files)
    analyses = [_analyze(f, music_path, container_folders=container_folders) for f in files]
    rate, suppressed = _reliability(analyses)

    container_suppressed: dict[str, int] = {}
    for a in analyses:
        if a.container_folder is not None:
            container_suppressed[a.container_folder] = (
                container_suppressed.get(a.container_folder, 0) + 1
            )

    classified: list[_Classified] = []
    for a in analyses:
        result = _classify_file(a, stats, suppressed=suppressed)
        if result is not None:
            classified.append(result)
    classified.sort(key=lambda c: (_TIER_RANK[Tier(c.row.tier)], c.row.file_id))

    files_by_id = {f.file_id: f for f in files}
    kept, suppressed_dispositions, suppressed_by_folder = _apply_skip_filter(
        classified,
        files_by_id,
        dispositions or {},
    )
    return _assemble_report(
        [c.row for c in kept if not c.context],
        context_rows=[c.row for c in kept if c.context],
        total_files=len(files),
        rate=rate,
        suppressed=suppressed,
        suppressed_dispositions=suppressed_dispositions,
        suppressed_by_folder=suppressed_by_folder,
        container_suppressed=container_suppressed,
    )


def _assemble_report(  # noqa: PLR0913 - cohesive keyword-only report payload
    rows: list[MismatchRow],
    *,
    context_rows: list[MismatchRow],
    total_files: int,
    rate: float,
    suppressed: bool,
    suppressed_dispositions: dict[str, int],
    suppressed_by_folder: dict[str, dict[str, int]],
    container_suppressed: dict[str, int],
) -> MismatchReport:
    """Freeze the kept rows + post-filter counts + guard diagnostics into a report.

    Only *rows* feed the tier tallies and ``flagged``; *context_rows* are counted apart.
    """
    high = sum(1 for r in rows if r.tier == Tier.HIGH)
    medium = sum(1 for r in rows if r.tier == Tier.MEDIUM)
    low = sum(1 for r in rows if r.tier == Tier.LOW)
    summary = _summarize(
        high=high,
        medium=medium,
        low=low,
        folder_context=len(context_rows),
        total_files=total_files,
        suppressed=suppressed,
        suppressed_count=sum(suppressed_dispositions.values()),
        container_suppressed=container_suppressed,
    )
    return MismatchReport(
        rows=rows,
        total_files=total_files,
        flagged=len(rows),
        high=high,
        medium=medium,
        low=low,
        folder_context=len(context_rows),
        folder_context_rows=context_rows,
        disagreement_rate=rate,
        path_signal_suppressed=suppressed,
        summary=summary,
        suppressed=suppressed_dispositions,
        container_suppressed=container_suppressed,
        groups=[],
        suppressed_by_folder=suppressed_by_folder,
    )


def _summarize(  # noqa: PLR0913 - cohesive keyword-only summary inputs
    *,
    high: int,
    medium: int,
    low: int,
    folder_context: int,
    total_files: int,
    suppressed: bool,
    suppressed_count: int,
    container_suppressed: dict[str, int],
) -> str:
    """Build a short, plain human summary of the run.

    The ``suppressed_count`` and container clauses are each appended ONLY when they silenced
    something, so with zero dispositions and no container folders the summary is byte-for-byte
    identical to the pre-container detector.
    """
    note = " (path signal suppressed: folder-consistency only)" if suppressed else ""
    silenced = f" ({suppressed_count} silenced by disposition)" if suppressed_count else ""
    container_count = sum(container_suppressed.values())
    container = (
        f" ({container_count} file(s) in {len(container_suppressed)} container folder(s) "
        "path-suppressed)"
        if container_count
        else ""
    )
    context = f", plus {folder_context} folder-context file(s) that agree with their path"
    return (
        f"Flagged {high + medium + low} of {total_files} file(s): "
        f"{high} high, {medium} medium, {low} low{note}{context}.{silenced}{container}"
    )


# --- public entry --------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Strip *value* and return ``None`` when it is missing or blank."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _limit_report(report: MismatchReport, *, tier: str | None, limit: int | None) -> MismatchReport:
    """Narrow a report's ``rows`` to one *tier* and/or the first *limit*, counts unchanged.

    A *tier* query asks for defects of that tier, so it drops the context rows entirely (they
    carry a tier but sit outside the tier tallies); *limit* caps each list on its own.
    """
    rows = report.rows
    context_rows = report.folder_context_rows
    if tier is not None:
        rows = [r for r in rows if r.tier == tier]
        context_rows = []
    if limit is not None:
        rows = rows[:limit]
        context_rows = context_rows[:limit]
    if rows is report.rows and context_rows is report.folder_context_rows:
        return report
    return replace(report, rows=rows, folder_context_rows=context_rows)


def _expand_folder(
    report: MismatchReport,
    folder: str,
    *,
    tier: str | None,
    limit: int | None,
) -> MismatchReport:
    """Return the flat rows of EXACTLY *folder* (path equality, never LIKE), tier/limit applied."""
    rows = [r for r in report.rows if r.folder == folder]
    context_rows = [r for r in report.folder_context_rows if r.folder == folder]
    narrowed = replace(report, rows=rows, folder_context_rows=context_rows)
    return _limit_report(narrowed, tier=tier, limit=limit)


def _group_by_folder(rows: list[MismatchRow]) -> dict[str, list[MismatchRow]]:
    """Bucket *rows* by their folder, preserving each folder's row order."""
    buckets: dict[str, list[MismatchRow]] = {}
    for row in rows:
        buckets.setdefault(row.folder, []).append(row)
    return buckets


def _build_groups(
    rows: list[MismatchRow],
    context_rows: list[MismatchRow],
    stats: _FolderStats,
    suppressed_by_folder: dict[str, dict[str, int]],
) -> list[MismatchGroup]:
    """Collapse flagged *rows* + *context_rows* into one group per folder (folder-sorted).

    A folder appears when either list holds a row, but ONLY the flagged rows feed
    ``tag_values``/``tiers``/``fields``/``file_ids``, so the ``stage_tags_batch`` fix flow can
    never pick up a context file (one that already agrees with its path).
    """
    by_folder = _group_by_folder(rows)
    context_by_folder = _group_by_folder(context_rows)
    groups: list[MismatchGroup] = []
    for folder in sorted(by_folder.keys() | context_by_folder.keys()):
        folder_rows = by_folder.get(folder, [])
        folder_context = context_by_folder.get(folder, [])
        tag_values: dict[str, int] = {}
        tiers: dict[str, int] = {}
        fields: set[str] = set()
        for r in folder_rows:
            tag_values[r.tag_value] = tag_values.get(r.tag_value, 0) + 1
            tiers[r.tier] = tiers.get(r.tier, 0) + 1
            fields.add(r.field)
        groups.append(
            MismatchGroup(
                folder=folder,
                path_artist=(folder_rows or folder_context)[0].path_artist,
                file_count=stats.file_count.get(folder, 0),
                flagged=len(folder_rows),
                folder_context=len(folder_context),
                tag_values=tag_values,
                tiers=tiers,
                fields=sorted(fields),
                file_ids=sorted(r.file_id for r in folder_rows),
                suppressed=dict(suppressed_by_folder.get(folder, {})),
            ),
        )
    return groups


def _grouped_report(
    report: MismatchReport,
    stats: _FolderStats,
    *,
    tier: str | None,
    limit: int | None,
) -> MismatchReport:
    """Build the grouped view: tier filters rows, group by folder, then *limit* caps groups.

    A *tier* query drops the context rows, exactly as the flat view does.
    """
    rows = report.rows if tier is None else [r for r in report.rows if r.tier == tier]
    context_rows = report.folder_context_rows if tier is None else []
    groups = _build_groups(rows, context_rows, stats, report.suppressed_by_folder)
    if limit is not None:
        groups = groups[:limit]
    return replace(report, rows=[], folder_context_rows=[], groups=groups)


def detect_mismatches(
    settings: Settings,
    *,
    tier: str | None = None,
    limit: int | None = None,
    group: bool = False,
    folder: str | None = None,
) -> MismatchReport:
    """Detect files whose ``albumartist``/``artist`` tag disagrees with their folder path.

    A read-only scan over the ``files``/``file_tags`` snapshot: no tag writes, nothing staged,
    no network. Every non-missing tracked file is classified into a HIGH/MEDIUM/LOW confidence
    tier (or left unflagged); the library-wide path-disagreement rate drives a reliability
    guard that suppresses the HIGH/MEDIUM path tiers when the path likely does not encode
    artist (``path_signal_suppressed``). A file in a mixed-albumartist folder that agrees with
    its own path is review context, not a defect: it lands in ``folder_context_rows`` outside
    ``flagged`` and the tier counts. Files under a configured ``container_folders`` top
    folder have their path signal suppressed and are counted in the report's
    ``container_suppressed`` map. Files with a still-fresh disposition (set via
    :func:`set_mismatch_status`) are dropped from the flagged rows and reported in the report's
    ``suppressed`` map.

    *tier* narrows the returned ``rows`` to one tier (``high`` | ``medium`` | ``low``); *limit*
    caps rows (or groups, in the grouped view); the counts always describe the whole library
    minus fresh dispositions. *group* returns one compact :class:`MismatchGroup` per folder
    (``rows`` then empty); *folder* returns the flat rows of exactly that folder (exact path
    equality, never a prefix/LIKE match) and takes precedence over *group*. Raises
    :class:`ValueError` when no music path is configured (mirrors
    :func:`tagmend.engine.library.scan_library`) or for an unknown *tier*.
    """
    if settings.music_path is None:
        message = "music_path not configured — run `tagmend config-set music_path <dir>`"
        raise ValueError(message)
    if tier is not None and tier not in _TIERS:
        message = f"unknown tier: {tier!r} (expected one of {sorted(_TIERS)})"
        raise ValueError(message)

    music_path = settings.music_path
    container_folders = frozenset(fold(name) for name in settings.container_folders)
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        file_rows = store.list_files(connection)
        tag_values = store.load_tag_values(connection, _DETECT_FIELDS)
        dispositions = store.load_mismatch_statuses(connection)
        files = [
            _FileInput(
                file_id=row.id,
                folder=row.folder,
                filename=row.filename,
                albumartist=_clean(tag_values.get(row.id, {}).get("albumartist")),
                artist=_clean(tag_values.get(row.id, {}).get("artist")),
            )
            for row in file_rows
            if not row.is_missing
        ]
    finally:
        connection.close()

    report = _classify(
        files,
        music_path,
        dispositions=dispositions,
        container_folders=container_folders,
    )
    logger.info(
        "detect complete: total=%d flagged=%d high=%d medium=%d low=%d context=%d rate=%.3f "
        "suppressed_path=%s silenced=%d",
        report.total_files,
        report.flagged,
        report.high,
        report.medium,
        report.low,
        report.folder_context,
        report.disagreement_rate,
        report.path_signal_suppressed,
        sum(report.suppressed.values()),
    )

    if folder is not None:
        return _expand_folder(report, folder, tier=tier, limit=limit)
    if group:
        return _grouped_report(report, _folder_stats(files), tier=tier, limit=limit)
    return _limit_report(report, tier=tier, limit=limit)


# --- disposition verbs (the module's only writers; status rows only, never tags) -----


def _snapshot_source(tags: dict[str, list[str]]) -> tuple[str | None, str | None]:
    """Snapshot the disagreeing tag by detect priority: albumartist-if-present, else artist.

    Returns ``(source_field, source_value)`` using the SAME cleaning the detector applies, so
    the freshness re-check compares like with like. Both ``None`` when the file has neither a
    non-blank ``albumartist`` nor ``artist``.
    """
    albumartist = _first_clean(tags, "albumartist")
    if albumartist is not None:
        return "albumartist", albumartist
    artist = _first_clean(tags, "artist")
    if artist is not None:
        return "artist", artist
    return None, None


def _first_clean(tags: dict[str, list[str]], name: str) -> str | None:
    """Return the cleaned ordinal-0 value of *name*, or ``None`` when absent/blank."""
    values = tags.get(name, [])
    return _clean(values[0]) if values else None


def _mismatch_scope(
    conn: sqlite3.Connection,
    *,
    file_ids: list[int] | None,
    value: str | None,
) -> list[int]:
    """Resolve the in-scope file ids for the disposition verbs, in ascending id order.

    *file_ids* (when given) win; otherwise *value* matches every file carrying it as
    ``artist`` OR ``albumartist`` (the union across both name fields, mirroring
    :func:`tagmend.engine.artists._artist_scope`). With neither given the scope is empty.
    """
    if file_ids is not None:
        return store.files_in_scope(conn, file_ids=file_ids)
    if value is None:
        return []
    matched = set(store.files_by_tag_value(conn, "artist", value))
    matched.update(store.files_by_tag_value(conn, "albumartist", value))
    return sorted(matched)


def set_mismatch_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
    status: str,
) -> int:
    """Set a sticky mismatch disposition (or clear it with ``pending``) for every file in scope.

    ``legit_ignore`` silences a false positive; ``misfiled_deferred`` defers a genuinely
    misfiled file — both snapshot the file's current disagreeing tag (``source_field`` /
    ``source_value``) so a later tag change makes the disposition stale and the file
    re-surfaces on the next detect. ``pending`` deletes any row (re-queue). Scope is
    *file_ids* when given, else every file carrying *value* as ``artist`` OR ``albumartist``.
    Returns the number of files affected. Raises :class:`ValueError` for an unknown *status*.
    Owns its transaction; writes only ``file_mismatch_status`` rows.
    """
    if status not in _USER_MISMATCH_STATUSES:
        message = f"invalid status: {status!r} (expected legit_ignore|misfiled_deferred|pending)"
        raise ValueError(message)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        scoped = _mismatch_scope(connection, file_ids=file_ids, value=value)
        now = _utc_now()
        for fid in scoped:
            if status == "pending":
                store.delete_mismatch_status(connection, fid)
            else:
                source_field, source_value = _snapshot_source(store.get_tags(connection, fid))
                store.set_mismatch_status(
                    connection,
                    file_id=fid,
                    status=status,
                    source_field=source_field,
                    source_value=source_value,
                    now=now,
                )
        connection.commit()
    finally:
        connection.close()

    logger.info("set mismatch status=%s for %d file(s)", status, len(scoped))
    return len(scoped)


def reset_mismatch_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> int:
    """Delete the mismatch disposition for every file in scope (back to ``pending``).

    Same value-across-both-fields scoping as :func:`set_mismatch_status`. Returns the number
    of files affected. Owns its transaction.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        scoped = _mismatch_scope(connection, file_ids=file_ids, value=value)
        for fid in scoped:
            store.delete_mismatch_status(connection, fid)
        connection.commit()
    finally:
        connection.close()

    logger.info("reset mismatch status for %d file(s)", len(scoped))
    return len(scoped)
