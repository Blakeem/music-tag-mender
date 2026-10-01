"""Song identification by audio: settle ``title``, ``tracknumber`` and ``discnumber`` per folder.

The song axis (:data:`tagmend.engine.axis.SONG_AXIS`) identifies a file by its audio rather than
its tags. One :func:`resolve_songs` call runs these stages, top to bottom:

1. Selection stage. A folder holding a pending in-scope file is selected. Every present file of
   a selected folder votes, whatever its status, and only the pending in-scope files receive an
   outcome. A folder is cold when a voter needs fpcalc or an AcoustID request. ``limit`` counts
   cold folders only, and warm folders run on every call.
2. Fingerprint stage. A ``fingerprint_cache`` row at the files-row signature, else fpcalc.
3. Lookup stage. An ``acoustid_cache`` row, else one AcoustID request.
4. Recording gate stage. Per voter, the dominant recordings its audio names, or no contribution.
5. Stamp check stage. A gated pending voter whose tagged release its audio is absent from sends
   the folder to the rebind route.
6. Route selector. The rebind route reports ranked candidate releases and stages nothing. The
   anchored route checks each voter against the release it already names. The convergence route
   settles the folder on one release its voters share.
7. Outcome router. A fill stages the blank song fields (``origin='auto'``) and records ``done``.
   A verified file records ``done``. Held, review, ``lookup_empty`` and error outcomes are
   reported and store nothing. A gated target with no ``artist`` and no ``albumartist`` whose
   dominant recordings credit one artist also gets a review row naming it, never a stage.

The manual release path (``release_mbid``) skips the stamp check and the route selector. It
assigns every file in scope to its track on that release, all or nothing, and stages the whole
release stamp through :func:`tagmend.engine.staging.stage_tags_batch` (``origin='manual'``).

The resolver never writes ``no_match``: an empty AcoustID answer can be a timeout, so it never
becomes a permanent status. Like the rest of the conn-owning layer, the public functions here own
their connection and commit. The building blocks in :mod:`tagmend.engine.store` never commit.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tagmend.engine import (
    axis,
    axis_status,
    clock,
    db,
    path_keys,
    release_match,
    schema,
    staging,
    store,
)
from tagmend.engine.acoustid import (
    AcoustidClient,
    AcoustidError,
    AcoustidKeyError,
    AcoustidRecording,
    AcoustidReleaseRef,
    AcoustidResult,
    Fingerprint,
    Fingerprinter,
    FingerprintError,
    FingerprintTimeout,
    FingerprintUnreadableError,
    StoredFingerprint,
    get_fingerprint,
    get_lookup,
    put_fingerprint,
    put_lookup,
)
from tagmend.engine.detector_core import parse_position
from tagmend.engine.musicbrainz import MusicBrainzClient, MusicBrainzError
from tagmend.engine.text_keys import alnum_key, loose_key, title_key
from tagmend.engine.validation import check_limit
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable

    from tagmend.config import Settings
    from tagmend.engine.musicbrainz import MBMedium, MBRelease, MBReleaseSource, MBTrack

logger = get_logger(__name__)

_TITLE: Final = "title"
_TRACK: Final = "tracknumber"
_DISC: Final = "discnumber"
_ALBUM_ID: Final = "musicbrainz_albumid"
_RECORDING_ID: Final = "musicbrainz_trackid"
_ARTIST: Final = "artist"
_ALBUM_ARTIST: Final = "albumartist"
_ARTIST_ID: Final = "musicbrainz_artistid"
_ORIGINAL_DATE: Final = "originaldate"

# Recording gate thresholds, as the song-axis decision run measured them.
_SCORE_FLOOR: Final = 0.9
_DOMINANT_SHARE: Final = 0.95
_THIN_SOURCES: Final = 10
_LENGTH_GUARD_SECONDS: Final = 10

# A folder converges only when 3 voters in 5, and at least 3 (all of a smaller folder), agree.
_FLOOR_SHARE: Final = Fraction(3, 5)
_FLOOR_VOTERS: Final = 3

# The confirm fetch stage stops after this many release lookups per folder.
_CONFIRM_FETCHES: Final = 3
_OFFICIAL: Final = "official"
_UNFETCHED: Final = "unknown"
_NOT_FOUND: Final = "missing"

# Compared against the filename stem, because a wrong-stamp file's title tag names another
# release's track. ``cover`` is left out: a cover's title is the song's own.
_QUALIFIER: Final = re.compile(
    r"\b(?:instrumental|remix|live|demo|acoustic|edit|version|karaoke|mix|extended|single)\b",
    re.IGNORECASE,
)
_PLACEHOLDER_TITLE: Final = re.compile(r"^track\s*\d+$", re.IGNORECASE)

# A release that holds no value for one of these says nothing against the file's own value, so
# a stamp onto the release the file already names keeps it. A rebind still clears it, because a
# library server keeps showing a stale one.
_KEPT_ON_OWN_RELEASE: Final = (
    "date",
    "releasecountry",
    "musicbrainz_albumstatus",
    "barcode",
    "media",
    "musicbrainz_releasegroupid",
    "musicbrainz_albumtype",
    "catalognumber",
    "asin",
    "artists",
)
# The same rule keyed on the recording, because an ISRC names the recording on every release.
_KEPT_ON_OWN_RECORDING: Final = ("isrc",)

_HELD_KINDS: Final = (
    "disagreement",
    "slot_collision",
    "release_mismatch",
    "unconverged",
    "no_contribution",
)


# --- result --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolveSongsResult:
    """Immutable summary of one :func:`resolve_songs` call, JSON-ready for the MCP tool.

    ``release`` and ``unassigned`` are ``None`` on the auto path and set on the manual path.
    """

    settled: int
    staged_files: int
    errors: int
    error_items: list[dict[str, str]]
    pending_remaining: int
    cold_folders_remaining: int
    more: bool
    summary: str
    verified_files: int
    held_disagreement: int
    held_slot_collision: int
    held_release_mismatch: int
    held_unconverged: int
    held_no_contribution: int
    review_files: int
    lookup_empty: int
    skipped_manual: int
    mappings: list[dict[str, object]]
    rebind_folders: list[dict[str, object]]
    held_values: list[dict[str, object]]
    review_values: list[dict[str, object]]
    release: dict[str, object] | None = None
    unassigned: list[dict[str, object]] | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        payload: dict[str, object] = {
            "settled": self.settled,
            "staged_files": self.staged_files,
            "errors": self.errors,
            "error_items": [dict(item) for item in self.error_items],
            "pending_remaining": self.pending_remaining,
            "cold_folders_remaining": self.cold_folders_remaining,
            "more": self.more,
            "verified_files": self.verified_files,
            "held_disagreement": self.held_disagreement,
            "held_slot_collision": self.held_slot_collision,
            "held_release_mismatch": self.held_release_mismatch,
            "held_unconverged": self.held_unconverged,
            "held_no_contribution": self.held_no_contribution,
            "review_files": self.review_files,
            "lookup_empty": self.lookup_empty,
            "skipped_manual": self.skipped_manual,
            "mappings": [dict(row) for row in self.mappings],
            "rebind_folders": [dict(row) for row in self.rebind_folders],
            "held_values": [dict(row) for row in self.held_values],
            "review_values": [dict(row) for row in self.review_values],
            "summary": self.summary,
        }
        if self.release is not None:
            payload["release"] = self.release
            payload["unassigned"] = [dict(row) for row in self.unassigned or []]
        return payload


@dataclass(slots=True)
class _Tally:
    """Mutable accumulator for one run, frozen into the result at the end."""

    settled: int = 0
    staged_files: int = 0
    verified_files: int = 0
    held: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_HELD_KINDS, 0))
    lookup_empty: int = 0
    skipped_manual: int = 0
    cold_folders_remaining: int = 0
    error_items: list[dict[str, str]] = field(default_factory=list)
    held_values: list[dict[str, object]] = field(default_factory=list)
    review_values: list[dict[str, object]] = field(default_factory=list)
    mappings: list[dict[str, object]] = field(default_factory=list)
    rebind_folders: list[dict[str, object]] = field(default_factory=list)

    def add_error(self, file_id: int, message: str) -> None:
        """Record one transient failure. The file stays pending and the next call retries it."""
        self.error_items.append({"key": str(file_id), "message": message})


# --- stage data ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Voter:
    """One present file of a selected folder.

    *unsettled* marks a file whose song status is ``pending``, in scope or not. *target* marks an
    unsettled in-scope file, the only kind that receives an outcome.
    """

    row: store.FileRow
    tags: dict[str, list[str]]
    target: bool
    unsettled: bool

    @property
    def file_id(self) -> int:
        """The file's stable id."""
        return self.row.id

    @property
    def stem(self) -> str:
        """The filename without its extension, the recording gate's parity target."""
        return Path(self.row.filename).stem

    def value(self, name: str) -> str:
        """Return the first non-blank value of tag *name*, or empty."""
        return axis.first_nonblank(self.tags.get(name)) or ""


@dataclass(frozen=True, slots=True)
class _Evidence:
    """What the fingerprint and lookup stages learned about one voter."""

    duration: int | None = None
    result: AcoustidResult | None = None
    error: str | None = None

    @property
    def empty(self) -> bool:
        """Whether AcoustID answered with no match."""
        return self.result is not None and not self.result.results


@dataclass(frozen=True, slots=True)
class _Gate:
    """The recording gate's verdict on one voter.

    ``titled`` holds every titled recording at the score floor, ``group`` the top title group
    and ``dominant`` the group's members that pass the length guard. ``failure`` names why the
    voter gives no contribution, ``None`` when it passes.
    """

    titled: tuple[AcoustidRecording, ...] = ()
    group: tuple[AcoustidRecording, ...] = ()
    dominant: tuple[AcoustidRecording, ...] = ()
    failure: str | None = "no_titled_recording"

    @property
    def passed(self) -> bool:
        """Whether the voter contributes."""
        return self.failure is None

    @property
    def title(self) -> str:
        """The dominant recording title, empty when there is none."""
        return self.group[0].title if self.group else ""

    def refs(self) -> list[AcoustidReleaseRef]:
        """Every release slot the dominant recordings occupy."""
        return [ref for recording in self.dominant for ref in recording.releases]


@dataclass(frozen=True, slots=True)
class _Ballot:
    """One voter with its evidence and its gate verdict."""

    voter: _Voter
    evidence: _Evidence
    gate: _Gate


@dataclass(frozen=True, slots=True)
class _Slot:
    """One track of a release that a file's recording occupies."""

    release: MBRelease
    track: MBTrack
    recording: AcoustidRecording

    @property
    def key(self) -> tuple[str, int, int]:
        """(release, medium, position): two files claiming one key collide."""
        medium = release_match.medium_of(self.release, self.track)
        return self.release.mbid, medium, self.track.position


@dataclass(frozen=True, slots=True)
class _Convergence:
    """The folder convergence stage's verdict: the narrowed family and each voter's slots."""

    narrowed: tuple[str, ...]
    refs: dict[str, list[AcoustidReleaseRef]]
    names: dict[int, frozenset[tuple[int, int]]]
    uniform_totals: bool

    def assigned(self, file_id: int) -> tuple[int, int] | None:
        """Return the one slot *file_id* names when no other voter names it, else ``None``."""
        named = self.names.get(file_id, frozenset())
        if len(named) != 1 or self.rivals(file_id):
            return None
        return next(iter(named))

    def rivals(self, file_id: int) -> list[int]:
        """Return the other voters naming the one slot *file_id* names, empty otherwise."""
        named = self.names.get(file_id, frozenset())
        if len(named) != 1:
            return []
        slot = next(iter(named))
        return sorted(fid for fid, slots in self.names.items() if fid != file_id and slot in slots)


@dataclass(frozen=True, slots=True)
class _Ranking:
    """The confirm fetch stage's verdict: the representative and the candidate rows."""

    representative: MBRelease | None
    rows: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What the outcome router does with one target file."""

    file_id: int
    kind: str
    held: str = ""
    row: dict[str, object] | None = None
    review: dict[str, object] | None = None
    fill: dict[str, list[str]] = field(default_factory=dict)
    fill_only: frozenset[str] = frozenset()
    note: str = ""
    message: str = ""


class _Lookups:
    """The fingerprinter, the AcoustID client and the release source, each built on first use.

    A warm call needs none of them, so a ``limit=0`` tally runs with no fpcalc, no API key and
    no network. Releases are memoized per call so one release costs one lookup.
    """

    def __init__(  # noqa: PLR0913 - cohesive keyword-only injection seams
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        stack: ExitStack,
        *,
        fingerprinter: Fingerprinter | None,
        acoustid_client: AcoustidClient | None,
        releases: MBReleaseSource | None,
    ) -> None:
        """Keep the injected clients and what building the real ones needs."""
        self._settings = settings
        self._conn = conn
        self._stack = stack
        self._fingerprinter = fingerprinter
        self._acoustid = acoustid_client
        self._releases = releases
        self._release_memo: dict[str, MBRelease | None] = {}

    def fingerprinter(self) -> Fingerprinter:
        """Return the fingerprinter, building it from settings on first use."""
        if self._fingerprinter is None:
            self._fingerprinter = Fingerprinter.from_settings(self._settings)
        return self._fingerprinter

    def acoustid(self) -> AcoustidClient:
        """Return the AcoustID client, opening one from settings on first use."""
        if self._acoustid is None:
            self._acoustid = self._stack.enter_context(AcoustidClient.from_settings(self._settings))
        return self._acoustid

    def release(self, mbid: str, *, fresh: bool = False) -> MBRelease | None:
        """Return the release MusicBrainz holds under *mbid*. Raises :class:`MusicBrainzError`.

        *fresh* fetches past the memo and the cache, and the answer replaces both.
        """
        if mbid in self._release_memo and not fresh:
            return self._release_memo[mbid]
        if self._releases is None:
            client = MusicBrainzClient.from_settings(self._settings, self._conn)
            self._releases = self._stack.enter_context(client)
        found = self._releases.release_by_mbid(mbid, fresh=fresh)
        self._release_memo[mbid] = found
        return found


# --- public entry --------------------------------------------------------------------


def resolve_songs(  # noqa: PLR0913 - cohesive keyword-only scope + injection params
    settings: Settings,
    *,
    folder: str | None = None,
    file_ids: list[int] | None = None,
    release_mbid: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    fingerprinter: Fingerprinter | None = None,
    acoustid_client: AcoustidClient | None = None,
    releases: MBReleaseSource | None = None,
) -> ResolveSongsResult:
    """Settle the song fields of in-scope ``pending`` files by their audio (writes no disk).

    Scope is *file_ids* when given, else the files directly in *folder* (resolved by
    :func:`tagmend.engine.path_keys.folder_arg_key`), else the whole library. *limit* (default
    ``song_stage_limit``) counts cold folders, and ``0`` processes warm folders only. With
    *release_mbid* the call takes the manual release path over its scope, which must be given.

    A dry run writes the fingerprint, AcoustID and release caches and nothing else. A non-dry
    run raises :class:`ValueError` while anything is staged. Any run raises it for a negative
    *limit*, an unknown file id, a *folder* outside ``music_path`` and *release_mbid* without a
    scope. :class:`tagmend.engine.acoustid.AcoustidKeyError` and
    :class:`tagmend.engine.acoustid.FpcalcUnavailableError` stop the call, since they fail every
    file alike. *fingerprinter*, *acoustid_client* (already entered) and *releases* are injection
    seams for tests. Each is built from *settings* on first use when ``None``.
    """
    check_limit(limit)
    if release_mbid is not None and folder is None and file_ids is None:
        message = "release_mbid needs folder or file_ids: a release is never applied library-wide"
        raise ValueError(message)
    folder_key = None if folder is None else path_keys.folder_arg_key(settings, folder)
    effective_limit = settings.song_stage_limit if limit is None else limit
    scoped = folder is not None or file_ids is not None
    tally = _Tally()
    release_block: dict[str, object] | None = None
    unassigned: list[dict[str, object]] | None = None

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        if not dry_run and store.any_staged(connection):
            message = "commit or unstage pending changes first"
            raise ValueError(message)

        index = _present_by_folder(connection)
        scoped_ids = _scoped_ids(connection, index, folder_key=folder_key, file_ids=file_ids)
        with ExitStack() as stack:
            lookups = _Lookups(
                settings,
                connection,
                stack,
                fingerprinter=fingerprinter,
                acoustid_client=acoustid_client,
                releases=releases,
            )
            if release_mbid is None:
                run = _AutoRun(settings, connection, lookups, tally, dry_run=dry_run, scoped=scoped)
                run.resolve(index, scoped_ids, limit=effective_limit)
            else:
                release_block, unassigned = _resolve_release(
                    settings,
                    connection,
                    lookups,
                    tally,
                    scoped_ids=scoped_ids,
                    release_mbid=release_mbid,
                    dry_run=dry_run,
                )
        pending_remaining = len(store.pending_file_ids(connection, axis.SONG_AXIS, scoped_ids))
    finally:
        connection.close()

    return _build_result(
        tally,
        pending_remaining=pending_remaining,
        release=release_block,
        unassigned=unassigned,
        dry_run=dry_run,
    )


# --- selection stage -----------------------------------------------------------------


def _present_by_folder(conn: sqlite3.Connection) -> dict[str, list[store.FileRow]]:
    """Group every present file by its folder's path key, each group in file-id order."""
    grouped: dict[str, list[store.FileRow]] = {}
    for row in store.list_files(conn):
        if not row.is_missing:
            grouped.setdefault(path_keys.path_key(row.folder), []).append(row)
    return grouped


def _scoped_ids(
    conn: sqlite3.Connection,
    index: dict[str, list[store.FileRow]],
    *,
    folder_key: str | None,
    file_ids: list[int] | None,
) -> list[int]:
    """Return the file ids in scope, in ascending order. An unknown id raises ValueError."""
    if file_ids is not None:
        return store.files_in_scope(conn, file_ids=file_ids)
    if folder_key is not None:
        return [row.id for row in index.get(folder_key, [])]
    return sorted(row.id for rows in index.values() for row in rows)


def _selected_folders(pending: list[int], rows_by_id: dict[int, store.FileRow]) -> list[str]:
    """Return the folders holding a pending file, ordered by their first pending file id."""
    keys: dict[str, None] = {}
    for file_id in pending:
        keys.setdefault(path_keys.path_key(rows_by_id[file_id].folder), None)
    return list(keys)


# --- fingerprint and lookup stages ---------------------------------------------------


def _cached_evidence(
    conn: sqlite3.Connection,
    row: store.FileRow,
    now: datetime,
) -> _Evidence | None:
    """Return *row*'s evidence from the caches alone, or ``None`` when the voter is cold.

    A stored fpcalc failure is warm: the same bytes fail the same way until they change.
    """
    if row.size_bytes is None or row.mtime_ns is None:
        return _Evidence(error="the files row has no signature, rescan the library")
    stored = _stored_fingerprint(conn, row)
    if stored is None:
        return None
    if stored.fingerprint is None:
        return _Evidence(
            error=f"fpcalc exited {stored.fpcalc_exit} (stored until the file changes)"
        )
    cached = get_lookup(conn, stored.fingerprint, now)
    if cached is None:
        return None
    return _Evidence(duration=stored.fingerprint.duration, result=cached)


def _fresh_evidence(
    conn: sqlite3.Connection,
    row: store.FileRow,
    lookups: _Lookups,
    now: datetime,
) -> _Evidence:
    """Return *row*'s evidence, running fpcalc only on a signature miss.

    A lookup re-query reuses the stored fingerprint. Every cache write commits at once, so a
    failure later in the call never loses it.
    """
    cached = _cached_evidence(conn, row, now)
    if cached is not None:
        return cached
    stored = _stored_fingerprint(conn, row)
    fingerprint = None if stored is None else stored.fingerprint
    if fingerprint is None:
        try:
            fingerprint = lookups.fingerprinter().fingerprint(Path(row.folder) / row.filename)
        except (FingerprintTimeout, FingerprintUnreadableError) as exc:
            return _Evidence(error=str(exc))
        except FingerprintError as exc:
            _store_fingerprint(conn, row, exc.exit_code, None, now)
            return _Evidence(error=str(exc))
        _store_fingerprint(conn, row, 0, fingerprint, now)
    return _lookup(conn, fingerprint, lookups, now)


def _stored_fingerprint(conn: sqlite3.Connection, row: store.FileRow) -> StoredFingerprint | None:
    """Return *row*'s fpcalc outcome taken at its current files-row signature, if any."""
    if row.size_bytes is None or row.mtime_ns is None:
        return None
    return get_fingerprint(conn, row.id, row.size_bytes, row.mtime_ns)


def _store_fingerprint(
    conn: sqlite3.Connection,
    row: store.FileRow,
    exit_code: int | None,
    fingerprint: Fingerprint | None,
    now: datetime,
) -> None:
    """Store one fpcalc outcome at *row*'s signature. A failure without an exit is never stored."""
    if exit_code is None or row.size_bytes is None or row.mtime_ns is None:
        return
    put_fingerprint(
        conn,
        row.id,
        row.size_bytes,
        row.mtime_ns,
        fpcalc_exit=exit_code,
        fingerprint=fingerprint,
        now=now,
    )
    conn.commit()


def _lookup(
    conn: sqlite3.Connection,
    fingerprint: Fingerprint,
    lookups: _Lookups,
    now: datetime,
) -> _Evidence:
    """Return the AcoustID answer for *fingerprint*, cached or fetched. An error is never cached."""
    cached = get_lookup(conn, fingerprint, now)
    if cached is not None:
        return _Evidence(duration=fingerprint.duration, result=cached)
    try:
        result = lookups.acoustid().lookup(fingerprint)
    except AcoustidKeyError:
        raise
    except AcoustidError as exc:
        return _Evidence(duration=fingerprint.duration, error=str(exc))
    put_lookup(conn, fingerprint, result, now)
    conn.commit()
    return _Evidence(duration=fingerprint.duration, result=result)


# --- recording gate stage ------------------------------------------------------------


def _titled_recordings(result: AcoustidResult | None) -> tuple[AcoustidRecording, ...]:
    """Return every titled recording at the score floor, one per id, keeping the most sources."""
    best: dict[str, AcoustidRecording] = {}
    for match in result.results if result is not None else ():
        if match.score < _SCORE_FLOOR:
            continue
        for recording in match.recordings:
            kept = best.get(recording.id)
            if recording.title.strip() and (kept is None or recording.sources > kept.sources):
                best[recording.id] = recording
    return tuple(sorted(best.values(), key=lambda r: (-r.sources, r.id)))


def _qualifiers(text: str) -> frozenset[str]:
    """Return the version qualifiers *text* carries, lowercased."""
    return frozenset(word.lower() for word in _QUALIFIER.findall(text))


def _stem_holds(title: str, stem: str) -> bool:
    """Whether the folded filename *stem* contains the qualifier-stripped folded *title*."""
    bare = _QUALIFIER.sub(" ", title)
    key = alnum_key(bare)
    if key:
        return key in alnum_key(stem)
    loose = loose_key(bare)
    return bool(loose) and loose in loose_key(stem)


def _length_fits(recording_seconds: int | None, file_seconds: int | None) -> bool:
    """Whether a recording's length is within the guard of the file's, when both are known."""
    if recording_seconds is None or file_seconds is None:
        return True
    return abs(recording_seconds - file_seconds) <= _LENGTH_GUARD_SECONDS


def _gate(evidence: _Evidence, stem: str) -> _Gate:
    """Run the recording gate on one voter's evidence (pure).

    The top title group by summed sources must hold the dominant share and match the stem's
    qualifiers. A thinly sourced group also needs the stem to name it. A group member far from
    the file's length gives no contribution.
    """
    titled = _titled_recordings(evidence.result)
    if not titled:
        return _Gate()
    groups: dict[str, list[AcoustidRecording]] = {}
    for recording in titled:
        groups.setdefault(title_key(recording.title), []).append(recording)
    sources = {key: sum(r.sources for r in members) for key, members in groups.items()}
    top = max(sources, key=lambda key: sources[key])
    group = tuple(groups[top])
    total = sum(sources.values())
    share = Fraction(sources[top], total) if total else Fraction(int(len(groups) == 1))
    title = group[0].title

    failure: str | None = None
    if share < _DOMINANT_SHARE:
        failure = "ambiguous_share"
    elif _qualifiers(title) != _qualifiers(stem):
        failure = "qualifier_mismatch"
    elif sources[top] < _THIN_SOURCES and not _stem_holds(title, stem):
        failure = "thin_evidence"
    dominant = tuple(r for r in group if _length_fits(r.duration, evidence.duration))
    if failure is None and not dominant:
        failure = "length_mismatch"
    return _Gate(
        titled=titled,
        group=group,
        dominant=dominant if failure is None else (),
        failure=failure,
    )


# --- stamp check stage and route selector --------------------------------------------


def _stamp_fails(ballot: _Ballot) -> bool:
    """Whether a gated voter's tagged release is absent from its dominant recordings' releases."""
    album_id = ballot.voter.value(_ALBUM_ID)
    if not ballot.gate.passed or not album_id:
        return False
    return album_id not in {ref.id for ref in ballot.gate.refs()}


def _route(ballots: list[_Ballot]) -> str:
    """Pick the folder's route. First match wins: rebind, anchored, convergence.

    Only an unsettled voter's stamp check can pick rebind, whatever the call's scope. A settled
    voter is verified, the owner's ``manual`` call or staged to change, so it never holds its
    pending siblings on the rebind route.
    """
    if any(_stamp_fails(ballot) for ballot in ballots if ballot.voter.unsettled):
        return "rebind"
    if all(ballot.voter.value(_ALBUM_ID) for ballot in ballots):
        return "anchored"
    return "convergence"


# --- release assignment stage --------------------------------------------------------


def _slots_on(release: MBRelease, recordings: Iterable[AcoustidRecording]) -> list[_Slot]:
    """Return each track of *release* one of *recordings* occupies, matched by id, never position.

    The release-track id AcoustID gives for the release must name a track whose recording id
    is one of *recordings*.
    """
    owners = {recording.id: recording for recording in recordings}
    found: dict[tuple[str, int, int], _Slot] = {}
    for recording in owners.values():
        for ref in recording.releases:
            if ref.id != release.mbid or not ref.track_id:
                continue
            track = release.track_by_release_track_mbid(ref.track_id)
            owner = None if track is None else owners.get(track.recording_mbid)
            if track is not None and owner is not None:
                slot = _Slot(release=release, track=track, recording=owner)
                found.setdefault(slot.key, slot)
    return list(found.values())


def _claims(placements: dict[int, list[_Slot]]) -> dict[tuple[str, int, int], set[int]]:
    """Return every slot key with the files that name it."""
    claims: dict[tuple[str, int, int], set[int]] = {}
    for file_id, slots in placements.items():
        for slot in slots:
            claims.setdefault(slot.key, set()).add(file_id)
    return claims


# --- outcome rows --------------------------------------------------------------------


def _row(ballot: _Ballot, reason: str, release_mbid: str) -> dict[str, object]:
    """Return the base of a held row."""
    return {
        "file_id": ballot.voter.file_id,
        "folder": ballot.voter.row.folder,
        "filename": ballot.voter.row.filename,
        "reason": reason,
        "release_mbid": release_mbid,
    }


def _mapping(voter: _Voter, release_mbid: str, tags: dict[str, list[str]]) -> dict[str, object]:
    """Return one dry-run row: the values a call would stage on one file."""
    return {
        "file_id": voter.file_id,
        "folder": voter.row.folder,
        "filename": voter.row.filename,
        "release_mbid": release_mbid,
        "tags": tags,
    }


def _held(
    ballot: _Ballot,
    kind: str,
    reason: str,
    release_mbid: str,
    **extra: object,
) -> _Outcome:
    """Hold one target. A gated blank-title file also gets a review row with the audio's title."""
    review = None
    if ballot.gate.passed and _is_blank(_TITLE, ballot.voter.value(_TITLE)):
        review = {
            "file_id": ballot.voter.file_id,
            "folder": ballot.voter.row.folder,
            "filename": ballot.voter.row.filename,
            "field": _TITLE,
            "proposal": ballot.gate.title,
        }
    row = _row(ballot, reason, release_mbid) | extra
    return _Outcome(file_id=ballot.voter.file_id, kind="held", held=kind, row=row, review=review)


def _artist_review(ballot: _Ballot) -> dict[str, object] | None:
    """Return a gated artist-less target's review row naming its one credited artist, else None.

    Every dominant recording must credit exactly one artist, the same id on each. The artist
    axis owns ``artist``, so the song axis proposes it whatever the target's song outcome.
    """
    voter = ballot.voter
    dominant_credits = [recording.artists for recording in ballot.gate.dominant]
    tagged = bool(voter.value(_ARTIST) or voter.value(_ALBUM_ARTIST))
    if not voter.target or tagged or not ballot.gate.passed or not dominant_credits:
        return None
    if any(len(credit) != 1 for credit in dominant_credits):
        return None
    artist = dominant_credits[0][0]
    name = artist.name.strip()
    shared = {credit[0].id for credit in dominant_credits} == {artist.id}
    if not shared or not artist.id or not name:
        return None
    return {
        "file_id": voter.file_id,
        "folder": voter.row.folder,
        "filename": voter.row.filename,
        "field": _ARTIST,
        "proposal": name,
        _ARTIST_ID: artist.id,
    }


def _ungated(ballot: _Ballot) -> _Outcome:
    """Return the outcome of a target the recording gate did not pass.

    A lookup with results that fail the gate is held as ``no_contribution`` with the gate's reason.
    """
    file_id = ballot.voter.file_id
    if ballot.evidence.error is not None:
        return _Outcome(file_id=file_id, kind="error", message=ballot.evidence.error)
    if ballot.evidence.empty:
        return _Outcome(file_id=file_id, kind="lookup_empty")
    reason = ballot.gate.failure or "no_titled_recording"
    return _held(ballot, "no_contribution", reason, ballot.voter.value(_ALBUM_ID))


# --- verified rule and fill ----------------------------------------------------------


def _is_blank(name: str, value: str) -> bool:
    """Whether a song field counts as blank. A ``Track 01`` placeholder title does."""
    text = value.strip()
    return not text or (name == _TITLE and _PLACEHOLDER_TITLE.match(text) is not None)


def _medium(release: MBRelease, track: MBTrack) -> MBMedium | None:
    """Return the medium holding *track*, by identity."""
    return next((m for m in release.media if any(t is track for t in m.tracks)), None)


def _track_count(medium: MBMedium | None) -> int:
    """Return how many tracks *medium* holds, 0 when unknown."""
    return 0 if medium is None else medium.track_count or len(medium.tracks)


def _total(value: str) -> int | None:
    """Return the total of an ``n/total`` tag value, or ``None`` when it carries none."""
    _head, slash, tail = value.partition("/")
    return parse_position(tail) if slash else None


def _want(release: MBRelease, track: MBTrack, *, totals: bool) -> dict[str, str]:
    """Return what the release says each song field should hold."""
    medium = _medium(release, track)
    track_count = _track_count(medium)
    disc = 0 if medium is None else medium.position
    media = len(release.media)
    disc_total = f"/{media}" if totals else ""
    disc_value = f"{disc}{disc_total}" if media > 1 else "1"
    number = f"{track.position}/{track_count}" if totals and track_count else str(track.position)
    return {_TITLE: track.title, _TRACK: number, _DISC: disc_value}


def _agrees(name: str, have: str, release: MBRelease, track: MBTrack, *, totals: bool) -> bool:
    """Whether a non-blank song field agrees with *track* on *release* (the verified rule)."""
    if name == _TITLE:
        return release_match.text_key(have) == release_match.text_key(track.title)
    medium = _medium(release, track)
    total = _total(have)
    if name == _TRACK:
        expected_total = _track_count(medium)
        position_ok = release_match.track_number_agrees(release_match.position(have), track)
        return position_ok and (not totals or total is None or total == expected_total)
    media = len(release.media)
    expected_disc = (0 if medium is None else medium.position) if media > 1 else 1
    disc_ok = release_match.position(have) == str(expected_disc)
    return disc_ok and (not totals or total is None or total == media)


def _judge(ballot: _Ballot, release: MBRelease, track: MBTrack, *, totals: bool) -> _Outcome:
    """Fill blanks, verify, or hold on disagreement, for one assigned and confirmed target.

    A non-blank field is never rewritten. A single-medium release leaves a blank disc blank.
    """
    want = _want(release, track, totals=totals)
    fill: dict[str, list[str]] = {}
    fill_only: set[str] = set()
    wrong: dict[str, str] = {}
    for name in axis.SONG_AXIS.fields:
        have = ballot.voter.value(name)
        if _is_blank(name, have):
            if name != _DISC or len(release.media) > 1:
                fill[name] = [want[name]]
            # A placeholder is a value on disk, so only a true blank is fill-only.
            if not have:
                fill_only.add(name)
        elif not _agrees(name, have, release, track, totals=totals):
            wrong[name] = have

    if wrong:
        return _held(
            ballot,
            "disagreement",
            "disagreement",
            release.mbid,
            have=wrong,
            want={name: want[name] for name in wrong},
        )
    if not fill:
        return _Outcome(file_id=ballot.voter.file_id, kind="verified")
    return _Outcome(
        file_id=ballot.voter.file_id,
        kind="fill",
        fill=fill,
        fill_only=frozenset(fill_only & set(fill)),
        note=f"acoustid: release {release.mbid}",
        row=_mapping(ballot.voter, release.mbid, fill),
    )


# --- folder convergence and confirm fetch stages -------------------------------------


def _required_voters(voter_count: int) -> int:
    """Return how many voters the convergence floor needs in a folder of *voter_count*."""
    if voter_count <= _FLOOR_VOTERS:
        return voter_count
    return max(_FLOOR_VOTERS, math.ceil(_FLOOR_SHARE * voter_count))


def _narrow(family: list[str], refs: dict[str, list[AcoustidReleaseRef]], count: int) -> list[str]:
    """Narrow the family to releases of *count* tracks, else a medium of *count*, else keep it."""
    by_total = [rid for rid in family if any(r.release_track_count == count for r in refs[rid])]
    if by_total:
        return by_total
    by_medium = [rid for rid in family if any(r.medium_track_count == count for r in refs[rid])]
    return by_medium or family


def _uniform_totals(narrowed: list[str], refs: dict[str, list[AcoustidReleaseRef]]) -> bool:
    """Whether every narrowed member gives each medium one track count and one medium count."""
    members = [ref for rid in narrowed for ref in refs[rid]]
    per_medium: dict[int | None, set[int | None]] = {}
    for ref in members:
        per_medium.setdefault(ref.medium_position, set()).add(ref.medium_track_count)
    medium_counts = {ref.medium_count for ref in members}
    return len(medium_counts) == 1 and all(len(counts) == 1 for counts in per_medium.values())


def _converge(ballots: list[_Ballot]) -> _Convergence | None:
    """Run the folder convergence stage, or return ``None`` when the floor is missed.

    The floor applies twice: to the voters the recording gate passes and to the voters the
    max-coverage family covers.
    """
    required = _required_voters(len(ballots))
    gated = [ballot for ballot in ballots if ballot.gate.passed]
    coverage = Counter(rid for ballot in gated for rid in {ref.id for ref in ballot.gate.refs()})
    if len(gated) < required or not coverage:
        return None
    top = max(coverage.values())
    if top < required:
        return None

    family = sorted(rid for rid, count in coverage.items() if count == top)
    refs = {
        rid: [ref for ballot in gated for ref in ballot.gate.refs() if ref.id == rid]
        for rid in family
    }
    narrowed = _narrow(family, refs, len(ballots))
    names = {
        ballot.voter.file_id: frozenset(
            (ref.medium_position, ref.track_position)
            for ref in ballot.gate.refs()
            if ref.id in narrowed
            and ref.medium_position is not None
            and ref.track_position is not None
        )
        for ballot in gated
    }
    return _Convergence(
        narrowed=tuple(narrowed),
        refs=refs,
        names=names,
        uniform_totals=_uniform_totals(narrowed, refs),
    )


def _status_of(release: MBRelease | None) -> str:
    """Return a fetched release's status as the candidate rows report it."""
    if release is None:
        return _NOT_FOUND
    return release.status.casefold() or "none"


def _confirm(
    release_mbids: tuple[str, ...],
    refs: dict[str, list[AcoustidReleaseRef]],
    carried: frozenset[str],
    lookups: _Lookups,
) -> _Ranking:
    """Run the confirm fetch stage: the one ranking rule for representatives and candidates.

    Pre-rank: a release a folder file carries, then the earliest date, then the smallest id.
    The walk fetches members in that order until one is Official, at most three. Report order:
    Official, then unfetched, then any other status, each in pre-rank order.
    """
    order = sorted(
        release_mbids,
        key=lambda rid: (rid not in carried, refs[rid][0].date or "~", rid),
    )
    statuses: dict[str, str] = {}
    representative: MBRelease | None = None
    for rid in order[:_CONFIRM_FETCHES]:
        release = lookups.release(rid)
        statuses[rid] = _status_of(release)
        if release is not None and statuses[rid] == _OFFICIAL:
            representative = release
            break

    def rank(rid: str) -> tuple[int, int]:
        status = statuses.get(rid, _UNFETCHED)
        tier = 0 if status == _OFFICIAL else 1 if status == _UNFETCHED else 2
        return tier, order.index(rid)

    rows: list[dict[str, object]] = []
    for rid in sorted(order, key=rank):
        first = refs[rid][0]
        rows.append(
            {
                "release_mbid": rid,
                "title": first.title,
                "date": first.date,
                "country": first.country,
                "track_count": first.release_track_count,
                "medium_count": first.medium_count,
                "status": statuses.get(rid, _UNFETCHED),
                "carried": rid in carried,
            },
        )
    return _Ranking(representative=representative, rows=rows)


def _carried(ballots: list[_Ballot]) -> frozenset[str]:
    """Return every release id a voter in the folder carries."""
    return frozenset(album_id for b in ballots if (album_id := b.voter.value(_ALBUM_ID)))


# --- routes --------------------------------------------------------------------------


def _anchored(ballots: list[_Ballot], lookups: _Lookups) -> list[_Outcome]:
    """Check each gated voter against the release it already names.

    No floor applies, since no vote binds the folder. Unique claim spans every voter.
    """
    placements: dict[int, list[_Slot]] = {}
    for ballot in ballots:
        if ballot.gate.passed:
            release = lookups.release(ballot.voter.value(_ALBUM_ID))
            slots = [] if release is None else _slots_on(release, ballot.gate.dominant)
            placements[ballot.voter.file_id] = slots
    claims = _claims(placements)

    outcomes: list[_Outcome] = []
    for ballot in (b for b in ballots if b.voter.target):
        if not ballot.gate.passed:
            outcomes.append(_ungated(ballot))
            continue
        release_mbid = ballot.voter.value(_ALBUM_ID)
        slots = placements[ballot.voter.file_id]
        if not slots:
            outcomes.append(_held(ballot, "release_mismatch", "not_on_release", release_mbid))
        elif len(slots) > 1:
            outcomes.append(_held(ballot, "unconverged", "two_slots", release_mbid))
        elif others := sorted(claims[slots[0].key] - {ballot.voter.file_id}):
            outcomes.append(
                _held(
                    ballot, "slot_collision", "slot_collision", release_mbid, other_file_ids=others
                ),
            )
        else:
            outcomes.append(_judge(ballot, slots[0].release, slots[0].track, totals=True))
    return outcomes


def _converged(ballots: list[_Ballot], lookups: _Lookups) -> list[_Outcome]:
    """Settle an id-less folder on the one Official release its voters converge on."""
    targets = [ballot for ballot in ballots if ballot.voter.target]
    convergence = _converge(ballots)
    if convergence is None:
        return [_held_or_ungated(b, "unconverged", "floor_missed", "") for b in targets]
    ranking = _confirm(convergence.narrowed, convergence.refs, _carried(ballots), lookups)
    release = ranking.representative
    if release is None:
        return [_held_or_ungated(b, "unconverged", "no_official_release", "") for b in targets]

    outcomes: list[_Outcome] = []
    for ballot in targets:
        file_id = ballot.voter.file_id
        slot = convergence.assigned(file_id)
        if not ballot.gate.passed:
            outcomes.append(_ungated(ballot))
        elif rivals := convergence.rivals(file_id):
            outcomes.append(
                _held(
                    ballot, "slot_collision", "slot_collision", release.mbid, other_file_ids=rivals
                ),
            )
        elif slot is None:
            outcomes.append(_held(ballot, "unconverged", "not_single_slot", release.mbid))
        else:
            track = _track_at(release, ballot.gate.dominant, slot)
            outcomes.append(
                _held(ballot, "release_mismatch", "not_on_representative", release.mbid)
                if track is None
                else _judge(ballot, release, track, totals=convergence.uniform_totals),
            )
    return outcomes


def _held_or_ungated(ballot: _Ballot, kind: str, reason: str, release_mbid: str) -> _Outcome:
    """Hold a gated target, or report an ungated one by its evidence."""
    return _held(ballot, kind, reason, release_mbid) if ballot.gate.passed else _ungated(ballot)


def _track_at(
    release: MBRelease,
    recordings: tuple[AcoustidRecording, ...],
    slot: tuple[int, int],
) -> MBTrack | None:
    """Return the track at *slot* on *release* that one of *recordings* occupies by id."""
    for found in _slots_on(release, recordings):
        _rid, medium, position = found.key
        if (medium, position) == slot:
            return found.track
    return None


def _rebind_report(ballots: list[_Ballot], lookups: _Lookups) -> dict[str, object]:
    """Report a wrong-stamp folder: its tagged releases, the flagged files, ranked candidates.

    The candidate list is empty when the folder misses the convergence floor.
    """
    carried = _carried(ballots)
    tagged: list[dict[str, object]] = []
    for release_mbid in sorted(carried):
        release = lookups.release(release_mbid)
        tagged.append(
            {
                "release_mbid": release_mbid,
                "title": "" if release is None else release.title,
                "status": _status_of(release),
            },
        )
    convergence = _converge(ballots)
    candidates = (
        []
        if convergence is None
        else _confirm(convergence.narrowed, convergence.refs, carried, lookups).rows
    )
    return {
        "folder": ballots[0].voter.row.folder,
        "tagged_releases": tagged,
        "flagged_file_ids": [
            b.voter.file_id for b in ballots if b.voter.unsettled and _stamp_fails(b)
        ],
        "candidates": candidates,
    }


# --- auto path -----------------------------------------------------------------------


class _AutoRun:
    """One auto-path run: selection, evidence, routes and the outcome router, folder by folder."""

    def __init__(  # noqa: PLR0913 - cohesive run state
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        lookups: _Lookups,
        tally: _Tally,
        *,
        dry_run: bool,
        scoped: bool,
    ) -> None:
        """Keep the run's state. *scoped* turns on the dry-run per-file mapping rows."""
        self._settings = settings
        self._conn = conn
        self._lookups = lookups
        self._tally = tally
        self._dry_run = dry_run
        self._scoped = scoped
        self._now = datetime.now(UTC)

    def resolve(
        self,
        index: dict[str, list[store.FileRow]],
        scoped_ids: list[int],
        *,
        limit: int,
    ) -> None:
        """Process every warm selected folder and the first *limit* cold ones, in order."""
        rows_by_id = {row.id: row for rows in index.values() for row in rows}
        statuses = {
            file_id: store.derived_status(self._conn, axis.SONG_AXIS, file_id)
            for file_id in scoped_ids
            if file_id in rows_by_id
        }
        pending = [file_id for file_id, status in statuses.items() if status == "pending"]
        targets = frozenset(pending)
        self._tally.skipped_manual = sum(1 for status in statuses.values() if status == "manual")

        cold_taken = 0
        for key in _selected_folders(pending, rows_by_id):
            rows = index[key]
            evidence = self._warm_evidence(rows)
            if evidence is None:
                if cold_taken >= limit:
                    self._tally.cold_folders_remaining += 1
                    continue
                cold_taken += 1
                evidence = {
                    r.id: _fresh_evidence(self._conn, r, self._lookups, self._now) for r in rows
                }
            ballots = self._ballots(rows, evidence, targets)
            self._settle(ballots)

    def _warm_evidence(self, rows: list[store.FileRow]) -> dict[int, _Evidence] | None:
        """Return every voter's cached evidence, or ``None`` at the first cold voter."""
        evidence: dict[int, _Evidence] = {}
        for row in rows:
            cached = _cached_evidence(self._conn, row, self._now)
            if cached is None:
                return None
            evidence[row.id] = cached
        return evidence

    def _ballots(
        self,
        rows: list[store.FileRow],
        evidence: dict[int, _Evidence],
        pending: frozenset[int],
    ) -> list[_Ballot]:
        """Build each voter's ballot: its tags, its evidence and its gate verdict."""
        ballots: list[_Ballot] = []
        for row in rows:
            target = row.id in pending
            unsettled = target or (
                store.derived_status(self._conn, axis.SONG_AXIS, row.id) == "pending"
            )
            voter = _Voter(
                row=row,
                tags=store.get_tags(self._conn, row.id),
                target=target,
                unsettled=unsettled,
            )
            ballots.append(
                _Ballot(
                    voter=voter, evidence=evidence[row.id], gate=_gate(evidence[row.id], voter.stem)
                ),
            )
        return ballots

    def _settle(self, ballots: list[_Ballot]) -> None:
        """Route one folder, apply its outcomes and add its artist review rows.

        A MusicBrainz error leaves the folder pending. A rebind folder adds its report plus each
        ungated target's error, empty lookup or gate failure.
        """
        try:
            route = _route(ballots)
            if route == "rebind":
                self._tally.rebind_folders.append(_rebind_report(ballots, self._lookups))
                self._apply([_ungated(b) for b in ballots if b.voter.target and not b.gate.passed])
                return
            outcomes = (
                _anchored(ballots, self._lookups)
                if route == "anchored"
                else _converged(ballots, self._lookups)
            )
        except MusicBrainzError as exc:
            logger.warning("musicbrainz error in %s: %s", ballots[0].voter.row.folder, exc)
            for ballot in (b for b in ballots if b.voter.target):
                self._tally.add_error(ballot.voter.file_id, str(exc))
            return
        self._apply(outcomes)
        self._tally.review_values.extend(
            review for ballot in ballots if (review := _artist_review(ballot)) is not None
        )

    def _apply(self, outcomes: list[_Outcome]) -> None:
        """Run the outcome router: stage every fill, then record each ``done`` in one write."""
        done: list[int] = []
        for outcome in outcomes:
            if outcome.kind == "fill" and self._stage_fill(outcome):
                done.append(outcome.file_id)
            elif outcome.kind == "verified":
                self._tally.verified_files += 1
                done.append(outcome.file_id)
            elif outcome.kind == "held":
                self._hold(outcome)
            elif outcome.kind == "lookup_empty":
                self._tally.lookup_empty += 1
            elif outcome.kind == "error":
                self._tally.add_error(outcome.file_id, outcome.message)
        self._tally.settled += len(done)
        if self._dry_run or not done:
            return
        # Every stage above ran first, because stage_tags needs the write lock this
        # connection would otherwise hold.
        now = clock.utc_now()
        for file_id in done:
            store.record_outcome(
                self._conn, axis.SONG_AXIS, file_id=file_id, status="done", now=now
            )
        self._conn.commit()

    def _stage_fill(self, outcome: _Outcome) -> bool:
        """Stage one fill (``origin='auto'``, blank fields only). Return whether it settles."""
        if self._dry_run:
            self._tally.staged_files += 1
            if self._scoped and outcome.row is not None:
                self._tally.mappings.append(outcome.row)
            return True
        try:
            staged = staging.stage_tags(
                self._settings,
                file_id=outcome.file_id,
                tags=outcome.fill,
                origin="auto",
                note=outcome.note,
                fill_only=outcome.fill_only,
            )
        except ValueError as exc:
            self._tally.add_error(outcome.file_id, str(exc))
            return False
        # Disk gained every value since the scan: nothing staged, and the next rescan re-opens it.
        if staged:
            self._tally.staged_files += 1
        return staged

    def _hold(self, outcome: _Outcome) -> None:
        """Count one held target and keep its row, plus its review row when it has one."""
        self._tally.held[outcome.held] += 1
        if outcome.row is not None:
            self._tally.held_values.append(outcome.row)
        if outcome.review is not None:
            self._tally.review_values.append(outcome.review)


# --- manual release path -------------------------------------------------------------


def _resolve_release(  # noqa: PLR0913 - cohesive keyword-only run inputs
    settings: Settings,
    conn: sqlite3.Connection,
    lookups: _Lookups,
    tally: _Tally,
    *,
    scoped_ids: list[int],
    release_mbid: str,
    dry_run: bool,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Assign every file in scope to its track on *release_mbid*, then stage the whole stamp.

    All or nothing: one unassigned file stages nothing. Returns the release block and the
    unassigned rows. Raises :class:`ValueError` when MusicBrainz holds no such release or a
    listed file is missing on disk.
    """
    # A stamp writes the release's track ids, which MusicBrainz replaces over time, so a real
    # run reads the current tracklist and a dry run keeps reading the cache.
    release = lookups.release(release_mbid, fresh=not dry_run)
    if release is None:
        message = f"MusicBrainz holds no release {release_mbid}"
        raise ValueError(message)
    rows = _present_rows(conn, scoped_ids)

    now = datetime.now(UTC)
    ballots: list[_Ballot] = []
    for row in rows:
        evidence = _fresh_evidence(conn, row, lookups, now)
        voter = _Voter(row=row, tags=store.get_tags(conn, row.id), target=True, unsettled=True)
        ballots.append(_Ballot(voter=voter, evidence=evidence, gate=_gate(evidence, voter.stem)))
    placements = {b.voter.file_id: _slots_on(release, b.gate.titled) for b in ballots}
    claims = _claims(placements)

    unassigned: list[dict[str, object]] = []
    stamps: list[tuple[_Voter, dict[str, list[str]]]] = []
    for ballot in ballots:
        slots = placements[ballot.voter.file_id]
        reason = _evidence_reason(ballot) or _slot_reason(ballot, slots, claims)
        if reason == "error":
            tally.add_error(ballot.voter.file_id, ballot.evidence.error or "")
        if reason is not None:
            unassigned.append(
                {
                    "file_id": ballot.voter.file_id,
                    "filename": ballot.voter.row.filename,
                    "reason": reason,
                },
            )
        else:
            stamps.append((ballot.voter, _stamp(release, slots[0].track, ballot.voter)))

    block = _release_block(release)
    if unassigned or not stamps:
        return block, unassigned
    tally.staged_files = len(stamps)
    if dry_run:
        tally.mappings = [_mapping(voter, release.mbid, stamp) for voter, stamp in stamps]
        return block, unassigned
    entries = [(voter.file_id, stamp) for voter, stamp in stamps]
    staging.stage_tags_batch(
        settings,
        entries=entries,
        note=f"musicbrainz release {release.mbid}: {release.title}",
    )
    return block, unassigned


def _present_rows(conn: sqlite3.Connection, file_ids: list[int]) -> list[store.FileRow]:
    """Return the file rows of *file_ids*. A file missing on disk raises :class:`ValueError`."""
    rows: list[store.FileRow] = []
    for file_id in file_ids:
        row = store.get_file_by_id(conn, file_id)
        if row is None or row.is_missing:
            message = f"file_id={file_id} is missing on disk, so no release can be applied to it"
            raise ValueError(message)
        rows.append(row)
    return rows


def _evidence_reason(ballot: _Ballot) -> str | None:
    """Return why a file's evidence places it nowhere, or ``None`` when it can be placed."""
    if ballot.evidence.error is not None:
        return "error"
    if ballot.evidence.empty:
        return "lookup_empty"
    if not ballot.gate.titled:
        return "no_contribution"
    return None


def _slot_reason(
    ballot: _Ballot,
    slots: list[_Slot],
    claims: dict[tuple[str, int, int], set[int]],
) -> str | None:
    """Return why a placed file is not assigned, or ``None`` when it is.

    The auto tier's recording gate is not required here: the caller named the release, so one
    corroborated slot that no other file claims is enough.
    """
    if not slots:
        return "not_on_release"
    if len(slots) > 1:
        return "ambiguous_slot"
    slot = slots[0]
    if not _length_fits(slot.recording.duration, ballot.evidence.duration):
        return "length_mismatch"
    corroborated = slot.recording.id in {r.id for r in ballot.gate.group} or _stem_holds(
        slot.recording.title,
        ballot.voter.stem,
    )
    if not corroborated:
        return "uncorroborated"
    return None if claims[slot.key] == {ballot.voter.file_id} else "slot_collision"


def _one(value: str) -> list[str]:
    """Return *value* as a tag value list, empty (a delete) when it is blank."""
    return [value] if value.strip() else []


def _stamp(release: MBRelease, track: MBTrack, voter: _Voter) -> dict[str, list[str]]:
    """Return the whole release stamp for one file: every value written, every stale one cleared.

    Sort names are replaced from the credits, never cleared, because a library server keeps a
    stored sort name after the tag goes empty. On the file's own release a field of
    :data:`_KEPT_ON_OWN_RELEASE` the release leaves blank is left out, so it is kept, as is a
    field of :data:`_KEPT_ON_OWN_RECORDING` on the file's own recording. ``originaldate`` on
    the file's own release is written only when it refines the file's value (:func:`_refines`).
    """
    medium = _medium(release, track)
    track_count = _track_count(medium)
    own_release = voter.value(_ALBUM_ID) == release.mbid
    own_recording = voter.value(_RECORDING_ID) == track.recording_mbid
    kept = (_KEPT_ON_OWN_RELEASE if own_release else ()) + (
        _KEPT_ON_OWN_RECORDING if own_recording else ()
    )
    stamp: dict[str, list[str]] = {
        _TITLE: [track.title],
        _TRACK: [f"{track.position}/{track_count}"],
        _DISC: [f"{0 if medium is None else medium.position}/{len(release.media)}"],
        _ARTIST: _one(track.artist_credit),
        "artistsort": _one(track.artist_sort),
        _ARTIST_ID: list(track.artist_mbids),
        "artists": list(track.artist_names),
        _ALBUM_ARTIST: _one(release.artist_credit),
        "albumartistsort": _one(release.artist_sort),
        "musicbrainz_albumartistid": list(release.artist_mbids),
        "album": [release.title],
        "date": _one(release.date),
        _ORIGINAL_DATE: _one(release.first_release_date),
        "releasecountry": _one(release.country),
        "musicbrainz_albumstatus": _one(release_match.album_status(release)),
        "musicbrainz_albumtype": list(release.release_types),
        "catalognumber": list(release.catalog_numbers),
        "barcode": _one(release.barcode),
        "asin": _one(release.asin),
        "media": _one("" if medium is None else medium.format),
        _ALBUM_ID: [release.mbid],
        "musicbrainz_releasegroupid": _one(release.release_group_mbid),
        "musicbrainz_releasetrackid": _one(track.release_track_mbid),
        _RECORDING_ID: _one(track.recording_mbid),
        "isrc": list(track.isrcs),
    }

    for name in kept:
        if not stamp[name]:
            del stamp[name]
    if own_release and not _refines(voter.value(_ORIGINAL_DATE), release.first_release_date):
        del stamp[_ORIGINAL_DATE]
    return stamp


def _refines(held: str, date: str) -> bool:
    """Return whether *date* adds precision to *held*: *held* is blank, or its year or year-month.

    A different or equally precise value is the owner's, so the stamp never writes over it.
    """
    held_date = held.strip()
    return not held_date or date.startswith(f"{held_date}-")


def _release_block(release: MBRelease) -> dict[str, object]:
    """Return the release and its tracklist, for hand-staging an excluded file."""
    return {
        "release_mbid": release.mbid,
        "title": release.title,
        "status": release.status,
        "country": release.country,
        "date": release.date,
        "media": [
            {
                "position": medium.position,
                "format": medium.format,
                "tracks": [
                    {
                        "position": track.position,
                        "number": track.number,
                        "title": track.title,
                        "release_track_mbid": track.release_track_mbid,
                        "recording_mbid": track.recording_mbid,
                    }
                    for track in medium.tracks
                ],
            }
            for medium in release.media
        ],
    }


# --- result --------------------------------------------------------------------------


def _build_result(
    tally: _Tally,
    *,
    pending_remaining: int,
    release: dict[str, object] | None,
    unassigned: list[dict[str, object]] | None,
    dry_run: bool,
) -> ResolveSongsResult:
    """Freeze the run's tally into the public :class:`ResolveSongsResult`.

    ``more`` is ``cold_folders_remaining > 0``: held files stay pending by design, so only a
    cold folder is new work. A dry run advances too, because it fills the caches.
    """
    return ResolveSongsResult(
        settled=tally.settled,
        staged_files=tally.staged_files,
        errors=len(tally.error_items),
        error_items=list(tally.error_items),
        pending_remaining=pending_remaining,
        cold_folders_remaining=tally.cold_folders_remaining,
        more=tally.cold_folders_remaining > 0,
        summary=_summarize(
            tally,
            pending_remaining=pending_remaining,
            unassigned=unassigned,
            dry_run=dry_run,
        ),
        verified_files=tally.verified_files,
        held_disagreement=tally.held["disagreement"],
        held_slot_collision=tally.held["slot_collision"],
        held_release_mismatch=tally.held["release_mismatch"],
        held_unconverged=tally.held["unconverged"],
        held_no_contribution=tally.held["no_contribution"],
        review_files=len({row["file_id"] for row in tally.review_values}),
        lookup_empty=tally.lookup_empty,
        skipped_manual=tally.skipped_manual,
        mappings=list(tally.mappings),
        rebind_folders=list(tally.rebind_folders),
        held_values=list(tally.held_values),
        review_values=list(tally.review_values),
        release=release,
        unassigned=unassigned,
    )


def _summarize(
    tally: _Tally,
    *,
    pending_remaining: int,
    unassigned: list[dict[str, object]] | None,
    dry_run: bool,
) -> str:
    """Build a short, plain human summary of the run."""
    verb = "Would stage" if dry_run else "Staged"
    if unassigned is not None:
        if unassigned:
            return f"{len(unassigned)} file(s) are not assigned on the release, so nothing staged."
        return f"{verb} the release stamp on {tally.staged_files} file(s)."
    held = sum(tally.held.values())
    parts = [
        f"Settled {tally.settled} file(s): verified {tally.verified_files}. "
        f"{verb} {tally.staged_files} fill(s). Held {held}.",
    ]
    if tally.rebind_folders:
        parts.append(f"{len(tally.rebind_folders)} folder(s) need a manual release rebind.")
    if tally.error_items:
        parts.append(f"{len(tally.error_items)} file(s) errored and stay pending. Re-run to retry.")
    if tally.cold_folders_remaining:
        parts.append(f"{tally.cold_folders_remaining} cold folder(s) left. Call again to continue.")
    parts.append(f"{pending_remaining} file(s) in scope still pending.")
    return " ".join(parts)


# --- status tools --------------------------------------------------------------------


def set_song_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
    status: str,
) -> int:
    """Record ``manual`` on the song axis for every file in scope.

    Scope is *file_ids* when given, else every file whose ``album`` tag equals *value*: this axis
    has no lookup name field, and a whole album is the natural unit to exclude by hand. With
    neither the call changes nothing and returns 0. A ``manual`` file still votes for its folder.
    The row is sticky until :func:`reset_song_status`. Raises :class:`ValueError` for any
    *status* other than ``manual`` or an unknown file id.
    """
    return axis_status.set_manual_status(
        settings,
        axis.SONG_AXIS,
        file_ids=file_ids,
        value=value,
        status=status,
    )


def reset_song_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> int:
    """Delete the song status row of every file in scope, the one hand-back of ``manual``.

    Same scoping as :func:`set_song_status`. Returns the number of files affected.
    """
    return axis_status.reset_status(settings, axis.SONG_AXIS, file_ids=file_ids, value=value)
