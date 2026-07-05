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
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.lastfm import ArtistCorrection, CorrectionSource

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

# The two states ``set_artist_status`` is allowed to drive: ``manual`` writes a sticky
# exclusion row; ``pending`` deletes it (re-queue). There is no engine-owned ``no_match``
# on this axis.
_USER_STATUSES: Final = frozenset({"manual", "pending"})


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
    errors: int
    pending_remaining: int
    more: bool
    mappings: list[dict[str, str | None]]
    multi_artist_files: list[int]
    manual_files: list[int]
    no_correction_values: list[str]
    already_canonical_values: list[str]
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
            "errors": self.errors,
            "pending_remaining": self.pending_remaining,
            "more": self.more,
            "mappings": [dict(m) for m in self.mappings],
            "multi_artist_files": list(self.multi_artist_files),
            "manual_files": list(self.manual_files),
            "no_correction_values": list(self.no_correction_values),
            "already_canonical_values": list(self.already_canonical_values),
            "error_values": [dict(e) for e in self.error_values],
            "summary": self.summary,
        }


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
    error_values: list[dict[str, str]] = field(default_factory=list)
    # value -> correction (only those whose canonical form actually differs).
    corrections: dict[str, ArtistCorrection] = field(default_factory=dict)


# --- public entry --------------------------------------------------------------------


def resolve_artists(  # noqa: PLR0913 - cohesive keyword-only scope + injection params
    settings: Settings,
    *,
    artist: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    client: CorrectionSource | None = None,
) -> ResolveArtistsResult:
    """Normalize artist names: look up canonical forms and cascade-stage the changes.

    Gathers the distinct ``artist`` + ``albumartist`` values in scope, drops the guarded
    ones (empty / ``feat.`` / compilation sentinels), looks up each remaining value's
    canonical name via *client* (cached/paced), and builds a ``value → correction`` map of
    those that actually change. Then per file in scope it skips + reports any file whose
    ``artist``/``albumartist`` is multi-value, and otherwise stages the corrected name(s)
    plus the correction's MBID as an ``origin='auto'`` change (only ``artist`` /
    ``albumartist`` / ``musicbrainz_artistid`` touched — every other managed tag preserved).

    *limit* caps the number of distinct values processed per call; the remainder is
    reported via ``pending_remaining`` / ``more`` so a large library chunks. A transient
    Last.fm error leaves that value pending (not cached) and is reported, never aborting.

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
            _resolve_values(settings, connection, to_process, client, tally)

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


# --- lookup --------------------------------------------------------------------------


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
    """Look up one value's correction; record a change, a no-correction, or a transient error.

    A transient :class:`LastfmError` leaves the value pending (not cached) and is reported,
    never aborting the run. A correction equal to the current value is already canonical →
    no change, but recorded (``already_canonical``) so the outcome buckets sum to
    ``processed``.
    """
    try:
        correction = client.artist_correction(value)
    except LastfmError as exc:
        logger.warning("last.fm correction error for value=%r: %s", value, exc)
        tally.error_values.append({"value": value, "message": str(exc)})
        return

    if correction is None:
        tally.no_correction_values.append(value)
        return

    if correction.name != value:
        tally.corrections[value] = correction
    else:
        tally.already_canonical_values.append(value)


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


def _build_target(
    tags: dict[str, list[str]],
    corrections: dict[str, ArtistCorrection],
) -> dict[str, list[str]] | None:
    """Build the staged target for one file, or ``None`` when nothing changes.

    Starts from the file's managed subset (P0 — every other managed tag preserved), and for
    each single-valued ``artist``/``albumartist`` whose value has a correction, replaces it
    with the canonical name (accumulating both fields). The MBID rides along, name-change
    only: it is written only when the file is actually being changed.
    """
    target = dict(versioning.managed_subset(tags))
    mbid: str | None = None
    changed = False
    for field_name in _NAME_FIELDS:
        current = tags.get(field_name, [])
        if len(current) != 1:
            continue
        correction = corrections.get(current[0])
        if correction is None:
            continue
        target[field_name] = [correction.name]
        changed = True
        if correction.mbid:
            mbid = correction.mbid

    if not changed:
        return None
    if mbid is not None:
        target["musicbrainz_artistid"] = [mbid]
    return target


def _stage_target(
    settings: Settings,
    file_id: int,
    target: dict[str, list[str]],
) -> None:
    """Stage *target* for *file_id* as an ``origin='auto'`` change. ``stage_tags`` owns its conn."""
    canonical = target["artist"][0] if target.get("artist") else target.get("albumartist", [""])[0]
    staging.stage_tags(
        settings,
        file_id=file_id,
        managed_tags=target,
        origin="auto",
        note=f"lastfm: {canonical}",
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
        {"from": value, "to": correction.name, "mbid": correction.mbid}
        for value, correction in tally.corrections.items()
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
        errors=len(tally.error_values),
        pending_remaining=pending_remaining,
        more=more,
        mappings=mappings,
        multi_artist_files=list(tally.multi_artist_files),
        manual_files=list(tally.manual_files),
        no_correction_values=list(tally.no_correction_values),
        already_canonical_values=list(tally.already_canonical_values),
        error_values=list(tally.error_values),
        summary=summary,
    )


def _summarize(
    tally: _Tally,
    *,
    processed: int,
    pending_remaining: int,
) -> str:
    """Build a short, plain human summary of what was and was not processed."""
    parts = [
        f"Processed {processed} value(s): {len(tally.corrections)} corrected, "
        f"staged {tally.staged_files} file(s).",
        f"Skipped multi-artist {tally.skipped_multi_artist}, "
        f"sentinel/feat/empty {tally.skipped_sentinel}, "
        f"manual {tally.skipped_manual}; "
        f"{len(tally.already_canonical_values)} already canonical, "
        f"no correction {len(tally.no_correction_values)}.",
    ]
    if pending_remaining > 0:
        parts.append(f"{pending_remaining} value(s) still pending — call again to continue.")
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
