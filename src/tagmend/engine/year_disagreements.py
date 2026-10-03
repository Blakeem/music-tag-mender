"""Year coherence against MusicBrainz: years that contradict the release group's first release.

The year sibling of :mod:`tagmend.engine.release_disagreements`, which compares a file against
the release its own ``musicbrainz_albumid`` names. This compares a file's ``originaldate`` and
``date`` against the first-release year of the release group its
``(albumartist-else-artist, album)`` resolves to. That is the lookup and the cache
``resolve_years`` uses to fill a blank ``originaldate``, so a library ``resolve_years`` has
already swept costs no request.

The cached lookup keeps only the first-release YEAR, so both tiers compare years alone:

* ``high``: ``originaldate`` names a different year than the first release.
* ``medium``: ``date`` is earlier than the first release, which no release of the group can be.

A blank ``originaldate`` is not a row, because filling it is ``resolve_years``' job. A match by
name can land on the wrong release group (a re-release, a soundtrack, a compilation), so this
proposes nothing to stage and a human confirms each correction.

Read-only, like every ``detect_*`` tool. The only ledger writes are the lookup's cache rows.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

from tagmend.engine import axis, db, lookup_clients, path_keys, schema, store
from tagmend.engine.detector_core import (
    Tier,
    group_by_key,
    is_non_album_folder,
    narrow,
    ordered,
    tiers_by_file,
    validate_tier,
)
from tagmend.engine.musicbrainz import MusicBrainzClient, MusicBrainzError
from tagmend.engine.serialize import FieldDict
from tagmend.engine.text_keys import year_key
from tagmend.engine.validation import check_limit
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.musicbrainz import MBReleaseGroup, MBReleaseGroupCacheSource

logger = get_logger(__name__)

_ORIGINALDATE: Final = "originaldate"
_DATE: Final = "date"

# How many uncached release groups one call looks up when the caller names no limit. At the one
# request per second MusicBrainz asks for, this is about three minutes of wall clock.
_DEFAULT_RELEASE_LIMIT: Final = 200

_REASON_NON_ALBUM: Final = (
    "non-album folder (singles/remixes), where a single's year legitimately differs from the "
    "album it shares a name with"
)

# (folder, lookup artist, lookup album): one grouped line per album identity in a folder.
type _GroupKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class _FileInput:
    """One tracked file reduced to its lookup identity and the two years compared."""

    file_id: int
    folder: str
    filename: str
    artist: str | None = None
    album: str | None = None
    originaldate: str | None = None
    date: str | None = None


@dataclass(frozen=True, slots=True)
class _Resolved:
    """One looked-up album identity whose release group carries a usable first-release year."""

    release_group: MBReleaseGroup
    first_release_year: str


@dataclass(slots=True)
class _Lookups:
    """Every album identity's lookup outcome for one run."""

    resolved: dict[tuple[str, str], _Resolved] = field(default_factory=dict)
    checked: int = 0
    remaining: int = 0
    unknown: int = 0
    error_items: list[dict[str, str]] = field(default_factory=list)


# --- public result types -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class YearDisagreementRow(FieldDict):
    """One year field on one file that contradicts its release group's first release."""

    file_id: int
    folder: str
    filename: str
    artist: str
    album: str
    field: str
    have: str
    first_release_year: str
    release_group_mbid: str
    release_group_title: str
    tier: str  # Tier value
    reason: str

    @property
    def group_key(self) -> _GroupKey:
        """Return the grouped-view line this row belongs to."""
        return (self.folder, self.artist, self.album)


@dataclass(frozen=True, slots=True)
class YearDisagreementGroup(FieldDict):
    """One album identity's disagreements inside one folder.

    A folder holding several albums gets one group per album, because each resolves to its own
    release group and so its own first-release year. ``file_count`` counts the folder's present
    files of this identity. ``flagged`` counts files, so the groups sum to the headline count,
    and ``file_ids`` names the flagged files only.
    """

    folder: str
    artist: str
    album: str
    first_release_year: str
    release_group_mbid: str
    release_group_title: str
    file_count: int
    flagged: int
    folder_context: int
    tiers: dict[str, int]
    file_ids: list[int]
    fields: dict[str, int]

    @property
    def group_key(self) -> _GroupKey:
        """Return the key the rows of this group carry."""
        return (self.folder, self.artist, self.album)


@dataclass(frozen=True, slots=True)
class YearDisagreementsReport:
    """Immutable summary of one :func:`detect_year_disagreements` run, JSON-ready for the tool.

    ``flagged`` counts files with at least one contradicting year and ``flagged_fields`` counts
    the contradicting fields. Each file sits in the tier of its most severe row, so the tier
    counts sum to ``flagged``. ``folder_context`` counts files in non-album folders, outside
    both.
    """

    rows: list[YearDisagreementRow]
    total_files: int
    flagged: int
    flagged_fields: int
    high: int
    medium: int
    low: int
    release_groups_checked: int
    release_groups_remaining: int
    more: bool
    unknown_release_groups: int
    skipped_no_identity: int
    errors: int
    summary: str
    folder_context: int = 0
    folder_context_rows: list[YearDisagreementRow] = field(default_factory=list)
    error_items: list[dict[str, str]] = field(default_factory=list)
    groups: list[YearDisagreementGroup] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "rows": [r.to_dict() for r in self.rows],
            "total_files": self.total_files,
            "flagged": self.flagged,
            "flagged_fields": self.flagged_fields,
            "high": self.high,
            "medium": self.medium,
            "low": self.low,
            "folder_context": self.folder_context,
            "folder_context_rows": [r.to_dict() for r in self.folder_context_rows],
            "release_groups_checked": self.release_groups_checked,
            "release_groups_remaining": self.release_groups_remaining,
            "more": self.more,
            "unknown_release_groups": self.unknown_release_groups,
            "skipped_no_identity": self.skipped_no_identity,
            "errors": self.errors,
            "error_items": [dict(e) for e in self.error_items],
            "groups": [g.to_dict() for g in self.groups],
            "summary": self.summary,
        }


# --- comparison ----------------------------------------------------------------------


def _compare_one(
    file: _FileInput,
    artist: str,
    album: str,
    resolved: _Resolved,
) -> list[YearDisagreementRow]:
    """Return every year on *file* that contradicts its release group's first release."""
    first_year = resolved.first_release_year
    rows: list[YearDisagreementRow] = []

    def add(field_name: str, have: str, tier: Tier, reason: str) -> None:
        rows.append(
            YearDisagreementRow(
                file_id=file.file_id,
                folder=file.folder,
                filename=file.filename,
                artist=artist,
                album=album,
                field=field_name,
                have=have,
                first_release_year=first_year,
                release_group_mbid=resolved.release_group.release_group_mbid,
                release_group_title=resolved.release_group.album_title,
                tier=tier.value,
                reason=reason,
            ),
        )

    if file.originaldate and year_key(file.originaldate) != first_year:
        add(
            _ORIGINALDATE,
            file.originaldate,
            Tier.HIGH,
            f"the release group was first released in {first_year}",
        )
    date_year = year_key(file.date)
    if file.date and date_year is not None and date_year < first_year:
        add(
            _DATE,
            file.date,
            Tier.MEDIUM,
            f"no release can precede the release group's first release in {first_year}",
        )
    return rows


# --- lookups -------------------------------------------------------------------------


def _look_up(
    identities: list[tuple[str, str]],
    client: MBReleaseGroupCacheSource,
    *,
    release_limit: int,
) -> _Lookups:
    """Look up each album identity once, spending *release_limit* only on uncached ones.

    A cached answer costs no request, so it never counts toward the cap. That is what lets a
    re-run with the same cap reach the groups the previous run left behind.
    """
    lookups = _Lookups()
    network_budget = release_limit
    for artist, album in identities:
        cached = client.has_cached_album(artist, album)
        if not cached and network_budget == 0:
            lookups.remaining += 1
            continue
        if not cached:
            network_budget -= 1
        try:
            release_group = client.album_first_release(artist, album)
        except MusicBrainzError as exc:
            logger.warning("musicbrainz error for artist=%r album=%r: %s", artist, album, exc)
            lookups.error_items.append({"key": f"{artist} - {album}", "message": str(exc)})
            continue
        lookups.checked += 1
        first_year = None if release_group is None else year_key(release_group.original_date)
        if release_group is None or first_year is None:
            lookups.unknown += 1
            continue
        lookups.resolved[(artist, album)] = _Resolved(release_group, first_year)
    return lookups


# --- pure classifier -----------------------------------------------------------------


def _classify(
    files: list[_FileInput],
    client: MBReleaseGroupCacheSource,
    *,
    release_limit: int,
) -> YearDisagreementsReport:
    """Compare every file's years against its album identity's release group."""
    # Input: bucket by album identity, looking up only identities that carry a year to compare.
    by_identity: dict[tuple[str, str], list[_FileInput]] = defaultdict(list)
    identity_sizes: Counter[_GroupKey] = Counter()
    skipped = 0
    for f in files:
        if f.artist is None or f.album is None:
            skipped += 1
            continue
        identity_sizes[(f.folder, f.artist, f.album)] += 1
        if f.originaldate or f.date:
            by_identity[(f.artist, f.album)].append(f)

    # Process: one lookup per identity, then each of its files' years.
    lookups = _look_up(list(by_identity), client, release_limit=release_limit)
    rows: list[YearDisagreementRow] = []
    context_rows: list[YearDisagreementRow] = []
    for (artist, album), resolved in lookups.resolved.items():
        for f in by_identity[(artist, album)]:
            found = _compare_one(f, artist, album, resolved)
            if is_non_album_folder(f.folder):
                context_rows.extend(replace(r, reason=_REASON_NON_ALBUM) for r in found)
            else:
                rows.extend(found)

    # Output: the whole-library counts, which no later narrowing changes.
    tiers = tiers_by_file(rows)
    context_files = len({r.file_id for r in context_rows})
    return YearDisagreementsReport(
        rows=ordered(rows),
        total_files=len(files),
        flagged=sum(tiers.values()),
        flagged_fields=len(rows),
        high=tiers.get(Tier.HIGH.value, 0),
        medium=tiers.get(Tier.MEDIUM.value, 0),
        low=tiers.get(Tier.LOW.value, 0),
        release_groups_checked=lookups.checked,
        release_groups_remaining=lookups.remaining,
        more=lookups.remaining > 0,
        unknown_release_groups=lookups.unknown,
        skipped_no_identity=skipped,
        errors=len(lookups.error_items),
        summary=_summarize(
            rows=rows,
            tiers=tiers,
            context=context_files,
            lookups=lookups,
        ),
        folder_context=context_files,
        folder_context_rows=ordered(context_rows),
        error_items=lookups.error_items,
        groups=_build_groups(rows, context_rows, lookups, identity_sizes),
    )


def _group_key(item: YearDisagreementRow | YearDisagreementGroup) -> _GroupKey:
    """Return the grouped-view line *item* belongs to."""
    return item.group_key


def _build_groups(
    rows: list[YearDisagreementRow],
    context_rows: list[YearDisagreementRow],
    lookups: _Lookups,
    identity_sizes: Counter[_GroupKey],
) -> list[YearDisagreementGroup]:
    """Fold the rows into one line per album identity per folder, sorted by that key."""
    flagged_by_key = group_by_key(rows, _group_key)
    context_by_key = group_by_key(context_rows, _group_key)
    groups: list[YearDisagreementGroup] = []
    for key in sorted(flagged_by_key.keys() | context_by_key.keys()):
        folder, artist, album = key
        resolved = lookups.resolved[(artist, album)]
        base = YearDisagreementGroup(
            folder=folder,
            artist=artist,
            album=album,
            first_release_year=resolved.first_release_year,
            release_group_mbid=resolved.release_group.release_group_mbid,
            release_group_title=resolved.release_group.album_title,
            file_count=identity_sizes[key],
            flagged=0,
            folder_context=len({r.file_id for r in context_by_key.get(key, [])}),
            tiers={},
            file_ids=[],
            fields={},
        )
        groups.append(_refold_group(base, flagged_by_key.get(key, [])))
    return groups


def _refold_group(
    group: YearDisagreementGroup,
    rows: list[YearDisagreementRow],
) -> YearDisagreementGroup:
    """Return *group* with its flagged counts describing exactly *rows*, its flagged rows."""
    tiers = tiers_by_file(rows)
    return replace(
        group,
        flagged=sum(tiers.values()),
        tiers=dict(tiers),
        file_ids=sorted({r.file_id for r in rows}),
        fields=dict(Counter(r.field for r in rows)),
    )


def _summarize(
    *,
    rows: list[YearDisagreementRow],
    tiers: Counter[str],
    context: int,
    lookups: _Lookups,
) -> str:
    """Build a short, plain human summary of the run."""
    checked = lookups.checked
    if not rows:
        head = (
            f"Every checked year agrees with its release group's first release "
            f"({checked} release group(s) checked)."
        )
    else:
        head = (
            f"{sum(tiers.values())} file(s) carry a year that contradicts their release "
            f"group's first release, across {len(rows)} field(s) over {checked} release "
            f"group(s): {tiers.get(Tier.HIGH.value, 0)} high, "
            f"{tiers.get(Tier.MEDIUM.value, 0)} medium."
        )
    if context:
        head += f" {context} more sit in non-album folders (review context)."
    if lookups.unknown:
        head += f" {lookups.unknown} album(s) have no usable MusicBrainz release group."
    if lookups.remaining:
        head += (
            f" {lookups.remaining} release group(s) not yet looked up. Re-run, or raise "
            f"release_limit, to reach them."
        )
    if lookups.error_items:
        head += f" {len(lookups.error_items)} lookup(s) errored. Re-run to retry."
    return head


# --- public entry --------------------------------------------------------------------


def _load_inputs(connection: sqlite3.Connection) -> list[_FileInput]:
    """Read every present file's lookup identity and years out of the snapshot mirror.

    The identity is :func:`tagmend.engine.axis.lookup_identity`, the one ``resolve_years`` looks
    up, so both share the lookup's cache rows.
    """
    inputs: list[_FileInput] = []
    for row in store.list_files(connection):
        if row.is_missing:
            continue
        tags = store.get_tags(connection, row.id)
        identity = axis.lookup_identity(tags)
        originaldate = axis.first_nonblank(tags.get(_ORIGINALDATE))
        date = axis.first_nonblank(tags.get(_DATE))
        inputs.append(
            _FileInput(
                file_id=row.id,
                folder=row.folder,
                filename=row.filename,
                artist=identity.artist,
                album=identity.album,
                originaldate=None if originaldate is None else originaldate.strip(),
                date=None if date is None else date.strip(),
            ),
        )
    return inputs


def detect_year_disagreements(  # noqa: PLR0913 - cohesive keyword-only view + injection params
    settings: Settings,
    *,
    tier: str | None = None,
    limit: int | None = None,
    group: bool = False,
    folder: str | None = None,
    release_limit: int | None = None,
    client: MBReleaseGroupCacheSource | None = None,
) -> YearDisagreementsReport:
    """Report files whose years contradict their release group's first release.

    Reads the snapshot, so run ``scan_library`` first. *folder*, *tier* and *limit* narrow the
    VIEW alone, so the counts still describe the whole library. *folder* names exactly one
    folder and wins over *group*. *release_limit* caps the release groups looked up over the
    network this call (default 200). A cached answer never counts toward it, and the groups not
    reached are reported via ``release_groups_remaining``/``more``. *folder* is compared as a
    path (:func:`tagmend.engine.path_keys.folder_arg_key`). *client* injects an
    :class:`tagmend.engine.musicbrainz.MBReleaseGroupCacheSource` for tests. Raises
    :class:`ValueError` for an unknown *tier*, a negative *release_limit* or *limit*, or a
    *folder* outside ``music_path``.
    """
    check_limit(release_limit, name="release_limit")
    check_limit(limit)
    validate_tier(tier)
    folder_key = None if folder is None else path_keys.folder_arg_key(settings, folder)
    effective_limit = _DEFAULT_RELEASE_LIMIT if release_limit is None else release_limit

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        files = _load_inputs(connection)
        with lookup_clients.injected_or_owned(
            client,
            lambda: MusicBrainzClient.from_settings(settings, connection),
        ) as source:
            report = _classify(files, source, release_limit=effective_limit)
    finally:
        connection.close()

    logger.info(
        "year disagreements: flagged=%s context=%s over %s release group(s), %s file(s)",
        report.flagged,
        report.folder_context,
        report.release_groups_checked,
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
        key=_group_key,
        tier=tier,
        folder_key=folder_key,
        limit=limit,
        group=group,
    )
