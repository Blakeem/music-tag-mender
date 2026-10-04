"""Artist-name normalization: select, MusicBrainz by MBID, Last.fm getCorrection, cascade-stage.

The artist-side mirror of :mod:`tagmend.engine.genres`. Where a value's canonical form
differs, it cascade-stages the corrected name across every file carrying that value
(rewriting ``artist``, ``albumartist`` and each element of the multi-value ``artists`` list,
exact-match only) through the ``origin='auto'`` commit engine. Two lookup tiers decide the
canonical form, in order.

The MusicBrainz name tier handles every value whose files carry ``musicbrainz_artistid`` (or
``musicbrainz_albumartistid`` for ``albumartist``, or the ``musicbrainz_artistid`` entry at an
``artists`` element's own index while the two lists align) and looks the artist up by that id
through :meth:`tagmend.engine.musicbrainz.MusicBrainzClient.artist_by_mbid` (cached in
``musicbrainz_artist_cache``). It accepts a spelling that folds to the canonical name
(``source: musicbrainz``) or to a registered alias (``source: musicbrainz_alias``).

The Last.fm tier sees only the values left over. It asks
:meth:`tagmend.engine.lastfm.LastfmClient.artist_correction` for each one (cached in
``lastfm_correction_cache``).

Design notes (the spec):

* **No accidental deletion (P0):** the resolver stages only the fields it decides, and
  :func:`tagmend.engine.staging._stage_one` merges them onto the tags read from disk, so the
  commit's delete-on-absent write can never drop ``genre`` or any other managed tag. Each
  corrected field writes its own id field, and on the MusicBrainz tier its own sort field
  (``artist`` with ``musicbrainz_artistid`` and ``artistsort``, ``albumartist`` with
  ``musicbrainz_albumartistid`` and ``albumartistsort``). Last.fm publishes no sort name, so
  its tier leaves the sort field untouched. A corrected ``artists`` element keeps the list's
  order and length and rewrites its aligned ``musicbrainz_artistid`` entry.
* **Per-file accumulation:** a file whose ``artist`` and ``albumartist`` both need
  correction is staged once with both fields set (not two passes clobbering each other).
* **Guards (skip + report, never rewrite):** the ``feat``/``ft``/``featuring`` family,
  compilation sentinels (``various artists``/``various``/``va``), and empty values are
  dropped from the distinct-value scan, each ``artists`` element on its own. The multi-value
  guard is separate and runs per file: a file whose ``artist`` or ``albumartist`` list has
  ``len > 1`` is never staged on. A multi-element ``artists`` list is a collaboration, not a
  multi-value file.
* **Correction gate (post-lookup, held not staged):** Last.fm casing is not trustworthy, so
  a case-only difference is already canonical. A canonical name contained in the source is
  a collapsed multi-artist credit (``Skrillex & The Doors`` → ``Skrillex``) and is held
  regardless of MBID. That is the ``&``/``with``/``vs`` family the pre-lookup ``feat`` guard
  cannot see. A correction Last.fm pairs with no MBID is held for review. Held values are
  reported, never written.
* **Name/id disagreement (held, not staged):** a name MusicBrainz records under neither its
  canonical form nor an alias for the file's own id, or a value the library pairs with two
  different ids, lands in ``name_id_disagreement`` for review.
* **The file rule:** the selection is the first ``limit`` files that derive ``pending`` on
  :data:`tagmend.engine.axis.ARTIST_AXIS`. Every unguarded name value on them is resolved, each
  correction is cascade-staged on every in-scope carrier (present, not ``manual``, not
  multi-value), and each selected file records its highest-ranked value outcome: a multi-value
  file, a ``feat`` credit, a value with no correction or a held value records ``no_match``, a
  lookup error records nothing, anything else records ``done``. An unselected carrier gets
  no row. MusicBrainz results live in ``musicbrainz_artist_cache`` and getCorrection results in
  ``lastfm_correction_cache``.

Like the rest of the conn-owning layer, the public function here owns its connection and
commits; the building blocks in :mod:`tagmend.engine.store` never commit.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from tagmend.engine import (
    axis,
    clock,
    db,
    lookup_clients,
    schema,
    staging,
    store,
)
from tagmend.engine.lastfm import LastfmClient, LastfmError, LastfmKeyError
from tagmend.engine.musicbrainz import MusicBrainzClient, MusicBrainzError
from tagmend.engine.serialize import FieldDict
from tagmend.engine.text_keys import artist_name_key
from tagmend.engine.validation import check_limit
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from tagmend.config import Settings
    from tagmend.engine.lastfm import CorrectionSource
    from tagmend.engine.musicbrainz import MBArtist, MBArtistSource

logger = get_logger(__name__)

# The two name fields this normalizer looks up and rewrites (exact-match only). Their id and
# sort fields ride along on a changed file but never trigger a change on their own.
_NAME_FIELDS: Final = ("artist", "albumartist")

# The multi-value list a library server builds its artist entities from, ``artist`` then being
# only the display credit. Each element is a name value of its own, and Picard aligns the list
# with ``musicbrainz_artistid`` by position.
_ARTISTS_FIELD: Final = "artists"
_ARTISTS_ID_FIELD: Final = "musicbrainz_artistid"

# Every field whose values are looked up: the two name fields and each ``artists`` element.
_VALUE_FIELDS: Final = (*_NAME_FIELDS, _ARTISTS_FIELD)

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

# Each name field's own MusicBrainz id field. The pairing is positional in the file, not
# global: ``artist`` is the per-track credit and ``albumartist`` the per-release one, and the
# two routinely name different artists. Writing one field's id onto the other rebinds a file
# to an artist nobody asked for.
_ID_FIELDS: Final[Mapping[str, str]] = {
    "artist": "musicbrainz_artistid",
    "albumartist": "musicbrainz_albumartistid",
}

# Each name field's own sort field. A rewritten name leaves its sort name describing the OLD
# spelling, and a library server keeps a stored sort name even after the tag goes empty, so a
# stale one has to be REPLACED in the same commit rather than cleared later.
_SORT_FIELDS: Final[Mapping[str, str]] = {
    "artist": "artistsort",
    "albumartist": "albumartistsort",
}

# The three ways a value can reach `tally.corrections`, reported per mapping so a reviewer
# can tell an id-backed MusicBrainz fact from a Last.fm suggestion.
_SOURCE_MB: Final = "musicbrainz"
_SOURCE_MB_ALIAS: Final = "musicbrainz_alias"
_SOURCE_LASTFM: Final = "lastfm"


def _is_feat(value: str) -> bool:
    """Return whether *value* contains a ``feat``/``ft``/``featuring`` credit marker."""
    return _FEAT_RE.search(value) is not None


def _is_sentinel(value: str) -> bool:
    """Return whether *value* is a compilation sentinel (``various artists`` family)."""
    return value.strip().casefold() in _SENTINELS


def _is_guarded(value: str) -> bool:
    """Return whether *value* must be skipped outright (empty, feat., or a sentinel)."""
    return not value.strip() or _is_feat(value) or _is_sentinel(value)


def _name_values(tags: Mapping[str, list[str]]) -> list[str]:
    """Return every ``artist``, ``albumartist`` and ``artists`` value on a file, in that order."""
    return [value for field_name in _VALUE_FIELDS for value in tags.get(field_name, [])]


def _aligned_ids(tags: Mapping[str, list[str]]) -> list[str] | None:
    """Return the ``musicbrainz_artistid`` list when it aligns with ``artists``, else ``None``.

    Only equal lengths say which id names which element. Any other shape pairs no element.
    """
    elements = tags.get(_ARTISTS_FIELD, [])
    ids = tags.get(_ARTISTS_ID_FIELD, [])
    if not elements or len(ids) != len(elements):
        return None
    return list(ids)


def _is_placeholder(name: str) -> bool:
    """Return whether *name* is a MusicBrainz special-purpose placeholder (``[unknown]`` …)."""
    return _MB_PLACEHOLDER_RE.match(name.strip()) is not None


def _case_key(name: str) -> str:
    """Return *name* casefolded after NFC, so the two byte-forms of one accent compare equal."""
    return unicodedata.normalize("NFC", name).casefold()


def _is_case_only(value: str, canonical: str) -> bool:
    """Return whether *canonical* differs from *value* by casing alone (or not at all).

    Diacritics are deliberately not folded away: ``Antonio`` → ``Antônio`` is a spelling
    fix, not a casing opinion, and must stay eligible for staging.
    """
    return _case_key(value) == _case_key(canonical)


def _shrinks_credit(value: str, canonical: str) -> bool:
    """Return whether *canonical* is a strict fold-case substring of *value*.

    That shape is a multi-artist credit collapsed onto one member, which no MBID can
    justify. Equality is excluded so an already-canonical value is not read as a shrink.
    """
    folded_value = _case_key(value)
    folded_canonical = _case_key(canonical)
    return folded_canonical != folded_value and folded_canonical in folded_value


# --- result types --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolveArtistsResult(FieldDict):
    """Immutable summary of one :func:`resolve_artists` call, JSON-ready for the MCP tool."""

    settled: int
    staged_files: int
    corrected_values: int
    skipped_multi_artist: int
    skipped_sentinel: int
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
    no_correction_values: list[str]
    already_canonical_values: list[str]
    shrinks_credit_values: list[dict[str, str]]
    needs_review_values: list[dict[str, str]]
    name_id_disagreement_values: list[dict[str, str]]
    error_items: list[dict[str, str]]
    summary: str


@dataclass(frozen=True, slots=True)
class _Resolution:
    """One accepted value -> canonical-name change, tagged with the tier that decided it."""

    name: str
    mbid: str | None
    source: str
    # Only MusicBrainz publishes a sort name. Last.fm leaves this None, and the file's own
    # sort field is then left untouched rather than guessed at or destroyed.
    sort_name: str | None = None


@dataclass(slots=True)
class _Tally:
    """Mutable accumulator for one ``resolve_artists`` run, frozen into the result at end."""

    settled: int = 0
    staged_files: int = 0
    skipped_multi_artist: int = 0
    skipped_sentinel: int = 0
    multi_artist_files: list[int] = field(default_factory=list)
    no_correction_values: list[str] = field(default_factory=list)
    already_canonical_values: list[str] = field(default_factory=list)
    shrinks_credit_values: list[dict[str, str]] = field(default_factory=list)
    needs_review_values: list[dict[str, str]] = field(default_factory=list)
    name_id_disagreement_values: list[dict[str, str]] = field(default_factory=list)
    error_items: list[dict[str, str]] = field(default_factory=list)
    # Carriers whose stage raised: no outcome row, so each stays pending for the next call.
    failed_files: set[int] = field(default_factory=set)
    # value -> resolution: only the substantive ones a tier's gate accepts.
    corrections: dict[str, _Resolution] = field(default_factory=dict)


# --- public entry --------------------------------------------------------------------


def resolve_artists(  # noqa: PLR0913 - cohesive keyword-only scope + injection params
    settings: Settings,
    *,
    value: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    client: CorrectionSource | None = None,
    mb_client: MBArtistSource | None = None,
) -> ResolveArtistsResult:
    """Normalize artist names: look up canonical forms and cascade-stage the changes.

    Scope is *file_ids* when given, else every file carrying *value* as ``artist`` or
    ``albumartist``, else the whole library. The selection is the first *limit* (every one when
    ``None``) present files in scope that derive ``pending`` on the artist axis. Every distinct
    ``artist``, ``albumartist`` and ``artists`` element value on them is resolved except the
    guarded ones (empty / ``feat.`` / compilation sentinels). A value whose files carry its own
    MBID is looked up by that id on MusicBrainz via *mb_client* first. Every value left over is
    looked up on Last.fm getCorrection via *client*. Both tiers are cached and paced, and
    together they build a ``value → correction`` map of the values that actually change.

    Each correction is then staged as an ``origin='auto'`` change on every in-scope carrier: a
    present file that is not ``manual`` and not multi-value. Each corrected field writes its own
    id field, and on the MusicBrainz tier its own sort field. A corrected ``artists`` element is
    rewritten in place with its aligned ``musicbrainz_artistid`` entry. Every other managed tag
    is preserved. Finally each selected file records its outcome: ``no_match`` for a multi-value
    file, a ``feat`` credit, a value with no correction or a held value, nothing for a lookup
    error or a file that cannot be staged (the file stays ``pending``), ``done`` otherwise. An
    unselected carrier gets no row. A rejected Last.fm key raises :class:`LastfmKeyError` and
    stops the call.

    *dry_run* returns the proposed ``value → canonical`` mappings and the would-settle and
    would-stage counts and writes nothing. Lookups still run. A cached answer costs nothing and
    a cache miss makes a live request. A dry run skips the empty-staging precondition. A
    non-dry-run raises :class:`ValueError` if anything is already staged ("commit or unstage
    pending changes first"), and any run raises it for a negative *limit* or an unknown file
    id. *client* lets callers inject a :class:`tagmend.engine.lastfm.CorrectionSource` (a fake
    in tests). When ``None`` a real :class:`LastfmClient` is built and requires
    ``settings.lastfm_api_key``. *mb_client* injects an
    :class:`tagmend.engine.musicbrainz.MBArtistSource` the same way, and when ``None`` a real
    :class:`MusicBrainzClient` is built. Owns its connection, and ``stage_tags`` opens its own.
    """
    check_limit(limit)
    tally = _Tally()

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)

        if not dry_run and store.any_staged(connection):
            message = "commit or unstage pending changes first"
            raise ValueError(message)

        scoped_ids = store.files_in_scope(
            connection,
            value_fields=axis.ARTIST_AXIS.scope_fields,
            value=value,
            file_ids=file_ids,
        )
        pending = store.pending_file_ids(connection, axis.ARTIST_AXIS, scoped_ids)
        selected = pending if limit is None else pending[:limit]
        if selected:
            carriers = _carriers(connection, scoped_ids)
            values = _distinct_values(connection, selected, tally)
            if values:
                pairing = _value_mbids(connection, carriers)
                unresolved = _resolve_by_mbid(
                    settings,
                    connection,
                    values,
                    pairing,
                    mb_client,
                    tally,
                )
                if unresolved:
                    _resolve_values(settings, connection, unresolved, client, tally)
            _stage_files(settings, connection, carriers, tally, dry_run=dry_run)
            _settle_selected(connection, selected, tally, dry_run=dry_run)
        pending_remaining = len(store.pending_file_ids(connection, axis.ARTIST_AXIS, scoped_ids))
    finally:
        connection.close()

    return _build_result(tally, pending_remaining=pending_remaining, dry_run=dry_run)


# --- carriers ------------------------------------------------------------------------


def _carriers(conn: sqlite3.Connection, scoped_ids: list[int]) -> list[int]:
    """Return the present, non-``manual`` files in scope: the ones a correction may stage on.

    A ``manual`` file's values are a human decision, so no cascade rewrites them.
    """
    missing = store.missing_file_ids(conn)
    carriers: list[int] = []
    for fid in scoped_ids:
        if fid in missing:
            continue
        row = axis.get_outcome(conn, axis.ARTIST_AXIS, fid)
        if row is not None and row.status == "manual":
            continue
        carriers.append(fid)
    return carriers


# --- distinct-value scan -------------------------------------------------------------


def _distinct_values(
    conn: sqlite3.Connection,
    selected: list[int],
    tally: _Tally,
) -> list[str]:
    """Return the distinct, non-guarded ``artist``, ``albumartist`` and ``artists`` values.

    Reads *selected*. Mutates *tally* with the sentinel/guard skip count. Order is stable
    (first-seen).
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for fid in selected:
        for value in _name_values(store.get_tags(conn, fid)):
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
    id unambiguously describe each other. An ``artists`` element pairs with the
    ``musicbrainz_artistid`` entry at its own index while the two lists align. An empty set
    means the value carries no id anywhere. A set larger than one means the library disagrees
    with itself about who this is.
    """
    pairing: dict[str, set[str]] = {}
    for fid in candidate_ids:
        for name, mbid in _name_id_pairs(store.get_tags(conn, fid)):
            bucket = pairing.setdefault(name, set())
            if mbid:
                bucket.add(mbid)
    return pairing


def _name_id_pairs(tags: Mapping[str, list[str]]) -> list[tuple[str, str]]:
    """Return each pairable name value on a file with its stripped id, ``""`` when it has none."""
    pairs: list[tuple[str, str]] = []
    for field_name, id_field in _ID_FIELDS.items():
        names = tags.get(field_name, [])
        if len(names) != 1:
            continue
        ids = tags.get(id_field, [])
        pairs.append((names[0], ids[0].strip() if len(ids) == 1 else ""))
    elements = tags.get(_ARTISTS_FIELD, [])
    aligned = _aligned_ids(tags)
    for index, element in enumerate(elements):
        pairs.append((element, "" if aligned is None else aligned[index].strip()))
    return pairs


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
        with lookup_clients.injected_or_owned(
            client,
            lambda: MusicBrainzClient.from_settings(settings, conn),
        ) as source:
            settled |= _lookup_each(lookups, pairing, source, tally)

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
            tally.error_items.append({"key": value, "message": str(exc)})
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
    a name MusicBrainz has never heard of for this id. Both are reported and never staged.
    """
    if value == artist.name:
        tally.already_canonical_values.append(value)
        return

    folded = artist_name_key(value)
    if folded == artist_name_key(artist.name):
        tally.corrections[value] = _Resolution(
            artist.name,
            artist.mbid,
            _SOURCE_MB,
            sort_name=artist.sort_name,
        )
        return

    if any(folded == artist_name_key(alias) for alias in artist.aliases):
        tally.corrections[value] = _Resolution(
            artist.name,
            artist.mbid,
            _SOURCE_MB_ALIAS,
            sort_name=artist.sort_name,
        )
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
    with lookup_clients.injected_or_owned(
        client,
        lambda: LastfmClient.from_settings(settings, conn),
    ) as source:
        for value in values:
            _resolve_one_value(value, source, tally)


def _resolve_one_value(
    value: str,
    client: CorrectionSource,
    tally: _Tally,
) -> None:
    """Look up one value's correction and route it to exactly one outcome bucket.

    A :class:`LastfmError` leaves the value pending (not cached) and is reported, never
    aborting the run. A rejected key fails every lookup alike, so its
    :class:`LastfmKeyError` propagates and stops the call. A correction to a MusicBrainz
    special-purpose placeholder (``[unknown]``, ``[no artist]``, …) is not a real name and is
    treated exactly like no correction. The surviving corrections pass the gate in order:
    case-only (already canonical), credit shrink (held), no MBID (held). A name is therefore
    only staged when it is a substantive change Last.fm pairs with an MBID. That MBID is
    Last.fm's claim, not a MusicBrainz lookup. Every value lands in one bucket.
    """
    try:
        correction = client.artist_correction(value)
    except LastfmKeyError:
        raise
    except LastfmError as exc:
        logger.warning("last.fm correction error for value=%r: %s", value, exc)
        tally.error_items.append({"key": value, "message": str(exc)})
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
    carriers: list[int],
    tally: _Tally,
    *,
    dry_run: bool,
) -> None:
    """Stage the accumulated name change(s) on every carrier, all in this one staging run.

    A multi-value file is never a carrier, since one corrected value cannot say which of its
    names it replaces.
    """
    if not tally.corrections:
        return
    for fid in carriers:
        tags = store.get_tags(conn, fid)
        if _is_multi_value(tags):
            continue

        target = _build_target(tags, tally.corrections)
        if target is None:
            continue

        try:
            if dry_run:
                staging.would_stage(settings, conn, file_id=fid, tags=target.tags)
            else:
                _stage_target(settings, fid, target)
        except ValueError as exc:
            logger.warning("cannot stage artist correction for file_id=%s: %s", fid, exc)
            tally.error_items.append({"key": f"file_id={fid}", "message": str(exc)})
            tally.failed_files.add(fid)
            continue
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

    Holds only the fields this resolver decides. :func:`tagmend.engine.staging._stage_one`
    merges them onto the tags read from disk, so every other managed tag keeps its on-disk
    value (P0). *tags* is read only to find the current name values. For each single-valued
    ``artist``/``albumartist`` whose value has a resolution, it sets the canonical name
    (accumulating both fields). Each field's MBID and sort name ride along on that field's
    OWN id and sort fields, name-change only: writing ``musicbrainz_artistid`` for an
    ``albumartist`` correction would rebind the track artist to the album artist, and writing
    ``artistsort`` there would misfile it in a browse list. Each ``artists`` element is
    rewritten in place by :func:`_element_target`, so an ``artist`` corrected from X to Y turns
    every ``X`` element into ``Y`` in the same target.
    """
    target, element_resolutions = _element_target(tags, corrections)
    elements = target.get(_ARTISTS_FIELD, tags.get(_ARTISTS_FIELD, []))
    applied: list[_Resolution] = []
    for field_name in _NAME_FIELDS:
        current = tags.get(field_name, [])
        if len(current) != 1:
            continue
        resolution = corrections.get(current[0])
        if resolution is None:
            continue
        target[field_name] = [resolution.name]
        applied.append(resolution)
        # musicbrainz_artistid aligns with artists by position, so artist may set it only while
        # that list is absent or holds this one name.
        if resolution.mbid and (field_name != "artist" or elements in ([], [resolution.name])):
            target[_ID_FIELDS[field_name]] = [resolution.mbid]
        if resolution.sort_name:
            target[_SORT_FIELDS[field_name]] = [resolution.sort_name]
    applied.extend(element_resolutions)

    if not applied:
        return None
    first = applied[0]
    return _Target(tags=target, note=f"{first.source}: {first.name}")


def _element_target(
    tags: Mapping[str, list[str]],
    corrections: dict[str, _Resolution],
) -> tuple[dict[str, list[str]], list[_Resolution]]:
    """Return the rewritten ``artists`` list and aligned ids, plus the resolutions applied.

    The list keeps its order and length, and only the elements with a resolution change. Each
    one's id entry changes with it while the two lists align. Returns ``({}, [])`` when no
    element changes.
    """
    elements = tags.get(_ARTISTS_FIELD, [])
    aligned = _aligned_ids(tags)
    rewritten = list(elements)
    applied: list[_Resolution] = []
    for index, element in enumerate(elements):
        resolution = corrections.get(element)
        if resolution is None:
            continue
        rewritten[index] = resolution.name
        applied.append(resolution)
        if aligned is not None and resolution.mbid:
            aligned[index] = resolution.mbid

    if not applied:
        return {}, []
    target = {_ARTISTS_FIELD: rewritten}
    if aligned is not None:
        target[_ARTISTS_ID_FIELD] = aligned
    return target, applied


def _stage_target(
    settings: Settings,
    file_id: int,
    target: _Target,
) -> None:
    """Stage *target* for *file_id* as an ``origin='auto'`` change. ``stage_tags`` owns its conn."""
    staging.stage_tags(
        settings,
        file_id=file_id,
        tags=target.tags,
        origin="auto",
        note=target.note,
    )


# --- outcome stage -------------------------------------------------------------------


def _held_values(tally: _Tally) -> set[str]:
    """Return every value this call reported for review instead of staged.

    A value with no correction is included.
    """
    held = set(tally.no_correction_values)
    for bucket in (
        tally.needs_review_values,
        tally.shrinks_credit_values,
        tally.name_id_disagreement_values,
    ):
        held.update(entry["from"] for entry in bucket)
    return held


def _file_outcome(
    tags: dict[str, list[str]],
    held: set[str],
    errored: set[str],
) -> str | None:
    """Return a selected file's highest-ranked value outcome, or ``None`` to write nothing.

    Ranked: a multi-value file, then a ``feat`` credit or a held value (``no_match``, so the
    file stays on the review list), then a lookup error (nothing, so it stays ``pending``),
    then ``done`` for corrected, already canonical or guarded values.
    """
    if _is_multi_value(tags):
        return "no_match"
    values = _name_values(tags)
    if any(_is_feat(value) or value in held for value in values):
        return "no_match"
    if any(value in errored for value in values):
        return None
    return "done"


def _settle_selected(
    conn: sqlite3.Connection,
    selected: list[int],
    tally: _Tally,
    *,
    dry_run: bool,
) -> None:
    """Record each selected file's outcome, snapshotting the tags its staged change commits."""
    held = _held_values(tally)
    errored = {item["key"] for item in tally.error_items}
    outcomes: list[tuple[int, str]] = []
    for fid in selected:
        if fid in tally.failed_files:
            continue
        tags = store.get_tags(conn, fid)
        if _is_multi_value(tags):
            tally.skipped_multi_artist += 1
            tally.multi_artist_files.append(fid)
        status = _file_outcome(tags, held, errored)
        if status is not None:
            outcomes.append((fid, status))
    tally.settled += len(outcomes)

    if dry_run:
        return
    now = clock.utc_now()
    for fid, status in outcomes:
        store.record_outcome(conn, axis.ARTIST_AXIS, file_id=fid, status=status, now=now)
    conn.commit()


# --- result --------------------------------------------------------------------------


def _build_result(
    tally: _Tally,
    *,
    pending_remaining: int,
    dry_run: bool,
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
    return ResolveArtistsResult(
        settled=tally.settled,
        staged_files=tally.staged_files,
        corrected_values=len(tally.corrections),
        skipped_multi_artist=tally.skipped_multi_artist,
        skipped_sentinel=tally.skipped_sentinel,
        no_correction=len(tally.no_correction_values),
        already_canonical=len(tally.already_canonical_values),
        shrinks_credit=len(tally.shrinks_credit_values),
        needs_review=len(tally.needs_review_values),
        name_id_disagreement=len(tally.name_id_disagreement_values),
        errors=len(tally.error_items),
        pending_remaining=pending_remaining,
        more=not dry_run and tally.settled > 0 and pending_remaining > 0,
        mappings=mappings,
        multi_artist_files=list(tally.multi_artist_files),
        no_correction_values=list(tally.no_correction_values),
        already_canonical_values=list(tally.already_canonical_values),
        shrinks_credit_values=[dict(h) for h in tally.shrinks_credit_values],
        needs_review_values=[dict(h) for h in tally.needs_review_values],
        name_id_disagreement_values=[dict(d) for d in tally.name_id_disagreement_values],
        error_items=list(tally.error_items),
        summary=_summarize(tally, pending_remaining=pending_remaining, dry_run=dry_run),
    )


def _summarize(tally: _Tally, *, pending_remaining: int, dry_run: bool) -> str:
    """Build a short, plain human summary of what settled and what is left.

    A dry run records nothing, so its remainder is not resumable and is worded accordingly.
    """
    parts = [
        f"Settled {tally.settled} file(s): {len(tally.corrections)} value(s) corrected, "
        f"staged {tally.staged_files} file(s).",
        f"Multi-artist {tally.skipped_multi_artist} file(s), "
        f"sentinel/feat/empty {tally.skipped_sentinel} value(s), "
        f"{len(tally.already_canonical_values)} already canonical, "
        f"no correction {len(tally.no_correction_values)}.",
        f"Held (reported, never staged): credit shrink {len(tally.shrinks_credit_values)}, "
        f"needs review {len(tally.needs_review_values)}, "
        f"name/id disagreement {len(tally.name_id_disagreement_values)}.",
    ]
    if dry_run:
        parts.append(
            f"A dry run records nothing, so {pending_remaining} file(s) in scope stay pending "
            f"and an identical call previews the same files.",
        )
    elif pending_remaining > 0:
        parts.append(f"{pending_remaining} file(s) still pending. Call again to continue.")
    if tally.error_items:
        parts.append(
            f"{len(tally.error_items)} item(s) errored and their files stay pending. "
            f"Re-run to retry.",
        )
    return " ".join(parts)
