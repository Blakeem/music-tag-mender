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
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final

from tagmend.engine import db, path_keys, schema, store
from tagmend.engine.album_conflicts import group_key
from tagmend.engine.detector_core import (
    TIER_RANK,
    Tier,
    group_by_folder,
    parse_position,
    validate_tier,
)
from tagmend.engine.musicbrainz import MusicBrainzClient, MusicBrainzError
from tagmend.engine.validation import check_limit
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

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
_DEFAULT_RELEASE_LIMIT: Final = 200

_SLASH: Final = "/"

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
    """One folder's disagreements, compact enough to scan a whole library at a glance.

    ``flagged`` counts files, matching the headline count, so the groups sum to it. One file
    with two wrong fields is one, and ``flagged_fields`` counts the fields beside it.
    ``file_ids`` names the flagged files only, never a file that merely has a blank to fill.
    ``releases`` lists every release the folder's in-scope files name.
    """

    folder: str
    file_count: int
    flagged: int
    folder_context: int
    tiers: dict[str, int]
    file_ids: list[int]
    flagged_fields: int
    fills: int
    fields: dict[str, int]
    releases: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "folder": self.folder,
            "file_count": self.file_count,
            "flagged": self.flagged,
            "folder_context": self.folder_context,
            "tiers": self.tiers,
            "file_ids": self.file_ids,
            "flagged_fields": self.flagged_fields,
            "fills": self.fills,
            "fields": self.fields,
            "releases": [dict(r) for r in self.releases],
        }


@dataclass(frozen=True, slots=True)
class DisagreementsReport:
    """Immutable summary of one :func:`detect_disagreements` run, JSON-ready for the tool.

    ``flagged`` counts files with at least one contradiction and ``flagged_fields`` counts the
    contradicting fields. Each file sits in the tier of its most severe contradiction, so the
    tier counts sum to ``flagged``.
    """

    rows: list[DisagreementRow]
    total_files: int
    flagged: int
    flagged_fields: int
    high: int
    medium: int
    low: int
    fills: int
    releases_attempted: int
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
            "flagged_fields": self.flagged_fields,
            "high": self.high,
            "medium": self.medium,
            "low": self.low,
            "fills": self.fills,
            "fill_rows": [r.to_dict() for r in self.fill_rows],
            "releases_attempted": self.releases_attempted,
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
    """Return the comparison key for a free-text tag.

    Shares :func:`tagmend.engine.album_conflicts.group_key`, so the two detectors agree on
    what is cosmetic: casing, typographic character choice and whitespace runs. MusicBrainz
    writes typographic punctuation and taggers write ASCII, and no consumer distinguishes the
    two, so a curly apostrophe against a straight one is not a finding. Other punctuation
    stays significant: a colon against a hyphen is a real difference.
    """
    return group_key(value)


def _position(value: str | None) -> str:
    """Return the position part of an ``n`` / ``n/total`` tag value, without leading zeros.

    A non-decimal head such as the vinyl ``A1`` comes back verbatim, so a side designation
    still compares.
    """
    number = parse_position(value)
    if number is not None:
        return str(number)
    if not value:
        return ""
    return value.split(_SLASH, 1)[0].strip()


def _track_number_agrees(have: str, track: MBTrack) -> bool:
    """Return whether the file's track number names this track, in either spelling.

    A vinyl medium numbers its tracks by side (``A1``, ``B7``) while Picard writes the
    sequential position, so the two strings differ on every file of such a release without
    anything being wrong. Either spelling is a defensible reading, so either is accepted.
    """
    return _text_key(have) in {
        _text_key(_position(track.number)),
        _text_key(_position(str(track.position))),
    }


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

    rows.extend(_compare_track(file, release, track))
    return rows


def _compare_track(
    file: _FileInput,
    release: MBRelease,
    track: MBTrack,
) -> list[DisagreementRow]:
    """Return every track-level field on *file* that contradicts its matched *track*."""
    rows: list[DisagreementRow] = []

    def add(field_name: str, have: str, want: str, reason: str) -> None:
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
                tier=_tier_for(field_name).value,
                reason=reason,
            ),
        )

    have_number = _position(file.tracknumber)
    if have_number and not _track_number_agrees(have_number, track):
        add(
            "tracknumber",
            have_number,
            _position(track.number),
            f"the release says {track.number!r}",
        )
    elif not have_number and track.number:
        add("tracknumber", "", _position(track.number), "")

    for field_name, have_raw, want in (
        ("title", file.title, track.title),
        # The credit is per track, not per release: a guest track carries its own, and that
        # is the one the file should name.
        ("artist", file.artist, track.artist_credit),
        ("discnumber", _position(file.discnumber), _disc_expectation(release, track)),
    ):
        have = (have_raw or "").strip()
        if want and _text_key(have) != _text_key(want):
            add(field_name, have, want, f"the release says {want!r}")

    return rows


def _medium_of(release: MBRelease, track: MBTrack) -> int:
    """Return the 1-based disc position of the medium holding *track*, or 0 if unknown.

    Identity, not equality: :class:`MBTrack` is a frozen dataclass, so two equal-valued tracks
    on different media would otherwise resolve to whichever medium came first.
    """
    return next(
        (m.position for m in release.media if any(t is track for t in m.tracks)),
        0,
    )


def _disc_expectation(release: MBRelease, track: MBTrack) -> str:
    """Return the disc number this track should carry, or empty when there is nothing to say.

    A single-medium release says nothing: Picard routinely omits ``discnumber`` there, and
    proposing 1 on every such file would bury the report. A medium whose position did not
    parse says nothing either, because disc zero is not an answer.
    """
    single_medium = 1
    if len(release.media) <= single_medium:
        return ""
    position = _medium_of(release, track)
    return str(position) if position > 0 else ""


def _classify(
    files: list[_FileInput],
    client: MBReleaseSource,
    *,
    release_limit: int | None,
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
    cap = release_limit if release_limit is not None else len(order)
    to_check = order[:cap]

    # Process: one lookup per release, then every field of every file on it.
    rows: list[DisagreementRow] = []
    errors: list[dict[str, str]] = []
    unknown = 0
    unmatched = 0
    fetched = 0
    titles: dict[str, str] = {}
    for release_id in to_check:
        try:
            release = client.release_by_mbid(release_id)
        except MusicBrainzError as exc:
            logger.warning("musicbrainz release error for mbid=%r: %s", release_id, exc)
            errors.append({"release_id": release_id, "message": str(exc)})
            continue
        fetched += 1
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
    tiers = _tiers_by_file(contradictions)
    return DisagreementsReport(
        rows=_ordered(contradictions),
        total_files=len(files),
        flagged=sum(tiers.values()),
        flagged_fields=len(contradictions),
        high=tiers.get(Tier.HIGH.value, 0),
        medium=tiers.get(Tier.MEDIUM.value, 0),
        low=tiers.get(Tier.LOW.value, 0),
        fills=len(fills),
        releases_attempted=len(to_check),
        releases_checked=fetched,
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
            checked=fetched,
            remaining=len(order) - len(to_check),
            unknown=unknown,
            unmatched=unmatched,
            errors=len(errors),
        ),
        fill_rows=_ordered(fills),
        error_releases=errors,
        groups=_build_groups(rows, files, titles),
    )


def _ordered(rows: list[DisagreementRow]) -> list[DisagreementRow]:
    """Return *rows* most-severe first, then stably by location and field."""
    return sorted(rows, key=lambda r: (TIER_RANK[Tier(r.tier)], r.folder, r.filename, r.field))


def _tiers_by_file(contradictions: list[DisagreementRow]) -> Counter[str]:
    """Count files by their most severe contradiction, so the counts sum to the file count."""
    worst: dict[int, Tier] = {}
    for row in contradictions:
        tier = Tier(row.tier)
        current = worst.get(row.file_id)
        if current is None or TIER_RANK[tier] < TIER_RANK[current]:
            worst[row.file_id] = tier
    return Counter(tier.value for tier in worst.values())


def _releases_in(folder_files: list[_FileInput], titles: dict[str, str]) -> list[dict[str, object]]:
    """Return every release one folder's files name, with its title and file count."""
    counts = Counter(
        release_id for f in folder_files if (release_id := (f.release_id or "").strip())
    )
    return [
        {"release_id": release_id, "release_title": titles.get(release_id, ""), "file_count": n}
        for release_id, n in sorted(counts.items())
    ]


def _build_groups(
    rows: list[DisagreementRow],
    files: list[_FileInput],
    titles: dict[str, str],
) -> list[DisagreementGroup]:
    """Fold the rows into one line per folder, sorted by folder."""
    rows_by_folder = group_by_folder(rows)
    files_by_folder = group_by_folder(files)
    groups: list[DisagreementGroup] = []
    for folder in sorted(rows_by_folder):
        contradictions = [r for r in rows_by_folder[folder] if not r.is_fill]
        tiers = _tiers_by_file(contradictions)
        groups.append(
            DisagreementGroup(
                folder=folder,
                file_count=len(files_by_folder.get(folder, [])),
                flagged=sum(tiers.values()),
                folder_context=0,
                tiers=dict(tiers),
                file_ids=sorted({r.file_id for r in contradictions}),
                flagged_fields=len(contradictions),
                fills=len(rows_by_folder[folder]) - len(contradictions),
                fields=dict(Counter(r.field for r in contradictions)),
                releases=_releases_in(files_by_folder.get(folder, []), titles),
            ),
        )
    return groups


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
        head += f" {remaining} release(s) not yet checked. Raise release_limit to reach them."
    if errors:
        head += f" {errors} release lookup(s) errored and stay pending. Re-run to retry."
    return head


# --- view narrowing ------------------------------------------------------------------


def _narrow(
    report: DisagreementsReport,
    *,
    tier: str | None,
    folder_key: str | None,
    limit: int | None,
    group: bool,
) -> DisagreementsReport:
    """Return *report* with its rows filtered for display. The run counts never change.

    Groups ride only on the grouped view. A *folder_key* wins over *group*: that call returns
    the folder's flat rows and no groups, like every sibling detector.
    """
    rows = report.rows
    fill_rows = report.fill_rows
    if tier is not None:
        rows = [r for r in rows if r.tier == tier]
        fill_rows = [r for r in fill_rows if r.tier == tier]
    if folder_key is not None:
        rows = [r for r in rows if path_keys.path_key(r.folder) == folder_key]
        fill_rows = [r for r in fill_rows if path_keys.path_key(r.folder) == folder_key]
    if limit is not None:
        rows = rows[:limit]
        fill_rows = fill_rows[:limit]

    flat = not group or folder_key is not None
    groups = [] if flat else report.groups
    if limit is not None:
        groups = groups[:limit]
    return replace(
        report,
        rows=rows if flat else [],
        fill_rows=fill_rows if flat else [],
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
    path: Path | None = None,
    folder: str | None = None,
    file_ids: list[int] | None = None,
    release_limit: int | None = None,
    limit: int | None = None,
    group: bool = False,
    client: MBReleaseSource | None = None,
) -> DisagreementsReport:
    """Report files whose tags contradict the MusicBrainz release their album id names.

    Reads the snapshot, so run ``scan_library`` first.

    *path* and *file_ids* scope the RUN: the counts then describe only that scope, and no
    release outside it is fetched. *path* takes the folder itself and every folder under it.
    *folder* and *tier* narrow the VIEW alone, so the counts still describe the whole run.
    *folder* names exactly one folder and wins over *group*. *release_limit* caps the number of
    distinct releases fetched this call (default 200, about three minutes at MusicBrainz's
    requested one request per second) and the remainder is reported via
    ``releases_remaining``/``more``. *limit* caps the rows (or groups) returned without changing
    any count. *path* and *folder* are compared as paths
    (:func:`tagmend.engine.path_keys.folder_arg_key`). *client* injects an
    :class:`tagmend.engine.musicbrainz.MBReleaseSource` for tests. Raises :class:`ValueError`
    for a *folder* with neither *path* nor *file_ids*, an unknown *tier*, a negative
    *release_limit* or *limit*, or a *path* or *folder* outside ``music_path``.
    """
    if folder is not None and path is None and file_ids is None:
        message = (
            "detect_disagreements fetches from MusicBrainz, so scope the run with path=<folder> "
            "or file_ids. folder= only narrows a scoped run's view"
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
        if client is not None:
            report = _classify(files, client, release_limit=effective_limit)
        else:
            with MusicBrainzClient(
                settings.musicbrainz_user_agent,
                connection,
                rate_per_sec=settings.musicbrainz_rate_per_sec,
            ) as owned:
                report = _classify(files, owned, release_limit=effective_limit)
    finally:
        connection.close()

    logger.info(
        "disagreements: flagged=%s over %s release(s), %s file(s)",
        report.flagged,
        report.releases_checked,
        report.total_files,
    )
    return _narrow(report, tier=tier, folder_key=folder_key, limit=limit, group=group)
