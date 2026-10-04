"""Tag coherence against MusicBrainz: files whose tags contradict the release they name.

The third comparison in the coherence family. :mod:`tagmend.engine.mismatch` compares a
file's tags against the folder PATH, :mod:`tagmend.engine.album_conflicts` and
:mod:`tagmend.engine.track_conflicts` compare a file against its folder SIBLINGS, and this
compares a file against an EXTERNAL authority: the MusicBrainz release its own
``musicbrainz_albumid`` names.

That id makes the comparison a direct lookup with nothing to guess. Inside the release, a
file finds its own track by ``musicbrainz_releasetrackid`` (which names a track on this
release) or, failing that, ``musicbrainz_trackid`` (which names the recording). Position is
deliberately never used to match: a file whose numbering is wrong is exactly what this
detector is for, so matching on it would hide the defect it exists to find.

Two levels of field are checked, and they fail independently:

* **release-level** (``album``, ``albumartist``, ``date``, ``releasecountry``,
  ``musicbrainz_albumstatus``) need only the release, so they are checked even for a file
  carrying no track id at all.
* **track-level** (``title``, ``artist``, ``tracknumber``, ``discnumber``) need a matched
  track, and are skipped when there is none. ``artist`` is track-level because a credit is per
  track: a guest track carries its own, and that is the one the file should name.

``albumartist`` and ``artist`` are not compared by name when the credit names one artist and the
file's own id field (``musicbrainz_albumartistid``, ``musicbrainz_artistid``) holds exactly that
id. The artist axis owns that spelling, since it writes a single artist's canonical name where
the release prints its credited one.

A blank field is a **fill**, not a disagreement. The repo's glossary separates the two
(``gap`` is tag against absent, ``disagreement`` is tag against an external source), and so
does this report: ``flagged`` counts only fields where the file says something and the release
says something else, while ``fill_rows`` collects the fields the release can supply for free.
Keeping them together would bury a few hundred real contradictions under thousands of blanks.

Read-only, like every ``detect_*`` tool. It writes no tags and stages nothing. The only
ledger writes are the release lookup's own cache rows.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

from tagmend.engine import db, lookup_clients, path_keys, release_match, schema, store
from tagmend.engine.detector_core import (
    Tier,
    group_by_folder,
    narrow,
    ordered,
    tiers_by_file,
    validate_tier,
)
from tagmend.engine.musicbrainz import MusicBrainzClient, MusicBrainzError
from tagmend.engine.serialize import FieldDict
from tagmend.engine.text_keys import display_key
from tagmend.engine.validation import check_limit
from tagmend.log import get_logger

if TYPE_CHECKING:
    import os
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.musicbrainz import MBRelease, MBReleaseCacheSource, MBTrack

logger = get_logger(__name__)

_DETECT_FIELDS: Final = (
    "musicbrainz_albumid",
    "musicbrainz_releasetrackid",
    "musicbrainz_trackid",
    "album",
    "albumartist",
    "artist",
    "title",
    "tracknumber",
    "discnumber",
    "date",
    "releasecountry",
    "musicbrainz_albumstatus",
)

# Each artist name field's own id field. Picard writes one id per credited artist, so every value
# is read: a file naming two artists must not pass as its first.
_ALBUMARTIST_ID_FIELD: Final = "musicbrainz_albumartistid"
_ARTIST_ID_FIELD: Final = "musicbrainz_artistid"

# How many distinct releases one call fetches when the caller names no limit. At the one
# request per second MusicBrainz asks for, this is about three minutes of wall clock.
_DEFAULT_RELEASE_LIMIT: Final = 200

# MusicBrainz dates are ``YYYY`` / ``YYYY-MM`` / ``YYYY-MM-DD``.
_DATE_SEPARATOR: Final = "-"


# Fields that decide how a library groups, names and orders this file. A disagreement here is
# visible to anyone browsing.
_MEDIUM_FIELDS: Final = frozenset(
    {"album", "albumartist", "artist", "title", "tracknumber", "discnumber"},
)

_REASON_UNMATCHED: Final = "this file's release-track id is not on the release its album id names"


@dataclass(frozen=True, slots=True)
class _FileInput:
    """One tracked file reduced to the fields compared against its release."""

    file_id: int
    folder: str
    filename: str
    release_mbid: str | None = None
    release_track_mbid: str | None = None
    recording_mbid: str | None = None
    album: str | None = None
    albumartist: str | None = None
    artist: str | None = None
    title: str | None = None
    tracknumber: str | None = None
    discnumber: str | None = None
    date: str | None = None
    releasecountry: str | None = None
    musicbrainz_albumstatus: str | None = None
    albumartist_mbids: tuple[str, ...] = ()
    artist_mbids: tuple[str, ...] = ()


@dataclass(slots=True)
class _Lookups:
    """Every release's lookup outcome for one run."""

    found: dict[str, MBRelease] = field(default_factory=dict)
    attempted: int = 0
    checked: int = 0
    remaining: int = 0
    unknown: int = 0
    error_items: list[dict[str, str]] = field(default_factory=list)


# --- public result types -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleaseDisagreementRow(FieldDict):
    """One field on one file that contradicts the release the file names."""

    file_id: int
    folder: str
    filename: str
    release_mbid: str
    release_title: str
    field: str
    have: str
    want: str
    tier: str  # Tier value
    reason: str

    @property
    def is_fill(self) -> bool:
        """Return whether this row fills a blank rather than contradicting a value."""
        return not self.have


@dataclass(frozen=True, slots=True)
class ReleaseDisagreementGroup(FieldDict):
    """One folder's disagreements, compact enough to scan a whole library at a glance.

    ``flagged`` counts files, matching the headline count, so the groups sum to it. One file
    with two wrong fields is one, and ``flagged_fields`` counts the fields beside it.
    ``file_ids`` names the flagged files only, never a file that merely has a blank to fill.
    ``releases`` lists every release the folder's in-scope files name.
    """

    folder: str
    file_count: int
    flagged: int
    tiers: dict[str, int]
    file_ids: list[int]
    flagged_fields: int
    fills: int
    fields: dict[str, int]
    releases: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class ReleaseDisagreementsReport(FieldDict):
    """Immutable summary of one :func:`detect_release_disagreements` run, JSON-ready for the tool.

    ``flagged`` counts files with at least one contradiction and ``flagged_fields`` counts the
    contradicting fields. Each file sits in the tier of its most severe contradiction, so the
    tier counts sum to ``flagged``.
    """

    rows: list[ReleaseDisagreementRow]
    total_files: int
    flagged: int
    flagged_fields: int
    high: int
    medium: int
    low: int
    fills: int
    fill_rows: list[ReleaseDisagreementRow]
    releases_attempted: int
    releases_checked: int
    releases_remaining: int
    more: bool
    skipped_no_release_mbid: int
    unknown_releases: int
    unmatched_tracks: int
    errors: int
    error_items: list[dict[str, str]] = field(default_factory=list)
    groups: list[ReleaseDisagreementGroup] = field(default_factory=list)
    # Last, so the payload ends with it. Keyword-only keeps it required after the defaults.
    summary: str = field(kw_only=True)


# --- comparison helpers --------------------------------------------------------------


def _date_agrees(have: str, want: str) -> bool:
    """Return whether two dates agree, allowing the TAG to be the more precise of the two.

    MusicBrainz often carries a bare year where the tag carries the full date it came from,
    so ``1997-09-18`` against ``1997`` is agreement. The leniency is one-directional and
    component-aware: a shorter tag is not evidence, and ``1997-1`` is January, not a prefix of
    October. A blank tag is not agreement either, or the most useful fill of all would never
    be reported.
    """
    if not want:
        return True
    if not have:
        return False
    if have == want:
        return True
    return have.startswith(want) and have[len(want)] == _DATE_SEPARATOR


def _match_track(file: _FileInput, release: MBRelease) -> MBTrack | None:
    """Return the release track this file names, or ``None`` when it names none.

    Position is deliberately not a fallback: a wrong track number is one of the defects this
    detector reports, so matching on it would hide exactly what it exists to find.
    """
    if file.release_track_mbid:
        found = release.track_by_release_track_mbid(file.release_track_mbid)
        if found is not None:
            return found
    if file.recording_mbid:
        return release.track_by_recording_mbid(file.recording_mbid)
    return None


def _tier_for(field_name: str) -> Tier:
    """Return the tier a disagreement on *field_name* carries."""
    return Tier.MEDIUM if field_name in _MEDIUM_FIELDS else Tier.LOW


def _artist_axis_owns_spelling(
    have: str,
    file_mbids: tuple[str, ...],
    credit_mbids: tuple[str, ...],
) -> bool:
    """Return whether a non-blank artist name names the credit's one artist by id exactly."""
    return bool(have) and len(credit_mbids) == 1 and file_mbids == credit_mbids


def _track_number_reason(track: MBTrack, want_number: str) -> str:
    """Return the tracknumber reason, naming the position when the release numbers otherwise."""
    if release_match.position(track.number) == want_number:
        return f"the release says {track.number!r}"
    return f"the release says {track.number!r} (position {track.position})"


# --- lookups -------------------------------------------------------------------------


def _look_up(
    release_mbids: list[str],
    client: MBReleaseCacheSource,
    *,
    release_limit: int | None,
) -> _Lookups:
    """Look up each release once, spending *release_limit* only on uncached ones.

    A cached answer costs no request, so it never counts toward the cap. That is what lets a
    re-run with the same cap reach the releases the previous run left behind. ``None`` caps
    nothing.
    """
    lookups = _Lookups()
    network_budget = release_limit
    for release_mbid in release_mbids:
        cached = client.has_cached_release(release_mbid)
        if not cached and network_budget == 0:
            lookups.remaining += 1
            continue
        if not cached and network_budget is not None:
            network_budget -= 1
        lookups.attempted += 1
        try:
            release = client.release_by_mbid(release_mbid)
        except MusicBrainzError as exc:
            logger.warning("musicbrainz release error for mbid=%r: %s", release_mbid, exc)
            lookups.error_items.append({"key": release_mbid, "message": str(exc)})
            continue
        lookups.checked += 1
        if release is None:
            lookups.unknown += 1
            continue
        lookups.found[release_mbid] = release
    return lookups


# --- classifier ----------------------------------------------------------------------


def _release_expectations(
    file: _FileInput,
    release: MBRelease,
) -> tuple[tuple[str, str | None, str], ...]:
    """Return each release-level field as ``(field, the file's value, the release's value)``."""
    return (
        ("album", file.album, release.title),
        ("albumartist", file.albumartist, release.artist_credit),
        ("date", file.date, release.date),
        ("releasecountry", file.releasecountry, release.country),
        (
            "musicbrainz_albumstatus",
            file.musicbrainz_albumstatus,
            release_match.album_status(release),
        ),
    )


def _compare_one(
    file: _FileInput,
    release: MBRelease,
    track: MBTrack | None,
) -> list[ReleaseDisagreementRow]:
    """Return every field on *file* that contradicts *release* (and *track* when matched)."""
    rows: list[ReleaseDisagreementRow] = []

    def add(field_name: str, have: str, want: str, tier: Tier, reason: str) -> None:
        rows.append(
            ReleaseDisagreementRow(
                file_id=file.file_id,
                folder=file.folder,
                filename=file.filename,
                release_mbid=release.mbid,
                release_title=release.title,
                field=field_name,
                have=have,
                want=want,
                tier=tier.value,
                reason=reason,
            ),
        )

    if file.release_track_mbid and track is None:
        add(
            "musicbrainz_releasetrackid",
            file.release_track_mbid,
            "",
            Tier.HIGH,
            _REASON_UNMATCHED,
        )

    for field_name, have_raw, want in _release_expectations(file, release):
        if not want:
            continue
        have = (have_raw or "").strip()
        if field_name == "albumartist" and _artist_axis_owns_spelling(
            have, file.albumartist_mbids, release.artist_mbids
        ):
            continue
        agrees = (
            _date_agrees(have, want)
            if field_name == "date"
            else display_key(have) == display_key(want)
        )
        if not agrees:
            add(
                field_name,
                have,
                want,
                _tier_for(field_name),
                f"the release says {want!r}",
            )

    if track is None:
        return rows

    rows.extend(_compare_track(file, release, track))
    return rows


def _compare_track(
    file: _FileInput,
    release: MBRelease,
    track: MBTrack,
) -> list[ReleaseDisagreementRow]:
    """Return every track-level field on *file* that contradicts its matched *track*."""
    rows: list[ReleaseDisagreementRow] = []

    def add(field_name: str, have: str, want: str, reason: str) -> None:
        rows.append(
            ReleaseDisagreementRow(
                file_id=file.file_id,
                folder=file.folder,
                filename=file.filename,
                release_mbid=release.mbid,
                release_title=release.title,
                field=field_name,
                have=have,
                want=want,
                tier=_tier_for(field_name).value,
                reason=reason,
            ),
        )

    # The stamp writes the sequential position, so a vinyl side number is never proposed.
    want_number = (
        str(track.position) if track.position > 0 else release_match.position(track.number)
    )
    have_number = release_match.position(file.tracknumber)
    if have_number and not release_match.track_number_agrees(have_number, track):
        add("tracknumber", have_number, want_number, _track_number_reason(track, want_number))
    elif not have_number and want_number:
        add("tracknumber", "", want_number, "")

    for field_name, have_raw, want in (
        ("title", file.title, track.title),
        # The credit is per track, not per release: a guest track carries its own, and that
        # is the one the file should name.
        ("artist", file.artist, track.artist_credit),
        (
            "discnumber",
            release_match.position(file.discnumber),
            release_match.disc_expectation(release, track),
        ),
    ):
        have = (have_raw or "").strip()
        if field_name == "artist" and _artist_axis_owns_spelling(
            have, file.artist_mbids, track.artist_mbids
        ):
            continue
        if want and display_key(have) != display_key(want):
            add(field_name, have, want, f"the release says {want!r}")

    return rows


def _classify(
    files: list[_FileInput],
    client: MBReleaseCacheSource,
    *,
    release_limit: int | None,
) -> ReleaseDisagreementsReport:
    """Compare every in-scope file against the release it names, one lookup per release."""
    # Input: group by release so each is looked up at most once, in first-seen order.
    by_release: dict[str, list[_FileInput]] = defaultdict(list)
    skipped = 0
    for f in files:
        release_mbid = (f.release_mbid or "").strip()
        if not release_mbid:
            skipped += 1
            continue
        by_release[release_mbid].append(f)

    # Process: one lookup per release, then every field of every file on it.
    lookups = _look_up(list(by_release), client, release_limit=release_limit)
    rows: list[ReleaseDisagreementRow] = []
    unmatched = 0
    for release_mbid, release in lookups.found.items():
        for f in by_release[release_mbid]:
            track = _match_track(f, release)
            if track is None:
                unmatched += 1
            rows.extend(_compare_one(f, release, track))

    # Output: the contradictions and the blank fills are separate populations.
    contradictions = [r for r in rows if not r.is_fill]
    fills = [r for r in rows if r.is_fill]
    tiers = tiers_by_file(contradictions)
    titles = {release_mbid: release.title for release_mbid, release in lookups.found.items()}
    return ReleaseDisagreementsReport(
        rows=ordered(contradictions),
        total_files=len(files),
        flagged=sum(tiers.values()),
        flagged_fields=len(contradictions),
        high=tiers.get(Tier.HIGH.value, 0),
        medium=tiers.get(Tier.MEDIUM.value, 0),
        low=tiers.get(Tier.LOW.value, 0),
        fills=len(fills),
        fill_rows=ordered(fills),
        releases_attempted=lookups.attempted,
        releases_checked=lookups.checked,
        releases_remaining=lookups.remaining,
        more=lookups.remaining > 0,
        skipped_no_release_mbid=skipped,
        unknown_releases=lookups.unknown,
        unmatched_tracks=unmatched,
        errors=len(lookups.error_items),
        error_items=lookups.error_items,
        groups=_build_groups(rows, files, titles),
        summary=_summarize(
            rows=contradictions,
            fills=len(fills),
            tiers=tiers,
            checked=lookups.checked,
            remaining=lookups.remaining,
            unknown=lookups.unknown,
            unmatched=unmatched,
            errors=len(lookups.error_items),
        ),
    )


def _releases_in(folder_files: list[_FileInput], titles: dict[str, str]) -> list[dict[str, object]]:
    """Return every release one folder's files name, with its title and file count."""
    counts = Counter(
        release_mbid for f in folder_files if (release_mbid := (f.release_mbid or "").strip())
    )
    return [
        {
            "release_mbid": release_mbid,
            "release_title": titles.get(release_mbid, ""),
            "file_count": n,
        }
        for release_mbid, n in sorted(counts.items())
    ]


def _build_groups(
    rows: list[ReleaseDisagreementRow],
    files: list[_FileInput],
    titles: dict[str, str],
) -> list[ReleaseDisagreementGroup]:
    """Fold the rows into one line per folder, sorted by folder."""
    rows_by_folder = group_by_folder(rows)
    files_by_folder = group_by_folder(files)
    groups: list[ReleaseDisagreementGroup] = []
    for folder in sorted(rows_by_folder):
        base = ReleaseDisagreementGroup(
            folder=folder,
            file_count=len(files_by_folder.get(folder, [])),
            flagged=0,
            tiers={},
            file_ids=[],
            flagged_fields=0,
            fills=0,
            fields={},
            releases=_releases_in(files_by_folder.get(folder, []), titles),
        )
        groups.append(_refold_group(base, rows_by_folder[folder]))
    return groups


def _refold_group(
    group: ReleaseDisagreementGroup, rows: list[ReleaseDisagreementRow]
) -> ReleaseDisagreementGroup:
    """Return *group* with its counts describing exactly *rows*, contradictions and fills."""
    contradictions = [r for r in rows if not r.is_fill]
    tiers = tiers_by_file(contradictions)
    return replace(
        group,
        flagged=sum(tiers.values()),
        tiers=dict(tiers),
        file_ids=sorted({r.file_id for r in contradictions}),
        flagged_fields=len(contradictions),
        fills=len(rows) - len(contradictions),
        fields=dict(Counter(r.field for r in contradictions)),
    )


def _summarize(  # noqa: PLR0913 - one keyword per reported count, cohesive by design
    *,
    rows: list[ReleaseDisagreementRow],
    fills: int,
    tiers: Counter[str],
    checked: int,
    remaining: int,
    unknown: int,
    unmatched: int,
    errors: int,
) -> str:
    """Build a short, plain human summary of the run."""
    if not rows:
        head = f"Every file agrees with the release it names ({checked} release(s) checked)."
    else:
        head = (
            f"{sum(tiers.values())} file(s) disagree with the release they name "
            f"across {len(rows)} field(s), over {checked} release(s): "
            f"{tiers.get(Tier.HIGH.value, 0)} high, {tiers.get(Tier.MEDIUM.value, 0)} medium, "
            f"{tiers.get(Tier.LOW.value, 0)} low."
        )
    if fills:
        head += f" {fills} blank field(s) could be filled from the release."
    if unmatched:
        head += f" {unmatched} file(s) could not be matched to a track on their release."
    if unknown:
        head += f" {unknown} release id(s) are unknown to MusicBrainz."
    if remaining:
        head += (
            f" {remaining} release(s) not yet checked. Re-run, or raise release_limit, to "
            f"reach them."
        )
    if errors:
        head += f" {errors} release lookup(s) errored and stay pending. Re-run to retry."
    return head


# --- public entry --------------------------------------------------------------------


def _load_inputs(
    connection: sqlite3.Connection,
    scoped_ids: list[int] | None,
) -> list[_FileInput]:
    """Read every in-scope present file's detect fields out of the snapshot mirror."""
    tag_values = store.load_tag_values(connection, _DETECT_FIELDS)
    id_lists = store.load_tag_lists(connection, (_ALBUMARTIST_ID_FIELD, _ARTIST_ID_FIELD))
    wanted = None if scoped_ids is None else set(scoped_ids)
    inputs: list[_FileInput] = []
    for row in store.list_files(connection):
        if row.is_missing or (wanted is not None and row.id not in wanted):
            continue
        values = tag_values.get(row.id, {})
        ids = id_lists.get(row.id, {})
        inputs.append(
            _FileInput(
                file_id=row.id,
                folder=row.folder,
                filename=row.filename,
                release_mbid=values.get("musicbrainz_albumid"),
                release_track_mbid=values.get("musicbrainz_releasetrackid"),
                recording_mbid=values.get("musicbrainz_trackid"),
                album=values.get("album"),
                albumartist=values.get("albumartist"),
                artist=values.get("artist"),
                title=values.get("title"),
                tracknumber=values.get("tracknumber"),
                discnumber=values.get("discnumber"),
                date=values.get("date"),
                releasecountry=values.get("releasecountry"),
                musicbrainz_albumstatus=values.get("musicbrainz_albumstatus"),
                albumartist_mbids=_artist_mbids(ids.get(_ALBUMARTIST_ID_FIELD, [])),
                artist_mbids=_artist_mbids(ids.get(_ARTIST_ID_FIELD, [])),
            ),
        )
    return inputs


def _artist_mbids(values: list[str]) -> tuple[str, ...]:
    """Return a file's artist ids in order, one per tag value."""
    return tuple(mbid for value in values if (mbid := value.strip()))


def detect_release_disagreements(  # noqa: PLR0913 - cohesive keyword-only scope + injection params
    settings: Settings,
    *,
    tier: str | None = None,
    path: str | os.PathLike[str] | None = None,
    folder: str | None = None,
    file_ids: list[int] | None = None,
    release_limit: int | None = None,
    limit: int | None = None,
    group: bool = False,
    client: MBReleaseCacheSource | None = None,
) -> ReleaseDisagreementsReport:
    """Report files whose tags contradict the MusicBrainz release their album id names.

    Reads the snapshot, so run ``scan_library`` first.

    *path* and *file_ids* scope the RUN: the counts then describe only that scope, and no
    release outside it is fetched. *path* takes the folder itself and every folder under it.
    *folder* and *tier* narrow the VIEW alone, so the counts still describe the whole run.
    *folder* names exactly one folder and wins over *group*. *release_limit* caps the distinct
    releases fetched over the network this call (default 200, about three minutes at
    MusicBrainz's requested one request per second). A cached release never counts toward it,
    and the releases not reached are reported via ``releases_remaining``/``more``. *limit* caps
    the rows (or groups) returned without changing any count. *path* and *folder* are compared
    as paths (:func:`tagmend.engine.path_keys.folder_arg_key`). *client* injects an
    :class:`tagmend.engine.musicbrainz.MBReleaseCacheSource` for tests. Raises :class:`ValueError`
    for a *folder* with neither *path* nor *file_ids*, an unknown *tier*, a negative
    *release_limit* or *limit*, or a *path* or *folder* outside ``music_path``.
    """
    if folder is not None and path is None and file_ids is None:
        message = (
            "detect_release_disagreements fetches from MusicBrainz, so scope the run with "
            "path=<folder> or file_ids. folder= only narrows a scoped run's view"
        )
        raise ValueError(message)
    check_limit(release_limit, name="release_limit")
    check_limit(limit)
    validate_tier(tier)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)
    folder_key = None if folder is None else path_keys.folder_arg_key(settings, folder)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        scoped = None if file_ids is None else store.files_in_scope(connection, file_ids=file_ids)
        files = _load_inputs(connection, scoped)
        if root_key is not None:
            files = [
                f for f in files if path_keys.is_within(path_keys.path_key(f.folder), root_key)
            ]

        effective_limit = _DEFAULT_RELEASE_LIMIT if release_limit is None else release_limit
        with lookup_clients.injected_or_owned(
            client,
            lambda: MusicBrainzClient.from_settings(settings, connection),
        ) as source:
            report = _classify(files, source, release_limit=effective_limit)
    finally:
        connection.close()

    logger.info(
        "release disagreements: flagged=%s over %s release(s), %s file(s)",
        report.flagged,
        report.releases_checked,
        report.total_files,
    )
    return narrow(
        report,
        rows=report.rows,
        groups=report.groups,
        secondary_field="fill_rows",
        secondary_rows=report.fill_rows,
        secondary_in_tier=True,
        refold=_refold_group,
        tier=tier,
        folder_key=folder_key,
        limit=limit,
        group=group,
    )
