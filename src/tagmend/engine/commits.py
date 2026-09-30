"""Domain-neutral commit core: ``commits``-table ops + the shared crash-safe loop (M3).

This module owns everything about a *commit* that does not depend on whether the change
is a tag edit or a future file move:

* the ``commits`` table data access (``create_commit`` / ``set_commit_status`` /
  ``get_commit`` / ``get_applying_commits`` / ``list_commits`` / ``mark_interrupted``);
* the immutable result dataclasses a commit returns (:class:`CommitResult` and friends);
* the :class:`RevisionDomain` seam plus the one shared :func:`run_commit` loop that
  carries the delicate disk-first / append-then-delete-in-one-tx crash invariant.

It deliberately imports **neither** :mod:`tagmend.engine.staging` **nor**
:mod:`tagmend.engine.versioning` (which import it back), so there is no import cycle: a
concrete domain (e.g. ``staging.TagDomain``) implements the Protocol and is passed in.

Like the rest of the data-access layer, the ``commits``-table functions take an open
connection and **never commit**; the orchestrator owns the transaction. The one
exception is :func:`run_commit`, which owns the per-file ``conn.commit()`` calls because
the crash invariant lives in exactly where those commits fall.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Protocol, SupportsInt, cast

from tagmend.engine import db, schema
from tagmend.engine.validation import check_limit
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from tagmend.config import Settings

logger = get_logger(__name__)


def _as_int(value: object) -> int:
    """Coerce a sqlite-returned ``Any``/``object`` scalar to ``int`` for strict typing."""
    return int(cast("SupportsInt", value))


def _utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    return datetime.now(UTC).isoformat()


# --- commits table ------------------------------------------------------------------

# A commit is ``applying`` until every file it touched has been turned into a revision
# and its staged row deleted, then it flips to ``applied``. A lingering ``applying``
# row is a crash remnant; the next commit flips it to the terminal ``interrupted``.
_COMMIT_STATUSES: Final = frozenset({"applying", "applied", "interrupted"})

_COMMIT_COLUMNS = "id, created_at, origin, message, reverted_from, status"


@dataclass(frozen=True, slots=True)
class Commit:
    """One row from ``commits``: a group of changes applied together."""

    id: int
    created_at: str
    origin: str
    message: str | None
    reverted_from: int | None
    status: str


def _row_to_commit(row: tuple[object, ...]) -> Commit:
    """Build a typed :class:`Commit` from a raw sqlite tuple."""
    return Commit(
        id=_as_int(row[0]),
        created_at=str(row[1]),
        origin=str(row[2]),
        message=None if row[3] is None else str(row[3]),
        reverted_from=None if row[4] is None else _as_int(row[4]),
        status=str(row[5]),
    )


def create_commit(
    conn: sqlite3.Connection,
    *,
    origin: str,
    message: str | None,
    now: str,
    reverted_from: int | None = None,
) -> int:
    """Create a ``commits`` row with ``status='applying'`` and return its id."""
    cursor = conn.execute(
        """
        INSERT INTO commits (created_at, origin, message, reverted_from, status)
        VALUES (?, ?, ?, ?, 'applying')
        """,
        (now, origin, message, reverted_from),
    )
    new_id = cursor.lastrowid
    if new_id is None:  # pragma: no cover - defensive; INTEGER PK always assigns one
        error = "create_commit did not return a row id"
        raise RuntimeError(error)
    return int(new_id)


def set_commit_status(conn: sqlite3.Connection, commit_id: int, status: str) -> None:
    """Update a commit's status. Raises :class:`ValueError` for an unknown status."""
    if status not in _COMMIT_STATUSES:
        message = f"unknown commit status: {status!r}"
        raise ValueError(message)
    conn.execute("UPDATE commits SET status = ? WHERE id = ?", (status, commit_id))


def get_commit(conn: sqlite3.Connection, commit_id: int) -> Commit | None:
    """Return the commit with the given id, or ``None``."""
    cursor = conn.execute(
        f"SELECT {_COMMIT_COLUMNS} FROM commits WHERE id = ?",  # noqa: S608
        (commit_id,),
    )
    row = cursor.fetchone()
    return None if row is None else _row_to_commit(tuple(row))


def get_applying_commits(conn: sqlite3.Connection) -> list[Commit]:
    """Return every commit still in ``applying`` status (interrupted), in id order."""
    cursor = conn.execute(
        f"SELECT {_COMMIT_COLUMNS} FROM commits WHERE status = 'applying' ORDER BY id",  # noqa: S608
    )
    return [_row_to_commit(tuple(row)) for row in cursor.fetchall()]


def list_commits(conn: sqlite3.Connection, *, limit: int | None = None) -> list[Commit]:
    """Return commits newest first, optionally capped at *limit* rows."""
    sql = f"SELECT {_COMMIT_COLUMNS} FROM commits ORDER BY id DESC"  # noqa: S608
    cursor = conn.execute(sql) if limit is None else conn.execute(f"{sql} LIMIT ?", (limit,))
    return [_row_to_commit(tuple(row)) for row in cursor.fetchall()]


def mark_interrupted(conn: sqlite3.Connection) -> int:
    """Flip every lingering ``applying`` commit to ``interrupted``; return the count.

    Single-user model: any commit still ``applying`` at the start of a new commit is a
    crash remnant. Its already-committed files keep their revisions; its leftover staged
    rows stay staged and the new commit sweeps them up. Does not commit.
    """
    cursor = conn.execute(
        "UPDATE commits SET status = 'interrupted' WHERE status = 'applying'",
    )
    return cursor.rowcount


def list_commits_for(settings: Settings, *, limit: int | None = None) -> list[Commit]:
    """Conn-owning :func:`list_commits`: open the ledger and return commits. Read-only.

    Raises :class:`ValueError` for a negative *limit*.
    """
    check_limit(limit)
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        return list_commits(connection, limit=limit)
    finally:
        connection.close()


def get_commit_for(settings: Settings, commit_id: int) -> Commit | None:
    """Conn-owning :func:`get_commit`: open the ledger and return one commit. Read-only."""
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        return get_commit(connection, commit_id)
    finally:
        connection.close()


# --- commit results -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileCommitOutcome:
    """What happened to one file during a commit.

    ``status`` is ``committed``, ``noop``, ``missing``, ``changed_since_stage`` or ``error``.
    ``version`` is the new revision version, ``None`` for every status but ``committed``.
    ``detail`` says why a ``changed_since_stage`` or ``error`` file was left staged.
    """

    file_id: int
    version: int | None
    status: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class MissingFile:
    """A staged file gone from disk at commit time (flagged missing, its row dropped)."""

    file_id: int
    path: str


@dataclass(frozen=True, slots=True)
class CommitResult:
    """Immutable summary of a commit run."""

    commit_id: int | None
    committed: int
    noop: int
    missing: int
    changed_since_stage: int
    errors: int
    outcomes: tuple[FileCommitOutcome, ...]
    missing_files: tuple[MissingFile, ...]

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "commit_id": self.commit_id,
            "committed": self.committed,
            "noop": self.noop,
            "missing": self.missing,
            "changed_since_stage": self.changed_since_stage,
            "errors": self.errors,
            "outcomes": [
                {
                    "file_id": o.file_id,
                    "version": o.version,
                    "status": o.status,
                    "detail": o.detail,
                }
                for o in self.outcomes
            ],
            "missing_files": [{"file_id": m.file_id, "path": m.path} for m in self.missing_files],
        }


@dataclass(frozen=True, slots=True)
class _Applied:
    """Internal per-file result carrying the public outcome plus optional missing info."""

    outcome: FileCommitOutcome
    missing: MissingFile | None


def _summarize(*, commit_id: int | None, applied: list[_Applied]) -> CommitResult:
    """Fold per-file results into the public :class:`CommitResult`."""
    committed = sum(1 for a in applied if a.outcome.status == "committed")
    noop = sum(1 for a in applied if a.outcome.status == "noop")
    missing = sum(1 for a in applied if a.outcome.status == "missing")
    changed_since_stage = sum(1 for a in applied if a.outcome.status == "changed_since_stage")
    errors = sum(1 for a in applied if a.outcome.status == "error")
    return CommitResult(
        commit_id=commit_id,
        committed=committed,
        noop=noop,
        missing=missing,
        changed_since_stage=changed_since_stage,
        errors=errors,
        outcomes=tuple(a.outcome for a in applied),
        missing_files=tuple(a.missing for a in applied if a.missing is not None),
    )


# --- the seam + the one shared loop -------------------------------------------------


class RevisionDomain(Protocol):
    """A domain (tags | paths) the shared commit loop drives, one staged file at a time.

    Concrete, generics-free, SQL-name-free: a staged item is referenced only by its
    ``file_id`` and the domain reads its own payload. Not ``@runtime_checkable`` — it is
    never ``isinstance``-checked, only structurally satisfied by a concrete dataclass.
    """

    @property
    def name(self) -> str:  # 'tags' | 'paths' (logging only)
        """A short domain label used only in log messages."""
        ...

    @property
    def per_file_errors(self) -> tuple[type[Exception], ...]:
        """The exception classes that fail one file and leave the rest of the commit running.

        The domain names them, so this module never imports the disk library that raises them.
        """
        ...

    def list_staged_file_ids(self, conn: sqlite3.Connection) -> list[int]:
        """Return every staged file id, in a stable order."""
        ...

    def list_staged_file_ids_under(self, conn: sqlite3.Connection, root: Path) -> list[int]:
        """Return staged file ids whose file lives at *root* or nested under it."""
        ...

    def plan_order(self, conn: sqlite3.Connection, file_ids: list[int]) -> list[int]:
        """Return the iteration order (possibly reordered/filtered) for *file_ids*.

        Tags = identity; paths = topological move order + collision resolution (§15).
        """
        ...

    def resolve_path(self, conn: sqlite3.Connection, file_id: int) -> Path | None:
        """Return the on-disk path to act on, or ``None`` if the file is gone/unknown."""
        ...

    def changed_since_stage(self, conn: sqlite3.Connection, file_id: int, path: Path) -> bool:
        """Whether the file changed on disk after it was staged, in a way the commit would lose.

        ``False`` when the change already landed on disk (a crash before the DB commit), so
        the next commit still completes it.
        """
        ...

    def apply_to_disk(
        self,
        conn: sqlite3.Connection,
        file_id: int,
        path: Path,
        *,
        commit_id: int,
        now: str,
    ) -> int | None:
        """Do the disk action FIRST, then append the revision + delete the staged row.

        All DB writes are left in the open transaction (the caller commits). Returns the
        new revision version, or ``None`` for a no-op (target already equals current).
        """
        ...

    def flag_and_drop_missing(self, conn: sqlite3.Connection, file_id: int) -> None:
        """Flag the file missing and drop its staged row (it vanished from disk)."""
        ...

    def post_commit_file(self, conn: sqlite3.Connection, file_id: int) -> None:
        """Per-file follow-up after the commit landed (paths: prune empty dir; tags: no-op)."""
        ...


_CHANGED_SINCE_STAGE_DETAIL: Final = (
    "file changed on disk after it was staged. "
    "Re-stage it (stage_tags replaces the pending row) or unstage it."
)


def run_commit(
    conn: sqlite3.Connection,
    domain: RevisionDomain,
    *,
    commit_id: int,
    file_ids: list[int],
) -> list[_Applied]:
    """Apply each staged change for *file_ids* under *commit_id*; return per-file results.

    The one place the crash invariant lives. For each file, in ``plan_order``:

    * a path that is gone -> ``flag_and_drop_missing`` + commit -> a ``missing`` outcome.
    * a file edited on disk since it was staged -> nothing written, the staged row kept -> a
      ``changed_since_stage`` outcome.
    * otherwise the domain writes disk FIRST then appends the revision and deletes the
      staged row, leaving the tx dirty. This function owns the ``conn.commit()`` so a
      revision never becomes durable without its staged row already gone. A crash before
      that commit rolls both back, leaving the staged row for the next commit to re-apply.
    * one of ``domain.per_file_errors`` raised by the check or the apply -> rolled back, the
      staged row kept for a retry -> an ``error`` outcome, and the loop moves on. Any other
      exception is a bug and propagates.
    """
    applied: list[_Applied] = []
    for file_id in domain.plan_order(conn, file_ids):
        path = domain.resolve_path(conn, file_id)

        if path is None or not path.exists():
            domain.flag_and_drop_missing(conn, file_id)
            conn.commit()
            location = str(path) if path is not None else f"file_id={file_id}"
            applied.append(
                _Applied(
                    outcome=FileCommitOutcome(file_id=file_id, version=None, status="missing"),
                    missing=MissingFile(file_id=file_id, path=location),
                ),
            )
            continue

        applied.append(
            _Applied(
                outcome=_commit_one(conn, domain, file_id, path, commit_id=commit_id),
                missing=None,
            ),
        )
    return applied


def _commit_one(
    conn: sqlite3.Connection,
    domain: RevisionDomain,
    file_id: int,
    path: Path,
    *,
    commit_id: int,
) -> FileCommitOutcome:
    """Commit one present file: refuse it if changed since stage, else apply it durably."""
    try:
        if domain.changed_since_stage(conn, file_id, path):
            return FileCommitOutcome(
                file_id=file_id,
                version=None,
                status="changed_since_stage",
                detail=_CHANGED_SINCE_STAGE_DETAIL,
            )
        version = domain.apply_to_disk(conn, file_id, path, commit_id=commit_id, now=_utc_now())
        conn.commit()  # disk already done inside, so append + delete are now durable
    except domain.per_file_errors as exc:
        # Undoes only this file's uncommitted writes, so its staged row stays for a retry.
        conn.rollback()
        logger.warning("commit %d: file_id=%d failed: %s", commit_id, file_id, exc)
        return FileCommitOutcome(file_id=file_id, version=None, status="error", detail=str(exc))

    domain.post_commit_file(conn, file_id)
    conn.commit()
    status = "committed" if version is not None else "noop"
    return FileCommitOutcome(file_id=file_id, version=version, status=status)
