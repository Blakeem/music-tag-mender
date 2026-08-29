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
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from tagmend.engine import db, schema, store
from tagmend.engine.musicbrainz import MusicBrainzClient, MusicBrainzError
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.musicbrainz import MBRelease, MBReleaseSource, MBTrack

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

# How many distinct releases one call fetches when the caller names no limit. At the one
# request per second MusicBrainz asks for, this is about three minutes of wall clock.
_DEFAULT_LIMIT: Final = 200

_SLASH: Final = "/"


class Tier(StrEnum):
    """How much the disagreement costs a listener."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_TIER_RANK: Final = {Tier.HIGH: 0, Tier.MEDIUM: 1, Tier.LOW: 2}
_TIERS: Final = frozenset(t.value for t in Tier)

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
    release_id: str | None = None
    release_track_id: str | None = None
    recording_id: str | None = None
    album: str | None = None
    albumartist: str | None = None
    artist: str | None = None
    title: str | None = None
    tracknumber: str | None = None
    discnumber: str | None = None
    date: str | None = None
    releasecountry: str | None = None
    musicbrainz_albumstatus: str | None = None


# --- public result types -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DisagreementRow:
    """One field on one file that contradicts the release the file names."""

    file_id: int
    folder: str
    filename: str
    release_id: str
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

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "file_id": self.file_id,
            "folder": self.folder,
            "filename": self.filename,
            "release_id": self.release_id,
            "release_title": self.release_title,
            "field": self.field,
            "have": self.have,
            "want": self.want,
            "tier": self.tier,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class DisagreementGroup:
    """One folder's disagreements, compact enough to scan a whole library at a glance."""

    folder: str
    release_id: str
    release_title: str
    file_count: int
    flagged: int
    fills: int
    fields: dict[str, int]
    tiers: dict[str, int]
    file_ids: list[int]

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "folder": self.folder,
            "release_id": self.release_id,
            "release_title": self.release_title,
            "file_count": self.file_count,
            "flagged": self.flagged,
            "fills": self.fills,
            "fields": self.fields,
            "tiers": self.tiers,
            "file_ids": self.file_ids,
        }


@dataclass(frozen=True, slots=True)
class DisagreementsReport:
    """Immutable summary of one :func:`detect_disagreements` run, JSON-ready for the tool."""

    rows: list[DisagreementRow]
    total_files: int
    flagged: int
    high: int
    medium: int
    low: int
    fills: int
    releases_checked: int
    releases_remaining: int
    more: bool
    skipped_no_release_id: int
    unknown_releases: int
    unmatched_tracks: int
    errors: int
    summary: str
    fill_rows: list[DisagreementRow] = field(default_factory=list)
    error_releases: list[dict[str, str]] = field(default_factory=list)
    groups: list[DisagreementGroup] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "rows": [r.to_dict() for r in self.rows],
            "total_files": self.total_files,
            "flagged": self.flagged,
            "high": self.high,
            "medium": self.medium,
            "low": self.low,
            "fills": self.fills,
            "fill_rows": [r.to_dict() for r in self.fill_rows],
            "releases_checked": self.releases_checked,
            "releases_remaining": self.releases_remaining,
            "more": self.more,
            "skipped_no_release_id": self.skipped_no_release_id,
            "unknown_releases": self.unknown_releases,
            "unmatched_tracks": self.unmatched_tracks,
            "errors": self.errors,
            "error_releases": [dict(e) for e in self.error_releases],
            "groups": [g.to_dict() for g in self.groups],
            "summary": self.summary,
        }


# --- comparison helpers --------------------------------------------------------------


def _text_key(value: str) -> str:
    """Return the comparison key for a free-text tag: casing and whitespace runs are cosmetic.

    Punctuation stays significant for the same reason it does in
    :mod:`tagmend.engine.album_conflicts`: it changes the grouping key downstream.
    """
    return " ".join(value.casefold().split())


def _position(value: str | None) -> str:
    """Return the position part of an ``n`` / ``n/total`` tag value, without leading zeros."""
    if not value:
        return ""
    head = value.split(_SLASH, 1)[0].strip()
    return str(int(head)) if head.isdigit() else head


def _date_agrees(have: str, want: str) -> bool:
    """Return whether two dates agree, allowing the tag to be more precise than MusicBrainz.

    MusicBrainz often carries a bare year where the tag carries the full date it came from,
    so ``1997-09-18`` against ``1997`` is agreement, not a defect.
    """
    if not want:
        return True
    longer, shorter = (have, want) if len(have) >= len(want) else (want, have)
    return longer.startswith(shorter)


def _match_track(file: _FileInput, release: MBRelease) -> MBTrack | None:
    """Return the release track this file names, or ``None`` when it names none.

    Position is deliberately not a fallback: a wrong track number is one of the defects this
    detector reports, so matching on it would hide exactly what it exists to find.
    """
    if file.release_track_id:
        found = release.track_by_release_track_mbid(file.release_track_id)
        if found is not None:
            return found
    if file.recording_id:
        return release.track_by_recording_mbid(file.recording_id)
    return None


def _tier_for(field_name: str) -> Tier:
    """Return the tier a disagreement on *field_name* carries."""
    return Tier.MEDIUM if field_name in _MEDIUM_FIELDS else Tier.LOW


# --- pure classifier -----------------------------------------------------------------


def _release_expectations(release: MBRelease) -> dict[str, str]:
    """Return the release-level values every file on this release should carry."""
    return {
        "album": release.title,
        "albumartist": release.artist_credit,
        "date": release.date,
        "releasecountry": release.country,
        "musicbrainz_albumstatus": release.status,
    }


def _compare_one(
    file: _FileInput,
    release: MBRelease,
    track: MBTrack | None,
) -> list[DisagreementRow]:
    """Return every field on *file* that contradicts *release* (and *track* when matched)."""
    rows: list[DisagreementRow] = []

    def add(field_name: str, have: str, want: str, tier: Tier, reason: str) -> None:
        rows.append(
            DisagreementRow(
                file_id=file.file_id,
                folder=file.folder,
                filename=file.filename,
                release_id=release.mbid,
                release_title=release.title,
                field=field_name,
                have=have,
                want=want,
                tier=tier.value,
                reason=reason,
            ),
        )

    if file.release_track_id and track is None:
        add(
            "musicbrainz_releasetrackid",
            file.release_track_id,
            "",
            Tier.HIGH,
            _REASON_UNMATCHED,
        )

    for field_name, want in _release_expectations(release).items():
        if not want:
            continue
        have = (getattr(file, field_name) or "").strip()
        agrees = (
            _date_agrees(have, want) if field_name == "date" else _text_key(have) == _text_key(want)
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

    for field_name, have_raw, want in (
        ("title", file.title, track.title),
        # The credit is per track, not per release: a guest track carries its own, and that
        # is the one the file should name.
        ("artist", file.artist, track.artist_credit),
        ("tracknumber", _position(file.tracknumber), _position(track.number)),
        ("discnumber", _position(file.discnumber), str(_medium_of(release, track))),
    ):
        have = (have_raw or "").strip()
        if want and _text_key(have) != _text_key(want):
            add(field_name, have, want, _tier_for(field_name), f"the release says {want!r}")

    return rows


def _medium_of(release: MBRelease, track: MBTrack) -> int:
    """Return the 1-based disc position of the medium holding *track*."""
    return next(
        (m.position for m in release.media if track in m.tracks),
        1,
    )


def _classify(
    files: list[_FileInput],
    client: MBReleaseSource,
    *,
    limit: int | None,
) -> DisagreementsReport:
    """Compare every in-scope file against the release it names, one lookup per release."""
    # Input: group by release so each is fetched at most once, in first-seen order.
    by_release: dict[str, list[_FileInput]] = defaultdict(list)
    skipped = 0
    for f in files:
        release_id = (f.release_id or "").strip()
        if not release_id:
            skipped += 1
            continue
        by_release[release_id].append(f)

    order = list(by_release)
    cap = limit if limit is not None else len(order)
    to_check = order[:cap]

    # Process: one lookup per release, then every field of every file on it.
    rows: list[DisagreementRow] = []
    errors: list[dict[str, str]] = []
    unknown = 0
    unmatched = 0
    titles: dict[str, str] = {}
    for release_id in to_check:
        try:
            release = client.release_by_mbid(release_id)
        except MusicBrainzError as exc:
            logger.warning("musicbrainz release error for mbid=%r: %s", release_id, exc)
            errors.append({"release_id": release_id, "message": str(exc)})
            continue
        if release is None:
            unknown += 1
            continue
        titles[release_id] = release.title
        for f in by_release[release_id]:
            track = _match_track(f, release)
            if track is None:
                unmatched += 1
            rows.extend(_compare_one(f, release, track))

    # Output: the contradictions and the blank fills are separate populations.
    contradictions = [r for r in rows if not r.is_fill]
    fills = [r for r in rows if r.is_fill]
    tiers = Counter(r.tier for r in contradictions)
    return DisagreementsReport(
        rows=_ordered(contradictions),
        total_files=len(files),
        flagged=len(contradictions),
        high=tiers.get(Tier.HIGH.value, 0),
        medium=tiers.get(Tier.MEDIUM.value, 0),
        low=tiers.get(Tier.LOW.value, 0),
        fills=len(fills),
        releases_checked=len(to_check),
        releases_remaining=len(order) - len(to_check),
        more=len(order) > len(to_check),
        skipped_no_release_id=skipped,
        unknown_releases=unknown,
        unmatched_tracks=unmatched,
        errors=len(errors),
        summary=_summarize(
            rows=contradictions,
            fills=len(fills),
            tiers=tiers,
            checked=len(to_check),
            remaining=len(order) - len(to_check),
            unknown=unknown,
            unmatched=unmatched,
            errors=len(errors),
        ),
        fill_rows=_ordered(fills),
        error_releases=errors,
        groups=_build_groups(rows, by_release, titles),
    )


def _ordered(rows: list[DisagreementRow]) -> list[DisagreementRow]:
    """Return *rows* most-severe first, then stably by location and field."""
    return sorted(rows, key=lambda r: (_TIER_RANK[Tier(r.tier)], r.folder, r.filename, r.field))


def _build_groups(
    rows: list[DisagreementRow],
    by_release: dict[str, list[_FileInput]],
    titles: dict[str, str],
) -> list[DisagreementGroup]:
    """Fold the rows into one line per (folder, release) pair."""
    keyed: dict[tuple[str, str], list[DisagreementRow]] = defaultdict(list)
    for row in rows:
        keyed[(row.folder, row.release_id)].append(row)

    counts: dict[tuple[str, str], int] = defaultdict(int)
    for release_id, members in by_release.items():
        for f in members:
            counts[(f.folder, release_id)] += 1

    return [
        DisagreementGroup(
            folder=folder,
            release_id=release_id,
            release_title=titles.get(release_id, ""),
            file_count=counts[(folder, release_id)],
            flagged=len({r.file_id for r in group_rows if not r.is_fill}),
            fills=len([r for r in group_rows if r.is_fill]),
            fields=dict(Counter(r.field for r in group_rows if not r.is_fill)),
            tiers=dict(Counter(r.tier for r in group_rows if not r.is_fill)),
            file_ids=sorted({r.file_id for r in group_rows}),
        )
        for (folder, release_id), group_rows in keyed.items()
    ]


def _summarize(  # noqa: PLR0913 - one keyword per reported count, cohesive by design
    *,
    rows: list[DisagreementRow],
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
            f"{len({r.file_id for r in rows})} file(s) disagree with the release they name "
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
        head += f" {remaining} release(s) not yet checked — raise limit to reach them."
    if errors:
        head += f" {errors} release lookup(s) errored and stay pending — re-run to retry."
    return head


# --- view narrowing ------------------------------------------------------------------


def _narrow(
    report: DisagreementsReport,
    *,
    tier: str | None,
    folder: str | None,
    group: bool,
) -> DisagreementsReport:
    """Return *report* with its rows filtered for display; the run counts never change."""
    rows = report.rows
    fill_rows = report.fill_rows
    if tier is not None:
        rows = [r for r in rows if r.tier == tier]
        fill_rows = [r for r in fill_rows if r.tier == tier]
    if folder is not None:
        rows = [r for r in rows if r.folder == folder]
        fill_rows = [r for r in fill_rows if r.folder == folder]

    groups = report.groups
    if folder is not None:
        groups = [g for g in groups if g.folder == folder]

    return DisagreementsReport(
        rows=[] if group else rows,
        total_files=report.total_files,
        flagged=report.flagged,
        high=report.high,
        medium=report.medium,
        low=report.low,
        fills=report.fills,
        releases_checked=report.releases_checked,
        releases_remaining=report.releases_remaining,
        more=report.more,
        skipped_no_release_id=report.skipped_no_release_id,
        unknown_releases=report.unknown_releases,
        unmatched_tracks=report.unmatched_tracks,
        errors=report.errors,
        summary=report.summary,
        fill_rows=[] if group else fill_rows,
        error_releases=report.error_releases,
        groups=groups,
    )


# --- public entry --------------------------------------------------------------------


def _load_inputs(
    connection: sqlite3.Connection,
    scoped_ids: list[int] | None,
) -> list[_FileInput]:
    """Read every in-scope present file's detect fields out of the snapshot mirror."""
    tag_values = store.load_tag_values(connection, _DETECT_FIELDS)
    wanted = None if scoped_ids is None else set(scoped_ids)
    inputs: list[_FileInput] = []
    for row in store.list_files(connection):
        if row.is_missing or (wanted is not None and row.id not in wanted):
            continue
        values = tag_values.get(row.id, {})
        inputs.append(
            _FileInput(
                file_id=row.id,
                folder=row.folder,
                filename=row.filename,
                release_id=values.get("musicbrainz_albumid"),
                release_track_id=values.get("musicbrainz_releasetrackid"),
                recording_id=values.get("musicbrainz_trackid"),
                album=values.get("album"),
                albumartist=values.get("albumartist"),
                artist=values.get("artist"),
                title=values.get("title"),
                tracknumber=values.get("tracknumber"),
                discnumber=values.get("discnumber"),
                date=values.get("date"),
                releasecountry=values.get("releasecountry"),
                musicbrainz_albumstatus=values.get("musicbrainz_albumstatus"),
            ),
        )
    return inputs


def detect_disagreements(  # noqa: PLR0913 - cohesive keyword-only scope + injection params
    settings: Settings,
    *,
    tier: str | None = None,
    folder: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
    group: bool = False,
    client: MBReleaseSource | None = None,
) -> DisagreementsReport:
    """Report files whose tags contradict the MusicBrainz release their album id names.

    Reads the snapshot, so run ``scan_library`` first. *limit* caps the number of distinct
    releases fetched this call (default 200, about three minutes at MusicBrainz's requested
    one request per second) and the remainder is reported via ``releases_remaining``/``more``.
    *client* injects an :class:`tagmend.engine.musicbrainz.MBReleaseSource` for tests. Raises
    :class:`ValueError` for an unknown *tier*.
    """
    if tier is not None and tier not in _TIERS:
        message = f"unknown tier {tier!r}; expected one of {sorted(_TIERS)}"
        raise ValueError(message)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        scoped = None if file_ids is None else store.files_in_scope(connection, file_ids=file_ids)
        files = _load_inputs(connection, scoped)
        if folder is not None:
            files = [f for f in files if f.folder == folder]

        effective_limit = _DEFAULT_LIMIT if limit is None else limit
        if client is not None:
            report = _classify(files, client, limit=effective_limit)
        else:
            with MusicBrainzClient(
                settings.musicbrainz_user_agent,
                connection,
                rate_per_sec=settings.musicbrainz_rate_per_sec,
            ) as owned:
                report = _classify(files, owned, limit=effective_limit)
    finally:
        connection.close()

    logger.info(
        "disagreements: flagged=%s over %s release(s), %s file(s)",
        report.flagged,
        report.releases_checked,
        report.total_files,
    )
    return _narrow(report, tier=tier, folder=folder, group=group)
