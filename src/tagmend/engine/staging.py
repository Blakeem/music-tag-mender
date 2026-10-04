"""Tags staging area + commit orchestration.

The git "index → commit" step that sits above the per-file revision log in
:mod:`tagmend.engine.versioning`. A *staged* change records the desired target managed
tags for one file (``tag_revisions_staged``, one pending row per file). A *commit*
groups every currently-staged change under one ``commits`` row, applies each to disk via
the shared crash-safe loop in :mod:`tagmend.engine.commits`, and appends a real
``tag_revisions`` row, turning the staged row into history and deleting it.

This module supplies the **tags** :class:`TagDomain` (a concrete
:class:`tagmend.engine.commits.RevisionDomain`) and the conn-owning orchestrators
(:func:`stage_tags` / :func:`unstage_tags` / :func:`diff_tags` / :func:`commit_tags`).
The domain-neutral commit machinery (the ``commits`` table, the result dataclasses, and
``run_commit``) lives in :mod:`tagmend.engine.commits`.

Crash-safety follows PLAN.md §7 (the resume-free model): the staged table *is* the
journal. The baseline (version 0) is captured at **stage** time, so a crash mid-commit
followed by a rescan can never capture the wrong v0. A commit flips any lingering
``applying`` commit to ``interrupted`` (a crash remnant), then sweeps every still-staged
row under a **new** commit. Per file, the disk write happens first. Then ONE DB
transaction appends the revision *and* deletes the staged row, so a revision never
exists without its staged row already gone. Anything still staged was not durably
committed, and the next commit re-applies it idempotently.

Like :func:`tagmend.engine.versioning.revert_tags` and
:func:`tagmend.engine.library.scan_library`, every public function here owns its own
connection and commit. The building blocks in :mod:`tagmend.engine.store` never commit.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

import mutagen

from tagmend.engine import axis, clock, commits, db, path_keys, schema, store, versioning
from tagmend.engine.serialize import FieldDict
from tagmend.engine.tags import (
    MANAGED_SET_VERSION,
    MANAGED_TAGS,
    RELEASE_STAMP_TAGS,
    check_round_trip,
    ensure_writable,
    governed_tags,
    read_tags,
)
from tagmend.engine.validation import validate_file_pairs
from tagmend.log import get_logger

if TYPE_CHECKING:
    import os
    import sqlite3
    from collections.abc import Sequence

    from tagmend.config import Settings
    from tagmend.engine.commits import CommitResult

logger = get_logger(__name__)

# A staged change originates from automated resolution (``auto``) or a manual/LLM
# decision (``manual``). ``revert`` is never staged. The chosen origin flows into the
# revision the commit appends.
_STAGED_ORIGINS = frozenset({"auto", "manual"})

# (triggers, members): staging keeps every member a change omits, so a rewritten trigger leaves
# the rest naming the old entity. Sort names and the release stamp only follow a name, never lead.
_IDENTITY_GROUPS: Final[tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]] = (
    (("artist", "musicbrainz_artistid"), ("artist", "musicbrainz_artistid", "artistsort")),
    # Picard aligns these two lists by position, so one rewritten without the other pairs a
    # name with another artist's id.
    (("artists", "musicbrainz_artistid"), ("artists", "musicbrainz_artistid")),
    (
        ("albumartist", "musicbrainz_albumartistid"),
        ("albumartist", "musicbrainz_albumartistid", "albumartistsort"),
    ),
    (
        ("album", "musicbrainz_albumid", "musicbrainz_releasegroupid"),
        ("album", "musicbrainz_albumid", "musicbrainz_releasegroupid"),
    ),
    (
        ("title", "musicbrainz_trackid", "musicbrainz_releasetrackid"),
        ("title", "musicbrainz_trackid", "musicbrainz_releasetrackid"),
    ),
    (("album", "musicbrainz_albumid"), tuple(sorted(RELEASE_STAMP_TAGS - {"isrc"}))),
    # An ISRC names the recording, so only a new recording id leaves it stale, never a rebind.
    (("musicbrainz_trackid",), ("isrc",)),
)


# A NUL splits a value inside TagLib, and ID3v2.4 forbids line breaks in a text frame.
_FORBIDDEN_CHARACTERS: Final = ("\x00", "\r", "\n")

_CHANGED_SINCE_STAGE_DETAIL: Final = (
    "file changed on disk after it was staged. "
    "Re-stage it (stage_tags replaces the pending row) or unstage it."
)
_LANDED_UNRECORDED: Final = (
    "an interrupted commit already wrote its staged change. Run commit_tags to record it first"
)


def _clean_values(file_id: int, managed_tags: dict[str, list[str]]) -> dict[str, list[str]]:
    """Return caller-supplied values stripped and NFC-normalized, dropping the ones left empty.

    Navidrome trims only genre and single-valued artist fields and never Unicode-normalizes, so
    a trailing space or a decomposed accent splits one album or artist into two. A list left
    empty still means "delete the field". Raises :class:`ValueError` naming *file_id*, the key
    and the value for a NUL, CR or LF.
    """
    cleaned: dict[str, list[str]] = {}
    for key, values in managed_tags.items():
        for value in values:
            if any(character in value for character in _FORBIDDEN_CHARACTERS):
                message = (
                    f"cannot stage file_id={file_id}: {key} value {value!r} contains a NUL, "
                    "CR or LF"
                )
                raise ValueError(message)
        stripped = (unicodedata.normalize("NFC", value.strip()) for value in values)
        cleaned[key] = [value for value in stripped if value]
    return cleaned


def _drop_filled(
    requested: dict[str, list[str]],
    current: dict[str, list[str]],
    fill_only: frozenset[str],
) -> dict[str, list[str]]:
    """Return *requested* without the *fill_only* keys *current* already holds a value for."""
    return {
        key: values
        for key, values in requested.items()
        if key not in fill_only or not any(value.strip() for value in current.get(key, []))
    }


@dataclass(frozen=True, slots=True)
class TagDiffView(FieldDict):
    """A staged tag change enriched with the current→target diff (``git diff --staged``)."""

    file_id: int
    folder: str
    filename: str
    is_missing: bool
    origin: str
    note: str | None
    staged_at: str
    current: dict[str, list[str]]
    target: dict[str, list[str]]
    diff: dict[str, dict[str, list[str]]]
    stale_identity: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TagDomain:
    """The tags :class:`tagmend.engine.commits.RevisionDomain` driven by ``run_commit``.

    Frozen and stateless: it reads each staged file's payload from
    :mod:`tagmend.engine.store` by ``file_id``. ``plan_order`` and ``post_commit_file``
    are identity and no-op, because tag commits have no ordering or filesystem-cleanup concerns.
    ``droppable_frames`` is the ID3 frame id set every write is given.
    """

    droppable_frames: frozenset[str] = frozenset()

    @property
    def changed_since_stage_detail(self) -> str:
        """Re-staging replaces the row with one based on the file as it is now."""
        return _CHANGED_SINCE_STAGE_DETAIL

    @property
    def per_file_errors(self) -> tuple[type[Exception], ...]:
        """A locked, read-only or unreadable file, or a refused write, touches no other file."""
        return versioning.TAG_FILE_ERRORS

    def list_staged_file_ids(self, conn: sqlite3.Connection) -> list[int]:
        """Return every staged file id, in file_id order."""
        return [s.file_id for s in store.list_staged_tags(conn)]

    def list_staged_file_ids_under(self, conn: sqlite3.Connection, root_key: str) -> list[int]:
        """Return staged file ids whose file lives in the folder keyed *root_key* or under it."""
        return [s.file_id for s in store.list_staged_tags_under(conn, root_key)]

    def plan_order(self, conn: sqlite3.Connection, file_ids: list[int]) -> list[int]:  # noqa: ARG002
        """Tags have no move ordering: iterate in the given order."""
        return file_ids

    def resolve_path(self, conn: sqlite3.Connection, file_id: int) -> Path | None:
        """Return the on-disk path for *file_id*, or ``None`` if unknown/missing."""
        file_row = store.get_file_by_id(conn, file_id)
        if file_row is None or file_row.is_missing:
            return None
        return Path(file_row.folder) / file_row.filename

    def changed_since_stage(self, conn: sqlite3.Connection, file_id: int, path: Path) -> bool:
        """Whether *path* moved since staging AND no longer holds the staged target.

        A row staged before the signature existed (a NULL base) skips the check. A moved
        signature whose managed tags already equal the target is a crash after the disk write
        and before the DB commit, which the next commit must still complete.
        """
        staged = _require_staged(conn, file_id)
        if staged.base_size_bytes is None or staged.base_mtime_ns is None:
            return False
        stat_result = path.stat()
        base = (staged.base_size_bytes, staged.base_mtime_ns)
        if (stat_result.st_size, stat_result.st_mtime_ns) == base:
            return False
        current = versioning.managed_subset(read_tags(path).tags)
        return not _holds_target(current, staged.managed_tags, store.latest_revision(conn, file_id))

    def apply_to_disk(
        self,
        conn: sqlite3.Connection,
        file_id: int,
        path: Path,
        *,
        commit_id: int,
        now: str,
    ) -> int | None:
        """Write the staged tags to disk, then append the revision + delete the staged row.

        The disk write happens *before* any DB append, so a write failure aborts this
        file's transaction with the staged row intact for a later retry. The revision
        append, the axis status rows (:func:`_record_axis_outcomes`) and the staged-row delete
        are left in the open transaction (``run_commit`` commits them together), the invariant
        that prevents double-applying. A target the file already holds is not written, so a
        no-op commit never rewrites the file. A file whose latest revision predates the current
        managed set was staged by an older build. It commits only as the re-apply of that
        build's interrupted write, and otherwise raises :class:`ValueError`.
        """
        staged = _require_staged(conn, file_id)
        current = read_tags(path).tags
        latest = store.latest_revision(conn, file_id)
        stale = latest is not None and versioning.predates_managed_set(latest)

        # Never re-baseline here: a re-applied crash would observe the post-write disk. The
        # re-apply is recorded against the stale snapshot instead, so its change stays here.
        if stale and not _holds_target(
            versioning.managed_subset(current), staged.managed_tags, latest
        ):
            message = (
                f"file_id={file_id} was staged before managed set {MANAGED_SET_VERSION}, "
                "unstage and stage it again"
            )
            raise ValueError(message)

        # Baseline (version 0) is captured at stage time, so this is a defensive no-op. It reads
        # disk for the same reason stage time does: a baseline is what a revert restores, so
        # it can never come from the snapshot mirror, which may lag the file.
        versioning.ensure_baseline(conn, file_id, managed_tags=current, now=now)

        # Disk first, before any DB append. A stale re-apply is never written, since its target
        # lacks every newer field and the write would delete them.
        disk_diff: dict[str, dict[str, list[str]]] = (
            {}
            if stale
            else versioning.compute_diff(versioning.managed_subset(current), staged.managed_tags)
        )
        fresh = versioning.write_and_resync(
            conn,
            file_id,
            path,
            staged.managed_tags,
            write=bool(disk_diff),
            droppable_frames=self.droppable_frames,
            now=now,
        )
        _record_axis_outcomes(
            conn, file_id, staged=staged, disk_diff=disk_diff, fresh=fresh, now=now
        )
        version = versioning.append_revision(
            conn,
            file_id,
            managed_tags=fresh,
            origin=staged.origin,
            now=now,
            note=staged.note,
            commit_id=commit_id,
        )
        # Only the row read above, so a row another process staged since stays for a commit.
        store.delete_staged_tag(conn, file_id, staged_at=staged.staged_at)
        return version

    def flag_and_drop_missing(self, conn: sqlite3.Connection, file_id: int) -> None:
        """Flag the file missing and drop its staged row (it vanished from disk)."""
        store.flag_missing(conn, file_id, clock.utc_now())
        store.delete_staged_tag(conn, file_id)

    def post_commit_file(self, conn: sqlite3.Connection, file_id: int) -> None:
        """Tags have no per-file filesystem follow-up."""


def _holds_target(
    disk: dict[str, list[str]],
    target: dict[str, list[str]],
    latest: store.Revision | None,
) -> bool:
    """Whether the managed tags on *disk* already equal the staged *target*.

    When *latest* predates the current managed set, an older build staged the target and never
    wrote a field newer than its set, so only the target's fields and the fields *latest*'s set
    governed are compared.
    """
    if latest is None or not versioning.predates_managed_set(latest):
        return not versioning.compute_diff(disk, target)
    compared = set(target) | governed_tags(latest.managed_set)
    return all(disk.get(key, []) == target.get(key, []) for key in compared)


def _landed_unrecorded(
    conn: sqlite3.Connection,
    staged: store.StagedTag,
    signature: os.stat_result,
    current: dict[str, list[str]],
) -> bool:
    """Whether a commit cut by a crash already wrote *staged* to the file and recorded nothing.

    The file's *signature* left the staged base, and its *current* tags hold the staged target.
    The staged row is that write's only record until the next ``commit_tags`` completes it.
    """
    if staged.base_size_bytes is None or staged.base_mtime_ns is None:
        return False
    base = (staged.base_size_bytes, staged.base_mtime_ns)
    if (signature.st_size, signature.st_mtime_ns) == base:
        return False
    latest = store.latest_revision(conn, staged.file_id)
    return _holds_target(versioning.managed_subset(current), staged.managed_tags, latest)


def _require_staged(conn: sqlite3.Connection, file_id: int) -> store.StagedTag:
    """Return the staged row the commit loop is working on, which must exist."""
    staged = store.get_staged_tag(conn, file_id)
    if staged is None:  # pragma: no cover - defensive, file_id came from the staged list
        message = f"staged row vanished for file_id={file_id}"
        raise RuntimeError(message)
    return staged


def _record_axis_outcomes(  # noqa: PLR0913 - cohesive keyword-only commit-writer inputs
    conn: sqlite3.Connection,
    file_id: int,
    *,
    staged: store.StagedTag,
    disk_diff: dict[str, dict[str, list[str]]],
    fresh: dict[str, list[str]],
    now: str,
) -> None:
    """Keep each tag axis's status row true to the bytes just written (the commit writer).

    A ``manual`` change records ``manual`` on every axis whose fields it changed, replacing any
    row. The change is the staged row's ``changed_fields``, taken against disk at stage time,
    because a re-applied commit finds disk already equal to the target. A row staged before
    that column falls back to *disk_diff*. An ``auto`` change re-stamps the resolver's own row
    (:func:`_restamp_outcome`).
    """
    changed_fields = disk_diff.keys() if staged.changed_fields is None else staged.changed_fields
    for tag_axis in axis.TAG_AXES:
        if staged.origin != "manual":
            _restamp_outcome(
                conn, tag_axis, file_id, target=staged.managed_tags, fresh=fresh, now=now
            )
        elif any(name in changed_fields for name in tag_axis.fields):
            axis.put_outcome(conn, tag_axis, file_id=file_id, status="manual", tags=fresh, now=now)


def _restamp_outcome(  # noqa: PLR0913 - cohesive keyword-only commit-writer inputs
    conn: sqlite3.Connection,
    tag_axis: axis.Axis,
    file_id: int,
    *,
    target: dict[str, list[str]],
    fresh: dict[str, list[str]],
    now: str,
) -> None:
    """Re-stamp a ``done``/``no_match`` row from the read-back tags, status kept.

    Only the row the resolver wrote for this target qualifies: its identity snapshot equals the
    read-back identity and its value snapshot equals the target's values. The re-stamp absorbs
    the writer's value normalisation, and it never carries another axis's row across an
    identity change, so an auto artist correction re-opens the file's genre and year.
    """
    row = axis.get_outcome(conn, tag_axis, file_id)
    if row is None or row.status not in axis.RESOLVER_OUTCOMES:
        return
    if row.identity != axis.identity_of(tag_axis, fresh):
        return
    if row.values != axis.field_values(tag_axis, target):
        return
    axis.put_outcome(conn, tag_axis, file_id=file_id, status=row.status, tags=fresh, now=now)


@dataclass(frozen=True, slots=True)
class _StageInputs:
    """What :func:`_stage_one` writes from, once every refusal of :func:`_check_stage` passed."""

    base: os.stat_result
    current: dict[str, list[str]]
    remaining: dict[str, list[str]]


def _check_stage(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    tags: dict[str, list[str]],
    droppable_frames: frozenset[str],
    fill_only: frozenset[str],
) -> _StageInputs | None:
    """Run every refusal of a stage of *tags* on *file_id*, in order, and return its inputs.

    Shared by :func:`_stage_one` and :func:`would_stage`, so a dry run refuses what the real
    run refuses. Returns ``None`` when the *fill_only* guard leaves no caller-supplied key.
    Raises :class:`ValueError` naming *file_id* on any refusal. Writes nothing.
    """
    unmanaged = sorted(set(tags) - MANAGED_TAGS)
    if unmanaged:
        message = f"cannot stage non-managed tag(s) for file_id={file_id}: {', '.join(unmanaged)}"
        raise ValueError(message)
    requested = _clean_values(file_id, tags)

    file_row = store.get_file_by_id(conn, file_id)
    if file_row is None:
        message = f"unknown file_id={file_id}"
        raise ValueError(message)
    if file_row.is_missing:
        message = f"cannot stage a missing file (file_id={file_id})"
        raise ValueError(message)
    # A tag write replaces the file through a temp copy and changes what a move would render.
    if store.get_staged_path(conn, file_id) is not None:
        message = (
            f"file_id={file_id} has a staged path change. Run commit_paths or unstage_paths first"
        )
        raise ValueError(message)

    # The snapshot mirror can lag the file. The target and the baseline both delete what they
    # lack, so both come from disk.
    path = Path(file_row.folder) / file_row.filename
    try:
        # Stat before the read: an edit landing between the two makes the base older than the
        # tags, so the commit refuses the file rather than overwriting the edit.
        base = path.stat()
        current = read_tags(path).tags
    except (mutagen.MutagenError, OSError) as exc:  # type: ignore[attr-defined]
        # Reject at the boundary rather than staging a target built from nothing: the file
        # vanished or turned unreadable since the scan that wrote its row.
        message = f"cannot read tags from disk for file_id={file_id} ({path}): {exc}"
        raise ValueError(message) from exc
    existing = store.get_staged_tag(conn, file_id)
    if existing is not None and _landed_unrecorded(conn, existing, base, current):
        message = f"file_id={file_id}: {_LANDED_UNRECORDED}"
        raise ValueError(message)

    # A blank-fill decided from the snapshot mirror must still never overwrite a value the
    # file gained on disk since the last scan.
    remaining = _drop_filled(requested, current, fill_only)
    if requested and not remaining:
        return None

    # A staged row the writer must refuse would fail every commit and block revert_commit's
    # empty-staging guard until someone unstaged it by hand.
    try:
        ensure_writable(path, droppable_frames=droppable_frames)
        check_round_trip(path, remaining)
    except (mutagen.MutagenError, OSError, ValueError) as exc:  # type: ignore[attr-defined]
        message = f"cannot stage file_id={file_id}: {exc}"
        raise ValueError(message) from exc

    return _StageInputs(base=base, current=current, remaining=remaining)


def _stage_one(  # noqa: PLR0913 - cohesive keyword-only per-file staging payload
    conn: sqlite3.Connection,
    *,
    file_id: int,
    tags: dict[str, list[str]],
    origin: str,
    note: str | None,
    now: str,
    droppable_frames: frozenset[str],
    fill_only: frozenset[str] = frozenset(),
) -> bool:
    """Validate + stage one file's change on an OPEN connection (no commit).

    The shared per-file core of :func:`stage_tags` and :func:`stage_tags_batch`. Its refusals
    (:func:`_check_stage`) are an unmanaged key, a value holding a NUL, CR or LF, an unknown or
    missing *file_id*, a file holding a staged path change, a file unreadable on disk, a staged
    change an interrupted commit already wrote and recorded nothing for, a file the writer
    would refuse under *droppable_frames*, and a supplied value the container cannot store
    verbatim. Every caller-supplied value is cleaned (:func:`_clean_values`). A *fill_only* key
    is dropped when the file on disk already holds a value for it, and when that leaves no
    caller-supplied key, nothing is staged and ``False`` is returned.

    It then lazily captures the version-0 baseline (or the re-baseline
    :func:`tagmend.engine.versioning.observe_widened_fields` writes), records an external edit
    as a ``scan`` revision (:func:`tagmend.engine.versioning.observe_drift`), merges
    *tags* onto the file's current managed subset (P0: omitted keys are preserved),
    and upserts the staged row with the file's signature as its base, so the commit can refuse
    a file edited since, and with the caller's surviving keys as its ``supplied_keys``.
    Raises :class:`ValueError` naming *file_id* on any refusal. Leaves the transaction for the
    caller to commit or roll back.
    """
    inputs = _check_stage(
        conn,
        file_id=file_id,
        tags=tags,
        droppable_frames=droppable_frames,
        fill_only=fill_only,
    )
    if inputs is None:
        return False
    current = inputs.current

    # Capture v0 now (resume-free model): freeze the true original before any commit. A file
    # whose latest revision predates the current managed set is re-baselined for the same reason.
    # An external edit since the latest revision is observed here, never at commit time, where a
    # re-applied crashed write would read as drift. The commit revision then diffs from it.
    versioning.ensure_baseline(conn, file_id, managed_tags=current, now=now)
    versioning.observe_widened_fields(conn, file_id, managed_tags=current, now=now)
    versioning.observe_drift(conn, file_id, managed_tags=current, now=now)

    # No accidental deletion (P0): merge onto the current managed subset so omitted managed
    # keys are preserved through the commit's delete-on-absent write. The caller's values
    # win, and an explicit empty list still deletes a field.
    target = versioning.managed_subset(current)
    target.update(inputs.remaining)
    changed_fields = versioning.compute_diff(versioning.managed_subset(current), target).keys()

    store.upsert_staged_tag(
        conn,
        file_id=file_id,
        managed_tags=target,
        origin=origin,
        now=now,
        note=note,
        base_size_bytes=inputs.base.st_size,
        base_mtime_ns=inputs.base.st_mtime_ns,
        changed_fields=changed_fields,
        supplied_keys=inputs.remaining.keys(),
    )
    return True


def stage_tags(  # noqa: PLR0913 - cohesive keyword-only staging payload
    settings: Settings,
    *,
    file_id: int,
    tags: dict[str, list[str]],
    origin: str = "manual",
    note: str | None = None,
    fill_only: frozenset[str] = frozenset(),
) -> bool:
    """Record the desired target managed tags for *file_id* (replacing any pending one).

    Validates *origin* (``auto``/``manual``) and refuses everything the shared
    :func:`_stage_one` core refuses. Also captures the version-0 baseline now (read from the
    file on disk) if the file has none, so a later crash-then-rescan can never record the wrong
    original. Nothing on disk changes and no further history is recorded until
    :func:`commit_tags`. Owns its transaction.

    **No accidental deletion (P0).** *tags* is merged *onto* the file's current
    managed subset, so an omitted managed key means "leave it alone", not "delete it":
    staging ``{"genre": [...]}`` on a file rich in title/album/track/MB-id fields cannot
    wipe them at commit time (``commit_tags`` writes the staged target verbatim, and
    :func:`tagmend.engine.tags.write_managed_tags` deletes every managed key *absent* from
    that target). An explicit empty list still deletes a field. Each resolver stages only
    the fields it decides, and :func:`_stage_one` merges them onto the tags read from disk.

    **Value hygiene.** Every supplied value is stripped of leading and trailing whitespace and
    NFC-normalized, and a value left empty is dropped (a list left empty deletes the field). A
    value containing a NUL, CR or LF raises :class:`ValueError` and stages nothing.

    **Blank-fill guard.** A key in *fill_only* is dropped when the file on disk already holds a
    value for it, so a fill aimed by a stale snapshot never overwrites one. Returns ``False``
    when that leaves nothing to stage (no row is written), else ``True``.
    """
    if origin not in _STAGED_ORIGINS:
        message = f"invalid staged origin: {origin!r} (expected auto|manual)"
        raise ValueError(message)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        staged = _stage_one(
            connection,
            file_id=file_id,
            tags=tags,
            origin=origin,
            note=note,
            now=clock.utc_now(),
            droppable_frames=frozenset(settings.id3_droppable_frames),
            fill_only=fill_only,
        )
        connection.commit()
    finally:
        connection.close()

    if staged:
        logger.info("staged tags for file_id=%d (origin=%s)", file_id, origin)
    else:
        logger.info("file_id=%d already holds every fill-only value: nothing staged", file_id)
    return staged


def would_stage(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    file_id: int,
    tags: dict[str, list[str]],
    fill_only: frozenset[str] = frozenset(),
) -> bool:
    """Return whether :func:`stage_tags` would stage *tags* on *file_id*. Writes nothing.

    A resolver's dry run calls it on its own connection, so a file the real run would refuse
    raises the same :class:`ValueError` here instead of counting as staged.
    """
    inputs = _check_stage(
        conn,
        file_id=file_id,
        tags=tags,
        droppable_frames=frozenset(settings.id3_droppable_frames),
        fill_only=fill_only,
    )
    return inputs is not None


def _checked_tags(tags: object) -> dict[str, list[str]]:
    """Return one batch entry's *tags* as a managed-tag map, or raise :class:`ValueError`."""
    if not isinstance(tags, dict):
        message = f"tags must be a dict of name -> list of values, got {type(tags).__name__}"
        raise ValueError(message)  # noqa: TRY004 - batch rejections are uniformly ValueError
    for name, values in tags.items():
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            message = f"tags[{name!r}] must be a list of strings"
            raise ValueError(message)
    return cast("dict[str, list[str]]", tags)


def stage_tags_batch(
    settings: Settings,
    *,
    entries: Sequence[object],
    note: str | None = None,
) -> list[int]:
    """Stage N files' managed-tag changes in ONE connection / ONE transaction (all-or-nothing).

    *entries* is a sequence of ``(file_id, managed_tags)`` tuples. Every entry's SHAPE is
    checked first by :func:`tagmend.engine.validation.validate_file_pairs` (a
    :class:`ValueError` naming the index and what was wrong), then each is validated and staged
    through the SAME :func:`_stage_one` core as :func:`stage_tags`, so single and batch can
    never drift. The ``origin`` is hardcoded ``"manual"`` (this flow never auto-stages, so no
    origin parameter is exposed). A malformed entry, a duplicate ``file_id`` in one batch, or any
    invalid entry raises :class:`ValueError` and NOTHING is staged (the shared transaction is
    rolled back on close). A later :func:`commit_tags` groups the whole batch into ONE
    revertible commit. Returns the staged file ids in input order.

    ``tracknumber``/``discnumber`` values are never parsed or computed. Callers supply the full
    ``"n/total"`` strings.
    """
    validated = validate_file_pairs(entries, value_name="tags", check_value=_checked_tags)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        now = clock.utc_now()
        for file_id, managed_tags in validated:
            _stage_one(
                connection,
                file_id=file_id,
                tags=managed_tags,
                origin="manual",
                note=note,
                now=now,
                droppable_frames=frozenset(settings.id3_droppable_frames),
            )
        connection.commit()
    finally:
        connection.close()

    staged_ids = [file_id for file_id, _ in validated]
    logger.info("staged batch of %d file(s)", len(staged_ids))
    return staged_ids


def _refuse_landed_unstage(conn: sqlite3.Connection, staged: store.StagedTag, path: Path) -> None:
    """Raise :class:`ValueError` when an interrupted commit already wrote *staged* to *path*.

    A file that cannot be read shows no landed write, so its unstage proceeds.
    """
    try:
        signature = path.stat()
        current = read_tags(path).tags
    except (mutagen.MutagenError, OSError):  # type: ignore[attr-defined]
        return
    if _landed_unrecorded(conn, staged, signature, current):
        message = f"file_id={staged.file_id}: {_LANDED_UNRECORDED}"
        raise ValueError(message)


def unstage_tags(settings: Settings, *, file_id: int) -> int:
    """Drop the pending change for *file_id*. Returns the count of rows removed, 1 or 0.

    A known file with nothing staged returns 0, and an unknown *file_id* raises
    :class:`ValueError`, so a typo is never read as "nothing staged". A baseline or re-baseline
    captured at stage time stays (history is proportional to staged intent). It is harmless and
    never re-applied. A staged change an interrupted commit already wrote to the file is refused
    with :class:`ValueError`, since its row is that write's only record until ``commit_tags``.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        file_row = store.get_file_by_id(connection, file_id)
        if file_row is None:
            message = f"unknown file_id={file_id}"
            raise ValueError(message)
        staged = store.get_staged_tag(connection, file_id)
        if staged is not None and not file_row.is_missing:
            _refuse_landed_unstage(connection, staged, Path(file_row.folder) / file_row.filename)
        removed = 0 if staged is None else 1
        if staged is not None:
            store.delete_staged_tag(connection, file_id)
            connection.commit()
    finally:
        connection.close()
    return removed


def _current_managed(
    conn: sqlite3.Connection,
    file_row: store.FileRow,
) -> dict[str, list[str]]:
    """Return the file's current managed tags, from DISK when it can be read.

    The staged target is built from disk, so comparing it against the snapshot mirror would
    render a field the mirror merely lacks as an addition, a review surface inventing changes
    that will not happen. The mirror is the fallback for a file that is gone or unreadable,
    which is the only case where nothing better exists.
    """
    if not file_row.is_missing:
        try:
            return versioning.managed_subset(
                read_tags(Path(file_row.folder) / file_row.filename).tags,
            )
        except (mutagen.MutagenError, OSError):  # type: ignore[attr-defined]
            logger.warning("diff: unreadable file_id=%s, falling back to snapshot", file_row.id)
    return versioning.managed_subset(store.get_tags(conn, file_row.id))


def _stale_identity(
    diff: dict[str, dict[str, list[str]]],
    target: dict[str, list[str]],
    supplied: frozenset[str] | None,
) -> list[dict[str, object]]:
    """Report coupled identity fields this change rewrites one half of.

    A group's name and its MusicBrainz ids describe the same thing, so changing the name while
    keeping the old id leaves the file naming one entity and pointing at another. Only a
    trigger field counts as the change, so a sort-only edit flags nothing. A member in
    *supplied* is skipped: a value the caller wrote, even an unchanged one, is a confirmation.
    """
    confirmed = supplied or frozenset()
    stale: list[dict[str, object]] = []
    for triggers, members in _IDENTITY_GROUPS:
        changed = [field_name for field_name in triggers if field_name in diff]
        if not changed:
            continue
        for field_name in members:
            retained = target.get(field_name)
            if field_name not in diff and field_name not in confirmed and retained:
                stale.append(
                    {
                        "changed": changed[0],
                        "stale_field": field_name,
                        "stale_value": retained,
                    },
                )
    return stale


def diff_tags(
    settings: Settings,
    *,
    path: str | os.PathLike[str] | None = None,
) -> list[TagDiffView]:
    """Return staged-but-uncommitted tag changes enriched with the current→target diff.

    This is ``git diff --staged``. ``current`` is read from the file on disk, the same source
    staging merges onto, so ``diff`` is what the commit will write. The snapshot mirror is the
    fallback only for a file that is gone or unreadable. ``target`` is the staged managed tags and
    ``diff`` is :func:`tagmend.engine.versioning.compute_diff` between them (a no-op stage
    yields ``diff == {}`` but the row still appears). Optionally limited to files in the
    folder *path* or nested under it, resolved by
    :func:`tagmend.engine.path_keys.folder_arg_key` (which raises :class:`ValueError` for a
    folder outside ``music_path``). Owns its transaction (read-only).
    """
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        staged_rows = (
            store.list_staged_tags(connection)
            if root_key is None
            else store.list_staged_tags_under(connection, root_key)
        )
        views: list[TagDiffView] = []
        for staged in staged_rows:
            file_row = store.get_file_by_id(connection, staged.file_id)
            if file_row is None:  # pragma: no cover - staged FK guarantees the file row
                continue
            current = _current_managed(connection, file_row)
            target = staged.managed_tags
            diff = versioning.compute_diff(current, target)
            views.append(
                TagDiffView(
                    file_id=staged.file_id,
                    folder=file_row.folder,
                    filename=file_row.filename,
                    is_missing=file_row.is_missing,
                    origin=staged.origin,
                    note=staged.note,
                    staged_at=staged.staged_at,
                    current=current,
                    target=target,
                    diff=diff,
                    stale_identity=_stale_identity(diff, target, staged.supplied_keys),
                ),
            )
        return views
    finally:
        connection.close()


def _commit_origin(origins: set[str]) -> str:
    """Return ``auto`` only when every swept change came from a resolver, else ``manual``."""
    return "auto" if origins == {"auto"} else "manual"


def commit_tags(
    settings: Settings,
    *,
    message: str | None = None,
    path: str | os.PathLike[str] | None = None,
) -> CommitResult:
    """Apply every currently-staged tag change as one revertible commit and return a summary.

    First flips any lingering ``applying`` commit to ``interrupted`` (crash recovery in
    the resume-free model), then sweeps every still-staged row (optionally limited to the
    folder *path* and every folder nested under it) under a fresh commit and applies them
    file by file via :func:`tagmend.engine.commits.run_commit`. The commit's origin is
    ``auto`` only when every change it sweeps came from a resolver, and ``manual`` otherwise.
    A file gone from disk is flagged missing, its staged row dropped, and reported in
    ``missing_files``. A file edited on disk since it was staged reports
    ``changed_since_stage``, and a file that fails to write reports ``error`` with a
    ``detail``. Both keep their staged row, and the rest of the commit still completes.
    ``commit_id`` is ``None`` when nothing was staged. *path* resolves through
    :func:`tagmend.engine.path_keys.folder_arg_key`. Owns its transaction.
    """
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)

    domain = TagDomain(droppable_frames=frozenset(settings.id3_droppable_frames))
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)

        # Recovery first: any pre-existing 'applying' commit is a crash remnant.
        commits.mark_interrupted(connection)
        connection.commit()

        file_ids = (
            domain.list_staged_file_ids_under(connection, root_key)
            if root_key is not None
            else domain.list_staged_file_ids(connection)
        )
        if not file_ids:
            connection.commit()
            return commits.summarize(commit_id=None, applied=[])

        now = clock.utc_now()
        origin = _commit_origin(store.staged_origins(connection, file_ids))
        commit_id = commits.create_commit(connection, origin=origin, message=message, now=now)
        connection.commit()  # commit row durable before any per-file work

        applied = commits.run_commit(
            connection,
            domain,
            commit_id=commit_id,
            file_ids=file_ids,
        )

        commits.set_commit_status(connection, commit_id, "applied")
        connection.commit()
    finally:
        connection.close()

    result = commits.summarize(commit_id=commit_id, applied=applied)
    logger.info(
        "commit %d: committed=%d noop=%d missing=%d changed_since_stage=%d errors=%d",
        commit_id,
        result.committed,
        result.noop,
        result.missing,
        result.changed_since_stage,
        result.errors,
    )
    return result
