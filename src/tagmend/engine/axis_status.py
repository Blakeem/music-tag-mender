"""The one implementation behind every ``set_<axis>_status`` / ``reset_<axis>_status`` pair.

Parameterised by :class:`tagmend.engine.axis.Axis`. Scope is *file_ids* when given, else every
file carrying *value* in one of the axis's :attr:`~tagmend.engine.axis.Axis.scope_fields`. With
neither the scope is empty, because a status tool never blankets the library: a deleted
``manual`` row has no history to restore it from. An unknown id in *file_ids* raises
:class:`ValueError` naming it.

Each public function owns its connection and commits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from tagmend.engine import axis, clock, db, schema, store
from tagmend.engine.validation import require_choice
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from tagmend.config import Settings

logger = get_logger(__name__)

# A resolver alone decides done and no_match, and reset is the only hand-back of a manual row,
# so manual is the one state a human sets on a tag axis.
_MANUAL_STATUSES: Final = frozenset({"manual"})


def status_scope(
    conn: sqlite3.Connection,
    axis_: axis.Axis,
    *,
    file_ids: list[int] | None,
    value: str | None,
) -> list[int]:
    """Return the file ids a status tool acts on, in ascending id order."""
    if file_ids is None and value is None:
        return []
    return store.files_in_scope(
        conn,
        value_fields=axis_.scope_fields,
        value=value,
        file_ids=file_ids,
    )


def apply_status(
    settings: Settings,
    axis_: axis.Axis,
    *,
    file_ids: list[int] | None,
    value: str | None,
    write: Callable[[sqlite3.Connection, int, str], None],
) -> int:
    """Call ``write(conn, file_id, now)`` for every file in scope, in one transaction.

    Returns the number of files in scope.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        scoped = status_scope(connection, axis_, file_ids=file_ids, value=value)
        now = clock.utc_now()
        for file_id in scoped:
            write(connection, file_id, now)
        connection.commit()
    finally:
        connection.close()
    return len(scoped)


def set_manual_status(
    settings: Settings,
    axis_: axis.Axis,
    *,
    file_ids: list[int] | None,
    value: str | None,
    status: str,
) -> int:
    """Record ``manual`` on the tag *axis_* for every file in scope, snapshotting its tags.

    Raises :class:`ValueError` for any *status* other than ``manual``. Returns the number of
    files affected.
    """
    require_choice("status", status, _MANUAL_STATUSES)

    def write(conn: sqlite3.Connection, file_id: int, now: str) -> None:
        tags = store.get_tags(conn, file_id)
        axis.put_outcome(conn, axis_, file_id=file_id, status="manual", tags=tags, now=now)

    affected = apply_status(settings, axis_, file_ids=file_ids, value=value, write=write)
    logger.info("set %s status=manual for %d file(s)", axis_.name, affected)
    return affected


def reset_status(
    settings: Settings,
    axis_: axis.Axis,
    *,
    file_ids: list[int] | None,
    value: str | None,
) -> int:
    """Delete the *axis_* status row of every file in scope. Returns the number of files."""

    def write(conn: sqlite3.Connection, file_id: int, _now: str) -> None:
        axis.delete_status(conn, axis_, file_id)

    affected = apply_status(settings, axis_, file_ids=file_ids, value=value, write=write)
    logger.info("reset %s status for %d file(s)", axis_.name, affected)
    return affected
