"""Genre resolver: Last.fm top tags matched to the genre vocabulary and staged as ``genre``.

:func:`resolve_genres` supplies the Last.fm lookup and the genre stage to
:func:`tagmend.engine.axis_resolver.run`, which owns the selection, the grouping, the outcome
rows, refused files and the connection. Files group by their ``(artist, album)`` lookup
identity whatever ``genre_use_album_tags`` holds. That setting only adds the album's top tags
to the artist's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tagmend.engine import axis, axis_resolver, classify, ledger_lock, staging, store
from tagmend.engine.lastfm import (
    LastfmClient,
    LastfmError,
    LastfmKeyError,
    LastfmUnavailableError,
)
from tagmend.engine.serialize import FieldDict

if TYPE_CHECKING:
    import sqlite3

    from tagmend.config import Settings
    from tagmend.engine.classify import Vocabulary
    from tagmend.engine.lastfm import Tag, TagSource

_GENRE_FIELD = axis.GENRE_AXIS.fields[0]


# --- result types --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolveGenresResult(FieldDict):
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


# --- staging orchestration -----------------------------------------------------------


@ledger_lock.mutating
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
    ``albumartist`` (narrowed to *album* when given), else the whole library. The call settles
    up to *limit* (default ``genre_stage_limit``) present files in scope that derive ``pending``,
    in file-id order, and reads past a file staging refuses. They are grouped by
    ``(artist, album)``, and per group Last.fm top tags are looked up and classified. Each
    selected file then settles: ``done`` when the resolved genres equal its current ones,
    ``done`` and staged (``origin='auto'``, only ``genre`` changed) when they differ,
    ``no_match`` when nothing usable came back. A Last.fm lookup error leaves the group
    ``pending`` and is reported, and the call reads past it. A :class:`LastfmUnavailableError`
    also stops the call reading past refused files and failed groups. A rejected key raises
    :class:`LastfmKeyError` and stops the call. A file that cannot be staged is reported in
    ``error_items`` under ``file_id=<id>`` and stays ``pending``.

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
        lookup_error=LastfmError,
        unavailable_error=LastfmUnavailableError,
        group_key=_artist_key,
        stage=lambda conn, fid, resolved, dry_run: _stage_resolved(
            settings, conn, fid, resolved, dry_run=dry_run
        ),
        fatal_error=(LastfmKeyError,),
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
        return staging.would_stage(settings, conn, file_id=file_id, tags={_GENRE_FIELD: resolved})
    return staging.stage_tags(
        settings,
        file_id=file_id,
        tags={_GENRE_FIELD: resolved},
        origin="auto",
        note=f"lastfm: {', '.join(resolved)}",
    )
