"""Intra-folder album coherence: files in one folder that describe more than one release.

The release-level sibling of :mod:`tagmend.engine.track_conflicts`, which compares a file's
track slot against its folder siblings, and of :mod:`tagmend.engine.mismatch`, which compares
a file's tags against the folder PATH. Here the comparison is a file's **album identity**
against the same identity on its folder siblings.

A folder holding one album describes one release. Every music server groups tracks into an
album by some tuple of the release tags, so when a folder's files disagree on that tuple the
one album is presented as several. The identity used here follows Navidrome's album key:

* ``musicbrainz_albumid`` when present. It is an explicit claim about which release this is,
  and it settles the file on its own.
* otherwise the display album artist, the album title and the release date. The release date
  is an M4A's ``date``, a FLAC's or Ogg's ``releasedate`` else ``year``, and none for an MP3,
  whose ``date`` is a recording date. The display album artist is ``albumartist``, falling
  back to ``Various Artists`` when the compilation flag is set, then to ``artist``. The compilation
  marker outranks the track artist, which is what keeps a various-artists release with no
  album artist from scattering across every track's artist.

Titles and names are compared under :func:`tagmend.engine.text_keys.display_key`, which folds
casing, typographic character choice and whitespace runs and nothing else. Punctuation is
deliberately significant: ``The Crow: City of Angels`` and ``The Crow- City Of Angels`` are
two albums downstream. A date is compared verbatim, because ``2005`` and ``2005-06-01`` are
two grouping keys as well.

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
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

from tagmend.engine import db, path_keys, schema, store
from tagmend.engine.detector_core import (
    COMPILATION_TRUE,
    TIER_RANK,
    VARIOUS_ARTISTS,
    Tier,
    album_identity,
    display_album_artist,
    group_by_folder,
    is_non_album_folder,
    narrow,
    release_date,
    validate_tier,
)
from tagmend.engine.serialize import FieldDict
from tagmend.engine.text_keys import display_key
from tagmend.engine.validation import check_limit
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings

logger = get_logger(__name__)

# The snapshot fields the detector reads. ``compilation`` is read but never rewritten: it is
# only how a various-artists folder says it has no single album artist.
_DETECT_FIELDS: Final = (
    "album",
    "albumartist",
    "artist",
    "musicbrainz_albumid",
    "date",
    "releasedate",
    "year",
    "compilation",
)

# Picard writes a titled multi-disc medium into ``album`` as ``<release> (disc N: <title>)``,
# and a ``(bonus disc: <title>)`` for an unnumbered one. The suffix is deliberate, so the
# folder is reported at the low tier rather than as an error. It is only ever consulted
# once the two base titles already match, so a real title carrying the word cannot trip it.
_DISC_SUFFIX: Final = re.compile(r"\s*[(\[][^()\[\]]*\bdisc\b[^()\[\]]*[)\]]\s*$", re.IGNORECASE)


_REASON_HIGH: Final = (
    "this file's release id differs from the rest of the folder's, so it is a separate album"
)
_REASON_MEDIUM: Final = (
    "this file's album artist, album title or release date differs from the rest of the folder's"
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
    release_mbid: str | None = None
    date: str | None = None
    compilation: str | None = None

    @property
    def display_album_artist(self) -> str:
        """Return the album artist a server would group this file under."""
        return display_album_artist(self.albumartist, self.compilation, self.artist)

    @property
    def identity(self) -> tuple[str, ...]:
        """Return the tuple that decides which album this file belongs to."""
        return album_identity(self.release_mbid, self.display_album_artist, self.album, self.date)

    @property
    def identity_label(self) -> str:
        """Return a short human label for this file's identity, for the grouped view."""
        release_mbid = (self.release_mbid or "").strip()
        if release_mbid:
            return f"release:{release_mbid}"
        date = (self.date or "").strip()
        suffix = f" [{date}]" if date else ""
        return f"{self.display_album_artist} - {self.album or ''}{suffix}"


# --- public result types -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlbumConflictRow(FieldDict):
    """One flagged file: the identity it carries, the one its folder shares, and why."""

    file_id: int
    folder: str
    filename: str
    album: str | None
    albumartist: str | None
    release_mbid: str | None
    date: str | None
    identity: str
    majority_identity: str
    tier: str  # Tier value
    reason: str


@dataclass(frozen=True, slots=True)
class AlbumConflictGroup(FieldDict):
    """One folder's split, compact enough to scan a whole library at a glance.

    ``file_count`` counts every present file in the folder, blank album included. ``file_ids``
    names the flagged files only, so a fix flow driven off a group never rewrites the majority.
    """

    folder: str
    file_count: int
    flagged: int
    folder_context: int
    identities: int
    majority_identity: str
    majority_files: int
    tiers: dict[str, int]
    file_ids: list[int]


@dataclass(frozen=True, slots=True)
class AlbumConflictsReport(FieldDict):
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
    folder_context: int = 0
    folder_context_rows: list[AlbumConflictRow] = field(default_factory=list)
    groups: list[AlbumConflictGroup] = field(default_factory=list)
    # Last, so the payload ends with it. Keyword-only keeps it required after the defaults.
    summary: str = field(kw_only=True)


# --- pure classifier -----------------------------------------------------------------


def _base_title(album: str | None) -> str:
    """Return *album* with a trailing ``(disc N …)`` segment removed, folded."""
    return display_key(_DISC_SUFFIX.sub("", album or ""))


def _is_compilation_missing_its_album_artist(files: list[_FileInput]) -> bool:
    """Return whether this folder is one album whose files have no album artist between them.

    Three real soundtrack folders take this shape: every file agrees on the album title (a disc
    suffix aside), none carries an ``albumartist``, and the track artists all differ, so each
    file falls back to its own artist and the one album shows as one card per track. The
    ordinary minority rule is actively misleading here. It would name whichever guest artist
    appears most as the identity to normalize toward, when the real fix is the same on every
    file: give them all an album artist.
    """
    minimum = 2
    if len(files) < minimum:
        return False
    if any((f.albumartist or "").strip() for f in files):
        return False
    if any((f.compilation or "").strip() in COMPILATION_TRUE for f in files):
        return False
    if len({_base_title(f.album) for f in files}) != 1:
        return False

    # A dominant track artist means this is that artist's album with a guest or two, and the
    # album artist to fill in is theirs. Only when no artist holds half the folder is it a
    # compilation, where the album artist is a various-artists marker instead. Without this,
    # a normal album carrying one guest track would be read as a compilation.
    artists = Counter(display_key(f.artist or "") for f in files)
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
    if is_non_album_folder(files[0].folder):
        return _REASON_NON_ALBUM
    return None


def _tier_for(minority: _FileInput, majority: _FileInput) -> tuple[Tier, str]:
    """Return the tier and reason for *minority* against its folder's *majority* file."""
    minority_id = (minority.release_mbid or "").strip()
    majority_id = (majority.release_mbid or "").strip()
    # Fold-level tests, since raw strings differing only cosmetically carry no disc suffix. A
    # disc suffix is low only as the sole difference, so an album artist or date split stays medium.
    same_base_title = _base_title(minority.album) == _base_title(majority.album)
    same_album = display_key(minority.album or "") == display_key(majority.album or "")
    same_album_artist = display_key(minority.display_album_artist) == display_key(
        majority.display_album_artist,
    )
    same_date = (minority.date or "").strip() == (majority.date or "").strip()
    if minority_id or majority_id:
        return (Tier.HIGH, _REASON_HIGH)
    if same_base_title and not same_album and same_album_artist and same_date:
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
                release_mbid=f.release_mbid,
                date=f.date,
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
    shared = _compilation_identity(files)
    return [
        AlbumConflictRow(
            file_id=f.file_id,
            folder=f.folder,
            filename=f.filename,
            album=f.album,
            albumartist=f.albumartist,
            release_mbid=f.release_mbid,
            date=f.date,
            identity=f.identity_label,
            majority_identity=shared,
            tier=Tier.HIGH.value,
            reason=_REASON_NO_ALBUMARTIST,
        )
        for f in files
    ]


def _compilation_identity(files: list[_FileInput]) -> str:
    """Return the identity a compilation folder's files share once they carry an album artist.

    The files may differ only in a disc suffix, so the label drops it rather than name one disc.
    """
    return VARIOUS_ARTISTS + " - " + _DISC_SUFFIX.sub("", files[0].album or "").strip()


def _first_index(files: list[_FileInput], identity: tuple[str, ...]) -> int:
    """Return the position of the first file carrying *identity* (for stable tie-breaking)."""
    return next(i for i, f in enumerate(files) if f.identity == identity)


def _classify(files: list[_FileInput]) -> AlbumConflictsReport:
    """Group *files* by folder and report every folder describing more than one release."""
    # Input: a folder's size counts its blank-album files too, which the comparison skips.
    folder_sizes = Counter(f.folder for f in files)
    by_folder = group_by_folder(f for f in files if (f.album or "").strip())

    # Process: one pass per folder, splitting real defects from expected context.
    rows: list[AlbumConflictRow] = []
    context_rows: list[AlbumConflictRow] = []
    groups: list[AlbumConflictGroup] = []
    for folder, members in by_folder.items():
        if len({f.identity for f in members}) < 2:  # noqa: PLR2004 - one identity is coherent
            continue
        if _is_compilation_missing_its_album_artist(members):
            folder_rows = _compilation_rows(members)
            majority_label = _compilation_identity(members)
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
        base = AlbumConflictGroup(
            folder=folder,
            file_count=folder_sizes[folder],
            flagged=0,
            folder_context=len(folder_rows) if context_reason else 0,
            identities=len({f.identity for f in members}),
            majority_identity=majority_label,
            majority_files=majority_files,
            tiers={},
            file_ids=[],
        )
        groups.append(base if context_reason else _refold_group(base, folder_rows))
    groups.sort(key=lambda g: g.folder)

    # Output: the whole-library counts, which no later narrowing changes.
    tiers = Counter(r.tier for r in rows)
    return AlbumConflictsReport(
        rows=sorted(rows, key=lambda r: (TIER_RANK[Tier(r.tier)], r.folder, r.filename)),
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


def _refold_group(
    group: AlbumConflictGroup,
    rows: list[AlbumConflictRow],
) -> AlbumConflictGroup:
    """Return *group* with its flagged counts describing exactly *rows*, its flagged rows."""
    return replace(
        group,
        flagged=len(rows),
        tiers=dict(Counter(r.tier for r in rows)),
        file_ids=sorted(r.file_id for r in rows),
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
                release_mbid=values.get("musicbrainz_albumid"),
                date=release_date(values, row.filename),
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
    logger.info(
        "album conflicts: flagged=%s context=%s of %s file(s)",
        report.flagged,
        report.folder_context,
        report.total_files,
    )
    return narrow(
        report,
        rows=report.rows,
        groups=report.groups,
        secondary_field="folder_context_rows",
        secondary_rows=report.folder_context_rows,
        secondary_in_tier=False,
        refold=_refold_group,
        tier=tier,
        folder_key=folder_key,
        limit=limit,
        group=group,
    )
