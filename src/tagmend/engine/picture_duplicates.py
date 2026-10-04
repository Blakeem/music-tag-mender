"""detect_picture_duplicates: one embedded picture held by the albums of two or more album artists.

The comparison behind the finding noun ``duplicate``: a file's embedded picture against the
pictures of every other album, matched by the SHA-256 the scan stores in ``file_pictures``. An
art fetcher that matched an album by its title alone embeds one album's front in another
artist's album of the same title. Navidrome shows a track's embedded picture as that song's
cover, and as the album's cover when no cover image sits in the album's folders.

Albums are grouped as :func:`tagmend.engine.covers.detect_cover_gaps` groups them. A picture the
albums of one album artist share is not reported, since a series or a reissue reuses its art. A
zero-byte picture is never a duplicate. Navidrome cannot show one, so its files are listed apart.
Read-only over the snapshot. Only each reported folder's cover images are listed from disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tagmend.engine import covers, db, path_keys, schema, store
from tagmend.engine.detector_core import group_by_key
from tagmend.engine.serialize import FieldDict
from tagmend.engine.tags import TAG_READER_VERSION
from tagmend.engine.text_keys import artist_name_key
from tagmend.engine.validation import check_limit, require_music_path
from tagmend.log import get_logger

if TYPE_CHECKING:
    from tagmend.config import Settings

logger = get_logger(__name__)

_MIN_ALBUM_ARTISTS: Final = 2


@dataclass(frozen=True, slots=True)
class PictureAlbum(FieldDict):
    """One album holding a shared picture.

    ``folders`` are the folders of its files holding the picture, absolute, in the spelling the
    snapshot stores. ``cover_images`` names the images directly in them that Navidrome reads as
    an album cover, so an album that lists one keeps a cover once the picture is gone.
    """

    album_artist: str
    album: str | None
    folders: tuple[str, ...]
    files: int
    album_files: int
    file_ids: tuple[int, ...]
    cover_images: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PictureDuplicate(FieldDict):
    """One picture the albums of ``album_artists`` album artists embed."""

    sha256: str
    size_bytes: int
    mime: str
    album_artists: int
    albums: list[PictureAlbum]


@dataclass(frozen=True, slots=True)
class EmptyPicture(FieldDict):
    """One file holding a zero-byte picture. ``path`` is absolute."""

    file_id: int
    path: str


@dataclass(frozen=True, slots=True)
class PictureDuplicatesReport(FieldDict):
    """One :func:`detect_picture_duplicates` run. *limit* caps ``duplicates``, never the counts."""

    files_with_pictures: int
    distinct_pictures: int
    unread_files: int
    duplicates: list[PictureDuplicate]
    empty_pictures: list[EmptyPicture]
    summary: str


@dataclass(frozen=True, slots=True)
class _Holding:
    """The files of one album holding one picture, beside every present file of that album."""

    members: list[covers.AlbumTrack]
    holders: list[covers.AlbumTrack]


@dataclass(frozen=True, slots=True)
class _Shared:
    """One picture and the albums holding it, in the order of each album's first file holding it."""

    picture: store.PictureRow
    holdings: list[_Holding]

    @property
    def files(self) -> int:
        """Return how many files hold the picture."""
        return sum(len(holding.holders) for holding in self.holdings)


def _shared_pictures(
    tracks: list[covers.AlbumTrack],
    pictures: dict[int, list[store.PictureRow]],
) -> list[_Shared]:
    """Return each non-empty picture that the albums of two or more album artists hold."""
    # Input
    albums = group_by_key(tracks, lambda track: track.identity)
    first_rows: dict[str, store.PictureRow] = {}
    holders: dict[str, dict[tuple[str, ...], list[covers.AlbumTrack]]] = {}

    # Process: a file holding one picture twice counts once.
    for track in tracks:
        rows = {row.sha256: row for row in pictures.get(track.file_id, []) if row.size_bytes}
        for sha256, row in rows.items():
            first_rows.setdefault(sha256, row)
            holders.setdefault(sha256, {}).setdefault(track.identity, []).append(track)
    shared: list[_Shared] = []
    for sha256, by_album in holders.items():
        artists = {artist_name_key(albums[identity][0].album_artist) for identity in by_album}
        if len(artists) < _MIN_ALBUM_ARTISTS:
            continue
        holdings = [
            _Holding(members=albums[identity], holders=held) for identity, held in by_album.items()
        ]
        shared.append(_Shared(picture=first_rows[sha256], holdings=holdings))

    # Output
    return shared


def _touches(shared: _Shared, folder_key: str) -> bool:
    """Whether an album holding *shared* has a present file at or under *folder_key*."""
    return any(
        path_keys.is_within(track.folder_key, folder_key)
        for holding in shared.holdings
        for track in holding.members
    )


def _album_row(holding: _Holding) -> PictureAlbum:
    """Return the report row of one album holding a shared picture, its cover images read."""
    first = holding.members[0]
    spellings = {track.folder_key: track.folder for track in holding.holders}
    folders = tuple(spellings[key] for key in sorted(spellings))
    return PictureAlbum(
        album_artist=first.album_artist,
        album=first.album,
        folders=folders,
        files=len(holding.holders),
        album_files=len(holding.members),
        file_ids=tuple(track.file_id for track in holding.holders),
        cover_images=tuple(name for folder in folders for name in covers.cover_images_in(folder)),
    )


def _duplicate_row(shared: _Shared) -> PictureDuplicate:
    """Return the report row of one shared picture, its albums by album artist, then album."""
    albums = sorted(
        (_album_row(holding) for holding in shared.holdings),
        key=lambda album: (
            album.album_artist.casefold(),
            (album.album or "").casefold(),
            album.file_ids[0],
        ),
    )
    return PictureDuplicate(
        sha256=shared.picture.sha256,
        size_bytes=shared.picture.size_bytes,
        mime=shared.picture.mime,
        album_artists=len({artist_name_key(album.album_artist) for album in albums}),
        albums=albums,
    )


def _is_unread(row: store.FileRow) -> bool:
    """Return whether the snapshot lacks *row*'s pictures.

    A presence scan or a failed read leaves a new row with the current reader stamp and no read.
    """
    return row.tags_updated_at is None or row.reader_version < TAG_READER_VERSION


def _summarize(duplicates: int, empty: int, unread: int) -> str:
    """Build a short, plain human summary of the run."""
    parts = [
        f"{duplicates} picture(s) are embedded by the albums of two or more album artists."
        if duplicates
        else "No embedded picture is shared by the albums of two album artists."
    ]
    if empty:
        parts.append(f"{empty} file(s) hold a zero-byte picture.")
    if unread:
        parts.append(f"{unread} file(s) have no picture record yet. Run scan_library.")
    return " ".join(parts)


def detect_picture_duplicates(
    settings: Settings,
    *,
    path: str | None = None,
    limit: int | None = None,
) -> PictureDuplicatesReport:
    """Report each embedded picture the albums of two or more album artists hold.

    *path* keeps a duplicate when one of its albums has a file in this folder or any folder
    under it (:func:`tagmend.engine.path_keys.folder_arg_key`), with every album of that
    duplicate in the row. *path* also scopes the counts and ``empty_pictures``. *limit* caps
    ``duplicates``. Raises :class:`ValueError` for a negative *limit*, a *path* outside
    ``music_path`` and a missing ``music_path``.
    """
    # Input
    check_limit(limit)
    folder_key = None if path is None else path_keys.folder_arg_key(settings, path)
    music_path = require_music_path(settings)
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        tracks = covers.load_album_tracks(connection, music_path)
        pictures = store.load_present_pictures(connection)
        file_rows = {row.id: row for row in store.list_files(connection)}
    finally:
        connection.close()

    # Process
    scope = [
        track
        for track in tracks
        if folder_key is None or path_keys.is_within(track.folder_key, folder_key)
    ]
    shared = sorted(
        (
            entry
            for entry in _shared_pictures(tracks, pictures)
            if folder_key is None or _touches(entry, folder_key)
        ),
        key=lambda entry: (-len(entry.holdings), -entry.files, entry.picture.sha256),
    )
    empty = [
        EmptyPicture(file_id=track.file_id, path=str(Path(track.folder) / track.filename))
        for track in scope
        if any(row.size_bytes == 0 for row in pictures.get(track.file_id, []))
    ]
    unread = sum(1 for track in scope if _is_unread(file_rows[track.file_id]))
    held = [pictures[track.file_id] for track in scope if track.file_id in pictures]
    logger.info(
        "picture duplicates: %d shared picture(s), %d empty, %d unread file(s)",
        len(shared),
        len(empty),
        unread,
    )

    # Output
    return PictureDuplicatesReport(
        files_with_pictures=len(held),
        distinct_pictures=len({row.sha256 for rows in held for row in rows}),
        unread_files=unread,
        duplicates=[_duplicate_row(entry) for entry in shared[:limit]],
        empty_pictures=empty,
        summary=_summarize(len(shared), len(empty), unread),
    )
