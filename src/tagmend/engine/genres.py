"""Genre-tagging orchestration: select → look up Last.fm → classify → stage (M2 phase 1).

This is the LLM-facing entry point that ties the read path, the cached Last.fm client,
the classifier, and the revertible staging engine together. It owns the *selection* and
*status* policy from PLAN — Last.fm genre tagging ("Status model"); the lookups, the
classification, and the disk writes are delegated to the modules built in chunks 1-3.

Design notes (the spec):

* **"Done" is derived**, never stored — a file is done when it has a staged genre row
  or a committed ``origin='auto'`` revision. Only the two negative outcomes (``no_match``
  / ``manual``) live in ``file_genre_status``.
* **Lookup identity** is ``albumartist`` when present (better for compilations), else
  ``artist``; ``album`` is used only when ``genre_use_album_tags`` is on.
* **No accidental deletion (P0):** the staged target is built from the file's own managed
  subset with *only* ``genre`` replaced, so ``write_managed_tags``'s delete-on-absent
  behavior can never drop ``artist``/``albumartist``.
* **One connection** is owned here for selection, cache, and status writes; the
  :class:`tagmend.engine.lastfm.LastfmClient` shares it (eager cache commits), while
  :func:`tagmend.engine.staging.stage_tags` opens and owns its own connection per call.

Like the rest of the conn-owning layer, the public functions here own their connection
and commit; the building blocks in :mod:`tagmend.engine.store` never commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from tagmend.engine import classify, db, schema, staging, store, versioning
from tagmend.engine.lastfm import LastfmClient, LastfmError
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.classify import Vocabulary
    from tagmend.engine.lastfm import Tag, TagSource

logger = get_logger(__name__)

# The two states ``set_genre_status`` is allowed to drive (the LLM may exclude/re-include
# a file, but never set engine-owned outcomes like ``no_match``). ``manual`` writes a
# sticky status row; ``pending`` deletes it (re-queue).
_USER_STATUSES: Final = frozenset({"manual", "pending"})


def _utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string (the engine's timestamp form)."""
    return datetime.now(UTC).isoformat()


# --- lookup identity -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Identity:
    """The ``(artist, album)`` a file is looked up / classified against."""

    artist: str | None
    album: str | None


def _identity(tags: dict[str, list[str]]) -> _Identity:
    """Derive the lookup identity for a file's tags.

    Lookup artist is the first ``albumartist`` value when a non-empty one exists (better
    identity for compilations), else the first ``artist`` value; ``None`` when neither is
    present. Album is the first ``album`` value, or ``None``.
    """
    album_artist = tags.get("albumartist")
    artist = tags.get("artist")
    if album_artist:
        lookup_artist: str | None = album_artist[0]
    elif artist:
        lookup_artist = artist[0]
    else:
        lookup_artist = None

    album_values = tags.get("album")
    lookup_album = album_values[0] if album_values else None
    return _Identity(artist=lookup_artist, album=lookup_album)


# --- result types --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StageGenresResult:
    """Immutable summary of one :func:`stage_genres` call, JSON-ready for the MCP tool."""

    processed: int
    staged: int
    no_match: int
    skipped: dict[str, int]
    pending_remaining: int
    more: bool
    errors: list[dict[str, str]]
    no_match_artists: list[str]
    summary: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "processed": self.processed,
            "staged": self.staged,
            "no_match": self.no_match,
            "skipped": dict(self.skipped),
            "pending_remaining": self.pending_remaining,
            "more": self.more,
            "errors": list(self.errors),
            "no_match_artists": list(self.no_match_artists),
            "summary": self.summary,
        }


@dataclass(slots=True)
class _Tally:
    """Mutable accumulator for one ``stage_genres`` run, frozen into the result at the end."""

    staged: int = 0
    no_match: int = 0
    skipped_done: int = 0
    skipped_no_match: int = 0
    skipped_manual: int = 0
    skipped_no_artist: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    no_match_artists: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A processable file plus the identity it will be looked up against."""

    file_id: int
    identity: _Identity


# --- selection -----------------------------------------------------------------------


def _select(
    conn: sqlite3.Connection,
    candidate_ids: list[int],
    tally: _Tally,
) -> list[_Candidate]:
    """Classify each candidate into a bucket; return only the processable ones.

    Mutates *tally* with the skip counts. A candidate is processable when it is not
    already done (staged / committed auto revision), is not sticky ``manual``, and has no
    non-stale ``no_match`` for its current identity.
    """
    processable: list[_Candidate] = []
    for fid in candidate_ids:
        identity = _identity(store.get_tags(conn, fid))

        if identity.artist is None:
            tally.skipped_no_artist += 1
            continue

        if store.is_staged(conn, fid) or store.has_auto_revision(conn, fid):
            tally.skipped_done += 1
            continue

        decision = store.get_genre_status(conn, fid)
        if decision is not None:
            if decision.status == "manual":
                tally.skipped_manual += 1
                continue
            if decision.status == "no_match" and not _is_stale(decision, identity):
                tally.skipped_no_match += 1
                continue
            # A stale no_match (identity changed) falls through and is reprocessed.

        processable.append(_Candidate(file_id=fid, identity=identity))
    return processable


def _is_stale(decision: store.GenreStatusRow, identity: _Identity) -> bool:
    """Return whether a ``no_match`` decision is stale (its source identity changed)."""
    return decision.source_artist != identity.artist or decision.source_album != identity.album


# --- staging orchestration -----------------------------------------------------------


def stage_genres(  # noqa: PLR0913 - cohesive keyword-only scope + injection params
    settings: Settings,
    *,
    artist: str | None = None,
    album: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
    client: TagSource | None = None,
) -> StageGenresResult:
    """Look up Last.fm genres for the in-scope, not-yet-done files and stage the result.

    Selects the files in scope that are not already done (staged or committed ``auto``),
    not sticky ``manual``, and have no non-stale ``no_match``; caps them at *limit*
    (default ``genre_stage_limit``); groups them by ``(artist, album)``; and per group
    looks up Last.fm top tags, classifies them, and either stages the resolved genres
    (``origin='auto'``, only ``genre`` changed) or records a ``no_match``. A transient
    Last.fm error leaves the group pending and is reported, never aborting the call.

    *client* lets callers inject a :class:`tagmend.engine.lastfm.TagSource` (a fake in
    tests); when ``None`` a real :class:`LastfmClient` is built and requires
    ``settings.lastfm_api_key``. Raises :class:`ValueError` when a real client is needed
    but no API key is configured. Owns its connection; ``stage_tags`` opens its own.
    """
    effective_limit = limit if limit is not None else settings.genre_stage_limit
    vocab = classify.load_vocabulary()

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        candidate_ids = store.files_in_scope(
            connection,
            artist=artist,
            album=album,
            file_ids=file_ids,
        )

        tally = _Tally()
        processable = _select(connection, candidate_ids, tally)

        to_process = processable[:effective_limit]
        pending_remaining = len(processable) - len(to_process)

        if to_process:
            _process_groups(settings, connection, to_process, vocab, client, tally)
    finally:
        connection.close()

    return _build_result(
        tally,
        processed=len(to_process),
        pending_remaining=pending_remaining,
    )


def _process_groups(  # noqa: PLR0913 - cohesive orchestration inputs
    settings: Settings,
    conn: sqlite3.Connection,
    candidates: list[_Candidate],
    vocab: Vocabulary,
    client: TagSource | None,
    tally: _Tally,
) -> None:
    """Group *candidates* by identity and resolve each group via *client* (built if None)."""
    groups = _group_by_identity(candidates)

    if client is not None:
        for identity, fids in groups.items():
            _process_one_group(settings, conn, identity, fids, vocab, client, tally)
        return

    if not settings.lastfm_api_key:
        message = "no Last.fm API key configured — run `tagmend config-set lastfm_api_key <key>`"
        raise ValueError(message)
    with LastfmClient(
        settings.lastfm_api_key,
        conn,
        rate_per_sec=settings.lastfm_rate_per_sec,
    ) as owned_client:
        for identity, fids in groups.items():
            _process_one_group(settings, conn, identity, fids, vocab, owned_client, tally)


def _group_by_identity(candidates: list[_Candidate]) -> dict[_Identity, list[int]]:
    """Group file ids by their lookup identity, preserving first-seen group order."""
    groups: dict[_Identity, list[int]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.identity, []).append(candidate.file_id)
    return groups


def _process_one_group(  # noqa: PLR0913 - cohesive per-group inputs
    settings: Settings,
    conn: sqlite3.Connection,
    identity: _Identity,
    file_ids: list[int],
    vocab: Vocabulary,
    client: TagSource,
    tally: _Tally,
) -> None:
    """Resolve one ``(artist, album)`` group and stage / mark its files accordingly.

    A transient :class:`LastfmError` leaves the group's files pending (no status row),
    records the error, and returns without aborting the wider call. The group's status
    writes are committed together at the end.
    """
    # ``_select`` guarantees a non-None artist for every processable candidate.
    lookup_artist = identity.artist
    assert lookup_artist is not None  # noqa: S101 - selection invariant

    try:
        resolved = _resolve_group(settings, identity, vocab, client)
    except LastfmError as exc:
        logger.warning("last.fm error for artist=%r: %s", lookup_artist, exc)
        tally.errors.append({"artist": lookup_artist, "message": str(exc)})
        return

    if resolved:
        for fid in file_ids:
            _stage_resolved(settings, conn, fid, resolved)
            tally.staged += 1
        return

    now = _utc_now()
    for fid in file_ids:
        store.set_genre_status(
            conn,
            file_id=fid,
            status="no_match",
            source_artist=identity.artist,
            source_album=identity.album,
            now=now,
        )
        tally.no_match += 1
    tally.no_match_artists.add(lookup_artist)
    conn.commit()


def _resolve_group(
    settings: Settings,
    identity: _Identity,
    vocab: Vocabulary,
    client: TagSource,
) -> list[str]:
    """Look up + classify one group; return the resolved genres (empty = no match).

    ``artist_top_tags`` returning ``None`` (artist not on Last.fm) short-circuits to an
    empty result. Album tags are consulted only when enabled and an album is present.
    """
    lookup_artist = identity.artist
    assert lookup_artist is not None  # noqa: S101 - selection invariant

    artist_tags: list[Tag] | None = client.artist_top_tags(lookup_artist)
    if artist_tags is None:
        return []

    album_tags: list[Tag] | None = None
    if settings.genre_use_album_tags and identity.album is not None:
        album_tags = client.album_top_tags(lookup_artist, identity.album)

    return classify.resolve_genres(artist_tags, album_tags, vocab, settings)


def _stage_resolved(
    settings: Settings,
    conn: sqlite3.Connection,
    file_id: int,
    resolved: list[str],
) -> None:
    """Stage *resolved* genres for *file_id*, replacing ONLY ``genre`` (P0 — no deletion).

    The target starts from the file's own managed subset so every other managed tag
    (``artist``/``albumartist``/MBID) is preserved through the commit's delete-on-absent
    write. ``stage_tags`` opens and owns its own connection.
    """
    target = dict(versioning.managed_subset(store.get_tags(conn, file_id)))
    target["genre"] = resolved
    staging.stage_tags(
        settings,
        file_id=file_id,
        managed_tags=target,
        origin="auto",
        note=f"lastfm: {', '.join(resolved)}",
    )


def _build_result(
    tally: _Tally,
    *,
    processed: int,
    pending_remaining: int,
) -> StageGenresResult:
    """Freeze the run's tally + counts into the public :class:`StageGenresResult`."""
    skipped = {
        "done": tally.skipped_done,
        "no_match": tally.skipped_no_match,
        "manual": tally.skipped_manual,
        "no_artist": tally.skipped_no_artist,
    }
    more = pending_remaining > 0
    summary = _summarize(
        tally,
        processed=processed,
        pending_remaining=pending_remaining,
        skipped=skipped,
    )
    return StageGenresResult(
        processed=processed,
        staged=tally.staged,
        no_match=tally.no_match,
        skipped=skipped,
        pending_remaining=pending_remaining,
        more=more,
        errors=list(tally.errors),
        no_match_artists=sorted(tally.no_match_artists),
        summary=summary,
    )


def _summarize(
    tally: _Tally,
    *,
    processed: int,
    pending_remaining: int,
    skipped: dict[str, int],
) -> str:
    """Build a short, plain human summary of what was and was not processed."""
    skipped_total = sum(skipped.values())
    parts = [
        f"Processed {processed} file(s): staged {tally.staged}, no_match {tally.no_match}.",
        f"Skipped {skipped_total} "
        f"(done {skipped['done']}, no_match {skipped['no_match']}, "
        f"manual {skipped['manual']}, no_artist {skipped['no_artist']}).",
    ]
    if pending_remaining > 0:
        parts.append(f"{pending_remaining} still pending — call again to continue.")
    if tally.errors:
        parts.append(f"{len(tally.errors)} artist(s) errored and stay pending — re-run to retry.")
    return " ".join(parts)


# --- status tools --------------------------------------------------------------------


def set_genre_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    artist: str | None = None,
    status: str,
) -> int:
    """Set ``manual`` (exclude) or ``pending`` (re-queue) for every file in scope.

    ``manual`` writes a sticky status row recording the file's current lookup identity, so
    it is skipped until an explicit reset. ``pending`` deletes any status row, re-queuing
    the file. Returns the number of files affected. Raises :class:`ValueError` for an
    unknown *status*. Owns its transaction.
    """
    if status not in _USER_STATUSES:
        message = f"invalid status: {status!r} (expected manual|pending)"
        raise ValueError(message)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        scoped = store.files_in_scope(
            connection,
            artist=artist,
            file_ids=file_ids,
        )
        now = _utc_now()
        for fid in scoped:
            if status == "manual":
                identity = _identity(store.get_tags(connection, fid))
                store.set_genre_status(
                    connection,
                    file_id=fid,
                    status="manual",
                    source_artist=identity.artist,
                    source_album=identity.album,
                    now=now,
                )
            else:
                store.delete_genre_status(connection, fid)
        connection.commit()
    finally:
        connection.close()

    logger.info("set genre status=%s for %d file(s)", status, len(scoped))
    return len(scoped)


def reset_genre_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    artist: str | None = None,
) -> int:
    """Delete the genre status row for every file in scope (back to ``pending``).

    Returns the number of files affected. Owns its transaction.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        scoped = store.files_in_scope(
            connection,
            artist=artist,
            file_ids=file_ids,
        )
        for fid in scoped:
            store.delete_genre_status(connection, fid)
        connection.commit()
    finally:
        connection.close()

    logger.info("reset genre status for %d file(s)", len(scoped))
    return len(scoped)


@dataclass(frozen=True, slots=True)
class ArtistRow:
    """One distinct ``artist`` tag value with its file count, for ``list_artists``."""

    artist: str
    file_count: int

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {"artist": self.artist, "file_count": self.file_count}


def list_artists(settings: Settings) -> list[ArtistRow]:
    """Return each distinct ``artist`` tag value with its file count (value order).

    A discovery aid for scoping ``stage_genres`` by artist. Read-only.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = store.distinct_artists(connection)
    finally:
        connection.close()
    return [ArtistRow(artist=value, file_count=count) for value, count in rows]
