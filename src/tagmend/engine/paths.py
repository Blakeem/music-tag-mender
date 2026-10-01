"""The paths domain: tracked, revertible file moves with one ``files.id`` across every move.

The second :class:`tagmend.engine.commits.RevisionDomain`. A staged move
(``path_revisions_staged``) holds the target relative to ``music_path``, its key and the file's
signature at stage time. :func:`commit_paths` drives :class:`PathDomain` through the shared
crash-safe :func:`tagmend.engine.commits.run_commit`. Each move appends a ``path_revisions`` row,
and a revert stages revert rows and runs the same loop, so a crash during a revert leaves rows
the next :func:`commit_paths` sweeps.

Per file, :meth:`PathDomain.apply_to_disk` repoints the ``files`` row first, inside the open
transaction, so the unique path key refuses a tracked target before the disk is touched. Then it
moves the file with :func:`move_no_clobber`, which never overwrites, and appends the log row.
Nothing is durable until ``run_commit`` commits, after the move. A move that landed before a
crash is recognised by the location probe: the source is gone and the target is present. A
staged row is never deleted while its file sits at its target, so the next commit finishes it.

State transition table of one staged row. Each state is the disk state of the row's source and
target. Every state lists every event.

``at_source``: the source is present, and the target is absent or names the same folder entry.
    commit_paths         moves the file, logs the move, deletes the row
    unstage_paths        deletes the row
    re-stage (batch)     replaces the row, its signature read at the source
    scan_library         reads the source under its id
    revert_commit        refused while any row is staged
    crash in the loop    the move stays on disk, the ledger rolls back: ``landed``

``landed``: the source is gone, the target holds the stage signature.
    commit_paths         logs the move with no disk action, deletes the row
    unstage_paths        refused, naming commit_paths
    re-stage (batch)     the same target refreshes the signature, another target is refused
    scan_library         skips the target and never flags the file missing, so it keeps its id
    revert_commit        refused while any row is staged
    crash in the loop    the ledger rolls back: ``landed``

``landed_changed``: the source is gone, the target holds another signature.
    commit_paths         ``changed_since_stage``, the row stays
    unstage_paths        refused, naming commit_paths
    re-stage (batch)     the same target confirms the file there, even with the gate closed:
                         ``landed``. Another target is refused
    scan_library         as ``landed``
    revert_commit        refused while any row is staged
    crash in the loop    nothing is written: ``landed_changed``

``half_link``: both are present and are the same file, a POSIX move cut after its link.
    commit_paths         unlinks the source, logs the move, deletes the row
    unstage_paths        refused, naming commit_paths
    re-stage (batch)     the same target refreshes the signature, another target is refused
    scan_library         reads the source under its id and skips the target
    revert_commit        refused while any row is staged
    crash in the loop    the unlink stays, the ledger rolls back: ``landed``

``target_taken``: both are present and are different files.
    commit_paths         ``error``, the move refuses the target and the row stays
    unstage_paths        deletes the row
    re-stage (batch)     a free target replaces the row, the same target is held ``occupied``
    scan_library         reads the source under its id and skips the target
    revert_commit        refused while any row is staged
    crash in the loop    nothing moves: ``target_taken``

``gone``: neither is present.
    commit_paths         ``missing``, the file is flagged missing and the row deleted
    unstage_paths        deletes the row
    re-stage (batch)     held ``missing``
    scan_library         never flags the file missing while the row is staged
    revert_commit        refused while any row is staged
    crash in the loop    the flag and the row delete commit together or not at all: ``gone``
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tagmend.engine import clock, commits, db, mismatch, path_keys, schema, store
from tagmend.engine.path_text import clean_value
from tagmend.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tagmend.config import Settings

logger = get_logger(__name__)

# The location probe's six disk states of one staged row (the module docstring's table).
AT_SOURCE: Final = "at_source"
LANDED: Final = "landed"
LANDED_CHANGED: Final = "landed_changed"
HALF_LINK: Final = "half_link"
TARGET_TAKEN: Final = "target_taken"
GONE: Final = "gone"
# The states in which the file sits at its target, so dropping the row would orphan it there.
_AT_TARGET: Final = frozenset({LANDED, LANDED_CHANGED, HALF_LINK})

# The reasons stage_paths_batch refuses an entry.
OCCUPIED: Final = "occupied"
SHARED_TARGET: Final = "shared_target"
TOO_LONG: Final = "too_long"
CUE_REFERENCE: Final = "cue_reference"
STAGED_TAG: Final = "staged_tag"
INVALID_PATH: Final = "invalid_path"
UNKNOWN_FILE: Final = "unknown_file"
MISSING: Final = "missing"
LANDED_MOVE: Final = "landed_move"

_FORWARD_ORIGINS: Final = frozenset({"auto", "manual"})
_REVERT: Final = "revert"
_BASELINE_ORIGIN: Final = "scan"

_RESERVED_NAMES: Final = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    },
)
# Windows limits: NTFS caps one name at 255 UTF-16 units, and MAX_PATH leaves 259 characters.
_MAX_PART_UNITS: Final = 255
_MAX_PATH_CHARS: Final = 259

_PLAYLIST_SUFFIXES: Final = frozenset({".cue", ".m3u", ".m3u8"})
_CUE_FILE_LINE: Final = re.compile(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))\s+\S+\s*$', re.IGNORECASE)

STAGING_NOT_EMPTY: Final = (
    "staging area is not empty - commit or unstage pending changes before reverting. "
    "Run commit_paths to finish an interrupted path revert"
)

_BATCH_ENTRY_WIDTH: Final = 2


# --- the path component rules (pure) --------------------------------------------------


def check_components(relative_path: str) -> list[str]:
    """Return every way *relative_path* breaks the path component rules, empty when it obeys them.

    *relative_path* is relative to ``music_path``. Each part must keep its value under
    :func:`tagmend.engine.path_text.clean_value`, end in no dot or space and name no reserved
    device. A filename is checked whole, extension included, so a title like ``M.I.A.`` keeps
    its dots. The path must be relative and hold no ``..`` part.
    """
    candidate = Path(relative_path)
    if candidate.anchor or not candidate.parts:
        return [f"{relative_path!r} is not a file path relative to music_path"]

    problems: list[str] = []
    for part in candidate.parts:
        if part == "..":
            problems.append("a '..' part leaves its folder")
            continue
        if clean_value(part) != part:
            problems.append(
                f"part {part!r} holds a character a path part cannot hold, edge or repeated "
                "whitespace, or a non-NFC form",
            )
        if part.rstrip(". ") != part:
            problems.append(f"part {part!r} ends with a dot or a space")
        if part.split(".")[0].rstrip(" ").upper() in _RESERVED_NAMES:
            problems.append(f"part {part!r} is a reserved device name")
    return problems


def _utf16_units(text: str) -> int:
    """Return the UTF-16 code units of *text*, the measure NTFS caps a name by."""
    return len(text.encode("utf-16-le")) // 2


def check_length(music_path: Path, relative_path: str) -> list[str]:
    """Return every way *relative_path* under *music_path* breaks the Windows length limits."""
    problems = [
        f"part {part[:40]!r} is {_utf16_units(part)} UTF-16 units, over {_MAX_PART_UNITS}"
        for part in Path(relative_path).parts
        if _utf16_units(part) > _MAX_PART_UNITS
    ]
    full_length = len(str(music_path / relative_path))
    if full_length > _MAX_PATH_CHARS:
        problems.append(f"the full path is {full_length} characters, over {_MAX_PATH_CHARS}")
    return problems


# --- disk probes ----------------------------------------------------------------------


def _present(path: Path) -> bool:
    """Whether the exact name of *path* is an entry of its folder's listing.

    ``exists()`` answers for any casing on NTFS, so before a case-only rename it would call the
    target present.
    """
    try:
        with os.scandir(path.parent) as entries:
            return any(entry.name == path.name for entry in entries)
    except (FileNotFoundError, NotADirectoryError):
        return False


def _listing_holds_key(path: Path) -> bool:
    """Whether any entry of *path*'s folder has the path key of *path*'s name.

    A file where a folder must go blocks the target too, so it counts as holding the key.
    """
    wanted = path_keys.path_key(path.name)
    try:
        with os.scandir(path.parent) as entries:
            return any(path_keys.path_key(entry.name) == wanted for entry in entries)
    except FileNotFoundError:
        return False
    except NotADirectoryError:
        return True


def _signature(path: Path) -> tuple[int, int]:
    """Return *path*'s ``(size, mtime_ns)``, which a same-volume rename keeps."""
    stat_result = path.stat()
    return stat_result.st_size, stat_result.st_mtime_ns


def _existing_folder_name(parent: Path, name: str) -> str | None:
    """Return the name of the folder in *parent* whose key equals *name*'s, or ``None``."""
    wanted = path_keys.path_key(name)
    try:
        with os.scandir(parent) as entries:
            return next(
                (e.name for e in entries if e.is_dir() and path_keys.path_key(e.name) == wanted),
                None,
            )
    except (FileNotFoundError, NotADirectoryError):
        return None


def respell_folders(music_path: Path, relative_path: str) -> str:
    """Return *relative_path* with each existing folder spelled as it is on disk.

    A destination folder whose key equals an existing folder's key reuses that folder's spelling,
    so a move never records a second spelling of one folder. The filename keeps the caller's
    spelling, since a filename case change is a rename the caller asked for.
    """
    parts = Path(relative_path).parts
    spelled: list[str] = []
    parent = music_path
    for part in parts[:-1]:
        name = _existing_folder_name(parent, part) or part
        spelled.append(name)
        parent = parent / name
    return str(Path(*spelled, parts[-1]))


def _case_blind_volume(music_path: Path) -> bool:
    """Whether *music_path*'s volume ignores case while path keys keep it. Stats, writes nothing.

    Path keys fold case only on Windows, so a case-insensitive POSIX mount would let the mover
    write into ``Abba`` while the ledger records ``ABBA``.
    """
    if path_keys.path_key("A") != "A":
        return False
    parts = Path(os.path.normpath(music_path)).parts
    for index in range(len(parts) - 1, 0, -1):
        swapped = parts[index].swapcase()
        if swapped == parts[index]:
            continue
        real = Path(*parts[: index + 1])
        probe = Path(*parts[:index], swapped)
        return real.exists() and probe.exists() and real.samefile(probe)
    return False


def volume_refusal(music_path: Path) -> str | None:
    """Return why the path tools refuse *music_path*'s volume, or ``None`` when they may run."""
    if not _case_blind_volume(music_path):
        return None
    return (
        f"{music_path} sits on a volume that ignores case while TagMend's path keys keep it, so "
        "a move could split one folder into two spellings. The path tools refuse this volume"
    )


def _require_music_path(settings: Settings) -> Path:
    """Return ``music_path``, or raise :class:`ValueError` when it is not configured."""
    if settings.music_path is None:
        message = "music_path not configured. Run `tagmend config-set music_path <dir>`"
        raise ValueError(message)
    return settings.music_path


def _require_volume(music_path: Path) -> None:
    """Raise :class:`ValueError` when the volume check refuses *music_path*."""
    refusal = volume_refusal(music_path)
    if refusal is not None:
        raise ValueError(refusal)


def _relative(music_path: Path, path: Path) -> str:
    """Return *path* relative to *music_path*, or raise :class:`ValueError` when outside it."""
    if not path_keys.is_within(path_keys.path_key(path), path_keys.path_key(music_path)):
        message = f"{path} is outside music_path {music_path}"
        raise ValueError(message)
    depth = len(Path(os.path.normpath(music_path)).parts)
    return str(Path(*Path(os.path.normpath(path)).parts[depth:]))


def _playlist_entries(sheet: Path) -> list[str]:
    """Return the file names a cue sheet's ``FILE`` lines or a playlist's entries name."""
    try:
        raw = sheet.read_bytes()
    except OSError as exc:
        logger.warning("could not read %s: %s", sheet, exc)
        return []
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Cue sheets ripped on Windows are mostly ANSI.
        text = raw.decode("cp1252", errors="replace")
    lines = [line.strip() for line in text.splitlines()]
    if sheet.suffix.lower() != ".cue":
        return [line for line in lines if line and not line.startswith("#")]
    matches = (_CUE_FILE_LINE.match(line) for line in lines)
    return [match.group(1) or match.group(2) for match in matches if match is not None]


def _cue_references(source: Path) -> list[str]:
    """Return the cue sheets and playlists in *source*'s folder that name *source*."""
    folder = source.parent
    wanted = path_keys.path_key(source)
    try:
        with os.scandir(folder) as entries:
            sheets = [
                Path(e.path)
                for e in entries
                if e.is_file() and Path(e.name).suffix.lower() in _PLAYLIST_SUFFIXES
            ]
    except OSError:
        return []
    return sorted(
        sheet.name
        for sheet in sheets
        if any(path_keys.path_key(folder / name) == wanted for name in _playlist_entries(sheet))
    )


def _one_entry(source: Path, target: Path) -> bool:
    """Whether two present names reach one folder entry, as two folder spellings do on NTFS.

    ``samefile`` alone reads such a pair as a half link, whose finish would unlink the only copy.
    """
    return source.name == target.name and source.parent.samefile(target.parent)


def _locate(source: Path, target: Path, staged: store.StagedPath) -> str:
    """Return the disk state of one staged row (the module docstring's table)."""
    source_here = _present(source)
    target_here = _present(target)
    if source_here and target_here:
        if _one_entry(source, target):
            return AT_SOURCE
        return HALF_LINK if source.samefile(target) else TARGET_TAKEN
    if source_here:
        return AT_SOURCE
    if not target_here:
        return GONE
    if _signature(target) == (staged.base_size_bytes, staged.base_mtime_ns):
        return LANDED
    return LANDED_CHANGED


# --- the disk mover and the pruner ----------------------------------------------------


def move_no_clobber(source: Path, target: Path) -> None:
    """Move *source* to *target*, creating the target's folders, and never overwrite a file.

    Windows ``rename`` refuses an existing target. POSIX ``rename`` overwrites one silently, so
    there the move is a hard link, which refuses an existing target, then an unlink.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        source.rename(target)
        return
    target.hardlink_to(source)
    source.unlink()


def _finish_half_link(source: Path) -> None:
    """Remove the source name of a file a cut POSIX move already linked at its target."""
    source.unlink()


def _prune(music_path: Path, folder: Path) -> list[Path]:
    """Remove *folder* and each parent it empties, below ``music_path``, until one is refused.

    ``rmdir`` refuses a folder holding anything, a hidden file included, so only empty folders
    go and nothing is deleted.
    """
    root_key = path_keys.path_key(music_path)
    removed: list[Path] = []
    current = folder
    while True:
        key = path_keys.path_key(current)
        if key == root_key or not path_keys.is_within(key, root_key):
            return removed
        try:
            current.rmdir()
        except OSError:
            return removed
        removed.append(current)
        current = current.parent


# --- the RevisionDomain -----------------------------------------------------------------


def _require_staged(conn: sqlite3.Connection, file_id: int) -> store.StagedPath:
    """Return the staged move the commit loop is working on, which must exist."""
    staged = store.get_staged_path(conn, file_id)
    if staged is None:  # pragma: no cover - defensive, file_id came from the staged list
        message = f"staged path row vanished for file_id={file_id}"
        raise RuntimeError(message)
    return staged


def _source_of(conn: sqlite3.Connection, file_id: int) -> Path:
    """Return the path the ``files`` row records for *file_id*, which must exist."""
    row = store.get_file_by_id(conn, file_id)
    if row is None:  # pragma: no cover - defensive, the staged row's foreign key holds it
        message = f"file row vanished for file_id={file_id}"
        raise RuntimeError(message)
    return Path(row.folder) / row.filename


def _next_path_version(conn: sqlite3.Connection, file_id: int) -> int:
    """Return the version the next ``path_revisions`` row of *file_id* takes."""
    revisions = store.get_path_revisions(conn, file_id)
    return revisions[-1].version + 1 if revisions else 0


@dataclass(frozen=True, slots=True)
class PathDomain:
    """The paths :class:`tagmend.engine.commits.RevisionDomain` driven by ``run_commit``.

    Stateless apart from ``music_path``, which every stored path is relative to.
    """

    music_path: Path
    name: str = "paths"

    @property
    def per_file_errors(self) -> tuple[type[Exception], ...]:
        """A refused move, a vanished source or a tracked target fails one file alone."""
        return (OSError, ValueError, sqlite3.IntegrityError)

    def list_staged_file_ids(self, conn: sqlite3.Connection) -> list[int]:
        """Return every staged file id, in file_id order."""
        return [staged.file_id for staged in store.list_staged_paths(conn)]

    def list_staged_file_ids_under(self, conn: sqlite3.Connection, root_key: str) -> list[int]:
        """Return staged file ids whose file's recorded source is under *root_key*."""
        return [staged.file_id for staged in store.list_staged_paths_under(conn, root_key)]

    def plan_order(self, conn: sqlite3.Connection, file_ids: list[int]) -> list[int]:  # noqa: ARG002
        """Return *file_ids* unchanged: staging refuses a target another tracked file holds."""
        return file_ids

    def resolve_path(self, conn: sqlite3.Connection, file_id: int) -> Path | None:
        """Return where the file is: its source, its target when the move landed, else ``None``."""
        staged = _require_staged(conn, file_id)
        source = _source_of(conn, file_id)
        target = self.music_path / staged.to_path
        state = _locate(source, target, staged)
        if state == GONE:
            return None
        return target if state in {LANDED, LANDED_CHANGED} else source

    def changed_since_stage(self, conn: sqlite3.Connection, file_id: int, path: Path) -> bool:
        """Whether the file at *path* no longer carries the signature it had when staged."""
        staged = _require_staged(conn, file_id)
        if staged.base_size_bytes is None or staged.base_mtime_ns is None:
            return False
        return _signature(path) != (staged.base_size_bytes, staged.base_mtime_ns)

    def apply_to_disk(
        self,
        conn: sqlite3.Connection,
        file_id: int,
        path: Path,
        *,
        commit_id: int,
        now: str,
    ) -> int | None:
        """Repoint the row, move the file, then append the log row and delete the staged row.

        All three writes stay in the open transaction. The row update runs first so the unique
        path key refuses a tracked target before the disk is touched. When the log append fails
        after a move this call made, the file moves back before the error propagates.
        """
        staged = _require_staged(conn, file_id)
        source = _source_of(conn, file_id)
        target = self.music_path / staged.to_path
        from_path = _relative(self.music_path, source)

        store.relocate_file(conn, file_id, folder=str(target.parent), filename=target.name, now=now)
        moved = _move(source, target, resolved=path)
        try:
            version = _next_path_version(conn, file_id)
            store.insert_path_revision(
                conn,
                file_id=file_id,
                version=version,
                commit_id=commit_id,
                origin=staged.origin,
                from_path=from_path,
                to_path=staged.to_path,
                now=now,
                reverted_from=staged.reverted_from,
                note=staged.note,
            )
            store.delete_staged_path(conn, file_id)
        except self.per_file_errors:
            if moved:
                _move_back(target, source)
            raise
        return version

    def flag_and_drop_missing(self, conn: sqlite3.Connection, file_id: int) -> None:
        """Flag the file missing and drop its staged row: neither its source nor target is here."""
        store.flag_missing(conn, file_id, clock.utc_now())
        store.delete_staged_path(conn, file_id)

    def post_commit_file(self, conn: sqlite3.Connection, file_id: int) -> None:
        """Prune the folder the move vacated, and each parent it empties, below ``music_path``."""
        revisions = store.get_path_revisions(conn, file_id)
        if not revisions:  # pragma: no cover - defensive, the commit just appended one
            return
        latest = revisions[-1]
        vacated = (self.music_path / latest.from_path).parent
        if path_keys.path_key(vacated) == path_keys.path_key(
            (self.music_path / latest.to_path).parent,
        ):
            return
        removed = _prune(self.music_path, vacated)
        if removed:
            logger.info("pruned %d emptied folder(s) below %s", len(removed), vacated)


def _move(source: Path, target: Path, *, resolved: Path) -> bool:
    """Bring the file to *target* from wherever the probe *resolved* it. True when this moved it.

    Compared as strings: Windows path equality ignores case, which would read a case-only
    rename's source as its target.
    """
    if str(resolved) == str(target):
        return False
    if _present(target) and not _one_entry(source, target) and source.samefile(target):
        _finish_half_link(source)
        return False
    move_no_clobber(source, target)
    return True


def _move_back(target: Path, source: Path) -> None:
    """Return a moved file to its source after the ledger refused the move. Logs a failure.

    A file the move-back leaves at its target keeps its staged row, so the next commit
    finishes the move.
    """
    try:
        move_no_clobber(target, source)
    except OSError:
        logger.exception("could not move %s back to %s", target, source)


# --- staging --------------------------------------------------------------------------


def _validate_batch_entries(entries: Sequence[object]) -> list[tuple[int, str]]:
    """Narrow an untyped *entries* sequence to ``(file_id, to_path)`` pairs, or raise."""
    validated: list[tuple[int, str]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, tuple) or len(entry) != _BATCH_ENTRY_WIDTH:
            message = f"entry {index}: expected a (file_id, to_path) pair"
            raise ValueError(message)
        file_id, to_path = entry
        if not isinstance(file_id, int) or isinstance(file_id, bool):
            message = f"entry {index}: file_id must be an integer, got {type(file_id).__name__}"
            raise ValueError(message)  # noqa: TRY004 - batch rejections are uniformly ValueError
        if not isinstance(to_path, str):
            message = f"entry {index} (file_id={file_id}): to_path must be a string"
            raise ValueError(message)  # noqa: TRY004 - batch rejections are uniformly ValueError
        validated.append((file_id, to_path))
    seen: set[int] = set()
    for file_id, _ in validated:
        if file_id in seen:
            message = f"duplicate file_id={file_id} in batch"
            raise ValueError(message)
        seen.add(file_id)
    return validated


def _batch_relative(music_path: Path, to_path: str) -> str | None:
    """Return *to_path* relative to *music_path*. ``None`` for an absolute path outside it."""
    candidate = Path(to_path)
    if not candidate.is_absolute():
        return os.path.normpath(to_path)
    try:
        return _relative(music_path, candidate)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class _Move:
    """One validated batch entry, ready to stage."""

    file_id: int
    from_path: str
    to_path: str
    to_key: str
    base_size_bytes: int
    base_mtime_ns: int


@dataclass(frozen=True, slots=True)
class _EntryPlan:
    """One batch entry's verdict: the reasons it is held, or the move to stage.

    ``confirming`` marks a move that only confirms the file already found at its staged target.
    """

    index: int
    file_id: int
    to_key: str | None
    reasons: tuple[tuple[str, str], ...]
    move: _Move | None
    confirming: bool = False


@dataclass(frozen=True, slots=True)
class _EntryInputs:
    """One batch entry's file row, its source, both paths relative to ``music_path``."""

    row: store.FileRow
    source: Path
    from_path: str
    relative: str


def _entry_inputs(
    conn: sqlite3.Connection,
    music_path: Path,
    file_id: int,
    raw_to_path: str,
) -> _EntryInputs | tuple[str, str]:
    """Return one batch entry's inputs, or the one reason it is held before its target is read."""
    row = store.get_file_by_id(conn, file_id)
    if row is None:
        return UNKNOWN_FILE, "no file has this id"
    if row.is_missing:
        return MISSING, "the last scan found it missing. Rescan first"
    relative = _batch_relative(music_path, raw_to_path)
    if relative is None:
        return INVALID_PATH, f"{raw_to_path} is outside music_path"
    source = Path(row.folder) / row.filename
    try:
        from_path = _relative(music_path, source)
    except ValueError as exc:
        return INVALID_PATH, str(exc)
    return _EntryInputs(row=row, source=source, from_path=from_path, relative=relative)


def _path_problems(music_path: Path, relative: str, ext: str) -> list[tuple[str, str]]:
    """Return the ``invalid_path`` and ``too_long`` reasons of a batch target."""
    reasons = [(INVALID_PATH, problem) for problem in check_components(relative)]
    if not reasons and Path(relative).suffix.lower() != ext:
        reasons.append((INVALID_PATH, f"{relative} does not keep the file's extension {ext}"))
    reasons.extend((TOO_LONG, problem) for problem in check_length(music_path, relative))
    return reasons


def _landed_conflict(
    conn: sqlite3.Connection,
    music_path: Path,
    source: Path,
    to_path: str,
    file_id: int,
) -> tuple[bool, tuple[str, str] | None]:
    """Return whether this entry confirms a landed move, and the reason it is held, if any.

    A file already at its staged target keeps its row until a commit logs the move, so only a
    re-stage to the same target, which confirms the file found there, may replace that row. The
    probe matches exact names, so another casing of the target would read the file as gone.
    """
    existing = store.get_staged_path(conn, file_id)
    if existing is None:
        return False, None
    if _locate(source, music_path / existing.to_path, existing) not in _AT_TARGET:
        return False, None
    if existing.to_path == to_path:
        return True, None
    detail = f"its staged move to {existing.to_path} already landed on disk. Run commit_paths first"
    return False, (LANDED_MOVE, detail)


def _target_reasons(  # noqa: PLR0913 - cohesive keyword-only target-safety inputs
    conn: sqlite3.Connection,
    *,
    file_id: int,
    source: Path,
    target: Path,
    to_path: str,
    to_key: str,
    confirming: bool,
) -> list[tuple[str, str]]:
    """Return the ``occupied``, ``shared_target`` and ``cue_reference`` reasons of one entry.

    The file's own current key never occupies its target, so a case-only rename is not held by
    its own folder entry.
    """
    reasons: list[tuple[str, str]] = []
    target_key = path_keys.path_key(target)
    holder = store.file_id_at_key(conn, target_key)
    if holder is not None and holder != file_id:
        reasons.append((OCCUPIED, f"file_id={holder} is tracked at {to_path}"))
    elif not confirming and target_key != path_keys.path_key(source) and _listing_holds_key(target):
        reasons.append((OCCUPIED, f"{to_path} is taken on disk"))
    claimant = store.staged_file_id_at_target(conn, to_key)
    if claimant is not None and claimant != file_id:
        reasons.append((SHARED_TARGET, f"file_id={claimant} is already staged to {to_path}"))
    sheets = _cue_references(source)
    if sheets:
        detail = f"{', '.join(sheets)} in its folder names it. Edit or move the sheet first"
        reasons.append((CUE_REFERENCE, detail))
    return reasons


def _plan_entry(
    conn: sqlite3.Connection,
    music_path: Path,
    index: int,
    entry: tuple[int, str],
) -> _EntryPlan:
    """Validate one batch entry against every destination-safety hold."""
    file_id, raw_to_path = entry
    inputs = _entry_inputs(conn, music_path, file_id, raw_to_path)
    if isinstance(inputs, tuple):
        return _EntryPlan(index, file_id, None, (inputs,), None)
    source = inputs.source
    from_path = inputs.from_path

    reasons = _path_problems(music_path, inputs.relative, inputs.row.ext)
    if store.get_staged_tag(conn, file_id) is not None:
        reasons.append((STAGED_TAG, "it has a staged tag change. Run commit_tags or unstage_tags"))
    # A target the rules refuse, or one too long to list reliably, is never probed on disk.
    if any(reason in {INVALID_PATH, TOO_LONG} for reason, _ in reasons):
        return _EntryPlan(index, file_id, None, tuple(reasons), None)

    to_path = respell_folders(music_path, inputs.relative)
    to_key = path_keys.path_key(to_path)
    target = music_path / to_path
    # A key match passes only as a filename case change, since another folder spelling names
    # the file's own entry on NTFS.
    if to_key == path_keys.path_key(from_path) and Path(to_path).name == source.name:
        reasons.append((INVALID_PATH, f"the file already sits at {to_path}"))
    confirming, landed = _landed_conflict(conn, music_path, source, to_path, file_id)
    source_here = _present(source)
    if landed is not None:
        reasons.append(landed)
    elif not confirming and not source_here:
        reasons.append((MISSING, f"{from_path} is not on disk. Rescan first"))
    reasons.extend(
        _target_reasons(
            conn,
            file_id=file_id,
            source=source,
            target=target,
            to_path=to_path,
            to_key=to_key,
            confirming=confirming,
        ),
    )
    if reasons:
        return _EntryPlan(index, file_id, to_key, tuple(reasons), None)

    size, mtime_ns = _signature(source if source_here else target)
    move = _Move(file_id, from_path, to_path, to_key, size, mtime_ns)
    return _EntryPlan(index, file_id, to_key, (), move, confirming=confirming)


def _shared_in_call(plans: list[_EntryPlan]) -> list[_EntryPlan]:
    """Return *plans* with ``shared_target`` added to every entry whose key another entry shares."""
    counts: dict[str, int] = {}
    for plan in plans:
        if plan.to_key is not None:
            counts[plan.to_key] = counts.get(plan.to_key, 0) + 1
    shared: list[_EntryPlan] = []
    for plan in plans:
        if plan.to_key is None or counts[plan.to_key] == 1:
            shared.append(plan)
            continue
        reason = (SHARED_TARGET, "another entry of this call targets the same path")
        shared.append(
            _EntryPlan(plan.index, plan.file_id, plan.to_key, (*plan.reasons, reason), None)
        )
    return shared


def _ensure_path_baseline(conn: sqlite3.Connection, file_id: int, from_path: str, now: str) -> None:
    """Append the version-0 location of a file that has no path history yet."""
    if store.get_path_revisions(conn, file_id):
        return
    store.insert_path_revision(
        conn,
        file_id=file_id,
        version=0,
        commit_id=None,
        origin=_BASELINE_ORIGIN,
        from_path=from_path,
        to_path=from_path,
        now=now,
    )


def _require_open_gate(settings: Settings) -> None:
    """Raise :class:`ValueError` while :func:`tagmend.engine.mismatch.gate_state` is closed."""
    gate = mismatch.gate_state(settings)
    if gate.open:
        return
    message = (
        f"the path gate is closed: detect_mismatches flags {gate.flagged} file(s) and "
        f"{gate.exceptions_undecided} exception(s) are undecided. Fix their tags or record a "
        "decision with set_mismatch_status until detect_mismatches reads gate_open: true. "
        "Nothing was staged"
    )
    raise ValueError(message)


def stage_paths_batch(
    settings: Settings,
    *,
    entries: Sequence[object],
    note: str | None = None,
) -> list[int]:
    """Stage explicit moves for many files in ONE transaction, all or nothing, origin ``manual``.

    *entries* is a sequence of ``(file_id, to_path)`` pairs, *to_path* relative to
    ``music_path`` or absolute under it. A target folder whose key matches an existing folder
    reuses its on-disk spelling (:func:`respell_folders`). The call is refused whole while the
    mismatch gate is closed (:func:`tagmend.engine.mismatch.gate_state`), unless every entry
    confirms a landed move at its exact staged target, while the volume check refuses
    ``music_path``, and when any entry is held. Every held entry is listed with
    its reasons: ``occupied``, ``shared_target``, ``too_long``, ``cue_reference``,
    ``staged_tag``, ``invalid_path``, ``unknown_file``, ``missing`` and ``landed_move``. A file
    staged before keeps its version-0 location, captured at its first stage. Returns the staged
    file ids in input order. Raises :class:`ValueError` on any refusal, and nothing is staged.
    """
    validated = _validate_batch_entries(entries)
    music_path = _require_music_path(settings)
    _require_volume(music_path)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        plans = _shared_in_call(
            [
                _plan_entry(connection, music_path, index, entry)
                for index, entry in enumerate(validated)
            ],
        )
        held = [
            f"entry {plan.index} (file_id={plan.file_id}): {reason}: {detail}"
            for plan in plans
            for reason, detail in plan.reasons
        ]
        if held:
            message = "stage_paths_batch staged nothing. Held entries:\n" + "\n".join(held)
            raise ValueError(message)
        # A confirmation decides no new path, and it is the only exit of a landed move whose
        # file closes the gate at its recorded folder.
        confirming_only = bool(plans) and all(plan.confirming for plan in plans)
        if not confirming_only:
            _require_open_gate(settings)
        now = clock.utc_now()
        for plan in plans:
            move = plan.move
            if move is None:  # pragma: no cover - defensive, a plan with no reason holds a move
                continue
            _ensure_path_baseline(connection, move.file_id, move.from_path, now)
            store.upsert_staged_path(
                connection,
                store.StagedPath(
                    file_id=move.file_id,
                    to_path=move.to_path,
                    to_key=move.to_key,
                    origin="manual",
                    note=note,
                    staged_at=now,
                    base_size_bytes=move.base_size_bytes,
                    base_mtime_ns=move.base_mtime_ns,
                    reverted_from=None,
                ),
            )
        connection.commit()
    finally:
        connection.close()

    staged_ids = [file_id for file_id, _ in validated]
    logger.info("staged %d path move(s)", len(staged_ids))
    return staged_ids


def _staged_in_scope(
    conn: sqlite3.Connection,
    root_key: str | None,
) -> list[store.StagedPath]:
    """Return the staged moves under *root_key*, or every staged move when it is ``None``."""
    if root_key is None:
        return store.list_staged_paths(conn)
    return store.list_staged_paths_under(conn, root_key)


def unstage_paths(
    settings: Settings,
    *,
    file_id: int | None = None,
    path: str | os.PathLike[str] | None = None,
) -> int:
    """Drop staged moves, by *file_id* or for every file under the folder *path*. Returns the count.

    Pass exactly one argument. *path* matches where a file sits now, which is its source while
    its move is staged. The call is refused whole, naming the file ids, when any matched file
    already sits at its target, since only a commit may finish that move. An unknown *file_id*
    raises :class:`ValueError`.
    """
    if (file_id is None) == (path is None):
        message = "pass exactly one of file_id and path"
        raise ValueError(message)
    music_path = _require_music_path(settings)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        if file_id is not None:
            if store.get_file_by_id(connection, file_id) is None:
                message = f"unknown file_id={file_id}"
                raise ValueError(message)
            existing = store.get_staged_path(connection, file_id)
            rows = [] if existing is None else [existing]
        else:
            rows = _staged_in_scope(connection, root_key)
        landed = [
            row.file_id
            for row in rows
            if _locate(_source_of(connection, row.file_id), music_path / row.to_path, row)
            in _AT_TARGET
        ]
        if landed:
            message = (
                f"file_id(s) {landed} already sit at their staged target on disk. Run "
                "commit_paths to finish those moves. Nothing was unstaged"
            )
            raise ValueError(message)
        for row in rows:
            store.delete_staged_path(connection, row.file_id)
        connection.commit()
    finally:
        connection.close()
    return len(rows)


@dataclass(frozen=True, slots=True)
class PathDiffView:
    """One staged move as ``diff_paths`` shows it. Both paths are relative to ``music_path``.

    ``state`` is the row's disk state, one of the module docstring's six.
    """

    file_id: int
    from_path: str
    to_path: str
    origin: str
    note: str | None
    staged_at: str
    state: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "file_id": self.file_id,
            "from_path": self.from_path,
            "to_path": self.to_path,
            "origin": self.origin,
            "note": self.note,
            "staged_at": self.staged_at,
            "state": self.state,
        }


def diff_paths(
    settings: Settings,
    *,
    path: str | os.PathLike[str] | None = None,
) -> list[PathDiffView]:
    """Return every staged move, or those under the folder *path*, with its disk state.

    Read-only. ``state`` names the row's place in the module docstring's table.
    """
    music_path = _require_music_path(settings)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        views: list[PathDiffView] = []
        for row in _staged_in_scope(connection, root_key):
            source = _source_of(connection, row.file_id)
            views.append(
                PathDiffView(
                    file_id=row.file_id,
                    from_path=_relative(music_path, source),
                    to_path=row.to_path,
                    origin=row.origin,
                    note=row.note,
                    staged_at=row.staged_at,
                    state=_locate(source, music_path / row.to_path, row),
                ),
            )
        return views
    finally:
        connection.close()


# --- commit ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PathProblem:
    """One staged move a commit left unfinished, with what to do about it."""

    file_id: int
    status: str
    to_path: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "file_id": self.file_id,
            "status": self.status,
            "to_path": self.to_path,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class PathCommitResult:
    """The summary of one :func:`commit_paths` call. ``problems`` lists every unfinished move."""

    commit_id: int | None
    committed: int
    noop: int
    missing: int
    changed_since_stage: int
    errors: int
    problems: tuple[PathProblem, ...]

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "commit_id": self.commit_id,
            "committed": self.committed,
            "noop": self.noop,
            "missing": self.missing,
            "changed_since_stage": self.changed_since_stage,
            "errors": self.errors,
            "problems": [problem.to_dict() for problem in self.problems],
        }


_PROBLEM_DETAILS: Final = {
    "missing": (
        "neither its source nor its target is on disk, so it was flagged missing and its row "
        "dropped. Run scan_library"
    ),
    "changed_since_stage": (
        "the file changed on disk after it was staged, and its row stays. Re-stage it with "
        "stage_paths_batch (to the same to_path when its move already landed) to confirm it, or "
        "run unstage_paths when it still sits at its source"
    ),
}


def _problem(outcome: commits.FileCommitOutcome, to_path: str) -> PathProblem:
    """Return the problem row of one unfinished commit outcome."""
    detail = _PROBLEM_DETAILS.get(outcome.status)
    if detail is None:
        detail = (
            f"{outcome.detail or 'the move failed'}. Its row stays staged. Fix the cause and run "
            "commit_paths again, or run unstage_paths"
        )
    return PathProblem(
        file_id=outcome.file_id, status=outcome.status, to_path=to_path, detail=detail
    )


def _path_commit_result(
    commit_id: int | None,
    summary: commits.CommitResult,
    targets: dict[int, str],
) -> PathCommitResult:
    """Fold a :class:`tagmend.engine.commits.CommitResult` into the paths summary."""
    return PathCommitResult(
        commit_id=commit_id,
        committed=summary.committed,
        noop=summary.noop,
        missing=summary.missing,
        changed_since_stage=summary.changed_since_stage,
        errors=summary.errors,
        problems=tuple(
            _problem(outcome, targets[outcome.file_id])
            for outcome in summary.outcomes
            if outcome.status not in {"committed", "noop"}
        ),
    )


def _commit_origin(origins: set[str]) -> str:
    """Return the origin every swept row shares among ``auto`` and ``revert``, else ``manual``."""
    if origins in ({"auto"}, {_REVERT}):
        return next(iter(origins))
    return "manual"


def _refuse_flagged(
    conn: sqlite3.Connection,
    settings: Settings,
    music_path: Path,
    staged: list[store.StagedPath],
) -> None:
    """Refuse the commit when a forward move's file flags with no decision covering the name.

    Revert rows restore a recorded location, so they skip the check and a crashed revert always
    commits. A file already at its target skips it too: no tool drops that row, and its
    recorded folder lost the siblings that moved before the crash.
    """
    forward = [
        row.file_id
        for row in staged
        if row.origin in _FORWARD_ORIGINS
        and _locate(_source_of(conn, row.file_id), music_path / row.to_path, row) not in _AT_TARGET
    ]
    flagged = mismatch.check_files(conn, settings, forward)
    if flagged:
        message = (
            f"detect_mismatches flags these staged files at their current path with no decision "
            f"covering the name: {flagged}. Fix their tags or record a decision with "
            "set_mismatch_status, or drop them with unstage_paths(file_id=...). Nothing moved"
        )
        raise ValueError(message)


def commit_paths(
    settings: Settings,
    *,
    path: str | os.PathLike[str] | None = None,
    message: str | None = None,
) -> PathCommitResult:
    """Apply every staged move, or those under the folder *path*, as one revertible commit.

    Any commit left ``applying`` is marked ``interrupted`` first, and its leftover rows are
    swept into this one. Before anything moves, every forward row (origin ``auto`` or
    ``manual``) whose file has not reached its target passes
    :func:`tagmend.engine.mismatch.check_files` at its recorded location, else the call is
    refused whole. Each move then runs through
    :func:`tagmend.engine.commits.run_commit` with :class:`PathDomain`, and the folders it
    empties are pruned. A move left unfinished keeps its row, except a ``missing`` one, and is
    listed under ``problems``. ``commit_id`` is ``None`` when nothing was staged. Raises
    :class:`ValueError` when ``music_path`` is unset or the volume check refuses it.
    """
    music_path = _require_music_path(settings)
    _require_volume(music_path)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)
    domain = PathDomain(music_path=music_path)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        commits.mark_interrupted(connection)
        connection.commit()

        staged = _staged_in_scope(connection, root_key)
        if not staged:
            return _path_commit_result(None, commits.summarize(commit_id=None, applied=[]), {})
        _refuse_flagged(connection, settings, music_path, staged)

        commit_id = commits.create_commit(
            connection,
            origin=_commit_origin({row.origin for row in staged}),
            message=message,
            now=clock.utc_now(),
        )
        connection.commit()  # commit row durable before any per-file work
        applied = commits.run_commit(
            connection,
            domain,
            commit_id=commit_id,
            file_ids=[row.file_id for row in staged],
        )
        commits.set_commit_status(connection, commit_id, "applied")
        connection.commit()
    finally:
        connection.close()

    result = _path_commit_result(
        commit_id,
        commits.summarize(commit_id=commit_id, applied=applied),
        {row.file_id: row.to_path for row in staged},
    )
    logger.info(
        "path commit %d: committed=%d missing=%d changed_since_stage=%d errors=%d",
        commit_id,
        result.committed,
        result.missing,
        result.changed_since_stage,
        result.errors,
    )
    return result


# --- history and revert ---------------------------------------------------------------


def history_paths(settings: Settings, file_id: int) -> list[store.PathRevision]:
    """Return every location *file_id* has had, oldest (version 0) first. Read-only.

    A file never staged for a move has no rows. Raises :class:`ValueError` for an unknown
    *file_id*.
    """
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        if store.get_file_by_id(connection, file_id) is None:
            message = f"unknown file_id={file_id}"
            raise ValueError(message)
        return store.get_path_revisions(connection, file_id)
    finally:
        connection.close()


def _target_problem(
    conn: sqlite3.Connection,
    file_id: int,
    source: Path,
    destination: Path,
) -> str | None:
    """Return why a revert cannot move the file at *source* to *destination*, or ``None``."""
    if _present(destination):
        return f"{destination} is already on disk"
    destination_key = path_keys.path_key(destination)
    holder = store.file_id_at_key(conn, destination_key)
    if holder is not None and holder != file_id:
        return f"file_id={holder} is tracked at {destination}"
    if destination_key != path_keys.path_key(source) and _listing_holds_key(destination):
        return f"{destination} is taken on disk"
    return None


def _stage_revert_row(  # noqa: PLR0913 - cohesive keyword-only revert-row payload
    conn: sqlite3.Connection,
    music_path: Path,
    *,
    file_id: int,
    source: Path,
    to_path: str,
    restores: int,
    note: str | None,
    now: str,
) -> None:
    """Stage the move of the file at *source* back to the recorded *to_path*, origin ``revert``."""
    respelled = respell_folders(music_path, to_path)
    size, mtime_ns = _signature(source)
    store.upsert_staged_path(
        conn,
        store.StagedPath(
            file_id=file_id,
            to_path=respelled,
            to_key=path_keys.path_key(respelled),
            origin=_REVERT,
            note=note,
            staged_at=now,
            base_size_bytes=size,
            base_mtime_ns=mtime_ns,
            reverted_from=restores,
        ),
    )


@dataclass(frozen=True, slots=True)
class PathRevertResult:
    """One :func:`revert_paths` call. ``status`` is ``reverted`` or why the move did not finish."""

    file_id: int
    target_version: int
    new_version: int | None
    commit_id: int | None
    to_path: str
    status: str
    detail: str | None
    dry_run: bool

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "file_id": self.file_id,
            "target_version": self.target_version,
            "new_version": self.new_version,
            "commit_id": self.commit_id,
            "to_path": self.to_path,
            "status": self.status,
            "detail": self.detail,
            "dry_run": self.dry_run,
        }


def _require_revert_source(
    conn: sqlite3.Connection,
    music_path: Path,
    file_id: int,
    revisions: list[store.PathRevision],
) -> Path:
    """Return where *file_id* sits, refusing a missing file or one moved outside TagMend."""
    row = store.get_file_by_id(conn, file_id)
    if row is None:
        message = f"unknown file_id={file_id}"
        raise ValueError(message)
    source = Path(row.folder) / row.filename
    if row.is_missing or not _present(source):
        message = f"file_id={file_id} is missing from disk. Run scan_library"
        raise ValueError(message)
    if not revisions:
        message = f"file_id={file_id} has no path history. Read history_paths"
        raise ValueError(message)
    if path_keys.path_key(_relative(music_path, source)) != path_keys.path_key(
        revisions[-1].to_path
    ):
        message = (
            f"file_id={file_id} sits at {source}, not at {revisions[-1].to_path} where its last "
            "path version put it, so a revert would not restore a recorded move"
        )
        raise ValueError(message)
    return source


def revert_paths(
    settings: Settings,
    file_id: int,
    version: int,
    *,
    note: str | None = None,
    dry_run: bool = False,
) -> PathRevertResult:
    """Move one file back to the location of its path *version*, under its own revert commit.

    The move is staged as an ``origin='revert'`` row and run through the commit loop, so a crash
    leaves a row :func:`commit_paths` finishes. Refused while any tag or path change is staged,
    for a missing file, a file moved outside TagMend, an unknown *version*, and a target that is
    on disk or tracked. The mismatch gate does not apply. *dry_run* keeps every refusal and
    changes nothing.
    """
    music_path = _require_music_path(settings)
    _require_volume(music_path)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        if store.get_file_by_id(connection, file_id) is None:
            message = f"unknown file_id={file_id}"
            raise ValueError(message)
        if store.any_staged(connection):
            raise ValueError(STAGING_NOT_EMPTY)
        revisions = store.get_path_revisions(connection, file_id)
        target = next((r for r in revisions if r.version == version), None)
        source = _require_revert_source(connection, music_path, file_id, revisions)
        if target is None:
            message = f"file_id={file_id} has no path version {version}. Read history_paths"
            raise ValueError(message)
        problem = _target_problem(connection, file_id, source, music_path / target.to_path)
        if problem is not None:
            raise ValueError(problem)
        if dry_run:
            return PathRevertResult(
                file_id=file_id,
                target_version=version,
                new_version=None,
                commit_id=None,
                to_path=target.to_path,
                status="reverted",
                detail=None,
                dry_run=True,
            )

        now = clock.utc_now()
        _stage_revert_row(
            connection,
            music_path,
            file_id=file_id,
            source=source,
            to_path=target.to_path,
            restores=version,
            note=note,
            now=now,
        )
        commit_id = commits.create_commit(connection, origin=_REVERT, message=note, now=now)
        connection.commit()  # the revert row and its commit become durable together
        applied = commits.run_commit(
            connection,
            PathDomain(music_path=music_path),
            commit_id=commit_id,
            file_ids=[file_id],
        )
        commits.set_commit_status(connection, commit_id, "applied")
        connection.commit()
    finally:
        connection.close()

    outcome = applied[0].outcome
    reverted = outcome.status == "committed"
    problem_row = None if reverted else _problem(outcome, target.to_path)
    return PathRevertResult(
        file_id=file_id,
        target_version=version,
        new_version=outcome.version,
        commit_id=commit_id,
        to_path=target.to_path,
        status="reverted" if reverted else outcome.status,
        detail=None if problem_row is None else problem_row.detail,
        dry_run=False,
    )


def _classify_revert(
    conn: sqlite3.Connection,
    music_path: Path,
    revision: store.PathRevision,
    latest_versions: dict[int, int],
) -> tuple[str, str | None, Path | None]:
    """Classify one row of the reverted commit, with a detail and the file's current location.

    The kind is ``revertable``, ``skipped_later_changes``, ``missing`` or ``error``. Only a
    ``revertable`` row carries a location.
    """
    if latest_versions.get(revision.file_id) != revision.version:
        return "skipped_later_changes", None, None
    row = store.get_file_by_id(conn, revision.file_id)
    if row is None or row.is_missing:
        return "missing", None, None
    source = Path(row.folder) / row.filename
    if not _present(source):
        return "missing", None, None
    problem = _target_problem(conn, revision.file_id, source, music_path / revision.from_path)
    if problem is not None:
        return "error", problem, None
    return "revertable", None, source


def _revert_outcome(
    revision: store.PathRevision,
    kind: str,
    detail: str | None,
    outcome: commits.FileCommitOutcome | None,
) -> commits.FileRevertOutcome:
    """Return the public outcome of one row of the reverted commit."""
    if kind != "revertable":
        return commits.FileRevertOutcome(revision.file_id, None, None, kind, detail)
    restores = revision.version - 1
    if outcome is None:
        return commits.FileRevertOutcome(revision.file_id, restores, None, "reverted")
    if outcome.status == "committed":
        return commits.FileRevertOutcome(revision.file_id, restores, outcome.version, "reverted")
    if outcome.status == "missing":
        return commits.FileRevertOutcome(revision.file_id, restores, None, "missing")
    problem = _problem(outcome, revision.from_path)
    return commits.FileRevertOutcome(revision.file_id, restores, None, "error", problem.detail)


def revert_commit_moves(
    conn: sqlite3.Connection,
    settings: Settings,
    commit_id: int,
    *,
    note: str | None,
    dry_run: bool,
) -> commits.RevertCommitResult:
    """Move every file *commit_id* moved back to its source, under ONE new revert commit.

    The path half of :func:`tagmend.engine.versioning.revert_commit`, which has already checked
    the target commit and that nothing is staged. A file a later path commit moved again is
    ``skipped_later_changes``, a file gone from disk is ``missing``, and a source now taken is an
    ``error``. The rest are staged as revert rows and run through the commit loop, so a crash
    leaves rows :func:`commit_paths` finishes. *dry_run* returns the classification only.
    """
    music_path = _require_music_path(settings)
    _require_volume(music_path)
    latest_versions = store.path_versions(conn)
    planned = [
        (revision, *_classify_revert(conn, music_path, revision, latest_versions))
        for revision in store.path_revisions_for_commit(conn, commit_id)
    ]
    revertable = [(revision, source) for revision, kind, _, source in planned if source is not None]
    if dry_run or not revertable:
        outcomes = [
            _revert_outcome(revision, kind, detail, None) for revision, kind, detail, _ in planned
        ]
        return commits.summarize_revert(
            commit_id=None, reverted_from=commit_id, dry_run=dry_run, outcomes=outcomes
        )

    now = clock.utc_now()
    for revision, source in revertable:
        _stage_revert_row(
            conn,
            music_path,
            file_id=revision.file_id,
            source=source,
            to_path=revision.from_path,
            restores=revision.version - 1,
            note=note,
            now=now,
        )
    new_commit = commits.create_commit(
        conn, origin=_REVERT, message=note, now=now, reverted_from=commit_id
    )
    conn.commit()  # every revert row and the commit become durable together
    applied = commits.run_commit(
        conn,
        PathDomain(music_path=music_path),
        commit_id=new_commit,
        file_ids=[revision.file_id for revision, _ in revertable],
    )
    commits.set_commit_status(conn, new_commit, "applied")
    conn.commit()

    by_file = {item.outcome.file_id: item.outcome for item in applied}
    outcomes = [
        _revert_outcome(revision, kind, detail, by_file.get(revision.file_id))
        for revision, kind, detail, _ in planned
    ]
    result = commits.summarize_revert(
        commit_id=new_commit, reverted_from=commit_id, dry_run=False, outcomes=outcomes
    )
    logger.info(
        "path revert of commit %d as commit %d: reverted=%d skipped=%d missing=%d errors=%d",
        commit_id,
        new_commit,
        result.reverted,
        result.skipped,
        result.missing,
        result.errors,
    )
    return result


# --- health ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StagingReport:
    """What ``check_health`` reports about the path staging area and the volume."""

    staged: int
    landed: tuple[int, ...]
    gone: tuple[int, ...]
    volume_refusal: str | None


def staging_report(settings: Settings) -> StagingReport:
    """Count the staged moves and name the ones already at their target or gone. Read-only."""
    music_path = settings.music_path
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = store.list_staged_paths(connection)
        if music_path is None:
            return StagingReport(staged=len(rows), landed=(), gone=(), volume_refusal=None)
        states = {
            row.file_id: _locate(_source_of(connection, row.file_id), music_path / row.to_path, row)
            for row in rows
        }
    finally:
        connection.close()
    return StagingReport(
        staged=len(rows),
        landed=tuple(file_id for file_id, state in states.items() if state in _AT_TARGET),
        gone=tuple(file_id for file_id, state in states.items() if state == GONE),
        volume_refusal=volume_refusal(music_path),
    )
