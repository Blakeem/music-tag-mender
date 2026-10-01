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

Sidecars are the non-audio files under an album folder: cover art, cue sheets, rip logs, scans.
When every tracked audio file of one folder is staged to one other folder, each sidecar is
staged in ``sidecar_moves_staged`` to the same relative place there, and every stage or unstage
call recomputes the sidecar rows of the folders it touches. After the commit loop, the sidecar
step moves each sidecar whose album audio has left the folder, appends a ``sidecar_moves`` row
and prunes the album folder once its last sidecar row leaves. A sidecar row takes the same disk
states. ``at_source`` and ``half_link`` move, and ``landed`` is logged with no disk action. A
``landed`` or ``landed_changed`` row is never dropped by a stage or unstage call. The commit
keeps a ``landed_changed`` row as ``changed_since_stage`` until a ``stage_paths`` covering it
confirms the file found at its target. ``target_taken`` keeps the row as an ``error``, and
``gone`` drops the row and logs nothing.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tagmend import config
from tagmend.engine import (
    clock,
    commits,
    db,
    mismatch,
    naming,
    path_keys,
    scan,
    schema,
    store,
)
from tagmend.engine.detector_core import parse_position
from tagmend.engine.path_text import part_problems
from tagmend.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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
        problems.extend(part_problems(part))
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


class _Speller:
    """Reads folder listings once each, for a plan that probes many paths in few folders.

    Rendered folders take the on-disk spelling of an existing folder with the same key, and a
    folder not on disk yet takes the first spelling the plan gave it, so one plan never records
    two spellings of one folder.
    """

    def __init__(self, music_path: Path) -> None:
        self._music_path = music_path
        self._listings: dict[str, dict[str, tuple[str, bool]] | None] = {}
        self._chosen: dict[str, str] = {}
        self._cues: dict[str, dict[str, list[str]]] = {}

    def _listing(self, folder: Path) -> dict[str, tuple[str, bool]] | None:
        """Return ``{entry key: (name, is_dir)}`` of *folder*, ``None`` when a file sits there."""
        key = path_keys.path_key(folder)
        if key not in self._listings:
            try:
                with os.scandir(folder) as entries:
                    listing = {path_keys.path_key(e.name): (e.name, e.is_dir()) for e in entries}
            except NotADirectoryError:
                self._listings[key] = None
                return None
            except OSError:
                # A missing folder, or a name too long for the volume (a bare OSError on
                # Windows), holds no entry. The length hold then names the long one.
                listing = {}
            self._listings[key] = listing
        return self._listings[key]

    def respell(self, relative_path: str) -> str:
        """Return *relative_path* with each folder spelled as on disk, or as first chosen."""
        parts = Path(relative_path).parts
        spelled: list[str] = []
        parent = self._music_path
        for part in parts[:-1]:
            entry = (self._listing(parent) or {}).get(path_keys.path_key(part))
            if entry is not None and entry[1]:
                name = entry[0]
            else:
                name = self._chosen.setdefault(path_keys.path_key(parent / part), part)
            spelled.append(name)
            parent = parent / name
        return str(Path(*spelled, parts[-1]))

    def present(self, path: Path) -> bool:
        """Whether the exact name of *path* is an entry of its folder's listing."""
        entry = (self._listing(path.parent) or {}).get(path_keys.path_key(path.name))
        return entry is not None and entry[0] == path.name

    def holds_key(self, path: Path) -> bool:
        """Whether any entry of *path*'s folder has *path*'s key, or a file blocks the folder."""
        listing = self._listing(path.parent)
        return listing is None or path_keys.path_key(path.name) in listing

    def cue_sheets(self, source: Path) -> list[str]:
        """Return the cue sheets and playlists in *source*'s folder that name *source*."""
        folder_key = path_keys.path_key(source.parent)
        if folder_key not in self._cues:
            self._cues[folder_key] = _cue_index(source.parent)
        return self._cues[folder_key].get(path_keys.path_key(source), [])


def respell_folders(music_path: Path, relative_path: str) -> str:
    """Return *relative_path* with each existing folder spelled as it is on disk.

    A destination folder whose key equals an existing folder's key reuses that folder's spelling,
    so a move never records a second spelling of one folder. The filename keeps the caller's
    spelling, since a filename case change is a rename the caller asked for.
    """
    return _Speller(music_path).respell(relative_path)


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


def _cue_index(folder: Path) -> dict[str, list[str]]:
    """Return ``{file key: sheet names}`` for each file the sheets in *folder* name."""
    try:
        with os.scandir(folder) as entries:
            sheets = sorted(
                Path(e.path)
                for e in entries
                if e.is_file() and Path(e.name).suffix.lower() in _PLAYLIST_SUFFIXES
            )
    except OSError:
        return {}
    index: dict[str, list[str]] = {}
    for sheet in sheets:
        for key in {path_keys.path_key(folder / name) for name in _playlist_entries(sheet)}:
            index.setdefault(key, []).append(sheet.name)
    return index


def _cue_references(source: Path) -> list[str]:
    """Return the cue sheets and playlists in *source*'s folder that name *source*."""
    return _cue_index(source.parent).get(path_keys.path_key(source), [])


def _one_entry(source: Path, target: Path) -> bool:
    """Whether two present names reach one folder entry, as two folder spellings do on NTFS.

    ``samefile`` alone reads such a pair as a half link, whose finish would unlink the only copy.
    """
    return source.name == target.name and source.parent.samefile(target.parent)


def _locate(source: Path, target: Path, staged: store.StagedPath) -> str:
    """Return the disk state of one staged row (the module docstring's table)."""
    return _locate_at(source, target, (staged.base_size_bytes, staged.base_mtime_ns))


def _locate_at(source: Path, target: Path, base: tuple[int | None, int | None]) -> str:
    """Return the disk state of a move from *source* to *target* staged at signature *base*."""
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
    if _signature(target) == base:
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

    ``music_path`` is the folder every stored path is relative to. ``pruned`` collects the
    folders the pruner removed, which the commit reports.
    """

    music_path: Path
    name: str = "paths"
    pruned: list[Path] = field(default_factory=list, compare=False)

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
                reverted_to_version=staged.reverted_to_version,
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
        self.pruned.extend(removed)


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


# --- sidecars: the non-audio files that move with their album folder -------------------

# A tag write's temp copy, which the writer swaps back over its file.
_TAG_TEMP_SUFFIX: Final = ".tagmend.tmp"

# What the sidecar step did with one row, besides the problem states it shares with audio.
SIDECAR_MOVED: Final = "moved"
SIDECAR_WAITING: Final = "waiting"

_SIDECAR_ERRORS: Final = (OSError, sqlite3.IntegrityError)


@dataclass(frozen=True, slots=True)
class SidecarHold:
    """A sidecar a stage leaves in its folder, because its target is taken or too long."""

    from_path: str
    to_path: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tools."""
        return {"from_path": self.from_path, "to_path": self.to_path, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class SidecarOutcome:
    """What one commit or revert did with one sidecar. Paths are relative to ``music_path``.

    ``status`` is ``moved`` (``reverted`` in a revert), ``waiting`` (its album's audio has not
    left the folder yet), ``missing`` (neither path is on disk, so the row is dropped),
    ``changed_since_stage`` or ``error`` (both keep the row), or a revert's
    ``skipped_later_changes``.
    """

    from_path: str
    to_path: str
    status: str
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tools."""
        return {
            "from_path": self.from_path,
            "to_path": self.to_path,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class _Unit:
    """An album folder whose every tracked audio file is staged to one other folder.

    Keys and paths are relative to ``music_path``. Of two units claiming one sidecar target,
    the one with the lower ``lowest_id`` keeps it.
    """

    key: str
    folder: str
    destination: str
    origin: str
    lowest_id: int


@dataclass(frozen=True, slots=True)
class _SidecarPlan:
    """The sidecar rows one call drops, confirms and inserts, and the sidecars it holds."""

    drops: tuple[str, ...]
    confirms: tuple[tuple[str, tuple[int, int]], ...]
    inserts: tuple[store.StagedSidecar, ...]
    held: tuple[SidecarHold, ...]


@dataclass(frozen=True, slots=True)
class _SidecarStep:
    """The sidecar step's outcome per row, the folders it pruned and the album folders it left."""

    outcomes: tuple[SidecarOutcome, ...]
    pruned: tuple[Path, ...]
    vacated: frozenset[Path]


def _is_link(entry: os.DirEntry[str]) -> bool:
    """Whether *entry* is a symlink or a junction, which the sidecar walks never enter."""
    return entry.is_symlink() or entry.is_junction()


def _sidecar_files(folder: Path) -> tuple[list[Path], bool]:
    """Return the non-audio files under *folder*, and whether any audio file sits under it.

    A subfolder holding audio is another album's folder, so it stays whole. An audio file the
    ledger does not track has no id to keep, so it stays too.
    """
    try:
        with os.scandir(folder) as listing:
            entries = sorted(listing, key=lambda entry: entry.name)
    except OSError:
        # A folder that cannot be listed stays where it is, like a folder holding audio.
        return [], True
    files: list[Path] = []
    holds_audio = False
    for entry in entries:
        if _is_link(entry):
            continue
        path = Path(entry.path)
        if entry.is_dir(follow_symlinks=False):
            inner, inner_audio = _sidecar_files(path)
            holds_audio = holds_audio or inner_audio
            if not inner_audio:
                files.extend(inner)
        elif path.suffix.lower() in scan.AUDIO_EXTENSIONS:
            holds_audio = True
        elif entry.is_file(follow_symlinks=False) and not entry.name.endswith(_TAG_TEMP_SUFFIX):
            files.append(path)
    return files, holds_audio


def _unit_depth(from_key: str, unit_key: str) -> int:
    """Return how many parts a sidecar's source path sits below its album folder."""
    return len(Path(from_key).parts) - len(Path(unit_key).parts)


def _unit_folder_of(path: str, depth: int) -> str:
    """Return the album folder of the sidecar at *path*, which sits *depth* parts below it."""
    return str(Path(path).parents[depth - 1])


def _destination_key(row: store.StagedSidecar) -> str:
    """Return the key of the folder the album of *row* moves to."""
    return path_keys.path_key(_unit_folder_of(row.to_path, _unit_depth(row.from_key, row.unit_key)))


def _in_scope(music_path: Path, relative: str, root_key: str | None) -> bool:
    """Whether *relative* sits at or under the folder keyed *root_key*, or no scope is given."""
    return root_key is None or path_keys.is_within(
        path_keys.path_key(music_path / relative), root_key
    )


def _locate_sidecar(music_path: Path, row: store.StagedSidecar) -> str:
    """Return the disk state of one staged sidecar move (the module docstring's table)."""
    return _locate_at(
        music_path / row.from_path,
        music_path / row.to_path,
        (row.base_size_bytes, row.base_mtime_ns),
    )


def _members_by_folder(conn: sqlite3.Connection) -> dict[str, list[store.FileRow]]:
    """Return the tracked audio files present on disk, keyed by their folder's key."""
    members: dict[str, list[store.FileRow]] = {}
    for row in store.list_files(conn):
        if not row.is_missing:
            members.setdefault(path_keys.path_key(row.folder), []).append(row)
    return members


def _staged_targets(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    """Return each staged audio file's ``(to_path, origin)``."""
    return {row.file_id: (row.to_path, row.origin) for row in store.list_staged_paths(conn)}


def _moving_unit(
    music_path: Path,
    members: list[store.FileRow],
    targets: dict[int, tuple[str, str]],
) -> _Unit | None:
    """Return the unit of *members* when all of them are staged to one other folder.

    *targets* maps each staged file id to its ``(to_path, origin)``. A revert row moves its
    file back alone, so its folder carries nothing. The library root is no album folder.
    """
    staged = [targets[member.id] for member in members if member.id in targets]
    if not members or len(staged) != len(members):
        return None
    if any(origin == _REVERT for _, origin in staged):
        return None
    folder = _relative(music_path, Path(members[0].folder))
    key = path_keys.path_key(folder)
    destinations = {path_keys.path_key(Path(to_path).parent) for to_path, _ in staged}
    if key == path_keys.path_key(".") or len(destinations) != 1 or key in destinations:
        return None
    first = min(members, key=lambda member: member.id)
    origins = {origin for _, origin in staged}
    return _Unit(
        key=key,
        folder=folder,
        destination=str(Path(targets[first.id][0]).parent),
        origin=_AUTO if origins == {_AUTO} else "manual",
        lowest_id=first.id,
    )


def _sidecar_hold(
    music_path: Path,
    from_key: str,
    to_path: str,
    claims: dict[str, str],
) -> str | None:
    """Return why the sidecar keyed *from_key* may not move to *to_path*, or ``None``.

    *claims* maps each target key a staged sidecar already holds to that sidecar's source.
    """
    too_long = check_length(music_path, to_path)
    if too_long:
        return "; ".join(too_long)
    to_key = path_keys.path_key(to_path)
    claimant = claims.get(to_key)
    if claimant is not None:
        return f"{claimant} is already staged to {to_path}"
    # The sidecar's own entry never takes its target, only another entry with the key does.
    if to_key != from_key and _listing_holds_key(music_path / to_path):
        return f"{to_path} is taken on disk"
    return None


def _units_to_recompute(
    music_path: Path,
    unit_keys: set[str],
    targets: dict[int, tuple[str, str]],
    members: dict[str, list[store.FileRow]],
    existing: list[store.StagedSidecar],
) -> dict[str, _Unit | None]:
    """Return every unit to recompute, keyed by folder key, with its plan when it moves.

    A unit with no present audio keeps its rows, since its audio already moved. A unit whose
    rows share a destination with a recomputed unit is recomputed too, so the lower file id
    wins a shared target whatever order the calls came in.
    """

    def members_of(key: str) -> list[store.FileRow]:
        return members.get(path_keys.path_key(music_path / key), [])

    units = {
        key: _moving_unit(music_path, members_of(key), targets)
        for key in unit_keys
        if members_of(key)
    }
    destinations = {path_keys.path_key(u.destination) for u in units.values() if u is not None}
    destinations |= {
        _destination_key(row) for row in existing if row.origin != _REVERT and row.unit_key in units
    }
    for row in existing:
        joins = row.origin != _REVERT and _destination_key(row) in destinations
        if joins and row.unit_key not in units and members_of(row.unit_key):
            units[row.unit_key] = _moving_unit(music_path, members_of(row.unit_key), targets)
    return units


def _carry(  # noqa: PLR0913 - cohesive keyword-only claim state and row payload
    music_path: Path,
    unit: _Unit,
    *,
    speller: _Speller,
    claims: dict[str, str],
    sources: set[str],
    note: str | None,
    now: str,
) -> tuple[list[store.StagedSidecar], list[SidecarHold]]:
    """Return the rows that move *unit*'s sidecars, and the sidecars it holds.

    *claims* maps each target key a staged row holds to that row's source, and *sources* holds
    every staged source key. Both grow with each row returned. A target folder that exists
    under another casing keeps its on-disk spelling, as an audio target does.
    """
    inserts: list[store.StagedSidecar] = []
    held: list[SidecarHold] = []
    destination_key = path_keys.path_key(unit.destination)
    found, _ = _sidecar_files(music_path / unit.folder)
    for source in found:
        from_path = _relative(music_path, source)
        from_key = path_keys.path_key(from_path)
        # A source with a staged row is already moving, and a file inside the destination
        # already sits with the album.
        if from_key in sources or path_keys.is_within(from_key, destination_key):
            continue
        to_path = speller.respell(
            str(Path(unit.destination) / Path(from_path).relative_to(unit.folder))
        )
        detail = _sidecar_hold(music_path, from_key, to_path, claims)
        if detail is not None:
            held.append(SidecarHold(from_path=from_path, to_path=to_path, detail=detail))
            continue
        size, mtime_ns = _signature(source)
        inserts.append(
            store.StagedSidecar(
                from_key=from_key,
                from_path=from_path,
                to_path=to_path,
                to_key=path_keys.path_key(to_path),
                unit_key=unit.key,
                base_size_bytes=size,
                base_mtime_ns=mtime_ns,
                origin=unit.origin,
                reverted_from=None,
                note=note,
                staged_at=now,
            ),
        )
        claims[path_keys.path_key(to_path)] = from_path
        sources.add(from_key)
    return inserts, held


def _plan_sidecars(
    conn: sqlite3.Connection,
    music_path: Path,
    unit_keys: set[str],
    targets: dict[int, tuple[str, str]],
    *,
    note: str | None,
) -> _SidecarPlan:
    """Recompute the sidecar rows of the album folders keyed *unit_keys*. Writes nothing.

    *unit_keys* are relative to ``music_path``, and *targets* maps every staged audio file to
    its ``(to_path, origin)`` as the call leaves the staging area. A recomputed unit drops its
    rows, and a moving one stages each sidecar whose target is free, the unit with the lowest
    file id first. A row whose sidecar already sits at its target is never dropped, since only
    a commit may log that move, and one that changed there is confirmed at its signature now.
    A revert row is never dropped, but it is confirmed like any other, or nothing would clear it.
    """
    existing = store.list_staged_sidecars(conn)
    units = _units_to_recompute(music_path, unit_keys, targets, _members_by_folder(conn), existing)
    touched = unit_keys | set(units)

    drops: list[str] = []
    confirms: list[tuple[str, tuple[int, int]]] = []
    claims: dict[str, str] = {}
    sources: set[str] = set()
    for row in existing:
        state = _locate_sidecar(music_path, row) if row.unit_key in touched else None
        recomputed = row.origin != _REVERT and row.unit_key in units
        if recomputed and state is not None and state not in _AT_TARGET:
            drops.append(row.from_key)
            continue
        if state == LANDED_CHANGED:
            confirms.append((row.from_key, _signature(music_path / row.to_path)))
        claims[row.to_key] = row.from_path
        sources.add(row.from_key)

    inserts: list[store.StagedSidecar] = []
    held: list[SidecarHold] = []
    now = clock.utc_now()
    speller = _Speller(music_path)
    moving = sorted((u for u in units.values() if u is not None), key=lambda u: u.lowest_id)
    for unit in moving:
        unit_inserts, unit_held = _carry(
            music_path,
            unit,
            speller=speller,
            claims=claims,
            sources=sources,
            note=note,
            now=now,
        )
        inserts.extend(unit_inserts)
        held.extend(unit_held)
    return _SidecarPlan(tuple(drops), tuple(confirms), tuple(inserts), tuple(held))


def _apply_sidecar_plan(conn: sqlite3.Connection, plan: _SidecarPlan) -> None:
    """Write *plan* into the open transaction, every drop before any insert."""
    for from_key in plan.drops:
        store.delete_staged_sidecar(conn, from_key)
    for from_key, signature in plan.confirms:
        store.set_staged_sidecar_signature(conn, from_key, signature)
    for staged in plan.inserts:
        store.insert_staged_sidecar(conn, staged)


def _restamp(
    conn: sqlite3.Connection,
    row: store.StagedSidecar,
    signature: tuple[int, int],
) -> store.StagedSidecar:
    """Return *row* staged at *signature*, recording a changed signature durably first.

    A sidecar's content plays no part in where it goes, so an edit since staging never holds
    it. A crash after its move must still find the target at the staged signature.
    """
    if signature == (row.base_size_bytes, row.base_mtime_ns):
        return row
    store.set_staged_sidecar_signature(conn, row.from_key, signature)
    conn.commit()
    return replace(row, base_size_bytes=signature[0], base_mtime_ns=signature[1])


def _sidecar_problem(
    conn: sqlite3.Connection,
    row: store.StagedSidecar,
    state: str,
) -> SidecarOutcome:
    """Return the outcome of a row the step cannot move, dropping it when both paths are gone."""
    if state == GONE:
        store.delete_staged_sidecar(conn, row.from_key)
        conn.commit()
        detail = "neither its source nor its target is on disk, so its row was dropped"
        return SidecarOutcome(row.from_path, row.to_path, MISSING, detail)
    if state == LANDED_CHANGED:
        folder = _unit_folder_of(row.from_path, _unit_depth(row.from_key, row.unit_key))
        detail = (
            "it changed at its target after its move landed, and its row stays. Run "
            f"stage_paths(path={folder!r}) to confirm the file found there"
        )
        return SidecarOutcome(row.from_path, row.to_path, "changed_since_stage", detail)
    detail = (
        f"{row.to_path} is taken on disk, and its row stays. Move that file away and run "
        f"commit_paths again, or drop the row with unstage_paths(path={row.from_path!r})"
    )
    return SidecarOutcome(row.from_path, row.to_path, "error", detail)


def _move_sidecar(
    conn: sqlite3.Connection,
    music_path: Path,
    row: store.StagedSidecar,
    *,
    commit_id: int,
) -> SidecarOutcome:
    """Move one staged sidecar, log it and drop its row in one transaction.

    The log row is written before the move and committed after it, so a crash between the two
    leaves the row staged with the sidecar at its target, which the next commit logs.
    """
    source = music_path / row.from_path
    target = music_path / row.to_path
    try:
        state = _locate_sidecar(music_path, row)
        if state in {GONE, LANDED_CHANGED, TARGET_TAKEN}:
            return _sidecar_problem(conn, row, state)
        staged = _restamp(conn, row, _signature(source)) if state == AT_SOURCE else row
        store.insert_sidecar_move(conn, staged, commit_id=commit_id, now=clock.utc_now())
        store.delete_staged_sidecar(conn, row.from_key)
        _move(source, target, resolved=target if state == LANDED else source)
        conn.commit()
    except _SIDECAR_ERRORS as exc:
        conn.rollback()
        logger.warning("commit %d: sidecar %s failed: %s", commit_id, row.from_path, exc)
        detail = (
            f"{exc}. Its row stays staged. Fix the cause and run commit_paths again, or drop "
            f"the row with unstage_paths(path={row.from_path!r})"
        )
        return SidecarOutcome(row.from_path, row.to_path, "error", detail)
    return SidecarOutcome(row.from_path, row.to_path, SIDECAR_MOVED)


def _subfolders(folder: Path) -> list[Path]:
    """Return every folder below *folder*, each after the folders below it, never via a link."""
    try:
        with os.scandir(folder) as listing:
            children = [
                Path(entry.path)
                for entry in listing
                if entry.is_dir(follow_symlinks=False) and not _is_link(entry)
            ]
    except OSError:
        return []
    return [*(nested for child in children for nested in _subfolders(child)), *children]


def _prune_unit(music_path: Path, folder: Path) -> list[Path]:
    """Remove the emptied folders below an album folder, deepest first, then the folder upward."""
    removed: list[Path] = []
    for candidate in [*_subfolders(folder), folder]:
        removed.extend(_prune(music_path, candidate))
    if removed:
        logger.info("pruned %d emptied folder(s) at %s", len(removed), folder)
    return removed


def _sidecar_step(
    conn: sqlite3.Connection,
    music_path: Path,
    *,
    commit_id: int,
    root_key: str | None,
) -> _SidecarStep:
    """Move every staged sidecar in scope whose album audio has left its folder, then prune.

    Runs after the commit loop. A sidecar waits while any staged audio file still sits in its
    album folder. Once the last sidecar row of an album folder leaves, the folder is pruned.
    """
    rows = [
        row
        for row in store.list_staged_sidecars(conn)
        if _in_scope(music_path, row.from_path, root_key)
    ]
    occupied = {
        path_keys.path_key(_source_of(conn, staged.file_id).parent)
        for staged in store.list_staged_paths(conn)
    }
    outcomes: list[SidecarOutcome] = []
    vacated: dict[str, Path] = {}
    for row in rows:
        folder = music_path / _unit_folder_of(
            row.from_path, _unit_depth(row.from_key, row.unit_key)
        )
        if path_keys.path_key(folder) in occupied:
            outcomes.append(SidecarOutcome(row.from_path, row.to_path, SIDECAR_WAITING))
            continue
        outcome = _move_sidecar(conn, music_path, row, commit_id=commit_id)
        outcomes.append(outcome)
        if outcome.status in {SIDECAR_MOVED, MISSING}:
            vacated[row.unit_key] = folder

    remaining = {row.unit_key for row in store.list_staged_sidecars(conn)}
    pruned = [
        removed
        for unit_key, folder in vacated.items()
        if unit_key not in remaining
        for removed in _prune_unit(music_path, folder)
    ]
    return _SidecarStep(tuple(outcomes), tuple(pruned), frozenset(vacated.values()))


def _left_behind(conn: sqlite3.Connection, music_path: Path, folders: set[Path]) -> list[str]:
    """Return the sidecars left with no staged move in each of *folders* its audio all left.

    A vacated folder also lists the sidecars of a vacated folder nested in it, so each sidecar
    is kept once, in first-seen order.
    """
    present = set(_members_by_folder(conn))
    staged = {row.from_key for row in store.list_staged_sidecars(conn)}
    root_key = path_keys.path_key(music_path)
    left: dict[str, str] = {}
    for folder in sorted(folders):
        key = path_keys.path_key(folder)
        if key == root_key or key in present:
            continue
        for source in _sidecar_files(folder)[0]:
            relative = _relative(music_path, source)
            relative_key = path_keys.path_key(relative)
            if relative_key not in staged:
                left.setdefault(relative_key, relative)
    return list(left.values())


def _emptied_release_folders(settings: Settings, folders: set[Path]) -> set[Path]:
    """Return the release folder above each disc folder of *folders* once no audio sits under it.

    Only a folder holding audio is a sidecar unit, so a release folder's own cover and scans
    above its disc folders follow no move. They are reported rather than left behind silently.
    """
    releases: set[Path] = set()
    for folder in folders:
        if mismatch.layout_of(settings, str(folder), "").disc_folder is None:
            continue
        _, holds_audio = _sidecar_files(folder.parent)
        if not holds_audio:
            releases.add(folder.parent)
    return releases


def _vacated_by(conn: sqlite3.Connection, music_path: Path, commit_id: int) -> set[Path]:
    """Return the folders the audio moves of *commit_id* left."""
    return {
        music_path / Path(revision.from_path).parent
        for revision in store.path_revisions_for_commit(conn, commit_id)
        if path_keys.path_key(Path(revision.from_path).parent)
        != path_keys.path_key(Path(revision.to_path).parent)
    }


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


@dataclass(frozen=True, slots=True)
class StagePathsBatchResult:
    """What one :func:`stage_paths_batch` call staged: its files and its sidecars."""

    file_ids: tuple[int, ...]
    sidecars_staged: int
    sidecars_held: tuple[SidecarHold, ...]

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "staged": len(self.file_ids),
            "file_ids": list(self.file_ids),
            "sidecars_staged": self.sidecars_staged,
            "sidecars_held": [hold.to_dict() for hold in self.sidecars_held],
        }


def stage_paths_batch(
    settings: Settings,
    *,
    entries: Sequence[object],
    note: str | None = None,
) -> StagePathsBatchResult:
    """Stage explicit moves for many files in ONE transaction, all or nothing, origin ``manual``.

    *entries* is a sequence of ``(file_id, to_path)`` pairs, *to_path* relative to
    ``music_path`` or absolute under it. A target folder whose key matches an existing folder
    reuses its on-disk spelling (:func:`respell_folders`). The call is refused whole while the
    mismatch gate is closed (:func:`tagmend.engine.mismatch.gate_state`), unless every entry
    confirms a landed move at its exact staged target, while the volume check refuses
    ``music_path``, and when any entry is held. A folder whose every file the call stages to
    one other folder stages its sidecars too. Every held entry is listed with
    its reasons: ``occupied``, ``shared_target``, ``too_long``, ``cue_reference``,
    ``staged_tag``, ``invalid_path``, ``unknown_file``, ``missing`` and ``landed_move``. A file
    staged before keeps its version-0 location, captured at its first stage. Returns the staged
    file ids in input order, the count of sidecar moves staged and the sidecars held in place.
    Raises :class:`ValueError` on any refusal, and nothing is staged.
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
        touched: set[str] = set()
        for plan in plans:
            move = plan.move
            if move is None:  # pragma: no cover - defensive, a plan with no reason holds a move
                continue
            touched.add(path_keys.path_key(Path(move.from_path).parent))
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
                    reverted_to_version=None,
                ),
            )
        sidecars = _plan_sidecars(
            connection, music_path, touched, _staged_targets(connection), note=note
        )
        _apply_sidecar_plan(connection, sidecars)
        connection.commit()
    finally:
        connection.close()

    result = StagePathsBatchResult(
        file_ids=tuple(file_id for file_id, _ in validated),
        sidecars_staged=len(sidecars.inserts),
        sidecars_held=sidecars.held,
    )
    logger.info(
        "staged %d path move(s), %d sidecar move(s), %d sidecar(s) held",
        len(result.file_ids),
        result.sidecars_staged,
        len(result.sidecars_held),
    )
    return result


# --- the planner: render every file, then hold what must not move ----------------------

# One file's place in the plan.
STATUS_AT_TARGET: Final = "at_target"
STATUS_CASE_ONLY: Final = "case_only"
STATUS_WILL_MOVE: Final = "will_move"
STATUS_HELD: Final = "held"
STATUS_KEPT_STAGED: Final = "kept_staged"
PLAN_STATUSES: Final = (
    STATUS_AT_TARGET,
    STATUS_CASE_ONLY,
    STATUS_WILL_MOVE,
    STATUS_HELD,
    STATUS_KEPT_STAGED,
)

# How a deviating file's path changes.
KIND_RENAME: Final = "rename"
KIND_MOVE: Final = "move"
KIND_CASE: Final = "case"

# The render holds. stage_paths applies them, and an explicit stage_paths_batch destination
# supersedes them. A required field that renders empty holds ``missing_<name>``.
MISSING_PREFIX: Final = "missing_"
MISSING_YEAR: Final = "missing_year"
MISSING_DISC: Final = "missing_disc"
ALBUM_SPLIT: Final = "album_split"
UNIT_MEMBER: Final = "unit_member"
TRACK_CONFLICT: Final = "track_conflict"
MERGE: Final = "merge"
DUPLICATE_RENDER: Final = "duplicate_render"

_AUTO: Final = "auto"
_NAMED_IDS: Final = 5
_HELD_CAP: Final = 50


@dataclass(slots=True)
class _Work:
    """One present file while the planner decides its fate. Holds accumulate in ``reasons``."""

    row: store.FileRow
    from_path: str
    folder_key: str
    values: dict[str, str]
    layout: mismatch.Layout
    kept: bool
    staged: store.StagedPath | None
    render: naming.Rendered | None = None
    to_path: str | None = None
    status: str = STATUS_HELD
    kind: str | None = None
    reasons: list[tuple[str, str]] = field(default_factory=list)
    replaces: bool = False

    @property
    def source(self) -> Path:
        """Where the ledger records the file."""
        return Path(self.row.folder) / self.row.filename

    @property
    def stays(self) -> bool:
        """Whether the file stays in its folder: held, or kept by a manual or revert row."""
        return bool(self.reasons) or self.status == STATUS_KEPT_STAGED

    @property
    def moving(self) -> bool:
        """Whether the file is planned to move and nothing holds it yet."""
        return self.status == STATUS_WILL_MOVE and not self.reasons

    def hold(self, reason: str, detail: str) -> bool:
        """Add *reason* unless the file already carries it. True when it was added."""
        if self.status == STATUS_KEPT_STAGED or any(name == reason for name, _ in self.reasons):
            return False
        self.reasons.append((reason, detail))
        return True


@dataclass(frozen=True, slots=True)
class FilePlan:
    """One present file's place in the path plan. Paths are relative to ``music_path``.

    ``status`` is ``at_target``, ``case_only`` (a folder differs only in case, which the
    folder's on-disk spelling absorbs, so nothing stages), ``will_move``, ``held`` (``reasons``
    says why) or ``kept_staged`` (a manual or revert move stays staged, untouched). ``kind`` is
    ``rename`` (same folder), ``move`` or ``case``. ``to_path`` is ``None`` when the tags render
    no path. ``rendered`` is the render before folder respelling, and ``levels`` gives each of
    its parts' pattern component index. ``replaces`` marks an ``auto`` row that a stage of this
    file's scope replaces.
    """

    file_id: int
    folder_key: str
    from_path: str
    to_path: str | None
    rendered: tuple[str, ...]
    levels: tuple[int | None, ...]
    status: str
    kind: str | None
    reasons: tuple[tuple[str, str], ...]
    replaces: bool

    @property
    def source_folder(self) -> str:
        """The folder the file sits in, relative to ``music_path``."""
        return str(Path(self.from_path).parent)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tools."""
        return {
            "file_id": self.file_id,
            "from_path": self.from_path,
            "to_path": self.to_path,
            "status": self.status,
            "kind": self.kind,
            "reasons": [{"reason": reason, "detail": detail} for reason, detail in self.reasons],
        }


def _named(ids: Sequence[int]) -> str:
    """Return up to :data:`_NAMED_IDS` file ids for a detail, with a count of the rest."""
    shown = ", ".join(str(file_id) for file_id in ids[:_NAMED_IDS])
    rest = len(ids) - _NAMED_IDS
    return f"file_id {shown}" + (f" and {rest} more" if rest > 0 else "")


def _load_works(
    conn: sqlite3.Connection,
    settings: Settings,
    music_path: Path,
    pattern: naming.Pattern,
) -> list[_Work]:
    """Read every present file under ``music_path`` with the tags and decisions it renders by."""
    root_key = path_keys.path_key(music_path)
    rows = [
        row
        for row in store.list_files(conn)
        if not row.is_missing and path_keys.is_within(path_keys.path_key(row.folder), root_key)
    ]
    names = tuple(sorted({*pattern.tag_names(), *naming.BASE_TAGS}))
    tag_values = store.load_tag_values(conn, names)
    keeps = {
        file_id
        for file_id in store.load_mismatch_statuses(conn)
        if mismatch.planner_keep(conn, file_id)
    }
    staged = {row.file_id: row for row in store.list_staged_paths(conn)}
    return [
        _Work(
            row=row,
            from_path=_relative(music_path, Path(row.folder) / row.filename),
            folder_key=path_keys.path_key(row.folder),
            values=tag_values.get(row.id, {}),
            layout=mismatch.layout_of(settings, row.folder, row.filename),
            kept=row.id in keeps,
            staged=staged.get(row.id),
        )
        for row in rows
    ]


def _render_input(work: _Work) -> naming.RenderInput:
    """Return what the renderer reads of *work*."""
    folder_parts = Path(work.from_path).parts[:-1]
    container = folder_parts[0] if work.layout.container and folder_parts else None
    return naming.RenderInput(
        file_id=work.row.id,
        values=work.values,
        container=container,
        kept_folder=folder_parts if work.kept else None,
        suffix=Path(work.row.filename).suffix,
    )


def _place(work: _Work, render: naming.Rendered, speller: _Speller) -> None:
    """Set *work*'s target, status and kind from its render, plus its blank-tag holds."""
    work.render = render
    raw = render.relative_path
    if raw is not None:
        work.to_path = speller.respell(raw)
    if work.staged is not None and work.staged.origin != _AUTO:
        work.status = STATUS_KEPT_STAGED
        return
    if render.missing is not None:
        name = render.missing
        work.hold(f"{MISSING_PREFIX}{name}", f"the pattern needs {{{name}}}, which is blank")
    elif work.to_path == work.from_path:
        work.status = STATUS_AT_TARGET if raw == work.from_path else STATUS_CASE_ONLY
        work.kind = None if raw == work.from_path else KIND_CASE
    elif work.to_path is not None:
        work.status = STATUS_WILL_MOVE
        same_folder = path_keys.path_key(Path(work.to_path).parent) == path_keys.path_key(
            Path(work.from_path).parent,
        )
        work.kind = KIND_RENAME if same_folder else KIND_MOVE
    # A kept folder stays, so the path information it carries is never lost.
    if work.kept:
        return
    if work.layout.release_years and not naming.year_of(work.values):
        detail = (
            f"its release folder {work.layout.release_folder} names a year and the file has no "
            "date or originaldate"
        )
        work.hold(MISSING_YEAR, detail)
    if work.layout.disc_folder is not None and not work.values.get("discnumber", "").strip():
        detail = f"it sits in the disc folder {work.layout.disc_folder} and its discnumber is blank"
        work.hold(MISSING_DISC, detail)


def _rendered_works(
    conn: sqlite3.Connection,
    settings: Settings,
    music_path: Path,
    pattern: naming.Pattern,
) -> tuple[list[_Work], _Speller]:
    """Load and render every present file, its target respelled on disk."""
    works = _load_works(conn, settings, music_path, pattern)
    renders = naming.render_library(pattern, [_render_input(work) for work in works])
    speller = _Speller(music_path)
    for work in works:
        _place(work, renders[work.row.id], speller)
    return works, speller


def _group[K](works: Sequence[_Work], key: Callable[[_Work], K | None]) -> dict[K, list[_Work]]:
    """Group *works* by *key*, leaving out a work whose key is ``None``."""
    groups: dict[K, list[_Work]] = {}
    for work in works:
        value = key(work)
        if value is not None:
            groups.setdefault(value, []).append(work)
    return groups


def _target_key(work: _Work) -> str | None:
    """Return the key of *work*'s target relative to ``music_path``."""
    return None if work.to_path is None else path_keys.path_key(work.to_path)


def _slot(work: _Work) -> tuple[str, str, int | None, int] | None:
    """Return the destination folder, album title and ``[disc-]NN`` slot of *work*, or ``None``.

    ``None`` means the file has no track number. A kept folder may hold several albums, each
    with its own track 1, so a slot counts per album title. The title is folded as a folder name
    spells it, so two spellings that render alike still share their slots.
    """
    track = parse_position(work.values.get("tracknumber"))
    if work.to_path is None or work.render is None or track is None:
        return None
    album = naming.album_title_key(work.values)
    return path_keys.path_key(Path(work.to_path).parent), album, work.render.disc, track


def _hold_shared_renders(works: list[_Work]) -> None:
    """Hold ``duplicate_render``, ``track_conflict`` and ``album_split`` across the library."""
    for members in _group(works, _target_key).values():
        if len(members) > 1:
            for work in members:
                others = [m.row.id for m in members if m is not work]
                work.hold(DUPLICATE_RENDER, f"{_named(others)} renders the same path")
    for members in _group(works, _slot).values():
        if len(members) > 1:
            for work in members:
                others = [m.row.id for m in members if m is not work]
                work.hold(TRACK_CONFLICT, f"{_named(others)} takes the same disc and track slot")
    rendered = [work for work in works if work.render is not None and work.to_path is not None]
    for members in _group(rendered, lambda work: work.folder_key).values():
        albums = {work.render.album_key for work in members if work.render is not None}
        if len(albums) > 1:
            detail = f"the files of this folder render to {len(albums)} album folders"
            for work in members:
                work.hold(ALBUM_SPLIT, detail)


def _replaceable(
    work: _Work,
    music_path: Path,
    scope_key: str | None,
) -> tuple[bool, tuple[str, str] | None]:
    """Return whether a stage in scope replaces *work*'s ``auto`` row, and its landed hold."""
    staged = work.staged
    if staged is None or staged.origin != _AUTO:
        return False, None
    if _locate(work.source, music_path / staged.to_path, staged) in _AT_TARGET:
        detail = f"its staged move to {staged.to_path} already landed on disk. Run commit_paths"
        return False, (LANDED_MOVE, detail)
    in_scope = scope_key is None or path_keys.is_within(work.folder_key, scope_key)
    return in_scope, None


def _target_holds(  # noqa: PLR0913 - cohesive keyword-only target-safety inputs
    work: _Work,
    *,
    music_path: Path,
    speller: _Speller,
    tracked: dict[str, int],
    claims: dict[str, int],
    staged_tags: set[int],
) -> None:
    """Hold one moving file by every destination-safety reason that applies to it."""
    if work.to_path is None:  # pragma: no cover - defensive, a moving file has a target
        return
    if work.row.id in staged_tags:
        work.hold(STAGED_TAG, "it has a staged tag change. Run commit_tags or unstage_tags")
    if not speller.present(work.source):
        work.hold(MISSING, f"{work.from_path} is not on disk. Rescan first")
    too_long = check_length(music_path, work.to_path)
    if too_long:
        # A path too long to list reliably is never probed on disk.
        work.hold(TOO_LONG, "; ".join(too_long))
        return
    target = music_path / work.to_path
    target_key = path_keys.path_key(target)
    holder = tracked.get(target_key)
    if holder is not None and holder != work.row.id:
        work.hold(OCCUPIED, f"file_id={holder} is tracked at {work.to_path}")
    elif target_key != path_keys.path_key(work.source) and speller.holds_key(target):
        work.hold(OCCUPIED, f"{work.to_path} is taken on disk")
    claimant = claims.get(path_keys.path_key(work.to_path))
    if claimant is not None and claimant != work.row.id:
        work.hold(SHARED_TARGET, f"file_id={claimant} is already staged to {work.to_path}")
    sheets = speller.cue_sheets(work.source)
    if sheets:
        detail = f"{', '.join(sheets)} in its folder names it. Edit or move the sheet first"
        work.hold(CUE_REFERENCE, detail)


def _hold_unsafe(
    conn: sqlite3.Connection,
    works: list[_Work],
    *,
    music_path: Path,
    speller: _Speller,
    scope_key: str | None,
) -> None:
    """Mark the ``auto`` rows a stage replaces, then hold every unsafe destination."""
    for work in works:
        work.replaces, landed = _replaceable(work, music_path, scope_key)
        if landed is not None:
            work.hold(*landed)
    replacing = {work.row.id for work in works if work.replaces}
    claims = {
        row.to_key: row.file_id
        for row in store.list_staged_paths(conn)
        if row.to_key is not None and row.file_id not in replacing
    }
    tracked = {
        path_keys.file_path_key(row.folder, row.filename): row.id for row in store.list_files(conn)
    }
    staged_tags = {row.file_id for row in store.list_staged_tags(conn)}
    for work in works:
        if work.moving:
            _target_holds(
                work,
                music_path=music_path,
                speller=speller,
                tracked=tracked,
                claims=claims,
                staged_tags=staged_tags,
            )


def _close_units(works: list[_Work]) -> bool:
    """Hold ``unit_member`` on every moving file whose folder has a file that stays."""
    changed = False
    for members in _group(works, lambda work: work.folder_key).values():
        staying = sorted(work.row.id for work in members if work.stays)
        if not staying:
            continue
        detail = f"{_named(staying)} in this folder stays, and a folder moves as a unit"
        for work in members:
            if work.moving:
                changed |= work.hold(UNIT_MEMBER, detail)
    return changed


def _disc_of(work: _Work) -> int:
    """Return the disc a file belongs to, a blank disc number counting as disc 1."""
    return parse_position(work.values.get("discnumber")) or 1


def _hold_merges(works: list[_Work], music_path: Path) -> bool:
    """Hold ``merge`` on files poured into a folder another folder also feeds, disc sets shared.

    A feeder is a source folder with an unheld file rendering to the destination. The
    destination itself feeds it when it holds a file that stays.
    """
    changed = False
    staying = _group([w for w in works if w.stays], lambda work: work.folder_key)

    def destination(work: _Work) -> str | None:
        if work.stays or work.to_path is None:
            return None
        return path_keys.path_key((music_path / work.to_path).parent)

    for dest, members in _group(works, destination).items():
        feeders: dict[str, set[int]] = {}
        for work in [*members, *staying.get(dest, [])]:
            feeders.setdefault(work.folder_key, set()).add(_disc_of(work))
        discs = list(feeders.values())
        if len(discs) == 1 or sum(map(len, discs)) == len(set().union(*discs)):
            continue
        folder = Path(members[0].to_path or "").parent
        detail = (
            f"{len(feeders)} folders feed {folder} and their disc numbers overlap. Join them "
            "with stage_paths_batch"
        )
        for work in members:
            if work.moving and work.folder_key != dest:
                changed |= work.hold(MERGE, detail)
    return changed


def _freeze(work: _Work) -> FilePlan:
    """Return the public plan of *work*, held when any reason holds it."""
    status = work.status
    if work.reasons and status != STATUS_KEPT_STAGED:
        status = STATUS_HELD
    render = work.render
    return FilePlan(
        file_id=work.row.id,
        folder_key=work.folder_key,
        from_path=work.from_path,
        to_path=work.to_path,
        rendered=() if render is None else render.parts,
        levels=() if render is None else render.levels,
        status=status,
        kind=work.kind,
        reasons=tuple(work.reasons),
        replaces=work.replaces,
    )


def plan_library(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    pattern: naming.Pattern,
    scope_key: str | None = None,
) -> list[FilePlan]:
    """Render every present file with *pattern* and decide which ones may move. Reads only.

    The render holds come first (``missing_<name>``, ``missing_year``, ``missing_disc``,
    ``duplicate_render``, ``track_conflict``, ``album_split``), then the destination-safety
    holds of a moving file (``landed_move``, ``staged_tag``, ``missing``, ``too_long``,
    ``occupied``, ``shared_target``, ``cue_reference``). A folder moves as a unit, so every moving
    file of a folder with a file that stays holds ``unit_member``, and a destination fed by
    more than one folder with shared disc numbers holds ``merge``. Those two repeat until no
    hold is added. *scope_key* marks the ``auto`` rows a stage of that folder replaces.
    """
    music_path = _require_music_path(settings)
    works, speller = _rendered_works(conn, settings, music_path, pattern)
    _hold_shared_renders(works)
    _hold_unsafe(conn, works, music_path=music_path, speller=speller, scope_key=scope_key)
    changed = True
    while changed:
        changed = _close_units(works) | _hold_merges(works, music_path)
    return [_freeze(work) for work in works]


def render_targets(
    conn: sqlite3.Connection,
    settings: Settings,
    pattern: naming.Pattern,
) -> dict[int, str | None]:
    """Return each present file's rendered target, respelled on disk, ``None`` when blank."""
    works, _ = _rendered_works(conn, settings, _require_music_path(settings), pattern)
    return {work.row.id: work.to_path for work in works}


@dataclass(frozen=True, slots=True)
class StagePathsResult:
    """What one :func:`stage_paths` call staged, or would stage on a dry run.

    ``matched`` counts the present files in scope. ``unstaged`` counts the ``auto`` rows in
    scope the new plan dropped. ``held`` counts held files per reason, and ``held_files`` lists
    the first :data:`_HELD_CAP` of them. ``staged_targets_under_path`` is set only when *path*
    matched no file: the staged moves whose target lies under it. ``sidecars_staged`` counts
    the sidecar moves the call leaves staged for the folders it touched, and ``sidecars_held``
    lists the sidecars it leaves in place because their target is taken.
    """

    dry_run: bool
    pattern: str
    matched: int
    staged: int
    folders: int
    kinds: dict[str, int]
    at_target: int
    case_only: int
    kept_staged: int
    unstaged: int
    held_count: int
    held: dict[str, int]
    held_files: tuple[FilePlan, ...]
    staged_targets_under_path: int | None
    sidecars_staged: int
    sidecars_held: tuple[SidecarHold, ...]

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "dry_run": self.dry_run,
            "pattern": self.pattern,
            "matched": self.matched,
            "staged": self.staged,
            "folders": self.folders,
            "kinds": self.kinds,
            "at_target": self.at_target,
            "case_only": self.case_only,
            "kept_staged": self.kept_staged,
            "unstaged": self.unstaged,
            "held_count": self.held_count,
            "held": self.held,
            "held_files": [plan.to_dict() for plan in self.held_files],
            "staged_targets_under_path": self.staged_targets_under_path,
            "sidecars_staged": self.sidecars_staged,
            "sidecars_held": [hold.to_dict() for hold in self.sidecars_held],
        }


def held_counts(plans: Sequence[FilePlan]) -> dict[str, int]:
    """Count the held files per reason, sorted by reason. A file counts once per reason."""
    counts: dict[str, int] = {}
    for plan in plans:
        if plan.status == STATUS_HELD:
            for reason, _ in plan.reasons:
                counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _staged_targets_under(conn: sqlite3.Connection, music_path: Path, root_key: str) -> int:
    """Count the staged moves whose target lies under the folder keyed *root_key*."""
    return sum(
        1
        for row in store.list_staged_paths(conn)
        if path_keys.is_within(path_keys.path_key(music_path / row.to_path), root_key)
    )


def _targets_after(
    conn: sqlite3.Connection,
    moving: list[FilePlan],
    replaced: list[FilePlan],
) -> dict[int, tuple[str, str]]:
    """Return each staged file's ``(to_path, origin)`` once the plan is written."""
    targets = _staged_targets(conn)
    for plan in replaced:
        targets.pop(plan.file_id, None)
    targets.update({plan.file_id: (plan.to_path, _AUTO) for plan in moving if plan.to_path})
    return targets


def _sidecar_units_under(
    conn: sqlite3.Connection,
    music_path: Path,
    root_key: str | None,
) -> set[str]:
    """Return the album folder keys of the staged sidecar moves whose source is in scope."""
    return {
        row.unit_key
        for row in store.list_staged_sidecars(conn)
        if _in_scope(music_path, row.from_path, root_key)
    }


def _write_plan(
    conn: sqlite3.Connection,
    music_path: Path,
    moving: list[FilePlan],
    replaced: list[int],
    note: str | None,
) -> None:
    """Delete every replaced ``auto`` row, then stage each moving file as ``auto``.

    Every delete runs before any insert because two replaced rows may swap targets, and the
    unique ``to_key`` index checks each insert on its own.
    """
    now = clock.utc_now()
    for file_id in replaced:
        store.delete_staged_path(conn, file_id)
    for plan in moving:
        if plan.to_path is None:  # pragma: no cover - defensive, a moving plan has a target
            continue
        size, mtime_ns = _signature(music_path / plan.from_path)
        _ensure_path_baseline(conn, plan.file_id, plan.from_path, now)
        store.upsert_staged_path(
            conn,
            store.StagedPath(
                file_id=plan.file_id,
                to_path=plan.to_path,
                to_key=path_keys.path_key(plan.to_path),
                origin=_AUTO,
                note=note,
                staged_at=now,
                base_size_bytes=size,
                base_mtime_ns=mtime_ns,
                reverted_to_version=None,
            ),
        )


def stage_paths(
    settings: Settings,
    *,
    path: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
    note: str | None = None,
) -> StagePathsResult:
    """Render the persisted naming pattern for every file under *path* and stage the moves.

    Every present file in scope is rendered (:func:`plan_library`), and each moving file is
    staged as an ``auto`` row. A folder moves as a unit or not at all. The call replaces its own
    ``auto`` rows in scope and never touches a ``manual`` or ``revert`` row, whose folder holds
    ``unit_member``. An ``auto`` row whose move already landed on disk is never replaced: the
    file holds ``landed_move``, which names ``commit_paths``.

    A path decision binds the planner. A file whose ``legit_ignore`` keep binds its current
    folder keeps that folder, and only its filename renders. No decision keeps a filename, so a
    filename rename the owner reverted is rendered again on the next call while the old
    filename still agrees with the tags. Change the pattern, or leave the folder out of *path*,
    to keep it.

    A folder whose every file is staged to one other folder carries its sidecars, the non-audio
    files under it, to the same relative place there. A sidecar whose target is taken stays,
    and of two folders claiming one target the folder with the lower file id wins. The call
    also recomputes the sidecar moves whose source sits under *path*, which confirms a sidecar
    that changed at its target after its move landed.

    Refused while ``detect_mismatches`` does not read ``gate_open: true`` and on a volume the
    volume check refuses, except with *dry_run*, which stages nothing. *path* matches where a
    file sits now (:func:`tagmend.engine.path_keys.folder_arg_key`). Raises
    :class:`ValueError` on a refusal, an invalid persisted pattern or a missing ``music_path``.
    """
    music_path = _require_music_path(settings)
    pattern = naming.effective_pattern(settings)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)
    if not dry_run:
        _require_volume(music_path)
        _require_open_gate(settings)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        plans = plan_library(connection, settings, pattern=pattern, scope_key=root_key)
        in_scope = [
            plan
            for plan in plans
            if root_key is None or path_keys.is_within(plan.folder_key, root_key)
        ]
        moving = [plan for plan in in_scope if plan.status == STATUS_WILL_MOVE]
        replaced = [plan for plan in in_scope if plan.replaces]
        staged_under = None
        if root_key is not None and not in_scope:
            staged_under = _staged_targets_under(connection, music_path, root_key)
        touched = {path_keys.path_key(plan.source_folder) for plan in (*moving, *replaced)}
        sidecars = _plan_sidecars(
            connection,
            music_path,
            touched | _sidecar_units_under(connection, music_path, root_key),
            _targets_after(connection, moving, replaced),
            note=note,
        )
        if not dry_run:
            _write_plan(connection, music_path, moving, [p.file_id for p in replaced], note)
            _apply_sidecar_plan(connection, sidecars)
            connection.commit()
    finally:
        connection.close()

    held = [plan for plan in in_scope if plan.status == STATUS_HELD]
    result = StagePathsResult(
        dry_run=dry_run,
        pattern=pattern.text,
        matched=len(in_scope),
        staged=len(moving),
        folders=len({plan.folder_key for plan in moving}),
        kinds={kind: sum(1 for p in moving if p.kind == kind) for kind in (KIND_RENAME, KIND_MOVE)},
        at_target=sum(1 for plan in in_scope if plan.status == STATUS_AT_TARGET),
        case_only=sum(1 for plan in in_scope if plan.status == STATUS_CASE_ONLY),
        kept_staged=sum(1 for plan in in_scope if plan.status == STATUS_KEPT_STAGED),
        unstaged=sum(1 for plan in replaced if plan.status != STATUS_WILL_MOVE),
        held_count=len(held),
        held=held_counts(held),
        held_files=tuple(held[:_HELD_CAP]),
        staged_targets_under_path=staged_under,
        sidecars_staged=len(sidecars.inserts),
        sidecars_held=sidecars.held,
    )
    logger.info(
        "stage_paths dry_run=%s matched=%d staged=%d held=%d unstaged=%d",
        dry_run,
        result.matched,
        result.staged,
        result.held_count,
        result.unstaged,
    )
    return result


@dataclass(frozen=True, slots=True)
class NamingSettings:
    """The naming settings :func:`set_naming_pattern` saved."""

    pattern: str
    default_pattern: str
    container_folders: tuple[str, ...]
    settings_path: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "pattern": self.pattern,
            "default_pattern": self.default_pattern,
            "container_folders": list(self.container_folders),
            "settings_path": self.settings_path,
        }


def set_naming_pattern(
    settings: Settings,
    *,
    pattern: str | None = None,
    container_folders: Sequence[str] | None = None,
) -> NamingSettings:
    """Save the naming pattern, the container folder list, or both, to ``settings.json``.

    ``None`` leaves that setting unchanged, and an empty *container_folders* clears the list.
    Set *container_folders* before the first ``set_mismatch_status`` decision, since
    ``detect_mismatches`` reads the same list. The pattern is validated by
    :func:`tagmend.engine.naming.parse_pattern` and each folder by
    :func:`tagmend.engine.naming.validate_container_folders`. Refused while any path move is
    staged, since a staged move was rendered by the settings in force. Raises
    :class:`ValueError` on any refusal, saving nothing.
    """
    if pattern is None and container_folders is None:
        message = "pass pattern, container_folders or both. Nothing was saved"
        raise ValueError(message)
    updates: dict[str, str] = {}
    saved_pattern = naming.pattern_text(settings)
    if pattern is not None:
        saved_pattern = naming.parse_pattern(pattern.strip()).text
        updates["naming_pattern"] = saved_pattern
    folders = settings.container_folders
    if container_folders is not None:
        folders = naming.validate_container_folders(container_folders)
        updates["container_folders"] = naming.folder_list_setting(folders)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        staged = len(store.list_staged_paths(connection))
    finally:
        connection.close()
    if staged:
        message = (
            f"{staged} path move(s) are staged under the current naming settings. Run "
            "commit_paths, or unstage_paths(path=...), first. Nothing was saved"
        )
        raise ValueError(message)

    written = config.set_settings(updates)
    logger.info("saved the naming settings: %s", ", ".join(sorted(updates)))
    return NamingSettings(
        pattern=saved_pattern,
        default_pattern=naming.DEFAULT_PATTERN,
        container_folders=folders,
        settings_path=str(written),
    )


def _staged_in_scope(
    conn: sqlite3.Connection,
    root_key: str | None,
) -> list[store.StagedPath]:
    """Return the staged moves under *root_key*, or every staged move when it is ``None``."""
    if root_key is None:
        return store.list_staged_paths(conn)
    return store.list_staged_paths_under(conn, root_key)


@dataclass(frozen=True, slots=True)
class UnstagePathsResult:
    """What one :func:`unstage_paths` call dropped: audio moves and sidecar moves."""

    removed: int
    sidecars_removed: int

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {"removed": self.removed, "sidecars_removed": self.sidecars_removed}


def _refuse_landed(
    conn: sqlite3.Connection,
    music_path: Path,
    rows: list[store.StagedPath],
    sidecars: list[store.StagedSidecar],
) -> None:
    """Refuse the unstage when a matched file or sidecar already sits at its staged target."""
    landed = [
        row.file_id
        for row in rows
        if _locate(_source_of(conn, row.file_id), music_path / row.to_path, row) in _AT_TARGET
    ]
    landed_sidecars = [
        row.from_path for row in sidecars if _locate_sidecar(music_path, row) in _AT_TARGET
    ]
    if not landed and not landed_sidecars:
        return
    named = [f"file_id(s) {landed}"] if landed else []
    if landed_sidecars:
        named.append(f"sidecar(s) {landed_sidecars}")
    message = (
        f"{' and '.join(named)} already sit at their staged target on disk. Run commit_paths "
        "to finish those moves. Nothing was unstaged"
    )
    raise ValueError(message)


def unstage_paths(
    settings: Settings,
    *,
    file_id: int | None = None,
    path: str | os.PathLike[str] | None = None,
) -> UnstagePathsResult:
    """Drop staged moves, by *file_id* or for every file and sidecar under the folder *path*.

    Pass exactly one argument. *path* matches where a file sits now, which is its source while
    its move is staged, and a sidecar by its staged source. A folder that loses a staged file
    no longer moves as a unit, so its sidecar moves are dropped too. The call is refused whole,
    naming them, when any matched file or sidecar already sits at its target, since only a
    commit may finish that move. An unknown *file_id* raises :class:`ValueError`.
    """
    if (file_id is None) == (path is None):
        message = "pass exactly one of file_id and path"
        raise ValueError(message)
    music_path = _require_music_path(settings)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        sidecars: list[store.StagedSidecar] = []
        if file_id is not None:
            if store.get_file_by_id(connection, file_id) is None:
                message = f"unknown file_id={file_id}"
                raise ValueError(message)
            existing = store.get_staged_path(connection, file_id)
            rows = [] if existing is None else [existing]
        else:
            rows = _staged_in_scope(connection, root_key)
            sidecars = [
                row
                for row in store.list_staged_sidecars(connection)
                if _in_scope(music_path, row.from_path, root_key)
            ]
        _refuse_landed(connection, music_path, rows, sidecars)
        touched = {
            path_keys.path_key(_relative(music_path, _source_of(connection, row.file_id).parent))
            for row in rows
        }
        for row in rows:
            store.delete_staged_path(connection, row.file_id)
        for sidecar in sidecars:
            store.delete_staged_sidecar(connection, sidecar.from_key)
        units = _plan_sidecars(
            connection, music_path, touched, _staged_targets(connection), note=None
        )
        _apply_sidecar_plan(connection, units)
        connection.commit()
    finally:
        connection.close()
    return UnstagePathsResult(removed=len(rows), sidecars_removed=len(sidecars) + len(units.drops))


@dataclass(frozen=True, slots=True)
class PathDiffView:
    """One staged move as ``diff_paths`` shows it. Both paths are relative to ``music_path``.

    ``state`` is the row's disk state, one of the module docstring's six. ``stale`` marks an
    ``auto`` row whose target the current tags and naming settings no longer render.
    """

    file_id: int
    from_path: str
    to_path: str
    origin: str
    note: str | None
    staged_at: str
    state: str
    stale: bool

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
            "stale": self.stale,
        }


def diff_paths(
    settings: Settings,
    *,
    path: str | os.PathLike[str] | None = None,
) -> list[PathDiffView]:
    """Return every staged move, or those under the folder *path*, with its disk state.

    Read-only. ``state`` names the row's place in the module docstring's table. An ``auto`` row
    is ``stale`` when the persisted naming settings render the file elsewhere now. The commit
    still applies the staged target, so stage again to follow the new render.
    """
    music_path = _require_music_path(settings)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = _staged_in_scope(connection, root_key)
        renders: dict[int, str | None] = {}
        if any(row.origin == _AUTO for row in rows):
            renders = render_targets(connection, settings, naming.effective_pattern(settings))
        views: list[PathDiffView] = []
        for row in rows:
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
                    stale=row.origin == _AUTO and renders.get(row.file_id) != row.to_path,
                ),
            )
        return views
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class SidecarDiffView:
    """One staged sidecar move as ``diff_paths`` shows it. Paths are relative to ``music_path``.

    ``state`` is the row's disk state, one of the module docstring's six.
    """

    from_path: str
    to_path: str
    origin: str
    note: str | None
    staged_at: str
    state: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "from_path": self.from_path,
            "to_path": self.to_path,
            "origin": self.origin,
            "note": self.note,
            "staged_at": self.staged_at,
            "state": self.state,
        }


def diff_sidecars(
    settings: Settings,
    *,
    path: str | os.PathLike[str] | None = None,
) -> list[SidecarDiffView]:
    """Return every staged sidecar move, or those under the folder *path*, with its disk state.

    Read-only. A row whose target was taken, or that changed after it landed, stays staged after
    its album's audio commits, and it keeps the staging area non-empty until a commit or an
    unstage clears it.
    """
    music_path = _require_music_path(settings)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = store.list_staged_sidecars(connection)
    finally:
        connection.close()
    return [
        SidecarDiffView(
            from_path=row.from_path,
            to_path=row.to_path,
            origin=row.origin,
            note=row.note,
            staged_at=row.staged_at,
            state=_locate_sidecar(music_path, row),
        )
        for row in rows
        if _in_scope(music_path, row.from_path, root_key)
    ]


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
    """The summary of one :func:`commit_paths` call. ``problems`` lists every unfinished move.

    ``sidecars_moved`` counts the sidecars moved and logged, ``sidecars_waiting`` those whose
    album audio still sits in their folder, and ``sidecar_problems`` every other sidecar row.
    ``sidecars_held`` lists the sidecars left with no staged move in a folder the commit's
    audio all left: their target was taken, the folder's files went to different folders, or
    they sit in the release folder above disc folders the commit emptied of audio.
    ``folders_pruned`` counts the emptied folders removed.
    """

    commit_id: int | None
    committed: int
    noop: int
    missing: int
    changed_since_stage: int
    errors: int
    problems: tuple[PathProblem, ...]
    sidecars_moved: int = 0
    sidecars_waiting: int = 0
    sidecars_held: tuple[str, ...] = ()
    folders_pruned: int = 0
    sidecar_problems: tuple[SidecarOutcome, ...] = ()

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
            "sidecars_moved": self.sidecars_moved,
            "sidecars_waiting": self.sidecars_waiting,
            "sidecars_held": list(self.sidecars_held),
            "folders_pruned": self.folders_pruned,
            "sidecar_problems": [outcome.to_dict() for outcome in self.sidecar_problems],
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


@dataclass(frozen=True, slots=True)
class _CommitSidecars:
    """The sidecar half of one commit: the step, the sidecars left behind, folders pruned."""

    step: _SidecarStep
    held: tuple[str, ...]
    pruned: int


def _path_commit_result(
    commit_id: int | None,
    summary: commits.CommitResult,
    targets: dict[int, str],
    sidecars: _CommitSidecars | None = None,
) -> PathCommitResult:
    """Fold a :class:`tagmend.engine.commits.CommitResult` and the sidecar step into the summary."""
    outcomes = () if sidecars is None else sidecars.step.outcomes
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
        sidecars_moved=sum(1 for outcome in outcomes if outcome.status == SIDECAR_MOVED),
        sidecars_waiting=sum(1 for outcome in outcomes if outcome.status == SIDECAR_WAITING),
        sidecars_held=() if sidecars is None else sidecars.held,
        folders_pruned=0 if sidecars is None else sidecars.pruned,
        sidecar_problems=tuple(
            outcome
            for outcome in outcomes
            if outcome.status not in {SIDECAR_MOVED, SIDECAR_WAITING}
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


def _pattern_message(settings: Settings) -> str:
    """Return the default commit message, which records the naming pattern in force."""
    return f"naming pattern {naming.pattern_text(settings)}"


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
    empties are pruned. The sidecar step then moves each staged sidecar whose source is in
    scope and whose album audio has left its folder, and prunes each album folder whose last
    sidecar row left. A move left unfinished keeps its row, except a ``missing`` one, and is
    listed under ``problems`` or ``sidecar_problems``. The commit is marked applied only after
    the sidecar step. ``commit_id`` is ``None`` when nothing was staged. Without a
    *message* the commit records the naming pattern in force. Raises :class:`ValueError` when
    ``music_path`` is unset or the volume check refuses it.
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
        sidecar_origins = {
            row.origin
            for row in store.list_staged_sidecars(connection)
            if _in_scope(music_path, row.from_path, root_key)
        }
        if not staged and not sidecar_origins:
            return _path_commit_result(None, commits.summarize(commit_id=None, applied=[]), {})
        _refuse_flagged(connection, settings, music_path, staged)

        commit_id = commits.create_commit(
            connection,
            origin=_commit_origin({row.origin for row in staged} | sidecar_origins),
            message=message if message is not None else _pattern_message(settings),
            now=clock.utc_now(),
        )
        connection.commit()  # commit row durable before any per-file work
        applied = commits.run_commit(
            connection,
            domain,
            commit_id=commit_id,
            file_ids=[row.file_id for row in staged],
        )
        step = _sidecar_step(connection, music_path, commit_id=commit_id, root_key=root_key)
        vacated = _vacated_by(connection, music_path, commit_id) | set(step.vacated)
        vacated |= _emptied_release_folders(settings, vacated)
        sidecars = _CommitSidecars(
            step=step,
            held=tuple(_left_behind(connection, music_path, vacated)),
            pruned=len(domain.pruned) + len(step.pruned),
        )
        commits.set_commit_status(connection, commit_id, "applied")
        connection.commit()
    finally:
        connection.close()

    result = _path_commit_result(
        commit_id,
        commits.summarize(commit_id=commit_id, applied=applied),
        {row.file_id: row.to_path for row in staged},
        sidecars,
    )
    logger.info(
        "path commit %d: committed=%d missing=%d changed_since_stage=%d errors=%d "
        "sidecars_moved=%d sidecars_waiting=%d",
        commit_id,
        result.committed,
        result.missing,
        result.changed_since_stage,
        result.errors,
        result.sidecars_moved,
        result.sidecars_waiting,
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
            reverted_to_version=restores,
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


@dataclass(frozen=True, slots=True)
class PathRevertCommitResult(commits.RevertCommitResult):
    """A path commit's revert, with what happened to each of its sidecars.

    ``sidecars`` holds one outcome per sidecar the commit moved, from where it sits now to
    where the revert puts it. ``reverted`` marks a sidecar moved back.
    """

    sidecars: tuple[SidecarOutcome, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        # A slots dataclass is rebuilt as a new class, which zero-argument super() cannot see.
        return {
            **commits.RevertCommitResult.to_dict(self),
            "sidecars_reverted": sum(1 for o in self.sidecars if o.status == "reverted"),
            "sidecars": [outcome.to_dict() for outcome in self.sidecars],
        }


def _with_sidecars(
    result: commits.RevertCommitResult,
    sidecars: Sequence[SidecarOutcome],
) -> PathRevertCommitResult:
    """Return *result* with the sidecar outcomes of the same revert."""
    values = {item.name: getattr(result, item.name) for item in fields(result)}
    return PathRevertCommitResult(**values, sidecars=tuple(sidecars))


# The revert kinds that leave an audio file where the reverted commit put it.
_AUDIO_STAYS: Final = frozenset({"skipped_later_changes", "error"})


def _audio_staying(
    conn: sqlite3.Connection,
    planned: Sequence[tuple[store.PathRevision, str, str | None, Path | None]],
) -> dict[tuple[str, str], list[tuple[str, int]]]:
    """Map ``(unit key, folder key)`` to the ``(kind, file_id)`` of audio the revert leaves.

    The unit key is the folder the file left, which names its sidecars' unit, so a unit merged
    into a shared destination never holds another unit's sidecars. The folder key is where the
    file sits now, since a later commit may have moved it away from those sidecars. A file gone
    from disk leaves no audio for a sidecar to stay with.
    """
    staying: dict[tuple[str, str], list[tuple[str, int]]] = {}
    row: store.FileRow | None = None
    unit_key = ""
    for revision, kind, _, _ in planned:
        row = store.get_file_by_id(conn, revision.file_id) if kind in _AUDIO_STAYS else None
        if row is None or row.is_missing or not _present(Path(row.folder) / row.filename):
            continue
        unit_key = path_keys.path_key(Path(revision.from_path).parent)
        staying.setdefault((unit_key, path_keys.path_key(row.folder)), []).append(
            (kind, revision.file_id)
        )
    return staying


def _classify_sidecar_revert(
    conn: sqlite3.Connection,
    music_path: Path,
    move: store.SidecarMove,
    staying: dict[tuple[str, str], list[tuple[str, int]]],
) -> tuple[str, str | None]:
    """Classify one sidecar move of the reverted commit, with a detail.

    The kind is ``revertable``, ``skipped_later_changes`` (a later move left or reached its
    target), ``missing`` (it is not at its target) or ``error`` (its source is taken now). A
    sidecar whose own unit's audio in *staying* still sits in its album folder takes that
    audio's kind, so the cover stays with its album.
    """
    album_key: str | None = None
    if store.sidecar_moved_later(conn, move.id, move.to_key):
        return "skipped_later_changes", None
    if not _present(music_path / move.to_path):
        return MISSING, None
    if _listing_holds_key(music_path / move.from_path):
        return "error", f"{move.from_path} is taken on disk"
    album_key = path_keys.path_key(
        music_path / _unit_folder_of(move.to_path, _unit_depth(move.from_key, move.unit_key))
    )
    held_audio = staying.get((move.unit_key, album_key))
    if held_audio:
        kind = held_audio[0][0]
        ids = [file_id for _, file_id in held_audio]
        return kind, f"its album's file_id(s) {ids} do not revert, so it stays with them"
    return "revertable", None


def _stage_sidecar_revert(
    conn: sqlite3.Connection,
    music_path: Path,
    move: store.SidecarMove,
    *,
    note: str | None,
    now: str,
) -> None:
    """Stage the move of a logged sidecar back to its source, origin ``revert``.

    Its album folder is now the folder its target sits in at the same depth, which the revert
    of the album's audio leaves.
    """
    to_path = respell_folders(music_path, move.from_path)
    depth = _unit_depth(move.from_key, move.unit_key)
    size, mtime_ns = _signature(music_path / move.to_path)
    store.insert_staged_sidecar(
        conn,
        store.StagedSidecar(
            from_key=move.to_key,
            from_path=move.to_path,
            to_path=to_path,
            to_key=path_keys.path_key(to_path),
            unit_key=path_keys.path_key(_unit_folder_of(move.to_path, depth)),
            base_size_bytes=size,
            base_mtime_ns=mtime_ns,
            origin=_REVERT,
            reverted_from=move.id,
            note=note,
            staged_at=now,
        ),
    )


def _sidecar_revert_outcome(
    move: store.SidecarMove,
    kind: str,
    detail: str | None,
    outcome: SidecarOutcome | None,
) -> SidecarOutcome:
    """Return the public outcome of one sidecar of the reverted commit."""
    if kind != "revertable":
        return SidecarOutcome(move.to_path, move.from_path, kind, detail)
    if outcome is None:
        return SidecarOutcome(move.to_path, move.from_path, "reverted")
    if outcome.status == SIDECAR_MOVED:
        return replace(outcome, status="reverted")
    return outcome


def revert_commit_moves(  # noqa: PLR0913 - the revert_commit surface plus its open connection
    conn: sqlite3.Connection,
    settings: Settings,
    commit_id: int,
    *,
    note: str | None,
    dry_run: bool,
    path: str | os.PathLike[str] | None = None,
) -> commits.RevertCommitResult:
    """Move every file and sidecar *commit_id* moved back to its source, under ONE new commit.

    The path half of :func:`tagmend.engine.versioning.revert_commit`, which has already checked
    the target commit and that nothing is staged. *path* keeps the files and sidecars sitting
    at or under it now, so one album folder, or one sidecar alone, reverts. A file a later
    path commit moved again is ``skipped_later_changes``, a file gone from disk is ``missing``,
    and a source now taken is an ``error``. A sidecar a later move left or reached is
    ``skipped_later_changes``, and one whose album audio in scope does not revert takes that
    audio's kind and stays with it. The rest are staged as revert rows and run through the commit
    loop, then the sidecar step, so a crash leaves rows :func:`commit_paths` finishes.
    *dry_run* returns the classification only.
    """
    music_path = _require_music_path(settings)
    _require_volume(music_path)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)
    latest_versions = store.path_versions(conn)
    planned = [
        (revision, *_classify_revert(conn, music_path, revision, latest_versions))
        for revision in store.path_revisions_for_commit(conn, commit_id)
        if root_key is None
        or path_keys.is_within(path_keys.path_key(_source_of(conn, revision.file_id)), root_key)
    ]
    staying = _audio_staying(conn, planned)
    sidecars = [
        (move, *_classify_sidecar_revert(conn, music_path, move, staying))
        for move in store.sidecar_moves_for_commit(conn, commit_id)
        if _in_scope(music_path, move.to_path, root_key)
    ]
    revertable = [(revision, source) for revision, kind, _, source in planned if source is not None]
    revertable_sidecars = [move for move, kind, _ in sidecars if kind == "revertable"]
    if dry_run or not (revertable or revertable_sidecars):
        outcomes = [
            _revert_outcome(revision, kind, detail, None) for revision, kind, detail, _ in planned
        ]
        summary = commits.summarize_revert(
            commit_id=None, reverted_from=commit_id, dry_run=dry_run, outcomes=outcomes
        )
        return _with_sidecars(
            summary,
            [_sidecar_revert_outcome(move, kind, detail, None) for move, kind, detail in sidecars],
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
    for move in revertable_sidecars:
        _stage_sidecar_revert(conn, music_path, move, note=note, now=now)
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
    step = _sidecar_step(conn, music_path, commit_id=new_commit, root_key=None)
    commits.set_commit_status(conn, new_commit, "applied")
    conn.commit()

    by_file = {item.outcome.file_id: item.outcome for item in applied}
    outcomes = [
        _revert_outcome(revision, kind, detail, by_file.get(revision.file_id))
        for revision, kind, detail, _ in planned
    ]
    by_source = {outcome.from_path: outcome for outcome in step.outcomes}
    result = _with_sidecars(
        commits.summarize_revert(
            commit_id=new_commit, reverted_from=commit_id, dry_run=False, outcomes=outcomes
        ),
        [
            _sidecar_revert_outcome(move, kind, detail, by_source.get(move.to_path))
            for move, kind, detail in sidecars
        ],
    )
    logger.info(
        "path revert of commit %d as commit %d: reverted=%d skipped=%d missing=%d errors=%d "
        "sidecars=%d",
        commit_id,
        new_commit,
        result.reverted,
        result.skipped,
        result.missing,
        result.errors,
        len(result.sidecars),
    )
    return result


# --- health ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StagingReport:
    """What ``check_health`` reports about the path staging area and the volume.

    ``sidecars_landed`` names the source of each staged sidecar already at its target.
    """

    staged: int
    landed: tuple[int, ...]
    gone: tuple[int, ...]
    sidecars: int
    sidecars_landed: tuple[str, ...]
    volume_refusal: str | None


def staging_report(settings: Settings) -> StagingReport:
    """Count the staged moves and sidecar moves, naming those at their target or gone. Read-only."""
    music_path = settings.music_path
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = store.list_staged_paths(connection)
        sidecar_rows = store.list_staged_sidecars(connection)
        if music_path is None:
            return StagingReport(
                staged=len(rows),
                landed=(),
                gone=(),
                sidecars=len(sidecar_rows),
                sidecars_landed=(),
                volume_refusal=None,
            )
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
        sidecars=len(sidecar_rows),
        sidecars_landed=tuple(
            row.from_path for row in sidecar_rows if _locate_sidecar(music_path, row) in _AT_TARGET
        ),
        volume_refusal=volume_refusal(music_path),
    )
