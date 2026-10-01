"""Blank-``album`` gap detection: group gapped files by folder, source a fill, propose it.

A grouped detect/report tool (decision-r2 C6). It groups files whose ``album`` tag is blank
across ALL ordinals by their folder and tries three grounding sources in order. The
**sibling** source fills blank files from a unanimous non-blank album value shared by their
folder mates. The **folder-parse** source fills a folder with no sibling values at all from its
parsed folder name, but only when the folder's own filenames self-corroborate it. The
review-only **mb_recording** source fills each blank file of a folder the first two sources
leave blank from a cached, paced MusicBrainz ``(artist, title)`` recording search. Each
proposal carries a confidence label and a provenance note pre-formatted for
``stage_tags_batch``. Nothing stages, nothing auto-commits. The first two sources are
network-free. The recording source is opt-out (``use_musicbrainz=False``) and its result is
``confidence: "review"`` (never green). The report feeds the existing ``stage_tags_batch →
diff_tags(path) → commit_tags(path)`` spine, where the human is the diff-gate.

The recording source's ONLY side effect is its persistent lookup cache
(``musicbrainz_recording_cache``): tags, status, and staging are untouched, so a re-run after
the first pass is network-free.

**Binding safety constraint (decision-r2 non-negotiable #1):** the ``stage_tags_batch``
merge does not guard against overwriting a present ``album`` — so a proposal is *only ever*
emitted for a file whose ``album`` is blank across every ordinal. That blank predicate
scans all ordinals via :func:`tagmend.engine.axis.first_nonblank` over
:func:`tagmend.engine.store.get_tags` (NOT the ordinal-0-only ``load_tag_values``), exactly
mirroring ``resolve_years``' ``skipped_no_album`` gate, so a file carrying a non-blank
album at any ordinal can never appear in a proposal.

Mirrors :mod:`tagmend.engine.mismatch` in shape (frozen result dataclasses with hand-written
``to_dict``, one group row per folder, ``limit``/``folder`` narrowing that leaves the
library-wide counts intact). Like the rest of the conn-owning layer, :func:`detect_album_gaps`
owns its connection (``connect`` → ``apply_schema`` → ``try/finally`` close); the only commits
are the recording client's cache writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tagmend.engine import axis, classify, db, lookup_clients, parsing, path_keys, schema, store
from tagmend.engine.detector_core import group_by_folder
from tagmend.engine.musicbrainz import MusicBrainzClient, MusicBrainzError
from tagmend.engine.tags import VERIFIABLE_SUFFIXES
from tagmend.engine.text_keys import alnum_key
from tagmend.engine.validation import check_limit
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.classify import Vocabulary
    from tagmend.engine.musicbrainz import MBRecordingSource

logger = get_logger(__name__)

# Fraction of a folder's filenames that must fold-contain the parsed album token before a
# folder-parse candidate is proposed (self-corroboration gate). Measured anchors: Bradley
# Nowell 14/14 proposes; Maphra YouTube 0/8 and Sublime Misc 0/35 propose nothing.
_CORROBORATION_THRESHOLD: Final = 0.6

# Minimum unanimous sibling witnesses for a green (bulk-stageable) proposal; a single
# witness (n=1) is never green — one witness is not corroboration.
_GREEN_MIN_WITNESSES: Final = 2

# Folded placeholder tokens that are never a real album title. A unanimous sibling value
# folding to one of these is proposed only as ``confirm/placeholder``, never green.
_PLACEHOLDER_DENYLIST: Final = frozenset(
    {"unreleased", "misc", "singles", "unknown", "various", "youtube", "mp3"},
)

# Per-folder source labels (the group's ``source`` field).
_SOURCE_SIBLING: Final = "sibling"
_SOURCE_FOLDER_PARSE: Final = "folder_parse"
_SOURCE_MB_RECORDING: Final = "mb_recording"
_SOURCE_STAYS_BLANK: Final = "stays_blank"
# A folder whose recording lookups failed and proposed nothing, kept apart from a real miss.
_SOURCE_LOOKUP_ERROR: Final = "lookup_error"
# A folder whose every blank file has a format the writer refuses, so no fill could be staged.
_SOURCE_UNWRITABLE: Final = "unwritable"

# Per-proposal confidence labels + the confirm-reason vocabulary.
_CONF_GREEN: Final = "green"
_CONF_CONFIRM: Final = "confirm"
_CONF_REVIEW: Final = "review"
_REASON_GENRE_LIKE: Final = "genre_like"
_REASON_PLACEHOLDER: Final = "placeholder"
_REASON_N1_WEAK: Final = "n1_weak"
_REASON_FOLDER_PARSE: Final = "folder_parse"
_REASON_MB_RECORDING: Final = "mb_recording"

# The fixed provenance note for a review-confidence proposal (decision-r2 C6).
_NOTE_MB_RECORDING: Final = "musicbrainz: recording search"


# --- inputs --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FileInput:
    """One tracked file reduced to what the detector reads.

    ``album`` is the first non-blank ``album`` value across ALL ordinals (the exact
    ``resolve_years`` identity rule), or ``None`` when the file's album is blank
    everywhere, the only files that may ever be proposed. ``artist`` (the
    ``albumartist``-else-``artist`` identity) and ``title`` are the ``(artist, title)`` the
    MusicBrainz recording source looks a blank file up against. Each is ``None`` when blank
    (the Python-strip rule), in which case the file is never sent to MusicBrainz. ``writable``
    is whether the file's extension names a container the tag writer can verify.
    """

    file_id: int
    folder: str
    filename: str
    album: str | None
    artist: str | None = None
    title: str | None = None
    writable: bool = True


# --- public result types -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlbumGapProposal:
    """One blank file's proposed ``album`` fill with its confidence + provenance note."""

    file_id: int
    filename: str
    proposed: str
    confidence: str  # _CONF_GREEN | _CONF_CONFIRM | _CONF_REVIEW
    reason: str | None  # None when green, else the confirm or review reason
    note: str  # pre-formatted for stage_tags_batch (e.g. "sibling: unanimous n=11")

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "file_id": self.file_id,
            "filename": self.filename,
            "proposed": self.proposed,
            "confidence": self.confidence,
            "reason": self.reason,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class AlbumGapGroup:
    """One folder's blank-album files collapsed into a sourced group with its proposals."""

    folder: str
    blank_count: int  # files in the folder whose album is blank across all ordinals
    file_count: int  # present tracked files in the folder (blank or not)
    file_ids: list[int]  # the folder's blank-album file ids, sorted
    sibling_histogram: dict[str, int]  # distinct non-blank album value -> sibling count
    source: str  # one of the _SOURCE_* labels
    proposals: list[AlbumGapProposal]  # at most one per blank file (empty for stays_blank)
    errors: int = 0  # recording lookups in this folder that failed
    unwritable: int = 0  # blank files whose format the writer refuses, never proposed

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "folder": self.folder,
            "blank_count": self.blank_count,
            "file_count": self.file_count,
            "file_ids": self.file_ids,
            "sibling_histogram": self.sibling_histogram,
            "source": self.source,
            "proposals": [p.to_dict() for p in self.proposals],
            "errors": self.errors,
            "unwritable": self.unwritable,
        }


@dataclass(frozen=True, slots=True)
class AlbumGapsReport:
    """Immutable summary of one :func:`detect_album_gaps` run, JSON-ready for the MCP tool.

    ``groups`` is the (folder-sorted, optionally narrowed) worklist. ``total_files`` and the
    ``green`` / ``confirm`` / ``review`` / ``stays_blank`` / ``errors`` / ``unwritable`` counts
    describe the WHOLE library and are unaffected by a ``limit``/``folder`` narrowing (mirroring
    the mismatch report). Every blank file lands in exactly one of those six, so
    ``green + confirm + review + stays_blank + errors + unwritable == total_blank``. ``review``
    is the MusicBrainz recording source (never green), and ``errors`` counts its failed lookups,
    itemized in ``error_items``. ``unwritable`` counts the blank files whose format the tag
    writer refuses (WAV, AIFF, WMA, raw AAC). They get no proposal, since staging refuses them.
    """

    groups: list[AlbumGapGroup]
    total_files: int
    total_blank: int
    green: int
    confirm: int
    review: int
    stays_blank: int
    summary: str
    errors: int = 0
    error_items: list[dict[str, str]] = field(default_factory=list)
    unwritable: int = 0

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "groups": [g.to_dict() for g in self.groups],
            "total_files": self.total_files,
            "total_blank": self.total_blank,
            "green": self.green,
            "confirm": self.confirm,
            "review": self.review,
            "stays_blank": self.stays_blank,
            "errors": self.errors,
            "error_items": [dict(e) for e in self.error_items],
            "unwritable": self.unwritable,
            "summary": self.summary,
        }


# --- pure classifier -----------------------------------------------------------------


def _sibling_histogram(folder_files: list[_FileInput]) -> dict[str, int]:
    """Count distinct non-blank ``album`` values among a folder's files (the sibling votes)."""
    histogram: dict[str, int] = {}
    for f in folder_files:
        if f.album is not None:
            histogram[f.album] = histogram.get(f.album, 0) + 1
    return histogram


def _is_genre_like(value: str, vocab: Vocabulary) -> bool:
    """Return True when *value* reads as a genre string rather than an album title.

    Two signals: the whole value is a known genre, or EVERY whitespace-separated word of it
    is one. The all-words rule catches concatenated genre strings whose compound has no
    vocabulary key (the live ``"Reggaeton Dembow"`` trap) while sparing real titles that
    merely contain a genre word (``"House of Balloons"`` — ``of`` is not a genre).
    """
    if vocab.match(value) is not None:
        return True
    words = value.split()
    return bool(words) and all(vocab.match(word) is not None for word in words)


def _sibling_reason(value: str, witnesses: int, vocab: Vocabulary) -> str | None:
    """Return the confirm reason for a unanimous sibling *value*, or ``None`` when green.

    Green-light (``None``) requires ALL of: ``witnesses >= _GREEN_MIN_WITNESSES``, the value
    is not genre-like (:func:`_is_genre_like` — whole-value or all-words vocabulary match),
    and its fold-key is not a placeholder. Otherwise the most informative confirm reason is
    returned: a genre string first, then a placeholder label, then a single-witness (n=1)
    value.
    """
    if _is_genre_like(value, vocab):
        return _REASON_GENRE_LIKE
    if alnum_key(value) in _PLACEHOLDER_DENYLIST:
        return _REASON_PLACEHOLDER
    if witnesses < _GREEN_MIN_WITNESSES:
        return _REASON_N1_WEAK
    return None


def _sibling_source(
    blank_files: list[_FileInput],
    histogram: dict[str, int],
    vocab: Vocabulary,
) -> tuple[str, list[AlbumGapProposal]]:
    """Build the sibling-source proposals, or leave the folder blank when siblings are mixed.

    A single distinct sibling value (unanimous, any n>=1) always proposes for every blank
    file. A green-light value stages bulk, otherwise it is a ``confirm`` with the reason.
    Two or more distinct sibling values (mixed) yield no proposal, so the folder stays blank
    (folder-parse never runs when any sibling value exists).
    """
    if len(histogram) != 1:
        return _SOURCE_STAYS_BLANK, []
    value, witnesses = next(iter(histogram.items()))
    reason = _sibling_reason(value, witnesses, vocab)
    confidence = _CONF_GREEN if reason is None else _CONF_CONFIRM
    note = f"sibling: unanimous n={witnesses}"
    proposals = [
        AlbumGapProposal(
            file_id=f.file_id,
            filename=f.filename,
            proposed=value,
            confidence=confidence,
            reason=reason,
            note=note,
        )
        for f in blank_files
    ]
    return _SOURCE_SIBLING, proposals


def _folder_parse_source(
    folder: str,
    folder_files: list[_FileInput],
    blank_files: list[_FileInput],
) -> tuple[str, list[AlbumGapProposal]]:
    """Build the folder-parse proposals for an all-blank folder, gated by self-corroboration.

    Parses the folder's leaf name; a parsed album is proposed (always ``confirm``, never
    green) only when the fraction of the folder's filenames that fold-contain the album
    token meets :data:`_CORROBORATION_THRESHOLD`. A folder that does not parse, or parses but
    is not corroborated, stays blank.
    """
    parsed = parsing.parse_folder(Path(folder).name)
    if parsed is None:
        return _SOURCE_STAYS_BLANK, []

    total = len(folder_files)
    if total == 0:  # pragma: no cover - a gap group always has at least the blank file(s)
        return _SOURCE_STAYS_BLANK, []
    hits = sum(1 for f in folder_files if parsing.fold_contains(f.filename, parsed.album))
    if hits / total < _CORROBORATION_THRESHOLD:
        return _SOURCE_STAYS_BLANK, []

    note = f"folder-parse: self-corroborated {hits}/{total}"
    proposals = [
        AlbumGapProposal(
            file_id=f.file_id,
            filename=f.filename,
            proposed=parsed.album,
            confidence=_CONF_CONFIRM,
            reason=_REASON_FOLDER_PARSE,
            note=note,
        )
        for f in blank_files
    ]
    return _SOURCE_FOLDER_PARSE, proposals


def _recording_source(
    blank_files: list[_FileInput],
    client: MBRecordingSource,
) -> tuple[str, list[AlbumGapProposal], list[dict[str, str]]]:
    """Build the review-only MusicBrainz recording-source proposals for an all-blank folder.

    Runs ONLY when the sibling and folder-parse sources produced nothing (a ``stays_blank``
    folder). Each blank file carrying a non-blank ``artist`` AND ``title`` is looked up
    ``(artist, title)`` against the recording's release-group title, and a hit becomes a
    ``review`` proposal (never green). Files lacking artist or title are never sent to
    MusicBrainz. A transient :class:`MusicBrainzError` leaves that file unproposed without
    aborting the folder and is returned as one error item. With no hits the folder is
    ``lookup_error`` when any lookup failed, else ``stays_blank``.
    """
    proposals: list[AlbumGapProposal] = []
    error_items: list[dict[str, str]] = []
    for f in blank_files:
        if f.artist is None or f.title is None:
            continue
        try:
            resolved = client.recording_search(f.artist, f.title)
        except MusicBrainzError as exc:
            logger.warning(
                "musicbrainz recording error for artist=%r title=%r: %s",
                f.artist,
                f.title,
                exc,
            )
            error_items.append({"key": f"{f.artist} - {f.title}", "message": str(exc)})
            continue
        if resolved is None:
            continue
        proposals.append(
            AlbumGapProposal(
                file_id=f.file_id,
                filename=f.filename,
                proposed=resolved.album_title,
                confidence=_CONF_REVIEW,
                reason=_REASON_MB_RECORDING,
                note=_NOTE_MB_RECORDING,
            ),
        )
    if proposals:
        return _SOURCE_MB_RECORDING, proposals, error_items
    if error_items:
        return _SOURCE_LOOKUP_ERROR, [], error_items
    return _SOURCE_STAYS_BLANK, [], []


def _classify_folder(
    folder: str,
    folder_files: list[_FileInput],
    blank_files: list[_FileInput],
    vocab: Vocabulary,
    client: MBRecordingSource | None,
) -> tuple[AlbumGapGroup, list[dict[str, str]]]:
    """Classify one folder (known to hold >=1 blank file) into a sourced :class:`AlbumGapGroup`.

    The sibling and folder-parse sources run purely. When they yield nothing and a *client* is
    supplied, the review-only MusicBrainz recording source gets a last pass at the blank files.
    A blank file the writer cannot verify is counted in ``unwritable`` and given to no source.
    Returns the group plus the recording lookups that failed.
    """
    error_items: list[dict[str, str]] = []
    histogram = _sibling_histogram(folder_files)
    writable_blanks = [f for f in blank_files if f.writable]
    source = _SOURCE_UNWRITABLE
    proposals: list[AlbumGapProposal] = []
    if writable_blanks and histogram:
        source, proposals = _sibling_source(writable_blanks, histogram, vocab)
    elif writable_blanks:
        source, proposals = _folder_parse_source(folder, folder_files, writable_blanks)
    if source == _SOURCE_STAYS_BLANK and client is not None:
        source, proposals, error_items = _recording_source(writable_blanks, client)
    group = AlbumGapGroup(
        folder=folder,
        blank_count=len(blank_files),
        file_count=len(folder_files),
        file_ids=sorted(f.file_id for f in blank_files),
        sibling_histogram=histogram,
        source=source,
        proposals=proposals,
        errors=len(error_items),
        unwritable=len(blank_files) - len(writable_blanks),
    )
    return group, error_items


def _classify(
    files: list[_FileInput],
    vocab: Vocabulary,
    client: MBRecordingSource | None = None,
) -> AlbumGapsReport:
    """Classify constructed file inputs into a full :class:`AlbumGapsReport` (pure core).

    Groups by folder, keeps only folders holding at least one blank-album file, and sources
    each. The report's counts describe every blank file across all groups. When *client* is
    given, folders the sibling and folder-parse sources leave blank get the review-only
    recording source (the only part that is not pure/network-free).
    """
    gap_groups: list[AlbumGapGroup] = []
    error_items: list[dict[str, str]] = []
    grouped = group_by_folder(files)
    for folder in sorted(grouped):
        folder_files = grouped[folder]
        blank_files = [f for f in folder_files if f.album is None]
        if not blank_files:
            continue
        group, folder_errors = _classify_folder(folder, folder_files, blank_files, vocab, client)
        gap_groups.append(group)
        error_items.extend(folder_errors)
    return _assemble_report(gap_groups, error_items, total_files=len(files))


def _assemble_report(
    groups: list[AlbumGapGroup],
    error_items: list[dict[str, str]],
    *,
    total_files: int,
) -> AlbumGapsReport:
    """Freeze the classified groups + library-wide disposition counts into a report."""
    green = 0
    confirm = 0
    review = 0
    stays_blank = 0
    unwritable = 0
    for group in groups:
        for proposal in group.proposals:
            if proposal.confidence == _CONF_GREEN:
                green += 1
            elif proposal.confidence == _CONF_REVIEW:
                review += 1
            else:
                confirm += 1
        # A blank file with no proposal and no failed lookup stays blank (mixed siblings, an
        # uncorroborated folder-parse, or an MB recording miss). A failed lookup is an error,
        # never a miss, so a caller can tell an unreachable MusicBrainz from no ground.
        stays_blank += group.blank_count - len(group.proposals) - group.errors - group.unwritable
        unwritable += group.unwritable
    total_blank = sum(group.blank_count for group in groups)
    summary = _summarize(
        groups=len(groups),
        total_blank=total_blank,
        green=green,
        confirm=confirm,
        review=review,
        stays_blank=stays_blank,
        errors=len(error_items),
        unwritable=unwritable,
    )
    return AlbumGapsReport(
        groups=groups,
        total_files=total_files,
        total_blank=total_blank,
        green=green,
        confirm=confirm,
        review=review,
        stays_blank=stays_blank,
        summary=summary,
        errors=len(error_items),
        error_items=error_items,
        unwritable=unwritable,
    )


def _summarize(  # noqa: PLR0913 - cohesive keyword-only summary counts
    *,
    groups: int,
    total_blank: int,
    green: int,
    confirm: int,
    review: int,
    stays_blank: int,
    errors: int,
    unwritable: int,
) -> str:
    """Build a short, plain human summary of the run."""
    head = (
        f"{groups} folder group(s), {total_blank} blank-album file(s): "
        f"{green} green + {confirm} confirm + {review} review proposal(s), "
        f"{stays_blank} stay(s) blank."
    )
    if errors:
        head += f" {errors} lookup(s) failed and stay unresolved. Re-run to retry."
    if unwritable:
        head += f" {unwritable} file(s) get no proposal: the tag writer refuses their format."
    return head


# --- narrowing (library-wide counts preserved) ---------------------------------------


def _expand_folder(report: AlbumGapsReport, folder_key: str) -> AlbumGapsReport:
    """Return the report narrowed to exactly the folder keyed *folder_key*, never a subfolder."""
    groups = [g for g in report.groups if path_keys.path_key(g.folder) == folder_key]
    return replace(report, groups=groups)


def _limit_report(report: AlbumGapsReport, limit: int) -> AlbumGapsReport:
    """Cap the report's ``groups`` at the first *limit* (counts unchanged)."""
    if limit >= len(report.groups):
        return report
    return replace(report, groups=report.groups[:limit])


# --- public entry --------------------------------------------------------------------


def _gather_inputs(conn: sqlite3.Connection) -> list[_FileInput]:
    """Read every non-missing tracked file into a :class:`_FileInput` (album/artist/title).

    ``album``/``artist`` come from the shared ``resolve_years`` identity
    (:func:`tagmend.engine.axis.lookup_identity`: ``albumartist``-else-``artist``, first non-blank
    album at ANY ordinal), so the binding blank-only guarantee cannot drift. ``title`` is the
    file's first non-blank ``title``. The two carry the ``(artist, title)`` the review source
    looks a blank file up against.
    """
    files: list[_FileInput] = []
    for row in store.list_files(conn):
        if row.is_missing:
            continue
        tags = store.get_tags(conn, row.id)
        identity = axis.lookup_identity(tags)
        title = axis.first_nonblank(tags.get("title"))
        files.append(
            _FileInput(
                file_id=row.id,
                folder=row.folder,
                filename=row.filename,
                album=identity.album,
                artist=identity.artist,
                title=title,
                writable=Path(row.filename).suffix.lower() in VERIFIABLE_SUFFIXES,
            ),
        )
    return files


def _has_recording_candidates(report: AlbumGapsReport, files: list[_FileInput]) -> bool:
    """Whether any blank file with an artist AND title sits in a ``stays_blank`` folder.

    Gates the lazy MusicBrainz client construction: a run with no such candidate never opens an
    HTTP client (nor touches the network).
    """
    blank_folders = {g.folder for g in report.groups if g.source == _SOURCE_STAYS_BLANK}
    if not blank_folders:
        return False
    return any(
        f.folder in blank_folders
        and f.album is None
        and f.writable
        and f.artist is not None
        and f.title is not None
        for f in files
    )


def _resolve_recording_source(
    settings: Settings,
    conn: sqlite3.Connection,
    files: list[_FileInput],
    vocab: Vocabulary,
    client: MBRecordingSource | None,
) -> AlbumGapsReport:
    """Re-classify with the recording source active, using *client* or a lazily-built real one.

    The real :class:`MusicBrainzClient` caches into *conn* (its ONLY ledger writes) and paces
    itself. A fake injected via *client* bypasses both. Re-running the two pure sources is cheap
    and keeps the source ordering in one place.
    """
    with lookup_clients.injected_or_owned(
        client,
        lambda: MusicBrainzClient.from_settings(settings, conn),
    ) as source:
        return _classify(files, vocab, client=source)


def detect_album_gaps(
    settings: Settings,
    *,
    limit: int | None = None,
    folder: str | None = None,
    use_musicbrainz: bool = True,
    client: MBRecordingSource | None = None,
) -> AlbumGapsReport:
    """Detect blank-``album`` files, group them by folder, and propose grounded fills.

    A scan over the ``files``/``file_tags`` snapshot: no tag writes, nothing staged. Every
    non-missing tracked file whose ``album`` is blank across all ordinals is grouped by folder
    and given one source: ``sibling`` (a unanimous non-blank album value from folder mates),
    ``folder_parse`` (the parsed folder name, self-corroborated by the folder's filenames),
    ``mb_recording`` (a review-only MusicBrainz ``(artist, title)`` album lookup, never
    green), ``lookup_error`` (every recording lookup that ran failed), or ``stays_blank`` (no
    defensible ground). Only blank files are ever proposed, upholding the additive-fill
    guarantee the staging merge does not enforce.

    The sibling and folder-parse sources are pure and network-free. The recording source runs
    only for files those two leave blank AND that carry a non-blank artist and title.
    *use_musicbrainz* (default True) skips it entirely for a local-only, network-free run.
    The client is built LAZILY, only when such candidates exist, so a run without any never
    opens an HTTP client. MB results are cached, so a re-run after the first pass is
    network-free. Those cache writes are the tool's ONLY ledger writes (tags, status and
    staging are untouched). A failed lookup is counted in ``errors`` and itemized in
    ``error_items``, never folded into ``stays_blank``. *client* lets callers inject an
    :class:`tagmend.engine.musicbrainz.MBRecordingSource` (a fake in tests).

    *folder* returns exactly that folder's group, never a subfolder, compared as a path
    (:func:`tagmend.engine.path_keys.folder_arg_key`). *limit* caps the number of groups. The
    ``total_files``/``green``/``confirm``/``review``/``stays_blank``/``errors``/``unwritable``
    counts always describe the whole library. A blank file the tag writer cannot verify (WAV,
    AIFF, WMA, raw AAC) gets no proposal and is counted in ``unwritable``.
    Loads the genre vocabulary once per run (:class:`ValueError` on a corrupt vocabulary
    propagates to the MCP envelope). Raises :class:`ValueError` for a negative *limit* or a
    *folder* outside ``music_path``. Owns its connection.
    """
    check_limit(limit)
    folder_key = None if folder is None else path_keys.folder_arg_key(settings, folder)
    vocab = classify.load_vocabulary()

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        files = _gather_inputs(connection)
        report = _classify(files, vocab)
        if use_musicbrainz and _has_recording_candidates(report, files):
            report = _resolve_recording_source(settings, connection, files, vocab, client)
    finally:
        connection.close()

    logger.info(
        "album-gaps complete: groups=%d total_blank=%d green=%d confirm=%d review=%d "
        "stays_blank=%d errors=%d",
        len(report.groups),
        report.total_blank,
        report.green,
        report.confirm,
        report.review,
        report.stays_blank,
        report.errors,
    )

    if folder_key is not None:
        return _expand_folder(report, folder_key)
    if limit is not None:
        return _limit_report(report, limit)
    return report
