"""Album covers: which albums Navidrome shows with a cover, and which show none.

Navidrome takes an album's cover from an image in the album's folders whose lowercased name
matches ``cover.*``, ``folder.*`` or ``front.*`` (its default ``CoverArtPriority``), and then
from a picture embedded in a track. :func:`plan_album_covers` groups the present files into
albums by :func:`tagmend.engine.detector_core.album_identity` with the release date Navidrome
keys on, and gives each album one status. :func:`detect_cover_gaps` reports the albums that
show no cover. Both write nothing.

:func:`stage_covers` stages one cover image per ``gap`` album in ``cover_writes_staged``, bytes
included. :func:`unstage_covers` drops staged rows and :func:`diff_covers` shows them with their
live state. None of the three writes to the library. :func:`commit_covers` writes the staged
covers as one commit, logged in ``cover_writes``. It creates new files and never overwrites one.
:func:`revert_cover_commit` undoes such a commit. It sends each cover the commit created to the
OS trash and writes each cover the commit removed again.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import sqlite3
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import mutagen

from tagmend.engine import (
    clock,
    commits,
    db,
    ledger_lock,
    mismatch,
    path_keys,
    paths,
    scan,
    schema,
    store,
)
from tagmend.engine.coverart import CoverArtClient, CoverArtError
from tagmend.engine.detector_core import (
    album_identity,
    display_album_artist,
    group_by_key,
    release_date,
)
from tagmend.engine.lookup_clients import injected_or_owned
from tagmend.engine.serialize import FieldDict
from tagmend.engine.tags import has_embedded_picture
from tagmend.engine.validation import check_limit, require_music_path
from tagmend.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from tagmend.config import Settings
    from tagmend.engine.coverart import CoverArtFront, CoverArtKind, CoverArtSource

logger = get_logger(__name__)

COVER_PATTERNS: Final = ("cover.*", "folder.*", "front.*")
IMAGE_SUFFIXES: Final = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"})

STATUS_COVERED_BY_FILE: Final = "covered_by_file"
STATUS_COVERED_BY_PICTURE: Final = "covered_by_picture"
STATUS_LIBRARY_ROOT: Final = "library_root"
STATUS_SHARED_FOLDER: Final = "shared_folder"
STATUS_SCATTERED: Final = "scattered"
STATUS_GAP: Final = "gap"
COVERED_STATUSES: Final = frozenset({STATUS_COVERED_BY_FILE, STATUS_COVERED_BY_PICTURE})

MAX_COVER_BYTES: Final = 32 * 1024 * 1024

SOURCE_OWNER_FILE: Final = "owner_file"
SOURCE_FOLDER_IMAGE: Final = "folder_image"
SOURCE_RELEASE: Final = "release"
SOURCE_RELEASE_GROUP: Final = "release_group"

SKIP_ALREADY_STAGED: Final = "already_staged"
SKIP_AMBIGUOUS_IMAGES: Final = "ambiguous_images"
SKIP_INVALID_IMAGE: Final = "invalid_image"
SKIP_LOOKUP_ERROR: Final = "lookup_error"
SKIP_NO_SOURCE: Final = "no_source"
SKIP_TARGET_TAKEN: Final = "target_taken"

STATE_READY: Final = "ready"
STATE_TARGET_TAKEN: Final = "target_taken"
STATE_COVERED_SINCE_STAGE: Final = "covered_since_stage"
STATE_ALBUM_MOVED: Final = "album_moved"
STATE_LANDED: Final = "landed"

_ORIGIN_AUTO: Final = "auto"
_ORIGIN_MANUAL: Final = "manual"
_SOURCE_SUFFIXES: Final = frozenset({".jpg", ".jpeg", ".png"})
_TARGET_NAMES: Final = {"jpeg": "cover.jpg", "png": "cover.png"}

_JPEG_SIGNATURE: Final = b"\xff\xd8\xff"
_JPEG_MARKER_PREFIX: Final = 0xFF
# SOF0 to SOF15 carry the frame size, except DHT (C4), JPG (C8) and DAC (CC) in the same range.
_JPEG_FRAME_MARKERS: Final = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# TEM and RST0 to RST7 stand alone, with no length field after them.
_JPEG_BARE_MARKERS: Final = frozenset({0x01, *range(0xD0, 0xD8)})
# SOS and EOI: no frame header follows the scan data or the end of the image.
_JPEG_SCAN_MARKERS: Final = frozenset({0xDA, 0xD9})
_JPEG_MIN_SEGMENT_LENGTH: Final = 2
_JPEG_FRAME_SIZE_BYTES: Final = 4
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_PNG_HEADER_END: Final = 24

_FIELDS: Final = (
    "album",
    "albumartist",
    "artist",
    "compilation",
    "musicbrainz_albumid",
    "musicbrainz_releasegroupid",
    "date",
    "releasedate",
    "year",
)


@dataclass(frozen=True, slots=True)
class AlbumCover(FieldDict):
    """One album's cover status.

    ``folders`` and ``target_folder`` are absolute, in the spelling the snapshot stores.
    ``target_folder`` is the folder a cover file belongs in, set for a ``gap`` album only.
    ``images`` lists the image files under it, relative to it.
    """

    identity: tuple[str, ...]
    album: str | None
    album_artist: str
    file_ids: tuple[int, ...]
    folders: tuple[str, ...]
    status: str
    target_folder: str | None
    release_mbid: str | None
    release_group_mbid: str | None
    images: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CoverGapsReport(FieldDict):
    """One :func:`detect_cover_gaps` run. A *limit* caps ``rows`` and never the counts."""

    albums: int
    covered_by_file: int
    covered_by_picture: int
    gap: int
    shared_folder: int
    scattered: int
    library_root: int
    rows: list[AlbumCover]
    summary: str


@dataclass(frozen=True, slots=True)
class AlbumTrack:
    """One present file reduced to what the album grouping and the cover rules read."""

    file_id: int
    folder: str
    folder_key: str
    filename: str
    identity: tuple[str, ...]
    album: str | None
    album_artist: str
    release_group_mbid: str | None


@dataclass(frozen=True, slots=True)
class _Library:
    """The present audio by folder, which the parent and shared-folder rules compare against.

    ``folder_keys`` is ``identities`` sorted, so a subtree is one bisected range.
    """

    music_key: str
    identities: dict[str, frozenset[tuple[str, ...]]]
    folder_keys: tuple[str, ...]


def load_album_tracks(conn: sqlite3.Connection, music_path: Path) -> list[AlbumTrack]:
    """Read every present file under ``music_path`` and the tags the album identity compares.

    The files come in file id order. A row left under an earlier ``music_path`` is skipped,
    since its cover target would sit outside the library.
    """
    music_key = path_keys.path_key(music_path)
    tag_values = store.load_tag_values(conn, _FIELDS)
    tracks: list[AlbumTrack] = []
    for row in store.list_files(conn):
        if row.is_missing or not path_keys.is_within(path_keys.path_key(row.folder), music_key):
            continue
        values = tag_values.get(row.id, {})
        artist = display_album_artist(
            values.get("albumartist"), values.get("compilation"), values.get("artist")
        )
        identity = album_identity(
            values.get("musicbrainz_albumid"),
            artist,
            values.get("album"),
            release_date(values, row.filename),
        )
        tracks.append(
            AlbumTrack(
                file_id=row.id,
                folder=row.folder,
                folder_key=path_keys.path_key(row.folder),
                filename=row.filename,
                identity=identity,
                album=values.get("album"),
                album_artist=artist,
                release_group_mbid=(values.get("musicbrainz_releasegroupid") or "").strip() or None,
            ),
        )
    return tracks


def _index_library(music_path: Path, tracks: list[AlbumTrack]) -> _Library:
    """Return the identities of the present audio in each folder."""
    grouped = group_by_key(tracks, lambda track: track.folder_key)
    identities = {key: frozenset(t.identity for t in members) for key, members in grouped.items()}
    return _Library(
        music_key=path_keys.path_key(music_path),
        identities=identities,
        folder_keys=tuple(sorted(identities)),
    )


def _audio_folders_under(library: _Library, root_key: str) -> set[str]:
    """Return the keys of the folders at or under *root_key* that hold present audio."""
    low, high = path_keys.subtree_bounds(root_key)
    keys = library.folder_keys
    under = set(keys[bisect_left(keys, low) : bisect_left(keys, high)])
    if root_key in library.identities:
        under.add(root_key)
    return under


def _lone_parent(library: _Library, folders: tuple[str, ...]) -> str | None:
    """Return the one parent of every folder in *folders*, when it lies strictly in the library."""
    parents = {
        path_keys.path_key(Path(folder).parent): str(Path(folder).parent) for folder in folders
    }
    if len(parents) != 1:
        return None
    parent_key, parent = next(iter(parents.items()))
    if parent_key == library.music_key or not path_keys.is_within(parent_key, library.music_key):
        return None
    return parent


def _holds_only(library: _Library, root: str, folder_keys: frozenset[str]) -> bool:
    """Whether every present audio file at or under *root* sits in one of *folder_keys*."""
    return _audio_folders_under(library, path_keys.path_key(root)) <= folder_keys


def _direct_images(folder: str) -> list[str]:
    """Return the names of the image files directly in *folder*, none when it is unlistable."""
    try:
        with os.scandir(folder) as listing:
            names = [
                entry.name
                for entry in listing
                if entry.is_file() and Path(entry.name).suffix.lower() in IMAGE_SUFFIXES
            ]
    except OSError:
        return []
    return sorted(names)


def _is_cover_name(name: str) -> bool:
    """Whether Navidrome reads the image *name* as an album cover."""
    return any(fnmatch.fnmatchcase(name.lower(), pattern) for pattern in COVER_PATTERNS)


def cover_images_in(folder: str) -> list[str]:
    """Return the names of the images directly in *folder* that Navidrome reads as a cover."""
    return [name for name in _direct_images(folder) if _is_cover_name(name)]


def _covered_by_file(library: _Library, folders: tuple[str, ...], keys: frozenset[str]) -> bool:
    """Whether a cover image sits in one of *folders*, or in the parent Navidrome also reads."""
    images = {folder: _direct_images(folder) for folder in folders}
    if any(_is_cover_name(name) for names in images.values() for name in names):
        return True
    parent = _lone_parent(library, folders)
    # Navidrome reads the parent's images only when the album's one folder holds no image.
    if parent is None or (len(folders) == 1 and images[folders[0]]):
        return False
    if not _holds_only(library, parent, keys):
        return False
    return any(_is_cover_name(name) for name in _direct_images(parent))


def _covered_by_picture(members: list[AlbumTrack]) -> bool:
    """Whether any of *members* embeds a picture, probed in file id order up to the first hit."""
    for track in members:
        path = Path(track.folder) / track.filename
        try:
            if has_embedded_picture(path):
                return True
        except (mutagen.MutagenError, OSError) as exc:  # type: ignore[attr-defined]
            logger.warning("cover probe: cannot read %s: %s", path, exc)
    return False


def _placement(
    settings: Settings,
    library: _Library,
    identity: tuple[str, ...],
    folders: tuple[str, ...],
    keys: frozenset[str],
) -> tuple[str, str | None]:
    """Return the status an uncovered album takes from where its files sit, and its target."""
    if len(folders) == 1:
        key = path_keys.path_key(folders[0])
        if key == library.music_key:
            return (STATUS_LIBRARY_ROOT, None)
        if library.identities.get(key, frozenset()) - {identity}:
            return (STATUS_SHARED_FOLDER, None)
        return (STATUS_GAP, folders[0])
    release = _lone_parent(library, folders)
    if release is None or not _is_disc_release(settings, library, release, folders, keys):
        return (STATUS_SCATTERED, None)
    return (STATUS_GAP, release)


def _is_disc_release(
    settings: Settings,
    library: _Library,
    release: str,
    folders: tuple[str, ...],
    keys: frozenset[str],
) -> bool:
    """Whether *folders* are disc folders that hold every present audio file under *release*."""
    if any(mismatch.layout_of(settings, folder, "").disc_folder is None for folder in folders):
        return False
    return _holds_only(library, release, keys)


def _images_under(target: str | None) -> tuple[str, ...]:
    """Return the image files under *target*, relative to it, by the sidecar walk's rule."""
    if target is None:
        return ()
    found, _ = paths.sidecar_files(Path(target))
    return tuple(
        sorted(
            str(path.relative_to(target)) for path in found if path.suffix.lower() in IMAGE_SUFFIXES
        )
    )


def _plan_album(settings: Settings, library: _Library, members: list[AlbumTrack]) -> AlbumCover:
    """Return the cover status of the album whose present files are *members*, in id order."""
    # Input
    first = members[0]
    spellings = {track.folder_key: track.folder for track in members}
    folders = tuple(spellings[key] for key in sorted(spellings))
    keys = frozenset(spellings)
    release_groups = {track.release_group_mbid for track in members} - {None}
    target: str | None = None

    # Process
    if _covered_by_file(library, folders, keys):
        status = STATUS_COVERED_BY_FILE
    elif _covered_by_picture(members):
        status = STATUS_COVERED_BY_PICTURE
    else:
        status, target = _placement(settings, library, first.identity, folders, keys)

    # Output
    return AlbumCover(
        identity=first.identity,
        album=first.album,
        album_artist=first.album_artist,
        file_ids=tuple(track.file_id for track in members),
        folders=folders,
        status=status,
        target_folder=target,
        release_mbid=first.identity[1] if first.identity[0] == "release" else None,
        release_group_mbid=next(iter(release_groups)) if len(release_groups) == 1 else None,
        images=_images_under(target),
    )


def plan_album_covers(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    folder_key: str | None = None,
) -> list[AlbumCover]:
    """Return the cover status of every album, in the order of each album's first file id.

    *folder_key* keeps the albums with a file at or under that folder. The shared-folder and
    parent rules still read the whole library. Raises :class:`ValueError` without ``music_path``.
    """
    music_path = require_music_path(settings)
    tracks = load_album_tracks(conn, music_path)
    library = _index_library(music_path, tracks)

    albums = group_by_key(tracks, lambda track: track.identity)
    selected = [
        members
        for members in albums.values()
        if folder_key is None
        or any(path_keys.is_within(track.folder_key, folder_key) for track in members)
    ]
    return [_plan_album(settings, library, members) for members in selected]


def _summarize(albums: int, counts: dict[str, int]) -> str:
    """Build a short, plain human summary of the run."""
    uncovered = albums - sum(counts[status] for status in COVERED_STATUSES)
    if not uncovered:
        return f"Every album shows a cover ({albums} album(s) checked)."
    return (
        f"{uncovered} of {albums} album(s) show no cover: {counts[STATUS_GAP]} gap, "
        f"{counts[STATUS_SHARED_FOLDER]} shared folder, {counts[STATUS_SCATTERED]} scattered, "
        f"{counts[STATUS_LIBRARY_ROOT]} at the library root."
    )


def detect_cover_gaps(
    settings: Settings,
    *,
    path: str | None = None,
    limit: int | None = None,
) -> CoverGapsReport:
    """Report the albums Navidrome shows without a cover. Read-only over the snapshot.

    *path* keeps the albums with a file in this folder or any folder under it, compared as a
    path (:func:`tagmend.engine.path_keys.folder_arg_key`). *limit* caps the rows. Raises
    :class:`ValueError` for a negative *limit*, a *path* outside ``music_path`` and a missing
    ``music_path``.
    """
    # Input
    check_limit(limit)
    folder_key = None if path is None else path_keys.folder_arg_key(settings, path)
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        albums = plan_album_covers(connection, settings, folder_key=folder_key)
    finally:
        connection.close()

    # Process
    counts = dict.fromkeys(
        (
            STATUS_COVERED_BY_FILE,
            STATUS_COVERED_BY_PICTURE,
            STATUS_GAP,
            STATUS_SHARED_FOLDER,
            STATUS_SCATTERED,
            STATUS_LIBRARY_ROOT,
        ),
        0,
    )
    for album in albums:
        counts[album.status] += 1
    rows = sorted(
        (album for album in albums if album.status not in COVERED_STATUSES),
        key=lambda album: (album.target_folder or album.folders[0], album.album or ""),
    )
    logger.info("cover gaps: %d of %d album(s) show no cover", len(rows), len(albums))

    # Output
    return CoverGapsReport(
        albums=len(albums),
        covered_by_file=counts[STATUS_COVERED_BY_FILE],
        covered_by_picture=counts[STATUS_COVERED_BY_PICTURE],
        gap=counts[STATUS_GAP],
        shared_folder=counts[STATUS_SHARED_FOLDER],
        scattered=counts[STATUS_SCATTERED],
        library_root=counts[STATUS_LIBRARY_ROOT],
        rows=rows if limit is None else rows[:limit],
        summary=_summarize(len(albums), counts),
    )


# --- the image check -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImageInfo(FieldDict):
    """The format and pixel size of one cover image. ``format`` is ``jpeg`` or ``png``."""

    format: str
    width: int
    height: int


def inspect_image(data: bytes) -> ImageInfo:
    """Return the format and pixel size of a JPEG or PNG image, read from its header.

    Raises :class:`ValueError` naming the problem for any other format, a truncated header, a
    zero dimension and an image over :data:`MAX_COVER_BYTES`.
    """
    if len(data) > MAX_COVER_BYTES:
        message = f"image is {len(data)} bytes, over the {MAX_COVER_BYTES}-byte cap"
        raise ValueError(message)
    if data.startswith(_JPEG_SIGNATURE):
        image_format, (width, height) = "jpeg", _jpeg_size(data)
    elif data.startswith(_PNG_SIGNATURE):
        image_format, (width, height) = "png", _png_size(data)
    else:
        message = "image is neither JPEG nor PNG"
        raise ValueError(message)
    if not width or not height:
        message = f"{image_format} image has a zero dimension ({width}x{height})"
        raise ValueError(message)
    return ImageInfo(format=image_format, width=width, height=height)


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Return the width and height of the first JPEG frame header, walking the segment lengths."""
    offset = 2
    while offset + 1 < len(data):
        if data[offset] != _JPEG_MARKER_PREFIX:
            message = f"JPEG has no segment marker at byte {offset}"
            raise ValueError(message)
        marker = data[offset + 1]
        if marker == _JPEG_MARKER_PREFIX:
            offset += 1
            continue
        if marker in _JPEG_BARE_MARKERS:
            offset += 2
            continue
        if marker in _JPEG_SCAN_MARKERS:
            message = "JPEG reaches its scan data before any frame header"
            raise ValueError(message)
        segment = offset + 2
        if marker in _JPEG_FRAME_MARKERS:
            return _jpeg_frame_size(data, segment)
        length = int.from_bytes(data[segment : segment + 2])
        if length < _JPEG_MIN_SEGMENT_LENGTH:
            message = f"JPEG segment at byte {offset} is truncated or has an invalid length"
            raise ValueError(message)
        offset = segment + length
    message = "JPEG is truncated before its frame header"
    raise ValueError(message)


def _jpeg_frame_size(data: bytes, segment: int) -> tuple[int, int]:
    """Return the width and height of the frame header whose length field starts at *segment*.

    The two length bytes are followed by one precision byte, then the height, then the width.
    """
    size = data[segment + 3 : segment + 3 + _JPEG_FRAME_SIZE_BYTES]
    if len(size) < _JPEG_FRAME_SIZE_BYTES:
        message = "JPEG is truncated inside its frame header"
        raise ValueError(message)
    return int.from_bytes(size[2:4]), int.from_bytes(size[0:2])


def _png_size(data: bytes) -> tuple[int, int]:
    """Return the width and height of the ``IHDR`` chunk that follows the PNG signature."""
    if len(data) < _PNG_HEADER_END or data[12:16] != b"IHDR":
        message = "PNG is truncated or does not open with its IHDR chunk"
        raise ValueError(message)
    return int.from_bytes(data[16:20]), int.from_bytes(data[20:24])


# --- stage_covers ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverStaged(FieldDict):
    """One cover :func:`stage_covers` staged, or would stage on a dry run.

    ``target_path`` is relative to ``music_path``. A dry run downloads no image, so its CAA rows
    leave ``format``, ``width``, ``height`` and ``size_bytes`` ``None``.
    """

    target_path: str
    album: str
    source_kind: str
    source_ref: str
    origin: str
    format: str | None
    width: int | None
    height: int | None
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class CoverSkipped(FieldDict):
    """One album :func:`stage_covers` staged no cover for. ``folder`` is relative to the library."""

    folder: str
    album: str
    reason: str
    detail: str | None


@dataclass(frozen=True, slots=True)
class StageCoversResult(FieldDict):
    """What one :func:`stage_covers` call staged and skipped.

    ``more`` is true when *limit* left a gap album waiting for an uncached CAA lookup.
    """

    staged: list[CoverStaged]
    skipped: list[CoverSkipped]
    more: bool
    dry_run: bool
    summary: str


@dataclass(frozen=True, slots=True)
class _Skip:
    """Why one gap album gets no cover."""

    reason: str
    detail: str | None


@dataclass(frozen=True, slots=True)
class _Source:
    """The image one album's cover comes from. A dry run's CAA pick holds no bytes."""

    kind: str
    ref: str
    origin: str
    content: bytes | None
    info: ImageInfo | None


@dataclass(frozen=True, slots=True)
class _Pick:
    """One gap album with its source and its target, both relative to ``music_path``."""

    album: AlbumCover
    folder: str
    target_path: str
    source: _Source


@dataclass(frozen=True, slots=True)
class _Selection:
    """The gap albums a call sources and the albums it skips outright."""

    work: list[AlbumCover]
    skipped: list[CoverSkipped]


@dataclass(slots=True)
class _LookupBudget:
    """The uncached CAA lookups one call may still send, ``None`` for no cap.

    ``deferred`` counts the gap albums left unvisited because no lookup was left for them.
    """

    left: int | None
    deferred: int = 0

    def admits(self, *, needs_request: bool) -> bool:
        """Spend one lookup on an album that needs a request, or defer it when none is left."""
        if not needs_request or self.left is None:
            return True
        if self.left == 0:
            self.deferred += 1
            return False
        self.left -= 1
        return True


def _album_label(album: AlbumCover) -> str:
    """Return the album's display name, its album artist and title."""
    return f"{album.album_artist} - {album.album}" if album.album else album.album_artist


def _album_folder(music_path: Path, album: AlbumCover) -> str:
    """Return the album's target folder, else its first folder, relative to *music_path*."""
    return os.path.relpath(album.target_folder or album.folders[0], music_path)


def _skip_row(music_path: Path, album: AlbumCover, skip: _Skip) -> CoverSkipped:
    """Build the result row of one skipped album."""
    return CoverSkipped(
        folder=_album_folder(music_path, album),
        album=_album_label(album),
        reason=skip.reason,
        detail=skip.detail,
    )


def _owner_image(music_path: Path, image: str | os.PathLike[str]) -> Path:
    """Return the owner's *image* as a path. A relative one resolves against *music_path*."""
    path = Path(image)
    if not path.is_absolute():
        path = music_path / path
    if not path.is_file():
        message = f"image {path} is not a regular file"
        raise ValueError(message)
    return path


def _read_image(path: Path) -> bytes:
    """Read *path*, at most one byte past the cap, so an oversized file fails the check."""
    with path.open("rb") as handle:
        return handle.read(MAX_COVER_BYTES + 1)


def _select(
    music_path: Path,
    albums: list[AlbumCover],
    staged_folders: set[str],
    *,
    owner: bool,
) -> _Selection:
    """Split *albums* into the gap albums to source and the albums skipped before any lookup.

    The owner's image needs exactly one gap album, and replaces that album's staged row.
    """
    gaps = [album for album in albums if album.status == STATUS_GAP]
    if owner and len(gaps) != 1:
        message = f"path selects {len(gaps)} album(s) with status gap, and image needs exactly one"
        raise ValueError(message)
    skipped = [
        _skip_row(music_path, album, _Skip(album.status, None))
        for album in albums
        if album.status != STATUS_GAP
    ]
    fresh: list[AlbumCover] = []
    for album in gaps:
        if not owner and path_keys.path_key(_album_folder(music_path, album)) in staged_folders:
            skipped.append(_skip_row(music_path, album, _Skip(SKIP_ALREADY_STAGED, None)))
        else:
            fresh.append(album)
    return _Selection(work=fresh, skipped=skipped)


def _local_source(kind: str, ref: str, data: bytes, origin: str) -> _Source | _Skip:
    """Return a source for the local image *data*, or skip it when it fails the image check."""
    try:
        info = inspect_image(data)
    except ValueError as exc:
        return _Skip(SKIP_INVALID_IMAGE, f"{ref}: {exc}")
    return _Source(kind=kind, ref=ref, origin=origin, content=data, info=info)


def _folder_image(album: AlbumCover) -> str | _Skip | None:
    """Return the image under the target folder that names the album's front, by tier.

    The tiers, first non-empty wins: a stem holding ``front``, a stem holding ``cover`` and not
    ``back``, the one image directly in the folder. Two or more names in the winning tier, or
    no tier and two or more candidates, skip the album, since the owner's images signal a choice.
    """
    candidates = [name for name in album.images if Path(name).suffix.lower() in _SOURCE_SUFFIXES]
    stems = {name: Path(name).stem.lower() for name in candidates}
    direct = [name for name in candidates if len(Path(name).parts) == 1]
    tiers = (
        [name for name in candidates if "front" in stems[name]],
        [name for name in candidates if "cover" in stems[name] and "back" not in stems[name]],
        direct if len(direct) == 1 else [],
    )
    chosen = next((tier for tier in tiers if tier), [])
    if len(chosen) == 1:
        return chosen[0]
    if chosen or len(candidates) > 1:
        return _Skip(SKIP_AMBIGUOUS_IMAGES, ", ".join(chosen or candidates))
    return None


def _caa_lookups(album: AlbumCover) -> list[tuple[str, CoverArtKind, str]]:
    """Return the CAA listings the album's front is read from, the release's first."""
    lookups: tuple[tuple[str, CoverArtKind, str | None], ...] = (
        (SOURCE_RELEASE, "release", album.release_mbid),
        (SOURCE_RELEASE_GROUP, "release-group", album.release_group_mbid),
    )
    return [(kind, caa_kind, mbid) for kind, caa_kind, mbid in lookups if mbid is not None]


def _caa_front(album: AlbumCover, source: CoverArtSource) -> tuple[str, CoverArtFront] | None:
    """Return the CAA front of the album's release, else of its release group, with its kind."""
    for kind, caa_kind, mbid in _caa_lookups(album):
        front = source.front_image(caa_kind, mbid)
        if front is not None:
            return kind, front
    return None


def _needs_caa_request(album: AlbumCover, source: CoverArtSource) -> bool:
    """Whether reading the album's CAA front sends a listing request the cache cannot answer.

    A cached listing answers with no request, so a cached front ends the walk.
    """
    for _, caa_kind, mbid in _caa_lookups(album):
        if not source.has_cached_listing(caa_kind, mbid):
            return True
        if source.front_image(caa_kind, mbid) is not None:
            return False
    return False


def _caa_source(album: AlbumCover, source: CoverArtSource, *, dry_run: bool) -> _Source | _Skip:
    """Return the CAA front as a source: the original, else the 1200 thumbnail, else ``large``.

    A dry run downloads nothing and reports the original's URL.
    """
    found = _caa_front(album, source)
    if found is None:
        return _Skip(SKIP_NO_SOURCE, None)
    kind, front = found
    if dry_run:
        return _Source(kind=kind, ref=front.image, origin=_ORIGIN_AUTO, content=None, info=None)
    for url in (front.image, front.thumbnail_1200, front.thumbnail_large):
        if url is None:
            continue
        data = source.fetch_image(url)
        try:
            info = inspect_image(data)
        except ValueError as exc:
            logger.info("stage_covers: CAA image %s fails the image check: %s", url, exc)
            continue
        return _Source(kind=kind, ref=url, origin=_ORIGIN_AUTO, content=data, info=info)
    return _Skip(
        SKIP_INVALID_IMAGE, f"no CAA image of {front.image} is a JPEG or PNG within the cap"
    )


def _local_pick(
    music_path: Path,
    album: AlbumCover,
    owner: tuple[Path, bytes] | None,
) -> _Source | _Skip | None:
    """Return the owner's image, else the folder front, else ``None`` when only CAA can serve.

    A folder front that fails its check ends the chain as a skip.
    """
    if owner is not None:
        owner_path, owner_bytes = owner
        return _local_source(SOURCE_OWNER_FILE, str(owner_path), owner_bytes, _ORIGIN_MANUAL)
    folder_pick = _folder_image(album)
    if isinstance(folder_pick, _Skip):
        return folder_pick
    if folder_pick is not None and album.target_folder is not None:
        path = Path(album.target_folder) / folder_pick
        ref = os.path.relpath(path, music_path)
        try:
            data = _read_image(path)
        except OSError as exc:
            return _Skip(SKIP_INVALID_IMAGE, f"{ref}: {exc}")
        return _local_source(SOURCE_FOLDER_IMAGE, ref, data, _ORIGIN_AUTO)
    return None


def _caa_pick(album: AlbumCover, source: CoverArtSource, *, dry_run: bool) -> _Source | _Skip:
    """Return the CAA front as a source, or skip the album when the lookup fails."""
    try:
        return _caa_source(album, source, dry_run=dry_run)
    except CoverArtError as exc:
        return _Skip(SKIP_LOOKUP_ERROR, str(exc))


def _target_name(picked: _Source) -> str:
    """Return the cover file name for *picked*, from its format or, unread, its URL suffix."""
    if picked.info is not None:
        return _TARGET_NAMES[picked.info.format]
    # An unread CAA original that is not PNG ends as JPEG, since every CAA thumbnail is JPEG.
    is_png = PurePosixPath(urlsplit(picked.ref).path).suffix.lower() == ".png"
    return _TARGET_NAMES["png" if is_png else "jpeg"]


def _target_taken(folder: str, name: str) -> bool:
    """Whether *folder* holds an entry named *name*, compared case-insensitively."""
    try:
        with os.scandir(folder) as listing:
            names = {entry.name.lower() for entry in listing}
    except OSError:
        return False
    return name.lower() in names


def _shows_cover(folder: str) -> bool:
    """Whether *folder* holds an image Navidrome reads as an album cover."""
    return bool(cover_images_in(folder))


def _place(music_path: Path, album: AlbumCover, picked: _Source | _Skip) -> _Pick | _Skip:
    """Return where the cover *picked* for one gap album goes, or why it takes none."""
    if isinstance(picked, _Skip):
        return picked
    target_folder = album.target_folder or album.folders[0]
    name = _target_name(picked)
    if _target_taken(target_folder, name) or _shows_cover(target_folder):
        return _Skip(SKIP_TARGET_TAKEN, name)
    folder = _album_folder(music_path, album)
    return _Pick(album=album, folder=folder, target_path=str(Path(folder) / name), source=picked)


def _staged_row(pick: _Pick, now: str, note: str | None) -> tuple[store.StagedCover, bytes]:
    """Build the staged row of *pick* and the bytes it holds."""
    content, info = pick.source.content, pick.source.info
    if content is None or info is None:
        message = f"no image bytes for {pick.target_path}, which only a dry run leaves"
        raise RuntimeError(message)
    row = store.StagedCover(
        target_key=path_keys.path_key(pick.target_path),
        target_path=pick.target_path,
        folder_key=path_keys.path_key(pick.folder),
        album_label=_album_label(pick.album),
        file_ids=pick.album.file_ids,
        source_kind=pick.source.kind,
        source_ref=pick.source.ref,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        image_format=info.format,
        width=info.width,
        height=info.height,
        origin=pick.source.origin,
        note=note,
        staged_at=now,
    )
    return row, content


def _write_picks(
    conn: sqlite3.Connection,
    picks: list[_Pick],
    staged_rows: list[store.StagedCover],
    note: str | None,
) -> None:
    """Stage every pick, replacing the rows already staged for its folder."""
    now = clock.utc_now()
    for pick in picks:
        row, content = _staged_row(pick, now, note)
        for earlier in staged_rows:
            if earlier.folder_key == row.folder_key:
                store.delete_staged_cover(conn, earlier.target_key)
        store.insert_staged_cover(conn, row, content)


def _staged_view(pick: _Pick) -> CoverStaged:
    """Build the result row of one pick."""
    content, info = pick.source.content, pick.source.info
    return CoverStaged(
        target_path=pick.target_path,
        album=_album_label(pick.album),
        source_kind=pick.source.kind,
        source_ref=pick.source.ref,
        origin=pick.source.origin,
        format=None if info is None else info.format,
        width=None if info is None else info.width,
        height=None if info is None else info.height,
        size_bytes=None if content is None else len(content),
    )


def _stage_summary(
    *, dry_run: bool, staged: int, skipped: list[CoverSkipped], deferred: int
) -> str:
    """Build a short, plain human summary of one :func:`stage_covers` call."""
    verb = "Would stage" if dry_run else "Staged"
    reasons = Counter(row.reason for row in skipped)
    counted = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
    text = f"{verb} {staged} cover(s). Skipped {len(skipped)} album(s)"
    text += f": {counted}." if counted else "."
    if deferred:
        text += f" {deferred} gap album(s) wait for a CAA lookup past the limit."
    return text


@ledger_lock.mutating
def stage_covers(  # noqa: PLR0913 - cohesive keyword-only scope, source and injection params
    settings: Settings,
    *,
    path: str | os.PathLike[str] | None = None,
    image: str | os.PathLike[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    note: str | None = None,
    client: CoverArtSource | None = None,
) -> StageCoversResult:
    """Stage one cover image for each ``gap`` album (writes no library file).

    The source is the first that yields: the owner's *image* (origin ``manual``), a front image
    under the target folder (origin ``auto``, copied, the owner's file never moves), the CAA
    front of the release, then of the release group. Two or more owner images with no single
    front skip the album as ``ambiguous_images`` with no CAA lookup. A front image that cannot
    be read or fails the image check skips the album as ``invalid_image``, also with no CAA
    lookup. A CAA original that is not a JPEG or PNG within the cap gives way to its 1200
    thumbnail, then ``large``. The target is ``cover.jpg`` or ``cover.png`` in the target folder.

    *path* keeps the albums with a file in this folder or any folder under it. Every album not
    ``gap`` is skipped with its status as the reason, and a gap album whose folder already holds
    a staged cover as ``already_staged``. *image* is for the one gap album *path* selects, and
    replaces that album's staged row. A relative *image* resolves against ``music_path``.
    *limit* caps the gap albums whose CAA lookup the cache cannot answer. An album sourced from
    the owner's image, a folder image or a cached listing never counts, so a re-run with the same
    *limit* reaches the albums the last run left. A dry run downloads no image and writes
    nothing but lookup cache rows. *note* is stored on each staged row and its eventual write.
    *client* injects a CAA source, such as a fake in tests.

    A real run is refused while a file or sidecar move is staged, since a staged move plans an
    album's sidecars at stage time. Raises :class:`ValueError` on a refusal, an invalid
    argument or a missing ``music_path``.
    """
    # Input
    check_limit(limit)
    music_path = require_music_path(settings)
    if image is not None and path is None:
        message = "image requires path, which selects the album it covers"
        raise ValueError(message)
    folder_key = None if path is None else path_keys.folder_arg_key(settings, path)
    owner_path = None if image is None else _owner_image(music_path, image)
    owner = None if owner_path is None else (owner_path, _read_image(owner_path))
    picks: list[_Pick] = []
    budget = _LookupBudget(left=limit)
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        if not dry_run and store.any_move_staged(connection):
            message = "a file or sidecar move is staged. Run commit_paths or unstage_paths first"
            raise ValueError(message)
        albums = plan_album_covers(connection, settings, folder_key=folder_key)
        staged_rows = store.list_staged_covers(connection)

        # Process
        selection = _select(
            music_path,
            albums,
            {row.folder_key for row in staged_rows},
            owner=owner is not None,
        )
        skipped = list(selection.skipped)
        with injected_or_owned(
            client, lambda: CoverArtClient.from_settings(settings, connection)
        ) as source:
            for album in selection.work:
                local = _local_pick(music_path, album, owner)
                needs_request = local is None and _needs_caa_request(album, source)
                if not budget.admits(needs_request=needs_request):
                    continue
                picked = local if local is not None else _caa_pick(album, source, dry_run=dry_run)
                placed = _place(music_path, album, picked)
                if isinstance(placed, _Skip):
                    skipped.append(_skip_row(music_path, album, placed))
                else:
                    picks.append(placed)
        if not dry_run:
            _write_picks(connection, picks, staged_rows, note)
            connection.commit()
    finally:
        connection.close()

    # Output
    logger.info(
        "stage_covers dry_run=%s staged=%d skipped=%d deferred=%d",
        dry_run,
        len(picks),
        len(skipped),
        budget.deferred,
    )
    return StageCoversResult(
        staged=[_staged_view(pick) for pick in picks],
        skipped=skipped,
        more=budget.deferred > 0,
        dry_run=dry_run,
        summary=_stage_summary(
            dry_run=dry_run, staged=len(picks), skipped=skipped, deferred=budget.deferred
        ),
    )


# --- unstage_covers and diff_covers ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverDiffView(FieldDict):
    """One staged cover as :func:`diff_covers` shows it, without its bytes.

    ``target_path`` is relative to ``music_path``. ``state`` is one of ``landed``,
    ``album_moved``, ``target_taken``, ``covered_since_stage`` and ``ready``.
    """

    target_path: str
    album: str
    file_ids: tuple[int, ...]
    source_kind: str
    source_ref: str
    origin: str
    format: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    note: str | None
    staged_at: str
    state: str


def _staged_in_scope(
    conn: sqlite3.Connection,
    music_path: Path,
    root_key: str | None,
) -> list[store.StagedCover]:
    """Return the staged covers whose target sits at or under *root_key*, else every one."""
    rows = store.list_staged_covers(conn)
    if root_key is None:
        return rows
    return [
        row
        for row in rows
        if path_keys.is_within(path_keys.path_key(str(music_path / row.target_path)), root_key)
    ]


def _album_moved(
    conn: sqlite3.Connection,
    settings: Settings,
    target: Path,
    file_ids: tuple[int, ...],
) -> bool:
    """Whether a file of *file_ids* is gone or sits outside *target* and its disc folders."""
    target_key = path_keys.path_key(target)
    for file_id in file_ids:
        row = store.get_file_by_id(conn, file_id)
        if row is None or row.is_missing:
            return True
        if path_keys.path_key(row.folder) == target_key:
            continue
        below = path_keys.path_key(Path(row.folder).parent) == target_key
        if not below or mismatch.layout_of(settings, row.folder, "").disc_folder is None:
            return True
    return False


def _refuse_landed(music_path: Path, rows: list[store.StagedCover]) -> None:
    """Refuse the unstage when a matched cover's staged bytes already sit at its target."""
    landed = [
        row.target_path for row in rows if _holds_bytes(music_path / row.target_path, row.sha256)
    ]
    if not landed:
        return
    message = (
        f"cover(s) {', '.join(landed)} already sit at their staged target on disk. Run "
        "commit_covers to log those writes. Nothing was unstaged"
    )
    raise ValueError(message)


@ledger_lock.mutating
def unstage_covers(settings: Settings, *, path: str | os.PathLike[str] | None = None) -> int:
    """Drop the staged covers whose target sits in *path* or any folder under it, else every one.

    Returns the count dropped. *path* is compared as a path
    (:func:`tagmend.engine.path_keys.folder_arg_key`). Raises :class:`ValueError` and drops
    nothing when a matched cover's staged bytes already sit at its target, a write a crash cut
    before its log, since its row is that write's only record until ``commit_covers``.
    """
    music_path = require_music_path(settings)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = _staged_in_scope(connection, music_path, root_key)
        _refuse_landed(music_path, rows)
        for row in rows:
            store.delete_staged_cover(connection, row.target_key)
        connection.commit()
    finally:
        connection.close()
    logger.info("unstage_covers removed=%d", len(rows))
    return len(rows)


def diff_covers(
    settings: Settings,
    *,
    path: str | os.PathLike[str] | None = None,
    limit: int | None = None,
) -> list[CoverDiffView]:
    """Return every staged cover, or those in *path* and under it, with its live state. Read-only.

    ``state`` is the first that holds, as :func:`commit_covers` meets it: ``landed`` (the
    staged bytes already sit at the target, and ``commit_covers`` logs them), ``album_moved``
    (a file staged with the cover is gone, or sits outside the target folder and the disc
    folders directly below it), ``target_taken`` (an entry sits at the target),
    ``covered_since_stage`` (an image Navidrome reads as a cover now sits in the folder), else
    ``ready``. *limit* caps the rows.
    """
    check_limit(limit)
    music_path = require_music_path(settings)
    root_key = None if path is None else path_keys.folder_arg_key(settings, path)
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = _staged_in_scope(connection, music_path, root_key)
        capped = rows if limit is None else rows[:limit]
        views = [
            CoverDiffView(
                target_path=row.target_path,
                album=row.album_label,
                file_ids=row.file_ids,
                source_kind=row.source_kind,
                source_ref=row.source_ref,
                origin=row.origin,
                format=row.image_format,
                width=row.width,
                height=row.height,
                size_bytes=row.size_bytes,
                sha256=row.sha256,
                note=row.note,
                staged_at=row.staged_at,
                state=_commit_state(connection, settings, music_path / row.target_path, row),
            )
            for row in capped
        ]
    finally:
        connection.close()
    return views


# --- commit_covers ---------------------------------------------------------------------

COMMIT_ERROR: Final = "error"

_COMMIT_ERRORS: Final = (OSError, sqlite3.Error)


@dataclass(frozen=True, slots=True)
class CoverWritten(FieldDict):
    """One cover :func:`commit_covers` logged. ``target_path`` is relative to ``music_path``."""

    target_path: str
    source_kind: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class CoverCommitError(FieldDict):
    """One cover :func:`commit_covers` left staged. ``target_path`` is relative to ``music_path``.

    ``reason`` is ``album_moved``, ``target_taken``, ``covered_since_stage`` or ``error``.
    """

    target_path: str
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class CommitCoversResult(FieldDict):
    """What one :func:`commit_covers` call wrote. ``commit_id`` is ``None`` with nothing staged.

    ``committed`` counts the covers written and logged, and ``written`` lists them. ``errors``
    counts the failed writes, and ``problems`` lists every cover left staged.
    """

    commit_id: int | None
    committed: int
    errors: int
    written: list[CoverWritten]
    problems: list[CoverCommitError]
    summary: str


def _holds_bytes(target: Path, sha256: str) -> bool:
    """Whether *target* is a file whose sha256 is *sha256*."""
    if not target.is_file():
        return False
    with target.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() == sha256


def _commit_state(
    conn: sqlite3.Connection,
    settings: Settings,
    target: Path,
    row: store.StagedCover,
) -> str:
    """Return the state one staged cover meets at commit time, the first that holds.

    A landed write leads, so its log never waits on an ``album_moved`` hold that an unstage
    refusal would deadlock against.
    """
    # The staged bytes at the target are a write that landed before a crash cut its commit.
    if _holds_bytes(target, row.sha256):
        return STATE_LANDED
    if _album_moved(conn, settings, target.parent, row.file_ids):
        return STATE_ALBUM_MOVED
    if _target_taken(str(target.parent), target.name):
        return STATE_TARGET_TAKEN
    if _shows_cover(str(target.parent)):
        return STATE_COVERED_SINCE_STAGE
    return STATE_READY


def _unstage_hint(row: store.StagedCover) -> str:
    """Return the ``unstage_covers`` call that drops *row*."""
    return f"unstage_covers(path={str(Path(row.target_path).parent)!r})"


def _held(row: store.StagedCover, state: str) -> CoverCommitError:
    """Build the error row of a cover a commit-time check leaves staged."""
    unstage = _unstage_hint(row)
    details = {
        STATE_ALBUM_MOVED: (
            "a file staged with the cover is gone or sits outside the target folder and its "
            f"disc folders. Run scan_library, drop the row with {unstage} and stage again"
        ),
        STATE_TARGET_TAKEN: (
            f"a file sits at the target. Move it away or drop the row with {unstage}"
        ),
        STATE_COVERED_SINCE_STAGE: (
            f"the folder now holds an image Navidrome reads as a cover. Drop the row with {unstage}"
        ),
    }
    return CoverCommitError(target_path=row.target_path, reason=state, detail=details[state])


def _staged_content(conn: sqlite3.Connection, target_key: str) -> bytes:
    """Return the bytes of the staged cover the commit is working on, which must exist."""
    content = store.staged_cover_content(conn, target_key)
    if content is None:  # pragma: no cover - defensive, the key came from the staged list
        message = f"staged cover row vanished for {target_key}"
        raise RuntimeError(message)
    return content


def _write_temp(temp: Path, content: bytes) -> None:
    """Write *content* to the new file *temp*, flushed to disk before any move publishes it."""
    with temp.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _log_cover(
    conn: sqlite3.Connection,
    row: store.StagedCover,
    content: bytes,
    *,
    commit_id: int,
) -> None:
    """Append the ``create`` row of *row* and drop its staged row, in the open transaction."""
    store.insert_cover_write(
        conn,
        store.CoverWrite(
            commit_id=commit_id,
            created_at=clock.utc_now(),
            origin=row.origin,
            action="create",
            reverted_from=None,
            path=row.target_path,
            path_key=row.target_key,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            source_kind=row.source_kind,
            source_ref=row.source_ref,
            content=content,
            note=row.note,
        ),
    )
    store.delete_staged_cover(conn, row.target_key)


def _commit_cover(
    conn: sqlite3.Connection,
    settings: Settings,
    music_path: Path,
    row: store.StagedCover,
    *,
    commit_id: int,
) -> CoverWritten | CoverCommitError:
    """Write one staged cover, log it and drop its row in one transaction.

    The log row is written before the move and committed after it, so a crash between the two
    leaves the row staged with the cover at its target, which the next commit logs as landed.
    """
    target = music_path / row.target_path
    temp = target.with_name(target.name + scan.TEMP_SUFFIX)
    try:
        state = _commit_state(conn, settings, target, row)
        if state not in {STATE_READY, STATE_LANDED}:
            return _held(row, state)
        content = _staged_content(conn, row.target_key)
        if state == STATE_LANDED:
            _log_cover(conn, row, content, commit_id=commit_id)
        else:
            # Only TagMend creates the temp, so a leftover one is a write a crash cut short.
            temp.unlink(missing_ok=True)
            _write_temp(temp, content)
            _log_cover(conn, row, content, commit_id=commit_id)
            paths.move_no_clobber(temp, target)
        conn.commit()
    except _COMMIT_ERRORS as exc:
        conn.rollback()
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        logger.warning("commit %d: cover %s failed: %s", commit_id, row.target_path, exc)
        detail = (
            f"{exc}. Its row stays staged. Fix the cause and run commit_covers again, or drop "
            f"the row with {_unstage_hint(row)}"
        )
        return CoverCommitError(target_path=row.target_path, reason=COMMIT_ERROR, detail=detail)
    if state == STATE_LANDED:
        logger.info("commit %d: cover %s had landed before a crash", commit_id, row.target_path)
    return CoverWritten(
        target_path=row.target_path, source_kind=row.source_kind, size_bytes=row.size_bytes
    )


def _commit_summary(
    commit_id: int | None,
    written: list[CoverWritten],
    problems: list[CoverCommitError],
) -> str:
    """Build a short, plain human summary of one :func:`commit_covers` call."""
    if commit_id is None:
        return "No cover is staged. No commit was created."
    reasons = Counter(problem.reason for problem in problems)
    counted = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
    text = f"Commit {commit_id} wrote {len(written)} cover(s)."
    if counted:
        text += f" {len(problems)} stay staged: {counted}."
    return text


@ledger_lock.mutating
def commit_covers(settings: Settings) -> CommitCoversResult:
    """Write every staged cover into its album folder as one commit.

    Any commit left ``applying`` is marked ``interrupted`` first, and its leftover rows are
    swept into this one. The rows run in target-key order. A row stays staged, listed under
    ``problems``, when a stage-time file left the target folder and its disc folders
    (``album_moved``), another file sits at the target (``target_taken``), the folder now
    shows a cover (``covered_since_stage``) or the write fails (``error``). The staged bytes
    already at the target are a write that landed before a crash, logged with no disk action.
    Otherwise the bytes go to a new temp file beside the target, which then moves onto the
    target and never overwrites a file. Each cover appends a ``create`` row to
    ``cover_writes``. The commit's origin is ``auto`` only when every row is ``auto``.
    ``versioning.revert_commit`` undoes the commit through :func:`revert_cover_commit`.

    ``commit_id`` is ``None`` when nothing is staged. Raises :class:`ValueError` when
    ``music_path`` is unset, and while a file or sidecar move is staged, since a staged move
    plans its album's sidecars at stage time and would leave a new cover behind.
    """
    # Input
    music_path = require_music_path(settings)
    written: list[CoverWritten] = []
    problems: list[CoverCommitError] = []
    commit_id: int | None = None
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        commits.mark_interrupted(connection)
        connection.commit()
        staged = store.list_staged_covers(connection)
        if staged and store.any_move_staged(connection):
            message = (
                "a file or sidecar move is staged. Run commit_paths or unstage_paths first, "
                "then commit_covers"
            )
            raise ValueError(message)

        # Process
        if staged:
            every_auto = all(row.origin == _ORIGIN_AUTO for row in staged)
            commit_id = commits.create_commit(
                connection,
                origin=_ORIGIN_AUTO if every_auto else _ORIGIN_MANUAL,
                message=f"write {len(staged)} staged cover(s)",
                now=clock.utc_now(),
            )
            connection.commit()  # commit row durable before any disk work
            for row in staged:
                outcome = _commit_cover(connection, settings, music_path, row, commit_id=commit_id)
                if isinstance(outcome, CoverWritten):
                    written.append(outcome)
                else:
                    problems.append(outcome)
            commits.set_commit_status(connection, commit_id, "applied")
            connection.commit()
    finally:
        connection.close()

    # Output
    errors = sum(1 for problem in problems if problem.reason == COMMIT_ERROR)
    logger.info(
        "cover commit %s: written=%d problems=%d errors=%d",
        commit_id,
        len(written),
        len(problems),
        errors,
    )
    return CommitCoversResult(
        commit_id=commit_id,
        committed=len(written),
        errors=errors,
        written=written,
        problems=problems,
        summary=_commit_summary(commit_id, written, problems),
    )


# --- revert_commit of a cover commit ----------------------------------------------------

REVERT_CHANGED: Final = "changed"

_REVERTABLE: Final = "revertable"
_REVERTED: Final = "reverted"
_SKIPPED_LATER: Final = "skipped_later_changes"
_ORIGIN_REVERT: Final = "revert"
_ACTION_CREATE: Final = "create"
_ACTION_REMOVE: Final = "remove"


@dataclass(frozen=True, slots=True)
class _RevertRun:
    """What every row of one cover revert shares. ``commit_id`` is the new revert commit.

    ``resumes`` holds when an earlier revert of the same commit is still ``applying`` or was
    ``interrupted``, so a created cover gone from disk may be one that revert trashed.
    """

    music_path: Path
    commit_id: int
    note: str | None
    trash: Callable[[Path], None]
    resumes: bool


def _classify_created(
    conn: sqlite3.Connection,
    target: Path,
    row: store.CoverWriteRow,
    *,
    resumes: bool,
) -> tuple[str, str | None]:
    """Classify a ``create`` row whose path no later cover row has, with a detail.

    A cover gone from disk is revertable when *resumes* holds, so its ``remove`` row is logged.
    """
    if store.sidecar_moved_from_after(conn, row.commit_id, row.path_key):
        return _SKIPPED_LATER, "a later path commit moved it, so it stays"
    if not target.exists():
        return (_REVERTABLE, None) if resumes else (paths.MISSING, None)
    if not _holds_bytes(target, row.sha256):
        return REVERT_CHANGED, "its bytes changed after the commit, so it stays"
    return _REVERTABLE, None


def _classify_cover_revert(
    conn: sqlite3.Connection,
    music_path: Path,
    row: store.CoverWriteRow,
    *,
    resumes: bool,
) -> tuple[str, str | None]:
    """Classify one ``cover_writes`` row of the reverted commit, with a detail.

    The kind is ``revertable``, ``skipped_later_changes`` (a later cover row has its path, or a
    later path commit moved the cover it created), ``missing`` or ``changed`` (the cover it
    created is gone or holds other bytes), or ``error`` (a file sits where it removed a cover).
    A removed cover already back at its path with its logged bytes is revertable, and so is a
    created cover gone from disk when *resumes* holds. Each is logged with no disk action.
    """
    target = music_path / row.path
    if store.cover_written_later(conn, row.id, row.path_key):
        return _SKIPPED_LATER, None
    if row.action == _ACTION_CREATE:
        return _classify_created(conn, target, row, resumes=resumes)
    if _holds_bytes(target, row.sha256):
        return _REVERTABLE, None
    if _target_taken(str(target.parent), target.name):
        return COMMIT_ERROR, f"{row.path} is taken on disk"
    return _REVERTABLE, None


def _revert_write(
    row: store.CoverWriteRow,
    run: _RevertRun,
    *,
    action: str,
    content: bytes | None,
) -> store.CoverWrite:
    """Build the ``cover_writes`` row that undoes *row* under the revert commit of *run*."""
    return store.CoverWrite(
        commit_id=run.commit_id,
        created_at=clock.utc_now(),
        origin=_ORIGIN_REVERT,
        action=action,
        reverted_from=row.id,
        path=row.path,
        path_key=row.path_key,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        source_kind=row.source_kind,
        source_ref=row.source_ref,
        content=content,
        note=run.note,
    )


def _removed_content(conn: sqlite3.Connection, row: store.CoverWriteRow) -> bytes:
    """Return the bytes the ``create`` row that the ``remove`` row *row* undid wrote."""
    content = None
    if row.reverted_from is not None:
        content = store.cover_write_content(conn, row.reverted_from)
    if content is None:  # pragma: no cover - defensive, every remove row undoes a create row
        message = f"cover_writes row {row.id} names no logged bytes for {row.path}"
        raise RuntimeError(message)
    return content


def _trash_cover(
    conn: sqlite3.Connection,
    run: _RevertRun,
    row: store.CoverWriteRow,
) -> tuple[str, str | None]:
    """Log the ``remove`` of a created cover and send the file to the trash, in one transaction.

    A trash failure rolls the row back, so the cover stays and nothing is logged. A cover
    already gone, which an interrupted revert of the same commit trashed, is logged with no
    disk action. A failed commit after the trash leaves the cover in the trash and unlogged.
    """
    target = run.music_path / row.path
    gone = not target.exists()
    trashed = False
    try:
        store.insert_cover_write(conn, _revert_write(row, run, action=_ACTION_REMOVE, content=None))
        if not gone:
            run.trash(target)
            trashed = True
        conn.commit()
    except _COMMIT_ERRORS as exc:
        conn.rollback()
        if trashed:
            logger.warning(
                "revert commit %d: cover %s went to the trash, and its remove row was not "
                "logged: %s",
                run.commit_id,
                row.path,
                exc,
            )
            return COMMIT_ERROR, f"{exc}. The cover is in the OS trash, and no remove row logs it"
        logger.warning("revert commit %d: cover %s stays: %s", run.commit_id, row.path, exc)
        return COMMIT_ERROR, str(exc)
    if gone:
        logger.info(
            "revert commit %d: cover %s was gone, as an interrupted revert left it",
            run.commit_id,
            row.path,
        )
    return _REVERTED, None


def _restore_cover(
    conn: sqlite3.Connection,
    run: _RevertRun,
    row: store.CoverWriteRow,
) -> tuple[str, str | None]:
    """Write a removed cover again and log its ``create`` row, in one transaction.

    The log row is written before the move and committed after it, as :func:`_commit_cover` does.
    A cover already back with its logged bytes is a write that landed before a crash cut its
    commit, so it is logged with no disk action.
    """
    target = run.music_path / row.path
    temp = target.with_name(target.name + scan.TEMP_SUFFIX)
    landed = False
    try:
        content = _removed_content(conn, row)
        landed = _holds_bytes(target, row.sha256)
        if not landed:
            # Only TagMend creates the temp, so a leftover one is a write a crash cut short.
            temp.unlink(missing_ok=True)
            _write_temp(temp, content)
        store.insert_cover_write(
            conn, _revert_write(row, run, action=_ACTION_CREATE, content=content)
        )
        if not landed:
            paths.move_no_clobber(temp, target)
        conn.commit()
    except _COMMIT_ERRORS as exc:
        conn.rollback()
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        logger.warning("revert commit %d: cover %s not written: %s", run.commit_id, row.path, exc)
        return COMMIT_ERROR, str(exc)
    if landed:
        logger.info("revert commit %d: cover %s had landed before a crash", run.commit_id, row.path)
    return _REVERTED, None


def _revert_cover(
    conn: sqlite3.Connection,
    run: _RevertRun,
    row: store.CoverWriteRow,
) -> tuple[str, str | None]:
    """Undo one row the plan pass found revertable, checked again so a change since then stays."""
    kind, detail = _classify_cover_revert(conn, run.music_path, row, resumes=run.resumes)
    if kind != _REVERTABLE:
        return kind, detail
    if row.action == _ACTION_CREATE:
        return _trash_cover(conn, run, row)
    return _restore_cover(conn, run, row)


def _cover_outcome(
    row: store.CoverWriteRow,
    kind: str,
    detail: str | None,
) -> paths.SidecarOutcome:
    """Return the public outcome of one cover row, keyed by its path."""
    status = _REVERTED if kind == _REVERTABLE else kind
    return paths.SidecarOutcome(row.path, row.path, status, detail)


def revert_cover_commit(  # noqa: PLR0913 - the revert_commit surface plus its connection and trash
    conn: sqlite3.Connection,
    settings: Settings,
    commit_id: int,
    *,
    note: str | None,
    dry_run: bool,
    trash: Callable[[Path], None],
) -> paths.PathRevertCommitResult:
    """Undo every cover *commit_id* created or removed, under ONE new ``revert`` commit.

    The cover half of :func:`tagmend.engine.versioning.revert_commit`, which has already checked
    the target commit and that nothing is staged. A created cover goes to *trash*, and its
    ``remove`` row is appended in the same transaction, so a trash error leaves the file in place
    and logs nothing. A removed cover is written again from the bytes its ``create`` row kept,
    through a temp file that never overwrites a file. A row is ``skipped_later_changes`` when a
    later cover row has its path, or a later path commit moved the cover it created. A created
    cover gone from disk is ``missing``, one holding other bytes is ``changed`` and stays, and a
    file where a removed cover goes is an ``error``. A crash can cut a revert between its disk
    action and its log. On the rerun, a removed cover already back with its logged bytes is
    logged with no disk action, and so is a created cover gone from disk while an earlier
    revert of the same commit is ``applying`` or ``interrupted``. Each row is reported under
    ``sidecars`` at its own path. *dry_run* returns the classification only. With nothing
    revertable, no commit is created.
    """
    # Input
    music_path = require_music_path(settings)
    resumes = commits.has_unfinished_revert(conn, commit_id)
    planned = [
        (row, *_classify_cover_revert(conn, music_path, row, resumes=resumes))
        for row in store.cover_writes_for_commit(conn, commit_id)
    ]
    new_commit: int | None = None
    outcomes: list[paths.SidecarOutcome] = []

    # Process
    if not dry_run and any(kind == _REVERTABLE for _, kind, _ in planned):
        new_commit = commits.create_commit(
            conn, origin=_ORIGIN_REVERT, message=note, now=clock.utc_now(), reverted_from=commit_id
        )
        conn.commit()  # commit row durable before any disk work
        run = _RevertRun(
            music_path=music_path, commit_id=new_commit, note=note, trash=trash, resumes=resumes
        )
        for row, kind, detail in planned:
            done = _revert_cover(conn, run, row) if kind == _REVERTABLE else (kind, detail)
            outcomes.append(_cover_outcome(row, *done))
        commits.set_commit_status(conn, new_commit, "applied")
        conn.commit()
    else:
        outcomes = [_cover_outcome(row, kind, detail) for row, kind, detail in planned]

    # Output
    result = paths.with_sidecars(
        commits.summarize_revert(
            commit_id=new_commit, reverted_from=commit_id, dry_run=dry_run, outcomes=[]
        ),
        outcomes,
    )
    logger.info(
        "cover revert of commit %d as commit %s: covers=%d reverted=%d",
        commit_id,
        new_commit,
        len(outcomes),
        sum(1 for outcome in outcomes if outcome.status == _REVERTED),
    )
    return result
