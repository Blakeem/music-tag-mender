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
* **One connection** is owned by :func:`tagmend.engine.axis_resolver.run` for selection,
  cache, and status writes. The :class:`tagmend.engine.lastfm.LastfmClient` shares it (eager
  cache commits), while :func:`tagmend.engine.staging.stage_tags` opens and owns its own
  connection per call.

Like the rest of the conn-owning layer, the public functions here own their connection
and commit. The building blocks in :mod:`tagmend.engine.store` never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tagmend.engine import axis, axis_resolver, axis_status, classify, staging, store
from tagmend.engine.lastfm import LastfmClient, LastfmError

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.classify import Vocabulary
    from tagmend.engine.lastfm import Tag, TagSource

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
    group ``pending`` and is reported, never aborting the call. A file that cannot be staged is
    reported in ``error_items`` under ``file_id=<id>`` and stays ``pending``.

    *dry_run* counts what would settle and stage without staging or writing any status row. It
    still reads the lookup cache and fetches on a cache miss.

    *client* lets callers inject a :class:`tagmend.engine.lastfm.TagSource` (a fake in
    tests). When ``None`` a real :class:`LastfmClient` is built and requires
    ``settings.lastfm_api_key``. A non-dry run raises :class:`ValueError` if anything is
    already staged ("commit or unstage pending changes first"). Any run raises it for a
    negative *limit*, an unknown file id, *album* without *value*, or when a real client is
    needed but no API key is configured. Owns its connection, and ``stage_tags`` opens its own.
    """
    vocab = classify.load_vocabulary()
    resolver: axis_resolver.AxisResolver[TagSource, list[str]] = axis_resolver.AxisResolver(
        axis_=axis.GENRE_AXIS,
        build_client=lambda conn: LastfmClient.from_settings(settings, conn),
        lookup=lambda source, identity: _resolve_group(settings, identity, vocab, source) or None,
        transient_error=LastfmError,
        group_key=_artist_key,
        stage=lambda conn, fid, resolved, dry_run: _stage_resolved(
            settings, conn, fid, resolved, dry_run=dry_run
        ),
    )

    outcome = axis_resolver.run(
        settings,
        resolver,
        value=value,
        album=album,
        file_ids=file_ids,
        limit=limit,
        default_limit=settings.genre_stage_limit,
        dry_run=dry_run,
        client=client,
    )

    no_match_artists = {
        identity.artist
        for identity, resolved in outcome.answers.items()
        if resolved is None and identity.artist is not None
    }
    return ResolveGenresResult(
        settled=outcome.settled,
        staged_files=outcome.staged_files,
        no_match=outcome.no_match,
        pending_remaining=outcome.pending_remaining,
        more=outcome.more,
        errors=outcome.errors,
        error_items=outcome.error_items,
        no_match_artists=sorted(no_match_artists),
        summary=outcome.summary,
    )


def _artist_key(identity: axis.LookupIdentity) -> str:
    """Key a failed group's error item by its looked-up artist."""
    # A pending file has a genre identity, which needs an artist.
    assert identity.artist is not None  # noqa: S101 - selection invariant
    return identity.artist


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

    return classify.classify_genres(
        artist_tags, album_tags, vocab, settings, lookup_artist=lookup_artist
    )


def _stage_resolved(
    settings: Settings,
    conn: sqlite3.Connection,
    file_id: int,
    resolved: list[str],
    *,
    dry_run: bool,
) -> bool:
    """Stage *resolved* genres for *file_id*, passing ONLY ``genre`` (P0, no deletion).

    A file already holding *resolved* stages nothing and returns ``False``, as does every file
    on a dry run that would not stage. :func:`tagmend.engine.staging._stage_one` merges the
    genre onto the tags read from disk, so every other managed tag keeps its on-disk value
    through the commit's delete-on-absent write. ``stage_tags`` opens and owns its own
    connection.
    """
    if store.get_tags(conn, file_id).get(_GENRE_FIELD, []) == resolved:
        return False
    if dry_run:
        return True
    return staging.stage_tags(
        settings,
        file_id=file_id,
        tags={_GENRE_FIELD: resolved},
        origin="auto",
        note=f"lastfm: {', '.join(resolved)}",
    )


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
