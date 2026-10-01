"""Genre-tagging orchestration: select → look up Last.fm → classify → stage (M2 phase 1).

This is the LLM-facing entry point that ties the read path, the cached Last.fm client,
the classifier, and the revertible staging engine together. The lookups, the classification,
and the disk writes are delegated to the modules built in chunks 1-3.

Design notes (the spec):

* **Selection is the classifier's ``pending`` set.** :func:`tagmend.engine.store.derived_status`
  on :data:`tagmend.engine.axis.GENRE_AXIS` decides it, over present files in scope, in file id
  order, capped at ``limit`` files. Every selected file leaves ``pending`` unless its lookup
  errors, so repeated capped calls terminate.
* **The outcome stage writes one row per selected file.** A resolved genre equal to the
  current one records ``done``. A differing one is staged and records ``done`` snapshotting the
  staged target. A lookup with nothing usable records ``no_match``. A transient Last.fm error
  writes nothing and the file stays ``pending``.
* **Lookup identity** is ``albumartist`` when present (better for compilations), else
  ``artist``. ``album`` is used only when ``genre_use_album_tags`` is on.
* **No accidental deletion (P0):** the resolver stages only ``genre``, the one field it
  decides, and :func:`tagmend.engine.staging._stage_one` merges it onto the tags read from
  disk, so ``write_managed_tags``'s delete-on-absent behavior can never drop
  ``artist``/``albumartist`` and a lagging snapshot mirror can never overwrite a newer value.
* **One connection** is owned here for selection, cache, and status writes. The
  :class:`tagmend.engine.lastfm.LastfmClient` shares it (eager cache commits), while
  :func:`tagmend.engine.staging.stage_tags` opens and owns its own connection per call.

Like the rest of the conn-owning layer, the public functions here own their connection
and commit. The building blocks in :mod:`tagmend.engine.store` never commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tagmend.engine import (
    axis,
    axis_status,
    classify,
    clock,
    db,
    lookup_clients,
    schema,
    staging,
    store,
)
from tagmend.engine.lastfm import LastfmClient, LastfmError
from tagmend.engine.validation import check_limit
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.classify import Vocabulary
    from tagmend.engine.lastfm import Tag, TagSource

logger = get_logger(__name__)

_GENRE_FIELD = axis.GENRE_AXIS.fields[0]


# --- result types --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolveGenresResult:
    """Immutable summary of one :func:`resolve_genres` call, JSON-ready for the MCP tool."""

    settled: int
    staged_files: int
    no_match: int
    pending_remaining: int
    more: bool
    errors: int
    error_items: list[dict[str, str]]
    no_match_artists: list[str]
    summary: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "settled": self.settled,
            "staged_files": self.staged_files,
            "no_match": self.no_match,
            "pending_remaining": self.pending_remaining,
            "more": self.more,
            "errors": self.errors,
            "error_items": [dict(item) for item in self.error_items],
            "no_match_artists": list(self.no_match_artists),
            "summary": self.summary,
        }


@dataclass(slots=True)
class _Tally:
    """Mutable accumulator for one ``resolve_genres`` run, frozen into the result at the end."""

    settled: int = 0
    staged_files: int = 0
    no_match: int = 0
    error_items: list[dict[str, str]] = field(default_factory=list)
    no_match_artists: set[str] = field(default_factory=set)


# --- staging orchestration -----------------------------------------------------------


def resolve_genres(  # noqa: PLR0913 - cohesive keyword-only scope + injection params
    settings: Settings,
    *,
    value: str | None = None,
    album: str | None = None,
    file_ids: list[int] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    client: TagSource | None = None,
) -> ResolveGenresResult:
    """Look up Last.fm genres for the in-scope ``pending`` files and stage the result.

    Scope is *file_ids* when given, else every file carrying *value* as ``artist`` or
    ``albumartist`` (narrowed to *album* when given), else the whole library. The selection is
    the first *limit* (default ``genre_stage_limit``) present files in scope that derive
    ``pending``. They are grouped by ``(artist, album)``, and per group Last.fm top tags are
    looked up and classified. Each selected file then settles: ``done`` when the resolved genres
    equal its current ones, ``done`` and staged (``origin='auto'``, only ``genre`` changed) when
    they differ, ``no_match`` when nothing usable came back. A transient Last.fm error leaves the
    group ``pending`` and is reported, never aborting the call.

    *dry_run* counts what would settle and stage without staging or writing any status row. It
    still reads the lookup cache and fetches on a cache miss.

    *client* lets callers inject a :class:`tagmend.engine.lastfm.TagSource` (a fake in
    tests). When ``None`` a real :class:`LastfmClient` is built and requires
    ``settings.lastfm_api_key``. A non-dry run raises :class:`ValueError` if anything is
    already staged ("commit or unstage pending changes first"). Any run raises it for a
    negative *limit*, an unknown file id, *album* without *value*, or when a real client is
    needed but no API key is configured. Owns its connection, and ``stage_tags`` opens its own.
    """
    check_limit(limit)
    effective_limit = limit if limit is not None else settings.genre_stage_limit
    vocab = classify.load_vocabulary()
    tally = _Tally()

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)

        # Staging replaces a file's pending row, so a genre run would silently discard a
        # manual fix to another field that is still waiting for its commit.
        if not dry_run and store.any_staged(connection):
            message = "commit or unstage pending changes first"
            raise ValueError(message)

        scoped_ids = store.files_in_scope(
            connection,
            value_fields=axis.GENRE_AXIS.scope_fields,
            value=value,
            album=album,
            file_ids=file_ids,
        )
        pending = store.pending_file_ids(connection, axis.GENRE_AXIS, scoped_ids)
        selected = pending[:effective_limit]
        if selected:
            _process_groups(settings, connection, selected, vocab, client, tally, dry_run=dry_run)
        pending_remaining = len(store.pending_file_ids(connection, axis.GENRE_AXIS, scoped_ids))
    finally:
        connection.close()

    return _build_result(tally, pending_remaining=pending_remaining, dry_run=dry_run)


def _process_groups(  # noqa: PLR0913 - cohesive orchestration inputs
    settings: Settings,
    conn: sqlite3.Connection,
    selected: list[int],
    vocab: Vocabulary,
    client: TagSource | None,
    tally: _Tally,
    *,
    dry_run: bool,
) -> None:
    """Group *selected* by identity and resolve each group via *client* (built if None)."""
    groups = _group_by_identity(conn, selected)

    with lookup_clients.injected_or_owned(
        client,
        lambda: LastfmClient.from_settings(settings, conn),
    ) as source:
        for identity, fids in groups.items():
            _process_one_group(
                settings,
                conn,
                identity,
                fids,
                vocab,
                source,
                tally,
                dry_run=dry_run,
            )


def _group_by_identity(
    conn: sqlite3.Connection,
    file_ids: list[int],
) -> dict[axis.LookupIdentity, list[int]]:
    """Group file ids by their lookup identity, preserving first-seen group order."""
    groups: dict[axis.LookupIdentity, list[int]] = {}
    for fid in file_ids:
        identity = axis.lookup_identity(store.get_tags(conn, fid))
        groups.setdefault(identity, []).append(fid)
    return groups


def _process_one_group(  # noqa: PLR0913 - cohesive per-group inputs
    settings: Settings,
    conn: sqlite3.Connection,
    identity: axis.LookupIdentity,
    file_ids: list[int],
    vocab: Vocabulary,
    client: TagSource,
    tally: _Tally,
    *,
    dry_run: bool,
) -> None:
    """Resolve one ``(artist, album)`` group and settle its files.

    A transient :class:`LastfmError` writes nothing, records the error, and returns without
    aborting the wider call, so the group's files stay ``pending``. The group's status rows
    are committed together at the end. A dry run counts the outcome and writes nothing.
    """
    # A pending file has a genre identity, which needs an artist.
    lookup_artist = identity.artist
    assert lookup_artist is not None  # noqa: S101 - selection invariant

    try:
        resolved = _resolve_group(settings, identity, vocab, client)
    except LastfmError as exc:
        logger.warning("last.fm error for artist=%r: %s", lookup_artist, exc)
        tally.error_items.append({"key": lookup_artist, "message": str(exc)})
        return

    changed: list[int] = []
    status = "no_match"
    if resolved:
        status = "done"
        changed = [
            fid for fid in file_ids if store.get_tags(conn, fid).get(_GENRE_FIELD, []) != resolved
        ]
    else:
        tally.no_match += len(file_ids)
        tally.no_match_artists.add(lookup_artist)
    tally.settled += len(file_ids)
    tally.staged_files += len(changed)

    # The lookup already happened, so a preview reports the outcome. Only the stage and the
    # status rows are withheld until the real run.
    if dry_run:
        return
    # Every stage runs before any row write, because stage_tags needs the write lock this
    # connection would otherwise hold.
    for fid in changed:
        _stage_resolved(settings, fid, resolved)
    now = clock.utc_now()
    for fid in file_ids:
        store.record_outcome(conn, axis.GENRE_AXIS, file_id=fid, status=status, now=now)
    conn.commit()


def _resolve_group(
    settings: Settings,
    identity: axis.LookupIdentity,
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

    return classify.classify_genres(artist_tags, album_tags, vocab, settings)


def _stage_resolved(settings: Settings, file_id: int, resolved: list[str]) -> None:
    """Stage *resolved* genres for *file_id*, passing ONLY ``genre`` (P0, no deletion).

    :func:`tagmend.engine.staging._stage_one` merges it onto the tags read from disk, so
    every other managed tag keeps its on-disk value through the commit's delete-on-absent
    write. ``stage_tags`` opens and owns its own connection.
    """
    staging.stage_tags(
        settings,
        file_id=file_id,
        tags={"genre": resolved},
        origin="auto",
        note=f"lastfm: {', '.join(resolved)}",
    )


def _build_result(
    tally: _Tally,
    *,
    pending_remaining: int,
    dry_run: bool,
) -> ResolveGenresResult:
    """Freeze the run's tally + counts into the public :class:`ResolveGenresResult`."""
    more = not dry_run and tally.settled > 0 and pending_remaining > 0
    return ResolveGenresResult(
        settled=tally.settled,
        staged_files=tally.staged_files,
        no_match=tally.no_match,
        pending_remaining=pending_remaining,
        more=more,
        errors=len(tally.error_items),
        error_items=list(tally.error_items),
        no_match_artists=sorted(tally.no_match_artists),
        summary=_summarize(tally, pending_remaining=pending_remaining, dry_run=dry_run),
    )


def _summarize(tally: _Tally, *, pending_remaining: int, dry_run: bool) -> str:
    """Build a short, plain human summary of what settled and what is left.

    A dry run records nothing, so its remainder is not resumable and is worded accordingly.
    """
    errors = len(tally.error_items)
    parts = [
        f"Settled {tally.settled} file(s): staged {tally.staged_files}, no_match {tally.no_match}.",
    ]
    if dry_run:
        parts.append(
            f"A dry run records nothing, so {pending_remaining} file(s) in scope stay pending "
            f"and an identical call previews the same files.",
        )
    elif pending_remaining > 0:
        parts.append(f"{pending_remaining} file(s) still pending. Call again to continue.")
    if errors > 0:
        parts.append(f"{errors} artist(s) errored and stay pending. Re-run to retry.")
    return " ".join(parts)


# --- status tools --------------------------------------------------------------------


def set_genre_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
    status: str,
) -> int:
    """Record ``manual`` on the genre axis for every file in scope.

    Scope is *file_ids* when given, else every file carrying *value* as ``artist`` or
    ``albumartist``. With neither the call changes nothing and returns 0. A ``manual`` row is
    sticky until :func:`reset_genre_status`. Returns the number of files affected. Raises
    :class:`ValueError` for any *status* other than ``manual`` or an unknown file id.
    """
    return axis_status.set_manual_status(
        settings,
        axis.GENRE_AXIS,
        file_ids=file_ids,
        value=value,
        status=status,
    )


def reset_genre_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> int:
    """Delete the genre status row of every file in scope, the one hand-back of ``manual``.

    Same scoping as :func:`set_genre_status`. Returns the number of files affected.
    """
    return axis_status.reset_status(
        settings,
        axis.GENRE_AXIS,
        file_ids=file_ids,
        value=value,
    )
