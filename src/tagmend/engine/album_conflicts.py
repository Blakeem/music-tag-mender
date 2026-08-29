"""Intra-folder album coherence: files in one folder that describe more than one release.

The release-level sibling of :mod:`tagmend.engine.track_conflicts`, which compares a file's
track slot against its folder siblings, and of :mod:`tagmend.engine.mismatch`, which compares
a file's tags against the folder PATH. Here the comparison is a file's **album identity**
against the same identity on its folder siblings.

A folder holding one album describes one release. Every music server groups tracks into an
album by some tuple of the release tags, so when a folder's files disagree on that tuple the
one album is presented as several. The identity used here is the tuple every such scheme
agrees on:

* ``musicbrainz_albumid`` when present. It is an explicit claim about which release this is,
  and it settles the file on its own.
* otherwise the display album artist, the album title and the year. The display album artist
  is ``albumartist``, falling back to ``Various Artists`` when the compilation flag is set,
  then to ``artist``. The compilation marker outranks the track artist, which is what keeps a
  various-artists release with no album artist from scattering across every track's artist.

Titles and names are compared under :func:`_group_key`, which folds casing, typographic
character choice and whitespace runs and nothing else. Punctuation is deliberately
significant: ``The Crow: City of Angels`` and ``The Crow- City Of Angels`` are two albums
downstream. A year is compared verbatim, because ``2005`` and ``2005-06-01`` are two grouping
keys as well.

The report names the **minority**: the files whose identity differs from the one most of the
folder shares, and every row carries that majority identity so a reviewer can see what the
folder mostly says. A count is not a verdict, so the majority is a starting point and never a
proposal. Read-only, like every ``detect_*`` tool. It writes nothing.

One shape gets its own treatment, because the minority rule is actively misleading on it.
When every file in a folder agrees on the album title, none carries an ``albumartist``, and
the track artists differ, the folder is a compilation missing its album artist. Every file
falls back to its own artist, so the one album shows as one card per track. Every file is
flagged, because every file needs the same fix.

A file with a blank ``album`` is skipped. It has no release identity to contradict, it is a
gap rather than a conflict, and :mod:`tagmend.engine.album_gaps` already reports it. Counting
it here would double-report it and let a blank identity win the majority vote.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tagmend.engine import db, schema, store
from tagmend.engine.mismatch import NON_ALBUM_FOLDERS, fold
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from tagmend.config import Settings

logger = get_logger(__name__)

# The snapshot fields the detector reads. ``compilation`` is read but never rewritten: it is
# only how a various-artists folder says it has no single album artist.
_DETECT_FIELDS: Final = (
    "album",
    "albumartist",
    "artist",
    "musicbrainz_albumid",
    "year",
    "compilation",
)

# The values Go's ``strconv.ParseBool`` accepts, which is what a server actually tests the
# compilation tag with. ``yes`` is a real tag value in the wild and reads as false.
_COMPILATION_TRUE: Final = frozenset({"1", "t", "T", "true", "TRUE", "True"})

# Picard writes a titled multi-disc medium into ``album`` as ``<release> (disc N: <title>)``,
# and a ``(bonus disc: <title>)`` for an unnumbered one. The suffix is deliberate, so the
# folder is reported at the low tier rather than as an error. It is only ever consulted
# once the two base titles already match, so a real title carrying the word cannot trip it.
_DISC_SUFFIX: Final = re.compile(r"\s*[(\[][^()\[\]]*\bdisc\b[^()\[\]]*[)\]]\s*$", re.IGNORECASE)

# Typographic characters a server folds to ASCII before grouping. Deliberately NOT
# :func:`tagmend.engine.mismatch.fold`, which strips every non-alphanumeric character: that
# would erase ``The Crow: City of Angels`` against ``The Crow- City Of Angels``, which really
# are two albums downstream and are exactly what this detector exists to find. Case and
# surrounding or repeated whitespace are cosmetic. Punctuation is not.
TYPOGRAPHIC: Final[Mapping[str, str]] = {
    chr(codepoint): plain
    for codepoints, plain in (
        ((0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212), "-"),
        ((0x2018, 0x2019), "'"),
        ((0x201C, 0x201D), '"'),
        ((0x00A0, 0x2009, 0x202F), " "),
    )
    for codepoint in codepoints
}

_UNKNOWN_ARTIST: Final = "[Unknown Artist]"
_VARIOUS_ARTISTS: Final = "Various Artists"


class Tier(StrEnum):
    """How strongly the folder's files contradict each other about which release they are."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_TIER_RANK: Final = {Tier.HIGH: 0, Tier.MEDIUM: 1, Tier.LOW: 2}
_TIERS: Final = frozenset(t.value for t in Tier)

_REASON_HIGH: Final = (
    "this file's release id differs from the rest of the folder's, so it is a separate album"
)
_REASON_MEDIUM: Final = (
    "this file's album artist, album title or year differs from the rest of the folder's"
)
_REASON_LOW: Final = "this file's album carries a different disc suffix on the same release title"
_REASON_NO_ALBUMARTIST: Final = (
    "the folder agrees on one album title but no file carries an album artist, so each is "
    "filed under its own track artist"
)
_REASON_NON_ALBUM: Final = (
    "folder name says it is not one album, so several releases here are expected"
)


# --- inputs / intermediate analysis --------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FileInput:
    """One tracked file reduced to the fields the detector reads (cleaned scalars)."""

    file_id: int
    folder: str
    filename: str
    album: str | None = None
    albumartist: str | None = None
    artist: str | None = None
    release_id: str | None = None
    year: str | None = None
    compilation: str | None = None

    @property
    def display_album_artist(self) -> str:
        """Return the album artist a server would group this file under.

        The fallback chain every scheme shares: the album artist, else a various-artists
        marker when the compilation flag is set, else the track artist, else an
        unknown-artist placeholder. The compilation marker outranks the track artist, so a
        various-artists release with no album artist stays one album instead of scattering
        across every track's artist.
        """
        if self.albumartist and self.albumartist.strip():
            return self.albumartist.strip()
        if (self.compilation or "").strip() in _COMPILATION_TRUE:
            return _VARIOUS_ARTISTS
        if self.artist and self.artist.strip():
            return self.artist.strip()
        return _UNKNOWN_ARTIST

    @property
    def identity(self) -> tuple[str, ...]:
        """Return the tuple that decides which album this file belongs to."""
        release_id = (self.release_id or "").strip()
        if release_id:
            return ("release", release_id)
        return (
            "name",
            group_key(self.display_album_artist),
            group_key(self.album or ""),
            (self.year or "").strip(),
        )

    @property
    def identity_label(self) -> str:
        """Return a short human label for this file's identity, for the grouped view."""
        release_id = (self.release_id or "").strip()
        if release_id:
            return f"release:{release_id}"
        year = (self.year or "").strip()
        suffix = f" [{year}]" if year else ""
        return f"{self.display_album_artist} - {self.album or ''}{suffix}"


# --- public result types -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlbumConflictRow:
    """One flagged file: the identity it carries, the one its folder shares, and why."""

    file_id: int
    folder: str
    filename: str
    album: str | None
    albumartist: str | None
    release_id: str | None
    year: str | None
    identity: str
    majority_identity: str
    tier: str  # Tier value
    reason: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "file_id": self.file_id,
            "folder": self.folder,
            "filename": self.filename,
            "album": self.album,
            "albumartist": self.albumartist,
            "release_id": self.release_id,
            "year": self.year,
            "identity": self.identity,
            "majority_identity": self.majority_identity,
            "tier": self.tier,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AlbumConflictGroup:
    """One folder's split, compact enough to scan a whole library at a glance."""

    folder: str
    file_count: int
    flagged: int
    folder_context: int
    identities: int
    majority_identity: str
    majority_files: int
    tiers: dict[str, int]
    file_ids: list[int]

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "folder": self.folder,
            "file_count": self.file_count,
            "flagged": self.flagged,
            "folder_context": self.folder_context,
            "identities": self.identities,
            "majority_identity": self.majority_identity,
            "majority_files": self.majority_files,
            "tiers": self.tiers,
            "file_ids": self.file_ids,
        }


@dataclass(frozen=True, slots=True)
class AlbumConflictsReport:
    """Immutable summary of one :func:`detect_album_conflicts` run, JSON-ready for the tool.

    The ``high``/``medium``/``low``/``flagged`` counts describe the whole library and are
    unaffected by a ``tier``/``limit``/``folder`` narrowing, so a filtered view still shows
    the full picture of what remains actionable. The tier counts always sum to ``flagged``;
    ``folder_context`` is outside both.
    """

    rows: list[AlbumConflictRow]
    total_files: int
    flagged: int
    high: int
    medium: int
    low: int
    summary: str
    folder_context: int = 0
    folder_context_rows: list[AlbumConflictRow] = field(default_factory=list)
    groups: list[AlbumConflictGroup] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "rows": [r.to_dict() for r in self.rows],
            "total_files": self.total_files,
            "flagged": self.flagged,
            "high": self.high,
            "medium": self.medium,
            "low": self.low,
            "folder_context": self.folder_context,
            "folder_context_rows": [r.to_dict() for r in self.folder_context_rows],
            "groups": [g.to_dict() for g in self.groups],
            "summary": self.summary,
        }


# --- pure classifier -----------------------------------------------------------------


def group_key(value: str) -> str:
    """Return the comparison key for an album or artist string.

    Casing, typographic character choice and whitespace runs are cosmetic. Every other
    difference separates two albums for anything reading these tags. Shared with
    :mod:`tagmend.engine.disagreements`, which needs the same judgement about what is
    cosmetic when it compares the same fields against MusicBrainz.
    """
    # NFC first, so the two byte-forms of one accented string compare equal. Deliberately not
    # NFKD-with-marks-stripped like :func:`tagmend.engine.mismatch.fold`: that folds an accent
    # away, and an accent really does separate two albums for anything reading these tags.
    folded = "".join(TYPOGRAPHIC.get(ch, ch) for ch in unicodedata.normalize("NFC", value))
    return " ".join(folded.casefold().split())


def _base_title(album: str | None) -> str:
    """Return *album* with a trailing ``(disc N …)`` segment removed, folded."""
    return group_key(_DISC_SUFFIX.sub("", album or ""))


def _is_compilation_missing_its_album_artist(files: list[_FileInput]) -> bool:
    """Return whether this folder is one album whose files have no album artist between them.

    Three real soundtrack folders take this shape: every file agrees on the album title, none
    carries an ``albumartist``, and the track artists all differ, so each file falls back to
    its own artist and the one album shows as one card per track. The ordinary minority rule
    is actively misleading here. It would name whichever guest artist appears most as the
    identity to normalize toward, when the real fix is the same on every file: give them all
    an album artist.
    """
    minimum = 2
    if len(files) < minimum:
        return False
    if any((f.albumartist or "").strip() for f in files):
        return False
    if any((f.compilation or "").strip() in _COMPILATION_TRUE for f in files):
        return False
    if len({group_key(f.album or "") for f in files}) != 1:
        return False

    # A dominant track artist means this is that artist's album with a guest or two, and the
    # album artist to fill in is theirs. Only when no artist holds half the folder is it a
    # compilation, where the album artist is a various-artists marker instead. Without this,
    # a normal album carrying one guest track would be read as a compilation.
    artists = Counter(group_key(f.artist or "") for f in files)
    if not artists:
        return False
    top = artists.most_common(1)[0][1]
    # No artist dominates when every one of them appears exactly once, whatever the folder
    # size. The proportional test alone can never be true for a two-file folder, which would
    # leave the smallest compilation with the misleading verdict this case exists to prevent.
    return top == 1 or top * 2 < len(files)


def _is_context_folder(files: list[_FileInput]) -> str | None:
    """Return the context reason when this folder is not meant to hold one album.

    A folder named for a collection rather than a release (``Singles``, ``Remixes``) holds
    several releases by design, so its split is expected and never a defect.
    """
    leaf = Path(files[0].folder).name
    if fold(leaf) in {fold(name) for name in NON_ALBUM_FOLDERS}:
        return _REASON_NON_ALBUM
    return None


def _tier_for(minority: _FileInput, majority: _FileInput) -> tuple[Tier, str]:
    """Return the tier and reason for *minority* against its folder's *majority* file."""
    minority_id = (minority.release_id or "").strip()
    majority_id = (majority.release_id or "").strip()
    if minority_id or majority_id:
        return (Tier.HIGH, _REASON_HIGH)
    # Both tests run at fold level. Comparing the raw strings for inequality made a folder
    # split by album artist or year, whose album strings differ only cosmetically, report a
    # disc suffix that is not there.
    minority_album = group_key(minority.album or "")
    majority_album = group_key(majority.album or "")
    if (
        _base_title(minority.album) == _base_title(majority.album)
        and minority_album != majority_album
    ):
        return (Tier.LOW, _REASON_LOW)
    return (Tier.MEDIUM, _REASON_MEDIUM)


def _rows_for_folder(files: list[_FileInput]) -> tuple[list[AlbumConflictRow], _FileInput, int]:
    """Return the minority rows for one split folder, plus its majority file and size."""
    counts = Counter(f.identity for f in files)
    # Ties go to the identity seen first, so the report is stable across runs.
    best = max(counts, key=lambda identity: (counts[identity], -_first_index(files, identity)))
    majority = next(f for f in files if f.identity == best)

    rows: list[AlbumConflictRow] = []
    for f in files:
        if f.identity == best:
            continue
        tier, reason = _tier_for(f, majority)
        rows.append(
            AlbumConflictRow(
                file_id=f.file_id,
                folder=f.folder,
                filename=f.filename,
                album=f.album,
                albumartist=f.albumartist,
                release_id=f.release_id,
                year=f.year,
                identity=f.identity_label,
                majority_identity=majority.identity_label,
                tier=tier.value,
                reason=reason,
            ),
        )
    return (rows, majority, counts[best])


def _compilation_rows(files: list[_FileInput]) -> list[AlbumConflictRow]:
    """Return one row per file for a folder that is one album with no album artist at all.

    ``majority_identity`` names the identity these files should share once they carry an album
    artist, in the same shape every other row uses, so a consumer parses one form.
    """
    shared = _VARIOUS_ARTISTS + " - " + (files[0].album or "")
    return [
        AlbumConflictRow(
            file_id=f.file_id,
            folder=f.folder,
            filename=f.filename,
            album=f.album,
            albumartist=f.albumartist,
            release_id=f.release_id,
            year=f.year,
            identity=f.identity_label,
            majority_identity=shared,
            tier=Tier.HIGH.value,
            reason=_REASON_NO_ALBUMARTIST,
        )
        for f in files
    ]


def _first_index(files: list[_FileInput], identity: tuple[str, ...]) -> int:
    """Return the position of the first file carrying *identity* (for stable tie-breaking)."""
    return next(i for i, f in enumerate(files) if f.identity == identity)


def _classify(files: list[_FileInput]) -> AlbumConflictsReport:
    """Group *files* by folder and report every folder describing more than one release."""
    # Input: the folders, in first-seen order so the report is stable.
    by_folder: dict[str, list[_FileInput]] = defaultdict(list)
    for f in files:
        if not (f.album or "").strip():
            continue
        by_folder[f.folder].append(f)

    # Process: one pass per folder, splitting real defects from expected context.
    rows: list[AlbumConflictRow] = []
    context_rows: list[AlbumConflictRow] = []
    groups: list[AlbumConflictGroup] = []
    for folder, members in by_folder.items():
        if len({f.identity for f in members}) < 2:  # noqa: PLR2004 - one identity is coherent
            continue
        if _is_compilation_missing_its_album_artist(members):
            folder_rows = _compilation_rows(members)
            majority_label = _VARIOUS_ARTISTS + " - " + (members[0].album or "")
            majority_files = 0
        else:
            folder_rows, majority, majority_files = _rows_for_folder(members)
            majority_label = majority.identity_label
        context_reason = _is_context_folder(members)
        if context_reason is not None:
            folder_rows = [replace(r, reason=context_reason) for r in folder_rows]
            context_rows.extend(folder_rows)
        else:
            rows.extend(folder_rows)
        groups.append(
            AlbumConflictGroup(
                folder=folder,
                file_count=len(members),
                flagged=0 if context_reason else len(folder_rows),
                folder_context=len(folder_rows) if context_reason else 0,
                identities=len({f.identity for f in members}),
                majority_identity=majority_label,
                majority_files=majority_files,
                tiers=dict(Counter(r.tier for r in folder_rows)) if not context_reason else {},
                file_ids=[f.file_id for f in members],
            ),
        )

    # Output: the whole-library counts, which no later narrowing changes.
    tiers = Counter(r.tier for r in rows)
    return AlbumConflictsReport(
        rows=sorted(rows, key=lambda r: (_TIER_RANK[Tier(r.tier)], r.folder, r.filename)),
        total_files=len(files),
        flagged=len(rows),
        high=tiers.get(Tier.HIGH.value, 0),
        medium=tiers.get(Tier.MEDIUM.value, 0),
        low=tiers.get(Tier.LOW.value, 0),
        summary=_summarize(
            flagged=len(rows),
            total=len(files),
            folders=sum(1 for g in groups if g.flagged),
            context=len(context_rows),
            tiers=tiers,
        ),
        folder_context=len(context_rows),
        folder_context_rows=context_rows,
        groups=groups,
    )


def _summarize(
    *,
    flagged: int,
    total: int,
    folders: int,
    context: int,
    tiers: Counter[str],
) -> str:
    """Build a short, plain human summary of the run."""
    if not flagged and not context:
        return f"Every folder describes one album ({total} file(s) checked)."
    head = (
        f"{flagged} file(s) across {folders} folder(s) describe a different album than "
        f"their folder siblings ({total} file(s) checked): "
        f"{tiers.get(Tier.HIGH.value, 0)} high, {tiers.get(Tier.MEDIUM.value, 0)} medium, "
        f"{tiers.get(Tier.LOW.value, 0)} low."
    )
    if context:
        head += f" {context} more sit in folders not meant to hold one album (review context)."
    return head


# --- view narrowing ------------------------------------------------------------------


def _narrow(
    report: AlbumConflictsReport,
    *,
    tier: str | None,
    folder: str | None,
    limit: int | None,
    group: bool = False,
) -> AlbumConflictsReport:
    """Return *report* with its rows filtered for display; the library counts never change.

    Every filter applies to the context rows as well as the flagged ones. Without that, a
    caller expanding one folder also received every ``Singles``/``Remixes`` context row in the
    library. Groups ride only on the grouped view, which is what ``detect_track_conflicts``
    does, so a flat call does not also ship a line per folder.
    """
    rows = report.rows
    context_rows = report.folder_context_rows
    if tier is not None:
        rows = [r for r in rows if r.tier == tier]
        context_rows = []
    if folder is not None:
        rows = [r for r in rows if r.folder == folder]
        context_rows = [r for r in context_rows if r.folder == folder]
    if limit is not None:
        rows = rows[:limit]
        context_rows = context_rows[:limit]

    groups = report.groups
    if folder is not None:
        groups = [g for g in groups if g.folder == folder]
    if limit is not None:
        groups = groups[:limit]

    return AlbumConflictsReport(
        rows=[] if group else rows,
        total_files=report.total_files,
        flagged=report.flagged,
        high=report.high,
        medium=report.medium,
        low=report.low,
        summary=report.summary,
        folder_context=report.folder_context,
        folder_context_rows=[] if group else context_rows,
        groups=groups if group else [],
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
                album=values.get("album"),
                albumartist=values.get("albumartist"),
                artist=values.get("artist"),
                release_id=values.get("musicbrainz_albumid"),
                year=values.get("year"),
                compilation=values.get("compilation"),
            ),
        )
    return inputs


def detect_album_conflicts(
    settings: Settings,
    *,
    tier: str | None = None,
    limit: int | None = None,
    group: bool = False,
    folder: str | None = None,
) -> AlbumConflictsReport:
    """Report files whose album identity differs from their folder siblings'.

    Read-only over the snapshot: run ``scan_library`` first. Raises :class:`ValueError` for an
    unknown *tier*.
    """
    if tier is not None and tier not in _TIERS:
        message = f"unknown tier {tier!r}; expected one of {sorted(_TIERS)}"
        raise ValueError(message)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        files = _load_inputs(connection)
    finally:
        connection.close()

    report = _classify(files)
    logger.info(
        "album conflicts: flagged=%s context=%s of %s file(s)",
        report.flagged,
        report.folder_context,
        report.total_files,
    )
    return _narrow(report, tier=tier, folder=folder, limit=limit, group=group)
