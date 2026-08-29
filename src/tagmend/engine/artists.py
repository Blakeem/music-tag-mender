"""Artist-name normalization: select → Last.fm getCorrection → cascade-stage (M4 phase 1).

The artist-side mirror of :mod:`tagmend.engine.genres`. It ties the read path, the cached
Last.fm correction client, and the revertible staging engine together: for each distinct
``artist``/``albumartist`` value in scope it looks up the canonical name via
:meth:`tagmend.engine.lastfm.LastfmClient.artist_correction`, and where the canonical form
differs it cascade-stages the corrected name across every file carrying that value
(rewriting ``artist`` and/or ``albumartist``, exact-match only) plus the correction's
``musicbrainz_artistid`` — through the existing ``origin='auto'`` commit engine.

Design notes (the spec):

* **No accidental deletion (P0):** the staged target is built from the file's own managed
  subset (:func:`tagmend.engine.versioning.managed_subset`) with only ``artist`` /
  ``albumartist`` / ``musicbrainz_artistid`` replaced, so the commit's delete-on-absent
  write can never drop ``genre`` or any other managed tag.
* **Per-file accumulation:** a file whose ``artist`` and ``albumartist`` both need
  correction is staged once with both fields set (not two passes clobbering each other).
* **Guards (skip + report, never rewrite):** the ``feat``/``ft``/``featuring`` family,
  compilation sentinels (``various artists``/``various``/``va``), and empty values are
  dropped from the distinct-value scan. The multi-value guard is separate and runs per
  file: any file whose ``artist`` or ``albumartist`` list has ``len > 1`` is skipped.
* **Correction gate (post-lookup, held not staged):** Last.fm casing is not trustworthy, so
  a case-only difference is already canonical. A canonical name contained in the source is
  a collapsed multi-artist credit (``Skrillex & The Doors`` → ``Skrillex``) and is held
  regardless of MBID — the ``&``/``with``/``vs`` family the pre-lookup ``feat`` guard cannot
  see. A correction MusicBrainz does not corroborate (no MBID) is held for review. Held
  values are reported, never written.
* **"Done" is derived**, never stored — re-running after commit is a no-op because an
  already-canonical value yields no change. No new table; getCorrection results live in
  the generic ``lastfm_cache``.

Like the rest of the conn-owning layer, the public function here owns its connection and
commits; the building blocks in :mod:`tagmend.engine.store` never commit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from tagmend.engine import axis, db, schema, staging, store, versioning
from tagmend.engine.lastfm import LastfmClient, LastfmError
from tagmend.engine.musicbrainz import MusicBrainzClient, MusicBrainzError
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from tagmend.config import Settings
    from tagmend.engine.lastfm import CorrectionSource
    from tagmend.engine.musicbrainz import MBArtist, MBArtistSource

logger = get_logger(__name__)

# The two name fields this normalizer rewrites (exact-match only) — the artist axis's
# field set, the single source of truth shared with the status derivation.
# ``musicbrainz_artistid`` rides along on a changed file but is never the trigger for a
# change on its own.
_NAME_FIELDS: Final = axis.ARTIST_AXIS.fields

# Compilation sentinels (fold-cased): never a real artist to correct.
_SENTINELS: Final = frozenset({"various artists", "various", "va"})

# The feat./ft./featuring family — a word-boundary, case-insensitive match anywhere in the
# value means it is a multi-artist credit we must not rewrite as a single canonical name.
_FEAT_RE: Final = re.compile(r"\b(?:feat|ft|featuring)\b\.?", re.IGNORECASE)

# The MusicBrainz special-purpose bracket convention (``[unknown]``, ``[no artist]``,
# ``[anonymous]``, …): a getCorrection can hand back one of these placeholders as the
# "canonical" name for junk album-artist labels. It is not a real artist, so a correction
# to a placeholder is treated exactly like no correction at all. Case-irrelevant by
# construction — the payload is punctuation-wrapped, not a cased word.
_MB_PLACEHOLDER_RE: Final = re.compile(r"^\[.*\]$")

# The two states ``set_artist_status`` is allowed to drive: ``manual`` writes a sticky
# exclusion row; ``pending`` deletes it (re-queue). There is no engine-owned ``no_match``
# on this axis.
_USER_STATUSES: Final = frozenset({"manual", "pending"})

# Each name field's own MusicBrainz id field. The pairing is positional in the file, not
# global: ``artist`` is the per-track credit and ``albumartist`` the per-release one, and the
# two routinely name different artists. Writing one field's id onto the other rebinds a file
# to an artist nobody asked for.
_ID_FIELDS: Final[Mapping[str, str]] = {
    "artist": "musicbrainz_artistid",
    "albumartist": "musicbrainz_albumartistid",
}

# Characters that separate the same name into different spellings. MusicBrainz writes real
# typography (``Static\u2010X`` carries U+2010, not a hyphen-minus) while taggers and
# filesystems substitute ASCII, and a word break is written as a dash by one source and a
# space by another (``Mindless Self\u2010Indulgence`` against MusicBrainz's spaced
# spelling). Folding decides SAMENESS only, and only against names MusicBrainz records for
# the id the file already carries, so it can never merge two different artists. The staged
# value is always MusicBrainz's own spelling.
_NAME_FOLD_MAP: Final[Mapping[str, str]] = {
    "-": " ",
    "\u2010": " ",
    "\u2011": " ",
    "\u2012": " ",
    "\u2013": " ",
    "\u2014": " ",
    "\u2212": " ",
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u00a0": " ",
    "\u2009": " ",
    "\u202f": " ",
}

# The three ways a value can reach `tally.corrections`, reported per mapping so a reviewer
# can tell an id-backed MusicBrainz fact from a Last.fm suggestion.
_SOURCE_MB: Final = "musicbrainz"
_SOURCE_MB_ALIAS: Final = "musicbrainz_alias"
_SOURCE_LASTFM: Final = "lastfm"


def _utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string (the engine's timestamp form)."""
    return datetime.now(UTC).isoformat()


def _is_feat(value: str) -> bool:
    """Return whether *value* contains a ``feat``/``ft``/``featuring`` credit marker."""
    return _FEAT_RE.search(value) is not None


def _is_sentinel(value: str) -> bool:
    """Return whether *value* is a compilation sentinel (``various artists`` family)."""
    return value.strip().casefold() in _SENTINELS


def _is_guarded(value: str) -> bool:
    """Return whether *value* must be skipped outright (empty, feat., or a sentinel)."""
    return not value.strip() or _is_feat(value) or _is_sentinel(value)


def _is_placeholder(name: str) -> bool:
    """Return whether *name* is a MusicBrainz special-purpose placeholder (``[unknown]`` …)."""
    return _MB_PLACEHOLDER_RE.match(name.strip()) is not None


def _is_case_only(value: str, canonical: str) -> bool:
    """Return whether *canonical* differs from *value* by casing alone (or not at all).

    Diacritics are deliberately not folded away: ``Antonio`` → ``Antônio`` is a spelling
    fix, not a casing opinion, and must stay eligible for staging.
    """
    return value.casefold() == canonical.casefold()


def _name_fold(value: str) -> str:
    """Fold *value* to a key ignoring casing, typography and dash-vs-space word breaks.

    Used only to decide whether two spellings are the same name. Never used as a value.
    """
    folded = "".join(_NAME_FOLD_MAP.get(ch, ch) for ch in value)
    return " ".join(folded.casefold().split())


def _shrinks_credit(value: str, canonical: str) -> bool:
    """Return whether *canonical* is a strict fold-case substring of *value*.

    That shape is a multi-artist credit collapsed onto one member, which no MBID can
    justify. Equality is excluded so an already-canonical value is not read as a shrink.
    """
    folded_value = value.casefold()
    folded_canonical = canonical.casefold()
    return folded_canonical != folded_value and folded_canonical in folded_value


# --- result types --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolveArtistsResult:
    """Immutable summary of one :func:`resolve_artists` call, JSON-ready for the MCP tool."""

    processed: int
    staged_files: int
    corrected_values: int
    skipped_multi_artist: int
    skipped_sentinel: int
    skipped_manual: int
    no_correction: int
    already_canonical: int
    shrinks_credit: int
    needs_review: int
    name_id_disagreement: int
    errors: int
    pending_remaining: int
    more: bool
    mappings: list[dict[str, str | None]]
    multi_artist_files: list[int]
    manual_files: list[int]
    no_correction_values: list[str]
    already_canonical_values: list[str]
    shrinks_credit_values: list[dict[str, str]]
    needs_review_values: list[dict[str, str]]
    name_id_disagreement_values: list[dict[str, str]]
    error_values: list[dict[str, str]]
    summary: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "processed": self.processed,
            "staged_files": self.staged_files,
            "corrected_values": self.corrected_values,
            "skipped_multi_artist": self.skipped_multi_artist,
            "skipped_sentinel": self.skipped_sentinel,
            "skipped_manual": self.skipped_manual,
            "no_correction": self.no_correction,
            "already_canonical": self.already_canonical,
            "shrinks_credit": self.shrinks_credit,
            "needs_review": self.needs_review,
            "name_id_disagreement": self.name_id_disagreement,
            "errors": self.errors,
            "pending_remaining": self.pending_remaining,
            "more": self.more,
            "mappings": [dict(m) for m in self.mappings],
            "multi_artist_files": list(self.multi_artist_files),
            "manual_files": list(self.manual_files),
            "no_correction_values": list(self.no_correction_values),
            "already_canonical_values": list(self.already_canonical_values),
            "shrinks_credit_values": [dict(h) for h in self.shrinks_credit_values],
            "needs_review_values": [dict(h) for h in self.needs_review_values],
            "name_id_disagreement_values": [dict(d) for d in self.name_id_disagreement_values],
            "error_values": [dict(e) for e in self.error_values],
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class _Resolution:
    """One accepted value -> canonical-name change, tagged with the tier that decided it."""

    name: str
    mbid: str | None
    source: str


@dataclass(slots=True)
class _Tally:
    """Mutable accumulator for one ``resolve_artists`` run, frozen into the result at end."""

    staged_files: int = 0
    skipped_multi_artist: int = 0
    skipped_sentinel: int = 0
    skipped_manual: int = 0
    multi_artist_files: list[int] = field(default_factory=list)
    manual_files: list[int] = field(default_factory=list)
    no_correction_values: list[str] = field(default_factory=list)
    already_canonical_values: list[str] = field(default_factory=list)
    shrinks_credit_values: list[dict[str, str]] = field(default_factory=list)
    needs_review_values: list[dict[str, str]] = field(default_factory=list)
    name_id_disagreement_values: list[dict[str, str]] = field(default_factory=list)
    error_values: list[dict[str, str]] = field(default_factory=list)
    # value -> resolution: only the substantive ones a tier's gate accepts.
    corrections: dict[str, _Resolution] = field(default_factory=dict)


# --- public entry --------------------------------------------------------------------


def resolve_artists(  # noqa: PLR0913 - cohesive keyword-only scope + injection params
    settings: Settings,
    *,
    artist: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    client: CorrectionSource | None = None,
    mb_client: MBArtistSource | None = None,
) -> ResolveArtistsResult:
    """Normalize artist names: look up canonical forms and cascade-stage the changes.

    Gathers the distinct ``artist`` + ``albumartist`` values in scope, drops the guarded
    ones (empty / ``feat.`` / compilation sentinels), looks up each remaining value's
    canonical name via *client* (cached/paced), and builds a ``value → correction`` map of
    those that actually change. Then per file in scope it skips + reports any file whose
    ``artist``/``albumartist`` is multi-value, and otherwise stages the corrected name(s)
    plus the correction's MBID as an ``origin='auto'`` change (only ``artist`` /
    ``albumartist`` / ``musicbrainz_artistid`` touched — every other managed tag preserved).

    *limit* caps the number of distinct values processed per call and the remainder is
    reported via ``pending_remaining`` / ``more``. It is a cap, not a cursor: a value that
    needs no change leaves no trace, so an identical repeat call re-processes the same
    values. Raise *limit*, or narrow with *artist* / *file_ids*, to reach the rest. A
    transient Last.fm error leaves that value pending (not cached) and is reported, never
    aborting.

    *dry_run* returns the proposed ``value → canonical`` mappings and the would-stage file
    count, stages nothing, and works from cache (no precondition). A non-dry-run raises
    :class:`ValueError` if anything is already staged ("commit or unstage pending changes
    first"). *client* lets callers inject a :class:`tagmend.engine.lastfm.CorrectionSource`
    (a fake in tests); when ``None`` a real :class:`LastfmClient` is built and requires
    ``settings.lastfm_api_key``. Owns its connection; ``stage_tags`` opens its own.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)

        if not dry_run and store.any_staged(connection):
            message = "commit or unstage pending changes first"
            raise ValueError(message)

        scoped_ids = store.files_in_scope(
            connection,
            artist=artist,
            file_ids=file_ids,
        )

        tally = _Tally()
        candidate_ids = _drop_manual_excluded(connection, scoped_ids, tally)
        values = _distinct_values(connection, candidate_ids, tally)

        to_process = values[: limit if limit is not None else len(values)]
        pending_remaining = len(values) - len(to_process)

        if to_process:
            pairing = _value_mbids(connection, candidate_ids)
            unresolved = _resolve_by_mbid(
                settings,
                connection,
                to_process,
                pairing,
                mb_client,
                tally,
            )
            if unresolved:
                _resolve_values(settings, connection, unresolved, client, tally)

        _stage_files(settings, connection, candidate_ids, tally, dry_run=dry_run)
    finally:
        connection.close()

    return _build_result(
        tally,
        processed=len(to_process),
        pending_remaining=pending_remaining,
    )


# --- manual-exclusion filter ---------------------------------------------------------


def _drop_manual_excluded(
    conn: sqlite3.Connection,
    candidate_ids: list[int],
    tally: _Tally,
) -> list[int]:
    """Drop sticky ``manual`` files from scope, recording them in *tally*.

    A ``manual`` file is ALWAYS skipped (no staleness re-check): its values are neither
    looked up nor staged. The mirror of the user-facing
    :func:`tagmend.engine.store.derived_artist_status` ``manual`` state.
    """
    kept: list[int] = []
    for fid in candidate_ids:
        decision = store.get_artist_status(conn, fid)
        if decision is not None and _decision_blocks(decision):
            tally.skipped_manual += 1
            tally.manual_files.append(fid)
            continue
        kept.append(fid)
    return kept


def _decision_blocks(decision: store.ArtistStatusRow) -> bool:
    """Whether a stored artist decision still blocks processing (sticky ``manual``).

    The shared sticky rule lives on the axis, so this skip path and the user-facing
    :func:`tagmend.engine.store.derived_artist_status` can never drift. The artist axis has
    no staleness re-check, so the current identity is irrelevant.
    """
    return axis.ARTIST_AXIS.decision_blocks(
        axis.StatusRow(
            status=decision.status,
            source_primary=decision.source_artist,
            source_secondary=decision.source_albumartist,
        ),
        axis.Identity(primary=None, secondary=None),
    )


# --- distinct-value scan -------------------------------------------------------------


def _distinct_values(
    conn: sqlite3.Connection,
    candidate_ids: list[int],
    tally: _Tally,
) -> list[str]:
    """Return the distinct, non-guarded ``artist`` + ``albumartist`` values in scope.

    Mutates *tally* with the sentinel/guard skip count. Order is stable (first-seen) so a
    ``limit`` chunks deterministically.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for fid in candidate_ids:
        tags = store.get_tags(conn, fid)
        for field_name in _NAME_FIELDS:
            for value in tags.get(field_name, []):
                if value in seen:
                    continue
                seen.add(value)
                if _is_guarded(value):
                    tally.skipped_sentinel += 1
                    continue
                ordered.append(value)
    return ordered


# --- value -> MBID pairing -----------------------------------------------------------


def _value_mbids(
    conn: sqlite3.Connection,
    candidate_ids: list[int],
) -> dict[str, set[str]]:
    """Map each name value in scope to the set of MusicBrainz ids the library pairs with it.

    The pairing is per field (``artist`` with ``musicbrainz_artistid``, ``albumartist`` with
    ``musicbrainz_albumartistid``) and only from single-valued fields, where the value and the
    id unambiguously describe each other. An empty set means the value carries no id anywhere;
    a set larger than one means the library disagrees with itself about who this is.
    """
    pairing: dict[str, set[str]] = {}
    for fid in candidate_ids:
        tags = store.get_tags(conn, fid)
        for field_name, id_field in _ID_FIELDS.items():
            names = tags.get(field_name, [])
            if len(names) != 1:
                continue
            ids = tags.get(id_field, [])
            bucket = pairing.setdefault(names[0], set())
            if len(ids) == 1 and ids[0].strip():
                bucket.add(ids[0].strip())
    return pairing


# --- the MusicBrainz name tier -------------------------------------------------------


def _resolve_by_mbid(  # noqa: PLR0913 - cohesive scope + injection params, mirrors _resolve_values
    settings: Settings,
    conn: sqlite3.Connection,
    values: list[str],
    pairing: dict[str, set[str]],
    client: MBArtistSource | None,
    tally: _Tally,
) -> list[str]:
    """Settle every value whose files already carry an MBID; return the rest for Last.fm.

    The MBID is the file's own claim about who the artist is, so this tier is a direct
    lookup with no candidate ranking and no ambiguity. A value MusicBrainz cannot settle
    (no id, an id it does not know) is handed back for the Last.fm tier.
    """
    with_ids = [value for value in values if pairing.get(value)]
    if not with_ids:
        return values

    ambiguous = [value for value in with_ids if len(pairing[value]) > 1]
    for value in ambiguous:
        tally.name_id_disagreement_values.append(
            {
                "from": value,
                "to": "",
                "mbid": ", ".join(sorted(pairing[value])),
                "reason": "the library pairs this name with more than one MusicBrainz id",
            },
        )

    settled = set(ambiguous)
    lookups = [value for value in with_ids if len(pairing[value]) == 1]
    if lookups:
        if client is not None:
            settled |= _lookup_each(lookups, pairing, client, tally)
        else:
            with MusicBrainzClient(
                settings.musicbrainz_user_agent,
                conn,
                rate_per_sec=settings.musicbrainz_rate_per_sec,
            ) as owned:
                settled |= _lookup_each(lookups, pairing, owned, tally)

    return [value for value in values if value not in settled]


def _lookup_each(
    values: list[str],
    pairing: dict[str, set[str]],
    client: MBArtistSource,
    tally: _Tally,
) -> set[str]:
    """Look each value's single MBID up and bucket it; return the values this tier settled."""
    settled: set[str] = set()
    for value in values:
        mbid = next(iter(pairing[value]))
        try:
            artist = client.artist_by_mbid(mbid)
        except MusicBrainzError as exc:
            logger.warning("musicbrainz artist error for mbid=%r: %s", mbid, exc)
            tally.error_values.append({"value": value, "message": str(exc)})
            settled.add(value)
            continue
        if artist is None:
            continue  # MusicBrainz does not know this id — let Last.fm try the name.
        _classify_against_mb(value, artist, tally)
        settled.add(value)
    return settled


def _classify_against_mb(value: str, artist: MBArtist, tally: _Tally) -> None:
    """Route one value to exactly one bucket against the artist its own MBID names.

    The ladder, in order. Exact canonical is already right. A difference in casing or
    typographic glyphs alone is the same name spelled differently, and MusicBrainz's casing
    IS trusted (unlike Last.fm's), so it is staged. A value MusicBrainz registers as an
    alias is a name for this artist, so merging it onto the canonical name is what makes one
    artist appear once. Anything else is either a credit that collapses onto one member, or
    a name MusicBrainz has never heard of for this id — both reported, never staged.
    """
    if value == artist.name:
        tally.already_canonical_values.append(value)
        return

    folded = _name_fold(value)
    if folded == _name_fold(artist.name):
        tally.corrections[value] = _Resolution(artist.name, artist.mbid, _SOURCE_MB)
        return

    if any(folded == _name_fold(alias) for alias in artist.aliases):
        tally.corrections[value] = _Resolution(artist.name, artist.mbid, _SOURCE_MB_ALIAS)
        return

    if _shrinks_credit(value, artist.name):
        tally.shrinks_credit_values.append({"from": value, "to": artist.name})
        return

    tally.name_id_disagreement_values.append(
        {
            "from": value,
            "to": artist.name,
            "mbid": artist.mbid,
            "reason": "no name MusicBrainz records for this id",
        },
    )


# --- the Last.fm correction tier -----------------------------------------------------


def _resolve_values(
    settings: Settings,
    conn: sqlite3.Connection,
    values: list[str],
    client: CorrectionSource | None,
    tally: _Tally,
) -> None:
    """Look up each value's correction via *client* (built if None); fill ``tally.corrections``."""
    if client is not None:
        for value in values:
            _resolve_one_value(value, client, tally)
        return

    if not settings.lastfm_api_key:
        message = "no Last.fm API key configured — run `tagmend config-set lastfm_api_key <key>`"
        raise ValueError(message)
    with LastfmClient(
        settings.lastfm_api_key,
        conn,
        rate_per_sec=settings.lastfm_rate_per_sec,
    ) as owned_client:
        for value in values:
            _resolve_one_value(value, owned_client, tally)


def _resolve_one_value(
    value: str,
    client: CorrectionSource,
    tally: _Tally,
) -> None:
    """Look up one value's correction and route it to exactly one outcome bucket.

    A transient :class:`LastfmError` leaves the value pending (not cached) and is reported,
    never aborting the run. A correction to a MusicBrainz special-purpose placeholder
    (``[unknown]``, ``[no artist]``, …) is not a real name and is treated exactly like no
    correction. The surviving corrections pass the gate in order — case-only (already
    canonical), credit shrink (held), no MBID (held) — so a name is only staged when it is a
    substantive change MusicBrainz corroborates. Every value lands in one bucket, so the
    buckets sum to ``processed``.
    """
    try:
        correction = client.artist_correction(value)
    except LastfmError as exc:
        logger.warning("last.fm correction error for value=%r: %s", value, exc)
        tally.error_values.append({"value": value, "message": str(exc)})
        return

    if correction is None or _is_placeholder(correction.name):
        tally.no_correction_values.append(value)
        return

    canonical = correction.name
    if _is_case_only(value, canonical):
        tally.already_canonical_values.append(value)
        return

    if _shrinks_credit(value, canonical):
        tally.shrinks_credit_values.append({"from": value, "to": canonical})
        return

    if not correction.mbid:
        tally.needs_review_values.append({"from": value, "to": canonical})
        return

    tally.corrections[value] = _Resolution(canonical, correction.mbid, _SOURCE_LASTFM)


# --- staging -------------------------------------------------------------------------


def _stage_files(
    settings: Settings,
    conn: sqlite3.Connection,
    candidate_ids: list[int],
    tally: _Tally,
    *,
    dry_run: bool,
) -> None:
    """Per file in scope: skip multi-value files, else stage the accumulated name change(s)."""
    for fid in candidate_ids:
        tags = store.get_tags(conn, fid)

        if _is_multi_value(tags):
            tally.skipped_multi_artist += 1
            tally.multi_artist_files.append(fid)
            continue

        target = _build_target(tags, tally.corrections)
        if target is None:
            continue

        if not dry_run:
            _stage_target(settings, fid, target)
        tally.staged_files += 1


def _is_multi_value(tags: dict[str, list[str]]) -> bool:
    """Return whether the file's ``artist`` or ``albumartist`` carries more than one value."""
    return any(len(tags.get(field_name, [])) > 1 for field_name in _NAME_FIELDS)


@dataclass(frozen=True, slots=True)
class _Target:
    """One file's staged tag target plus the provenance note describing why."""

    tags: dict[str, list[str]]
    note: str


def _build_target(
    tags: dict[str, list[str]],
    corrections: dict[str, _Resolution],
) -> _Target | None:
    """Build the staged target for one file, or ``None`` when nothing changes.

    Starts from the file's managed subset (P0 — every other managed tag preserved), and for
    each single-valued ``artist``/``albumartist`` whose value has a resolution, replaces it
    with the canonical name (accumulating both fields). Each field's MBID rides along on its
    OWN id field, name-change only: writing ``musicbrainz_artistid`` for an ``albumartist``
    correction would rebind the track artist to the album artist.
    """
    target = dict(versioning.managed_subset(tags))
    note = ""
    changed = False
    for field_name in _NAME_FIELDS:
        current = tags.get(field_name, [])
        if len(current) != 1:
            continue
        resolution = corrections.get(current[0])
        if resolution is None:
            continue
        target[field_name] = [resolution.name]
        changed = True
        if resolution.mbid:
            target[_ID_FIELDS[field_name]] = [resolution.mbid]
        if not note:
            note = f"{resolution.source}: {resolution.name}"

    if not changed:
        return None
    return _Target(tags=target, note=note)


def _stage_target(
    settings: Settings,
    file_id: int,
    target: _Target,
) -> None:
    """Stage *target* for *file_id* as an ``origin='auto'`` change. ``stage_tags`` owns its conn."""
    staging.stage_tags(
        settings,
        file_id=file_id,
        managed_tags=target.tags,
        origin="auto",
        note=target.note,
    )


# --- result --------------------------------------------------------------------------


def _build_result(
    tally: _Tally,
    *,
    processed: int,
    pending_remaining: int,
) -> ResolveArtistsResult:
    """Freeze the run's tally + counts into the public :class:`ResolveArtistsResult`."""
    mappings = [
        {
            "from": value,
            "to": resolution.name,
            "mbid": resolution.mbid,
            "source": resolution.source,
        }
        for value, resolution in tally.corrections.items()
    ]
    more = pending_remaining > 0
    summary = _summarize(
        tally,
        processed=processed,
        pending_remaining=pending_remaining,
    )
    return ResolveArtistsResult(
        processed=processed,
        staged_files=tally.staged_files,
        corrected_values=len(tally.corrections),
        skipped_multi_artist=tally.skipped_multi_artist,
        skipped_sentinel=tally.skipped_sentinel,
        skipped_manual=tally.skipped_manual,
        no_correction=len(tally.no_correction_values),
        already_canonical=len(tally.already_canonical_values),
        shrinks_credit=len(tally.shrinks_credit_values),
        needs_review=len(tally.needs_review_values),
        name_id_disagreement=len(tally.name_id_disagreement_values),
        errors=len(tally.error_values),
        pending_remaining=pending_remaining,
        more=more,
        mappings=mappings,
        multi_artist_files=list(tally.multi_artist_files),
        manual_files=list(tally.manual_files),
        no_correction_values=list(tally.no_correction_values),
        already_canonical_values=list(tally.already_canonical_values),
        shrinks_credit_values=[dict(h) for h in tally.shrinks_credit_values],
        needs_review_values=[dict(h) for h in tally.needs_review_values],
        name_id_disagreement_values=[dict(d) for d in tally.name_id_disagreement_values],
        error_values=list(tally.error_values),
        summary=summary,
    )


def _summarize(
    tally: _Tally,
    *,
    processed: int,
    pending_remaining: int,
) -> str:
    """Build a short, plain human summary of what was and was not processed.

    The unprocessed remainder is never described as resumable: no bucket other than an
    accepted correction writes a row, so nothing shrinks the distinct-value list between
    two identical calls, on the dry-run path or the real one.
    """
    parts = [
        f"Processed {processed} value(s): {len(tally.corrections)} corrected, "
        f"staged {tally.staged_files} file(s).",
        f"Skipped multi-artist {tally.skipped_multi_artist}, "
        f"sentinel/feat/empty {tally.skipped_sentinel}, "
        f"manual {tally.skipped_manual}; "
        f"{len(tally.already_canonical_values)} already canonical, "
        f"no correction {len(tally.no_correction_values)}.",
        f"Held (reported, never staged): credit shrink {len(tally.shrinks_credit_values)}, "
        f"needs review {len(tally.needs_review_values)}, "
        f"name/id disagreement {len(tally.name_id_disagreement_values)}.",
    ]
    if pending_remaining > 0:
        parts.append(
            f"Processed the first {processed} of {processed + pending_remaining} value(s) "
            f"in scope. An identical call re-processes the same values instead of "
            f"advancing, because a value needing no change leaves no trace. "
            f"Raise limit above {processed}, or scope with artist= / file_ids=, to reach "
            f"the remaining {pending_remaining} value(s).",
        )
    if tally.error_values:
        parts.append(
            f"{len(tally.error_values)} value(s) errored and stay pending — re-run to retry.",
        )
    return " ".join(parts)


# --- status tools --------------------------------------------------------------------


def _artist_scope(
    conn: sqlite3.Connection,
    *,
    file_ids: list[int] | None,
    value: str | None,
) -> list[int]:
    """Resolve the in-scope file ids for the artist status tools, in ascending id order.

    *file_ids* (when given) win; otherwise *value* matches every file carrying it as
    ``artist`` OR ``albumartist`` (the union across both name fields). With neither given,
    the scope is empty (the tools require an explicit target).
    """
    if file_ids is not None:
        return store.files_in_scope(conn, file_ids=file_ids)
    if value is None:
        return []
    matched = set(store.files_by_tag_value(conn, "artist", value))
    matched.update(store.files_by_tag_value(conn, "albumartist", value))
    return sorted(matched)


def set_artist_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
    status: str,
) -> int:
    """Set ``manual`` (exclude) or ``pending`` (re-queue) for every file in scope.

    Scope is *file_ids* when given, else every file carrying *value* as ``artist`` OR
    ``albumartist``. ``manual`` writes a sticky row (recording the file's current
    ``artist``/``albumartist`` for audit) so :func:`resolve_artists` always skips it;
    ``pending`` deletes any row, re-queuing the file. Returns the number of files affected.
    Raises :class:`ValueError` for an unknown *status*. Owns its transaction.
    """
    if status not in _USER_STATUSES:
        message = f"invalid status: {status!r} (expected manual|pending)"
        raise ValueError(message)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        scoped = _artist_scope(connection, file_ids=file_ids, value=value)
        now = _utc_now()
        for fid in scoped:
            if status == "manual":
                tags = store.get_tags(connection, fid)
                artist_values = tags.get("artist", [])
                albumartist_values = tags.get("albumartist", [])
                store.set_artist_status(
                    connection,
                    file_id=fid,
                    status="manual",
                    source_artist=artist_values[0] if artist_values else None,
                    source_albumartist=albumartist_values[0] if albumartist_values else None,
                    now=now,
                )
            else:
                store.delete_artist_status(connection, fid)
        connection.commit()
    finally:
        connection.close()

    logger.info("set artist status=%s for %d file(s)", status, len(scoped))
    return len(scoped)


def reset_artist_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> int:
    """Delete the artist status row for every file in scope (back to ``pending``).

    Same value-across-both-fields scoping as :func:`set_artist_status`. Returns the number
    of files affected. Owns its transaction.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        scoped = _artist_scope(connection, file_ids=file_ids, value=value)
        for fid in scoped:
            store.delete_artist_status(connection, fid)
        connection.commit()
    finally:
        connection.close()

    logger.info("reset artist status for %d file(s)", len(scoped))
    return len(scoped)
