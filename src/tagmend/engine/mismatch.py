"""Mislabeled-file DETECTION: albumartist-vs-path disagreement, confidence-tiered.

A **read-only** report that flags files whose ``albumartist`` (with an ``artist`` fallback)
disagrees with the file's folder path — the fingerprint of a MusicBrainz Picard release
mis-match that stamped the WRONG identity tags onto files whose filenames/paths kept the
truth (e.g. Ozzy's *Down to Earth* files tagged as *Jem*). It writes nothing, stages
nothing, and never hits the network — a pure read over the existing ``files``/``file_tags``
snapshot.

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
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tagmend.engine import db, schema, store
from tagmend.log import get_logger

if TYPE_CHECKING:
    from tagmend.config import Settings

logger = get_logger(__name__)

# The two scalar fields the detector reads per file (ordinal-0 value of each).
_DETECT_FIELDS: Final = ("albumartist", "artist")

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
class MismatchReport:
    """Immutable summary of one :func:`detect_mismatches` run, JSON-ready for the MCP tool.

    ``rows`` is the (tier-filtered, capped) worklist; the ``high``/``medium``/``low``/
    ``flagged`` counts always describe the WHOLE library so a filtered view still shows the
    full picture.
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

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "rows": [row.to_dict() for row in self.rows],
            "total_files": self.total_files,
            "flagged": self.flagged,
            "high": self.high,
            "medium": self.medium,
            "low": self.low,
            "disagreement_rate": self.disagreement_rate,
            "path_signal_suppressed": self.path_signal_suppressed,
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


def _analyze(f: _FileInput, music_path: Path) -> _Analysis:
    """Precompute a file's top-artist folder and its ``albumartist`` path-disagreement."""
    top = _top_artist(f.folder, music_path)
    disagrees = (
        f.albumartist is not None
        and not _is_va(f.albumartist)
        and _disagrees(f.albumartist, f.path, top)
    )
    return _Analysis(file=f, top_artist=top, albumartist_disagrees=disagrees)


def _reliability(analyses: list[_Analysis]) -> tuple[float, bool]:
    """Return ``(disagreement_rate, suppressed)`` over non-VA files that have an albumartist."""
    considered = [
        a for a in analyses if a.file.albumartist is not None and not _is_va(a.file.albumartist)
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


def _classify_albumartist(
    a: _Analysis,
    albumartist: str,
    stats: _FolderStats,
    *,
    suppressed: bool,
) -> MismatchRow | None:
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
    if tier is None and variant:
        # Naming-agnostic fallback: a messy folder, but this file agrees with its path (or
        # the path signal is suppressed). Survives the reliability guard.
        tier, reason = Tier.LOW, _REASON_VARIANT
    if tier is None:
        return None
    return _row(a, field="albumartist", tag_value=albumartist, tier=tier, reason=reason)


def _classify_artist_fallback(
    a: _Analysis,
    artist: str,
    *,
    suppressed: bool,
) -> MismatchRow | None:
    """Classify a file that has no ``albumartist`` via its primary ``artist`` (LOW), or ``None``.

    A path-based check, so it is suppressed by the reliability guard alongside HIGH/MEDIUM.
    """
    if suppressed:
        return None
    primary = _primary_artist(artist)
    if not _disagrees(primary, a.file.path, a.top_artist):
        return None
    return _row(a, field="artist", tag_value=artist, tier=Tier.LOW, reason=_REASON_ARTIST)


def _classify_file(
    a: _Analysis,
    stats: _FolderStats,
    *,
    suppressed: bool,
) -> MismatchRow | None:
    """Route one analyzed file to the albumartist classifier or the artist fallback."""
    f = a.file
    if f.albumartist is not None:
        if _is_va(f.albumartist):
            return None
        return _classify_albumartist(a, f.albumartist, stats, suppressed=suppressed)
    if f.artist is not None:
        return _classify_artist_fallback(a, f.artist, suppressed=suppressed)
    return None


def _classify(files: list[_FileInput], music_path: Path) -> MismatchReport:
    """Classify constructed file inputs into a full :class:`MismatchReport` (pure core).

    Assumes each input's ``albumartist``/``artist`` is already cleaned (``None`` or a
    non-empty string). Produces every flagged row and the library-wide tier counts + guard
    diagnostics; tier/limit narrowing is applied later by :func:`detect_mismatches`.
    """
    stats = _folder_stats(files)
    analyses = [_analyze(f, music_path) for f in files]
    rate, suppressed = _reliability(analyses)

    rows: list[MismatchRow] = []
    for a in analyses:
        row = _classify_file(a, stats, suppressed=suppressed)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: (_TIER_RANK[Tier(r.tier)], r.file_id))
    return _assemble_report(rows, total_files=len(files), rate=rate, suppressed=suppressed)


def _assemble_report(
    rows: list[MismatchRow],
    *,
    total_files: int,
    rate: float,
    suppressed: bool,
) -> MismatchReport:
    """Freeze the flagged rows + counts + guard diagnostics into a :class:`MismatchReport`."""
    high = sum(1 for r in rows if r.tier == Tier.HIGH)
    medium = sum(1 for r in rows if r.tier == Tier.MEDIUM)
    low = sum(1 for r in rows if r.tier == Tier.LOW)
    summary = _summarize(
        high=high,
        medium=medium,
        low=low,
        total_files=total_files,
        suppressed=suppressed,
    )
    return MismatchReport(
        rows=rows,
        total_files=total_files,
        flagged=len(rows),
        high=high,
        medium=medium,
        low=low,
        disagreement_rate=rate,
        path_signal_suppressed=suppressed,
        summary=summary,
    )


def _summarize(
    *,
    high: int,
    medium: int,
    low: int,
    total_files: int,
    suppressed: bool,
) -> str:
    """Build a short, plain human summary of the run."""
    note = " (path signal suppressed: folder-consistency only)" if suppressed else ""
    return (
        f"Flagged {high + medium + low} of {total_files} file(s): "
        f"{high} high, {medium} medium, {low} low{note}."
    )


# --- public entry --------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Strip *value* and return ``None`` when it is missing or blank."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _limit_report(report: MismatchReport, *, tier: str | None, limit: int | None) -> MismatchReport:
    """Narrow a report's ``rows`` to one *tier* and/or the first *limit*, counts unchanged."""
    rows = report.rows
    if tier is not None:
        rows = [r for r in rows if r.tier == tier]
    if limit is not None:
        rows = rows[:limit]
    if rows is report.rows:
        return report
    return replace(report, rows=rows)


def detect_mismatches(
    settings: Settings,
    *,
    tier: str | None = None,
    limit: int | None = None,
) -> MismatchReport:
    """Detect files whose ``albumartist``/``artist`` tag disagrees with their folder path.

    A pure, read-only scan over the ``files``/``file_tags`` snapshot: no tag writes, nothing
    staged, no network. Every non-missing tracked file is classified into a HIGH/MEDIUM/LOW
    confidence tier (or left unflagged); the library-wide path-disagreement rate drives a
    reliability guard that suppresses the HIGH/MEDIUM path tiers when the path likely does not
    encode artist (``path_signal_suppressed``). *tier* narrows the returned ``rows`` to one
    tier (``high`` | ``medium`` | ``low``) and *limit* caps them; the report's counts always
    describe the whole library. Raises :class:`ValueError` when no music path is configured
    (mirrors :func:`tagmend.engine.library.scan_library`) or for an unknown *tier*.
    """
    if settings.music_path is None:
        message = "music_path not configured — run `tagmend config-set music_path <dir>`"
        raise ValueError(message)
    if tier is not None and tier not in _TIERS:
        message = f"unknown tier: {tier!r} (expected one of {sorted(_TIERS)})"
        raise ValueError(message)

    music_path = settings.music_path
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        file_rows = store.list_files(connection)
        tag_values = store.load_tag_values(connection, _DETECT_FIELDS)
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

    report = _classify(files, music_path)
    logger.info(
        "detect complete: total=%d flagged=%d high=%d medium=%d low=%d rate=%.3f suppressed=%s",
        report.total_files,
        report.flagged,
        report.high,
        report.medium,
        report.low,
        report.disagreement_rate,
        report.path_signal_suppressed,
    )
    return _limit_report(report, tier=tier, limit=limit)
