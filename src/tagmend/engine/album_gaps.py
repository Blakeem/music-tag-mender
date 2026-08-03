"""Blank-``album`` gap detection: group gapped files by folder, tier them, propose fills.

A grouped detect/report tool (decision-r2 C6). It groups files whose ``album`` tag is blank
across ALL ordinals by their folder, computes three grounding tiers — a **sibling** tier
(blank files fill from a unanimous non-blank album value shared by their folder mates), a
**folder-parse** tier (a folder with no sibling values at all fills from its parsed folder
name, but only when the folder's own filenames self-corroborate it), and a review-only
**mb_recording** tier (a folder tiers 1-2 leave blank fills each blank file from a cached,
paced MusicBrainz ``(artist, title)`` → release-group-title recording search) — and emits
per-file proposals with a confidence label + provenance note pre-formatted for
``stage_tags_batch``. Nothing stages, nothing auto-commits. Tiers 1-2 are network-free; the
recording tier is opt-out (``use_musicbrainz=False``) and its result is ``confidence:
"review"`` (never green). The report feeds the existing ``stage_tags_batch → diff_tags(root)
→ commit_tags(root) → repend_axes`` spine, where the human is the diff-gate.

The recording tier's ONLY side effect is its persistent lookup cache
(``musicbrainz_recording_cache``): tags, status, and staging are untouched, so a re-run after
the first pass is network-free.

**Binding safety constraint (decision-r2 non-negotiable #1):** the ``stage_tags_batch``
merge does not guard against overwriting a present ``album`` — so a proposal is *only ever*
emitted for a file whose ``album`` is blank across every ordinal. That blank predicate
scans all ordinals via :func:`tagmend.engine.genres._first_nonblank` over
:func:`tagmend.engine.store.get_tags` (NOT the ordinal-0-only ``load_tag_values``), exactly
mirroring ``resolve_albums``' ``skipped_no_album`` gate, so a file carrying a non-blank
album at any ordinal can never appear in a proposal.

Mirrors :mod:`tagmend.engine.mismatch` in shape (frozen result dataclasses with hand-written
``to_dict``, one group row per folder, ``limit``/``folder`` narrowing that leaves the
library-wide counts intact). Like the rest of the conn-owning layer, :func:`detect_album_gaps`
owns its connection (``connect`` → ``apply_schema`` → ``try/finally`` close); the only commits
are the recording client's cache writes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tagmend.engine import classify, db, genres, parsing, schema, store
from tagmend.engine.musicbrainz import MusicBrainzClient, MusicBrainzError
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

# Per-folder tier labels (the group's ``tier`` field).
_TIER_SIBLING: Final = "sibling"
_TIER_FOLDER_PARSE: Final = "folder_parse"
_TIER_MB_RECORDING: Final = "mb_recording"
_TIER_STAYS_BLANK: Final = "stays_blank"

# Per-proposal confidence labels + the confirm-reason vocabulary.
_CONF_GREEN: Final = "green"
_CONF_CONFIRM: Final = "confirm"
_CONF_REVIEW: Final = "review"
_REASON_GENRE_LIKE: Final = "genre_like"
_REASON_PLACEHOLDER: Final = "placeholder"
_REASON_N1_WEAK: Final = "n1_weak"
_REASON_FOLDER_PARSE: Final = "folder_parse"
_REASON_MB_RECORDING: Final = "mb_recording"

# The fixed provenance note for a review-tier proposal (decision-r2 C6).
_NOTE_MB_RECORDING: Final = "musicbrainz: recording search"


# --- inputs --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FileInput:
    """One tracked file reduced to what the detector reads.

    ``album`` is the first non-blank ``album`` value across ALL ordinals (the exact
    ``resolve_albums`` identity rule), or ``None`` when the file's album is blank
    everywhere — the only files that may ever be proposed. ``artist`` (the
    ``albumartist``-else-``artist`` identity) and ``title`` are the ``(artist, title)`` the
    MusicBrainz recording tier looks a blank file up against; each is ``None`` when blank
    (the Python-strip rule), in which case the file is never sent to MusicBrainz.
    """

    file_id: int
    folder: str
    filename: str
    album: str | None
    artist: str | None = None
    title: str | None = None


# --- public result types -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GapProposal:
    """One blank file's proposed ``album`` fill with its confidence + provenance note."""

    file_id: int
    filename: str
    proposed: str
    confidence: str  # _CONF_GREEN | _CONF_CONFIRM
    reason: str | None  # None when green; else the confirm reason
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
class GapGroup:
    """One folder's blank-album files collapsed into a tiered group with its proposals."""

    folder: str
    blank_count: int  # files in the folder whose album is blank across all ordinals
    total_files: int  # tracked files in the folder (blank or not)
    sibling_histogram: dict[str, int]  # distinct non-blank album value -> sibling count
    tier: str  # _TIER_SIBLING | _TIER_FOLDER_PARSE | _TIER_STAYS_BLANK
    proposals: list[GapProposal]  # one per blank file (empty for stays_blank)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "folder": self.folder,
            "blank_count": self.blank_count,
            "total_files": self.total_files,
            "sibling_histogram": self.sibling_histogram,
            "tier": self.tier,
            "proposals": [p.to_dict() for p in self.proposals],
        }


@dataclass(frozen=True, slots=True)
class AlbumGapsReport:
    """Immutable summary of one :func:`detect_album_gaps` run, JSON-ready for the MCP tool.

    ``groups`` is the (folder-sorted, optionally narrowed) worklist; the ``green`` /
    ``confirm`` / ``review`` / ``stays_blank`` counts describe the WHOLE library and are
    unaffected by a ``limit``/``folder`` narrowing (mirroring the mismatch report). Every
    blank file lands in exactly one of those four, so
    ``green + confirm + review + stays_blank == total_blank``. ``review`` is the MusicBrainz
    recording tier (never green).
    """

    groups: list[GapGroup]
    total_blank: int
    green: int
    confirm: int
    review: int
    stays_blank: int
    summary: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "groups": [g.to_dict() for g in self.groups],
            "total_blank": self.total_blank,
            "green": self.green,
            "confirm": self.confirm,
            "review": self.review,
            "stays_blank": self.stays_blank,
            "summary": self.summary,
        }


# --- pure classifier -----------------------------------------------------------------


def _group_by_folder(files: list[_FileInput]) -> dict[str, list[_FileInput]]:
    """Bucket files by their folder path, preserving first-seen order within a folder."""
    groups: dict[str, list[_FileInput]] = {}
    for f in files:
        groups.setdefault(f.folder, []).append(f)
    return groups


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
    if classify.fold(value) in _PLACEHOLDER_DENYLIST:
        return _REASON_PLACEHOLDER
    if witnesses < _GREEN_MIN_WITNESSES:
        return _REASON_N1_WEAK
    return None


def _sibling_tier(
    blank_files: list[_FileInput],
    histogram: dict[str, int],
    vocab: Vocabulary,
) -> tuple[str, list[GapProposal]]:
    """Build the sibling-tier proposals, or leave the folder blank when siblings are mixed.

    A single distinct sibling value (unanimous, any n>=1) always proposes for every blank
    file; a green-light value stages bulk, otherwise it is a ``confirm`` with the reason.
    Two or more distinct sibling values (mixed) yield no proposal — the folder stays blank
    (folder-parse never runs when any sibling value exists).
    """
    if len(histogram) != 1:
        return _TIER_STAYS_BLANK, []
    value, witnesses = next(iter(histogram.items()))
    reason = _sibling_reason(value, witnesses, vocab)
    confidence = _CONF_GREEN if reason is None else _CONF_CONFIRM
    note = f"sibling: unanimous n={witnesses}"
    proposals = [
        GapProposal(
            file_id=f.file_id,
            filename=f.filename,
            proposed=value,
            confidence=confidence,
            reason=reason,
            note=note,
        )
        for f in blank_files
    ]
    return _TIER_SIBLING, proposals


def _folder_parse_tier(
    folder: str,
    folder_files: list[_FileInput],
    blank_files: list[_FileInput],
) -> tuple[str, list[GapProposal]]:
    """Build the folder-parse proposals for an all-blank folder, gated by self-corroboration.

    Parses the folder's leaf name; a parsed album is proposed (always ``confirm``, never
    green) only when the fraction of the folder's filenames that fold-contain the album
    token meets :data:`_CORROBORATION_THRESHOLD`. A folder that does not parse, or parses but
    is not corroborated, stays blank.
    """
    parsed = parsing.parse_folder(Path(folder).name)
    if parsed is None:
        return _TIER_STAYS_BLANK, []

    total = len(folder_files)
    if total == 0:  # pragma: no cover - a gap group always has at least the blank file(s)
        return _TIER_STAYS_BLANK, []
    hits = sum(1 for f in folder_files if parsing.fold_contains(f.filename, parsed.album))
    if hits / total < _CORROBORATION_THRESHOLD:
        return _TIER_STAYS_BLANK, []

    note = f"folder-parse: self-corroborated {hits}/{total}"
    proposals = [
        GapProposal(
            file_id=f.file_id,
            filename=f.filename,
            proposed=parsed.album,
            confidence=_CONF_CONFIRM,
            reason=_REASON_FOLDER_PARSE,
            note=note,
        )
        for f in blank_files
    ]
    return _TIER_FOLDER_PARSE, proposals


def _recording_tier(
    blank_files: list[_FileInput],
    client: MBRecordingSource,
) -> tuple[str, list[GapProposal]]:
    """Build the review-only MusicBrainz recording-tier proposals for an all-blank folder.

    Runs ONLY when tiers 1-2 produced nothing (a ``stays_blank`` folder). Each blank file
    carrying a non-blank ``artist`` AND ``title`` is looked up ``(artist, title)`` → the
    recording's release-group title; a hit becomes a ``review`` proposal (never green). Files
    lacking artist or title are never sent to MusicBrainz. A transient
    :class:`MusicBrainzError` leaves that file unproposed without aborting the folder. With no
    hits the folder stays blank.
    """
    proposals: list[GapProposal] = []
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
            continue
        if resolved is None:
            continue
        proposals.append(
            GapProposal(
                file_id=f.file_id,
                filename=f.filename,
                proposed=resolved.album_title,
                confidence=_CONF_REVIEW,
                reason=_REASON_MB_RECORDING,
                note=_NOTE_MB_RECORDING,
            ),
        )
    if not proposals:
        return _TIER_STAYS_BLANK, []
    return _TIER_MB_RECORDING, proposals


def _classify_folder(
    folder: str,
    folder_files: list[_FileInput],
    blank_files: list[_FileInput],
    vocab: Vocabulary,
    client: MBRecordingSource | None,
) -> GapGroup:
    """Classify one folder (known to hold >=1 blank file) into a tiered :class:`GapGroup`.

    Tiers 1-2 (sibling, folder-parse) run purely; when they yield nothing and a *client* is
    supplied, the review-only MusicBrainz recording tier gets a last pass at the blank files.
    """
    histogram = _sibling_histogram(folder_files)
    if histogram:
        tier, proposals = _sibling_tier(blank_files, histogram, vocab)
    else:
        tier, proposals = _folder_parse_tier(folder, folder_files, blank_files)
    if tier == _TIER_STAYS_BLANK and client is not None:
        tier, proposals = _recording_tier(blank_files, client)
    return GapGroup(
        folder=folder,
        blank_count=len(blank_files),
        total_files=len(folder_files),
        sibling_histogram=histogram,
        tier=tier,
        proposals=proposals,
    )


def _classify(
    files: list[_FileInput],
    vocab: Vocabulary,
    client: MBRecordingSource | None = None,
) -> AlbumGapsReport:
    """Classify constructed file inputs into a full :class:`AlbumGapsReport` (pure core).

    Groups by folder, keeps only folders holding at least one blank-album file, and tiers
    each. The report's counts describe every blank file across all groups. When *client* is
    given, folders that tiers 1-2 leave blank get the review-only recording tier (the only
    part that is not pure/network-free).
    """
    gap_groups: list[GapGroup] = []
    grouped = _group_by_folder(files)
    for folder in sorted(grouped):
        folder_files = grouped[folder]
        blank_files = [f for f in folder_files if f.album is None]
        if not blank_files:
            continue
        gap_groups.append(_classify_folder(folder, folder_files, blank_files, vocab, client))
    return _assemble_report(gap_groups)


def _assemble_report(groups: list[GapGroup]) -> AlbumGapsReport:
    """Freeze the classified groups + library-wide disposition counts into a report."""
    green = 0
    confirm = 0
    review = 0
    stays_blank = 0
    for group in groups:
        for proposal in group.proposals:
            if proposal.confidence == _CONF_GREEN:
                green += 1
            elif proposal.confidence == _CONF_REVIEW:
                review += 1
            else:
                confirm += 1
        # A blank file with no proposal stays blank (mixed siblings, an uncorroborated
        # folder-parse, or an MB recording miss). Sibling/folder-parse tiers propose exactly
        # one per blank file, so those groups contribute nothing here.
        stays_blank += group.blank_count - len(group.proposals)
    total_blank = sum(group.blank_count for group in groups)
    summary = _summarize(
        groups=len(groups),
        total_blank=total_blank,
        green=green,
        confirm=confirm,
        review=review,
        stays_blank=stays_blank,
    )
    return AlbumGapsReport(
        groups=groups,
        total_blank=total_blank,
        green=green,
        confirm=confirm,
        review=review,
        stays_blank=stays_blank,
        summary=summary,
    )


def _summarize(  # noqa: PLR0913 - cohesive keyword-only summary counts
    *,
    groups: int,
    total_blank: int,
    green: int,
    confirm: int,
    review: int,
    stays_blank: int,
) -> str:
    """Build a short, plain human summary of the run."""
    return (
        f"{groups} folder group(s), {total_blank} blank-album file(s): "
        f"{green} green + {confirm} confirm + {review} review proposal(s), "
        f"{stays_blank} stay(s) blank."
    )


# --- narrowing (library-wide counts preserved) ---------------------------------------


def _expand_folder(report: AlbumGapsReport, folder: str) -> AlbumGapsReport:
    """Return the report narrowed to EXACTLY *folder* (path equality, never a prefix/LIKE)."""
    groups = [g for g in report.groups if g.folder == folder]
    return replace(report, groups=groups)


def _limit_report(report: AlbumGapsReport, limit: int) -> AlbumGapsReport:
    """Cap the report's ``groups`` at the first *limit* (counts unchanged)."""
    if limit >= len(report.groups):
        return report
    return replace(report, groups=report.groups[:limit])


# --- public entry --------------------------------------------------------------------


def _gather_inputs(conn: sqlite3.Connection) -> list[_FileInput]:
    """Read every non-missing tracked file into a :class:`_FileInput` (album/artist/title).

    ``album``/``artist`` come from the shared ``resolve_albums`` identity
    (:func:`tagmend.engine.genres._identity` — ``albumartist``-else-``artist``, first non-blank
    album at ANY ordinal), so the binding blank-only guarantee cannot drift; ``title`` is the
    file's first non-blank ``title``. The two carry the ``(artist, title)`` the review tier
    looks a blank file up against.
    """
    files: list[_FileInput] = []
    for row in store.list_files(conn):
        if row.is_missing:
            continue
        tags = store.get_tags(conn, row.id)
        identity = genres._identity(tags)  # noqa: SLF001 - shared identity shape
        title = genres._first_nonblank(tags.get("title"))  # noqa: SLF001 - shared strip rule
        files.append(
            _FileInput(
                file_id=row.id,
                folder=row.folder,
                filename=row.filename,
                album=identity.album,
                artist=identity.artist,
                title=title,
            ),
        )
    return files


def _has_recording_candidates(report: AlbumGapsReport, files: list[_FileInput]) -> bool:
    """Whether any blank file with an artist AND title sits in a tiers-1-2 ``stays_blank`` folder.

    Gates the lazy MusicBrainz client construction: a run with no such candidate never opens an
    HTTP client (nor touches the network).
    """
    blank_folders = {g.folder for g in report.groups if g.tier == _TIER_STAYS_BLANK}
    if not blank_folders:
        return False
    return any(
        f.folder in blank_folders
        and f.album is None
        and f.artist is not None
        and f.title is not None
        for f in files
    )


def _resolve_recording_tier(
    settings: Settings,
    conn: sqlite3.Connection,
    files: list[_FileInput],
    vocab: Vocabulary,
    client: MBRecordingSource | None,
) -> AlbumGapsReport:
    """Re-classify with the recording tier active, using *client* or a lazily-built real one.

    The real :class:`MusicBrainzClient` caches into *conn* (its ONLY ledger writes) and paces
    itself; a fake injected via *client* bypasses both. Re-running the pure tiers 1-2 is cheap
    and keeps the tier ordering in one place.
    """
    if client is not None:
        return _classify(files, vocab, client=client)
    with MusicBrainzClient(
        settings.musicbrainz_user_agent,
        conn,
        rate_per_sec=settings.musicbrainz_rate_per_sec,
    ) as owned_client:
        return _classify(files, vocab, client=owned_client)


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
    and tiered — ``sibling`` (a unanimous non-blank album value from folder mates),
    ``folder_parse`` (the parsed folder name, self-corroborated by the folder's filenames),
    ``mb_recording`` (a review-only MusicBrainz ``(artist, title)`` → album lookup, never
    green), or ``stays_blank`` (no defensible ground). Only blank files are ever proposed,
    upholding the additive-fill guarantee the staging merge does not enforce.

    Tiers 1-2 are pure and network-free. The recording tier runs only for files that tiers 1-2
    leave blank AND that carry a non-blank artist and title; *use_musicbrainz* (default True)
    skips it entirely for a local-only, network-free run. The client is built LAZILY — only
    when such candidates actually exist — so a run without any never opens an HTTP client. MB
    results are cached, so a re-run after the first pass is network-free; those cache writes are
    the tool's ONLY ledger writes (tags/status/staging untouched). *client* lets callers inject
    an :class:`tagmend.engine.musicbrainz.MBRecordingSource` (a fake in tests).

    *folder* returns exactly that folder's group (exact path equality); *limit* caps the
    number of groups. The ``green``/``confirm``/``review``/``stays_blank`` counts always
    describe the whole library. Loads the genre vocabulary once per run (:class:`ValueError` on
    a corrupt vocabulary propagates to the MCP envelope). Owns its connection.
    """
    vocab = classify.load_vocabulary()

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        files = _gather_inputs(connection)
        report = _classify(files, vocab)
        if use_musicbrainz and _has_recording_candidates(report, files):
            report = _resolve_recording_tier(settings, connection, files, vocab, client)
    finally:
        connection.close()

    logger.info(
        "album-gaps complete: groups=%d total_blank=%d green=%d confirm=%d review=%d "
        "stays_blank=%d",
        len(report.groups),
        report.total_blank,
        report.green,
        report.confirm,
        report.review,
        report.stays_blank,
    )

    if folder is not None:
        return _expand_folder(report, folder)
    if limit is not None:
        return _limit_report(report, limit)
    return report
