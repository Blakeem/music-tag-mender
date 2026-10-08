"""Tests for the embedded-picture reader, the ``file_pictures`` snapshot and its detector."""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Any

import mutagen
import pytest

from conftest import PictureImage, embed_pictures, make_track, vorbis_picture_value
from tagmend import config, mcp_server
from tagmend.engine import db, library, staging, store
from tagmend.engine.library import scan_library
from tagmend.engine.picture_duplicates import detect_picture_duplicates
from tagmend.engine.tags import TAG_READER_VERSION, has_embedded_picture, read_pictures

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

_FRONT = b"\xff\xd8\xff\xe0 the front"
_BACK = b"\x89PNG\r\n\x1a\n the back"
_OTHER = b"\xff\xd8\xff\xe0 another front"
_TWO_IMAGES: list[PictureImage] = [
    (3, "image/jpeg", "front", _FRONT),
    (4, "image/png", "back", _BACK),
]
_SUFFIXES = [".mp3", ".flac", ".m4a", ".ogg"]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_back(suffix: str, ordinal: int, image: PictureImage) -> tuple[object, ...]:
    """Return what :func:`read_pictures` reports for *image*. MP4 keeps no type or description."""
    kind, mime, desc, data = image
    if suffix == ".m4a":
        return (ordinal, None, mime, "", len(data), _sha(data), data)
    return (ordinal, kind, mime, desc, len(data), _sha(data), data)


# --- the reader --------------------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("suffix", _SUFFIXES)
def test_read_pictures_lists_each_picture_in_container_order(
    tmp_path: Path,
    suffix: str,
    count: int,
) -> None:
    track = make_track(tmp_path / f"t{suffix}", {"title": ["Song"]})
    images = _TWO_IMAGES[:count]
    embed_pictures(track, images)

    pictures = read_pictures(track)

    assert [
        (p.ordinal, p.picture_type, p.mime, p.description, p.size_bytes, p.sha256, p.data)
        for p in pictures
    ] == [_read_back(suffix, ordinal, image) for ordinal, image in enumerate(images)]
    assert has_embedded_picture(track) is True


@pytest.mark.parametrize("suffix", _SUFFIXES)
def test_read_pictures_finds_none_in_a_bare_file(tmp_path: Path, suffix: str) -> None:
    track = make_track(tmp_path / f"t{suffix}", {"title": ["Song"]})

    assert read_pictures(track) == []
    assert has_embedded_picture(track) is False


def test_read_pictures_finds_none_in_a_file_mutagen_cannot_identify(tmp_path: Path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"plain text, no audio")

    assert read_pictures(notes) == []


@pytest.mark.parametrize(
    "bad_value",
    ["abc", base64.b64encode(b"short").decode("ascii")],
    ids=["not-base64", "not-a-picture-block"],
)
def test_an_undecodable_ogg_picture_is_skipped_with_a_warning(
    tmp_path: Path,
    tagmend_warnings: pytest.LogCaptureFixture,
    bad_value: str,
) -> None:
    track = make_track(tmp_path / "t.ogg", {"title": ["Song"]})
    audio: Any = mutagen.File(track)  # type: ignore[attr-defined]
    audio["metadata_block_picture"] = [bad_value, vorbis_picture_value(_TWO_IMAGES[0])]
    audio.save()

    pictures = read_pictures(track)

    assert [(picture.ordinal, picture.data) for picture in pictures] == [(1, _FRONT)]
    assert f"skipped undecodable picture 0 of {track}" in tagmend_warnings.text


# --- the snapshot ------------------------------------------------------------------------


def _ledger(settings: Settings, sql: str, params: tuple[object, ...] = ()) -> None:
    """Run one write statement against the ledger."""
    connection = db.connect(settings.db_path)
    try:
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def _file_id(settings: Settings, path: Path) -> int:
    connection = db.connect(settings.db_path)
    try:
        row = store.get_file(connection, str(path.parent), path.name)
    finally:
        connection.close()
    assert row is not None
    return row.id


def _stored(settings: Settings, path: Path) -> list[tuple[object, ...]]:
    """Return the ``file_pictures`` rows of *path* as plain tuples."""
    file_id = _file_id(settings, path)
    connection = db.connect(settings.db_path)
    try:
        rows = store.get_pictures(connection, file_id)
    finally:
        connection.close()
    return [
        (row.ordinal, row.picture_type, row.mime, row.description, row.size_bytes, row.sha256)
        for row in rows
    ]


def test_the_scan_records_a_new_files_pictures(engine_settings: Settings, music_dir: Path) -> None:
    track = make_track(music_dir / "Band" / "Blue" / "01.flac", {"title": ["Song"]})
    embed_pictures(track, _TWO_IMAGES)

    scan_library(engine_settings)

    assert _stored(engine_settings, track) == [
        (0, 3, "image/jpeg", "front", len(_FRONT), _sha(_FRONT)),
        (1, 4, "image/png", "back", len(_BACK), _sha(_BACK)),
    ]


def test_the_scan_refreshes_pictures_when_only_they_change(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "Band" / "Blue" / "01.mp3", {"title": ["Song"]})
    embed_pictures(track, _TWO_IMAGES[:1])
    scan_library(engine_settings)
    embed_pictures(track, _TWO_IMAGES)

    rescan = scan_library(engine_settings)

    # The tags read back identical, so the pictures were stored before that early return.
    assert (rescan.updated, rescan.tags_read) == (1, 0)
    assert [row[5] for row in _stored(engine_settings, track)] == [_sha(_FRONT), _sha(_BACK)]


def test_a_reader_version_bump_rereads_an_unchanged_file_once(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "Band" / "Blue" / "01.m4a", {"title": ["Song"]})
    embed_pictures(track, _TWO_IMAGES[:1])
    scan_library(engine_settings)
    # A ledger the previous reader filled holds no pictures and an older stamp.
    _ledger(engine_settings, "DELETE FROM file_pictures")
    _ledger(engine_settings, "UPDATE files SET reader_version = ?", (TAG_READER_VERSION - 1,))

    reread = scan_library(engine_settings)
    filled = _stored(engine_settings, track)
    _ledger(engine_settings, "DELETE FROM file_pictures")
    scan_library(engine_settings)

    assert reread.updated == 0
    assert [row[5] for row in filled] == [_sha(_FRONT)]
    # The current stamp sent the third scan past the file, so the emptied rows stay empty.
    assert _stored(engine_settings, track) == []


def test_a_tag_commit_refreshes_pictures_an_outside_edit_changed(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    track = make_track(music_dir / "Band" / "Blue" / "01.flac", {"title": ["Song"]})
    embed_pictures(track, _TWO_IMAGES[:1])
    scan_library(engine_settings)
    embed_pictures(track, [(3, "image/jpeg", "front", _OTHER)])
    staging.stage_tags(
        engine_settings,
        file_id=_file_id(engine_settings, track),
        tags={"title": ["Renamed"]},
    )

    committed = staging.commit_tags(engine_settings).committed
    # The commit stamped the new signature, so this scan skips the file.
    rescan = scan_library(engine_settings)

    assert (committed, rescan.updated) == (1, 0)
    assert [row[5] for row in _stored(engine_settings, track)] == [_sha(_OTHER)]


def test_get_file_shows_the_files_pictures(engine_settings: Settings, music_dir: Path) -> None:
    track = make_track(music_dir / "Band" / "Blue" / "01.mp3", {"title": ["Song"]})
    embed_pictures(track, _TWO_IMAGES[:1])
    scan_library(engine_settings)

    view = library.get_file(engine_settings, _file_id(engine_settings, track))

    assert view is not None
    assert view.to_dict()["pictures"] == [
        {
            "ordinal": 0,
            "picture_type": 3,
            "mime": "image/jpeg",
            "size_bytes": len(_FRONT),
            "sha256": _sha(_FRONT),
        },
    ]


# --- the detector ------------------------------------------------------------------------


def _album(folder: Path, album: str, artist: str, pictures: list[bytes | None]) -> list[Path]:
    """Write one MP3 per entry of *pictures* into *folder*, each embedding its picture, if any."""
    tags = {"artist": [artist], "albumartist": [artist], "album": [album], "date": ["2001"]}
    tracks: list[Path] = []
    for index, picture in enumerate(pictures, start=1):
        track = make_track(folder / f"{index:02d}.mp3", tags)
        if picture is not None:
            embed_pictures(track, [(3, "image/jpeg", "", picture)])
        tracks.append(track)
    return tracks


def _detect(settings: Settings, **kwargs: Any) -> Any:
    """Scan the library, then run the detector."""
    scan_library(settings)
    return detect_picture_duplicates(settings, **kwargs)


def test_a_picture_two_album_artists_share_is_a_duplicate(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    linkin = music_dir / "Linkin Park" / "Greatest Hits"
    ozzy = music_dir / "Ozzy Osbourne" / "Greatest Hits"
    _album(linkin, "Greatest Hits", "Linkin Park", [_FRONT, _FRONT])
    _album(ozzy, "Greatest Hits", "Ozzy Osbourne", [None, _FRONT, _FRONT])
    (ozzy / "folder.jpg").write_bytes(_OTHER)
    (ozzy / "back.jpg").write_bytes(_OTHER)

    report = _detect(engine_settings)

    assert (report.files_with_pictures, report.distinct_pictures, report.unread_files) == (4, 1, 0)
    [duplicate] = report.duplicates
    assert (duplicate.sha256, duplicate.size_bytes, duplicate.mime) == (
        _sha(_FRONT),
        len(_FRONT),
        "image/jpeg",
    )
    assert duplicate.album_artists == 2
    assert [
        (
            a.album_artist,
            a.album,
            a.folders,
            a.files,
            a.album_files,
            a.cover_images,
        )
        for a in duplicate.albums
    ] == [
        ("Linkin Park", "Greatest Hits", (str(linkin),), 2, 2, ()),
        ("Ozzy Osbourne", "Greatest Hits", (str(ozzy),), 2, 3, ("folder.jpg",)),
    ]
    held = tuple(_file_id(engine_settings, ozzy / name) for name in ("02.mp3", "03.mp3"))
    assert duplicate.albums[1].file_ids == held
    assert report.empty_pictures == []
    assert report.summary.startswith("1 picture(s) are embedded")


def test_one_album_artist_or_one_albums_disc_folders_sharing_a_picture_is_no_duplicate(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir / "Linkin Park" / "Hits", "Hits", "Linkin Park", [_FRONT])
    _album(music_dir / "Linkin Park" / "More Hits", "More Hits", "linkin-park", [_FRONT])
    _album(music_dir / "Band" / "Blue" / "CD1", "Blue", "Band", [_OTHER])
    _album(music_dir / "Band" / "Blue" / "CD2", "Blue", "Band", [_OTHER])

    report = _detect(engine_settings)

    assert report.duplicates == []
    assert (report.files_with_pictures, report.distinct_pictures) == (4, 2)
    assert report.summary == "No embedded picture is shared by the albums of two album artists."


def test_a_zero_byte_picture_is_listed_apart_and_never_a_duplicate(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    [alpha] = _album(music_dir / "Alpha" / "X", "X", "Alpha", [b""])
    [beta] = _album(music_dir / "Beta" / "X", "X", "Beta", [b""])

    report = _detect(engine_settings)

    assert report.duplicates == []
    assert [(row.file_id, row.path) for row in report.empty_pictures] == [
        (_file_id(engine_settings, alpha), str(alpha)),
        (_file_id(engine_settings, beta), str(beta)),
    ]
    assert "2 file(s) hold a zero-byte picture." in report.summary


def _two_duplicates(music_dir: Path) -> None:
    """Share one picture between two album artists and another among three."""
    _album(music_dir / "Alpha" / "X", "X", "Alpha", [_FRONT])
    _album(music_dir / "Beta" / "X", "X", "Beta", [_FRONT, None])
    for artist in ("Gamma", "Delta", "Epsilon"):
        _album(music_dir / artist / "Y", "Y", artist, [_OTHER])


def test_path_keeps_a_duplicate_with_every_album_and_limit_caps_the_rows(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _two_duplicates(music_dir)

    everything = _detect(engine_settings)
    narrowed = detect_picture_duplicates(engine_settings, path="beta")
    capped = detect_picture_duplicates(engine_settings, limit=1)

    assert [d.sha256 for d in everything.duplicates] == [_sha(_OTHER), _sha(_FRONT)]
    [kept] = narrowed.duplicates
    assert [album.album_artist for album in kept.albums] == ["Alpha", "Beta"]
    assert (narrowed.files_with_pictures, narrowed.distinct_pictures) == (1, 1)
    assert [d.sha256 for d in capped.duplicates] == [_sha(_OTHER)]
    assert capped.summary == everything.summary
    with pytest.raises(ValueError, match="must be >= 0"):
        detect_picture_duplicates(engine_settings, limit=-1)


def test_files_the_current_reader_has_not_read_are_counted_unread(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _two_duplicates(music_dir)
    scan_library(engine_settings)
    _ledger(engine_settings, "UPDATE files SET reader_version = 0 WHERE filename = '02.mp3'")

    report = detect_picture_duplicates(engine_settings)

    assert report.unread_files == 1
    assert report.summary.endswith("1 file(s) have no picture record yet. Run scan_library.")


def test_files_a_presence_scan_inserted_are_counted_unread(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _two_duplicates(music_dir)
    scan_library(engine_settings, mode=library.ScanMode.PRESENCE)

    report = detect_picture_duplicates(engine_settings)

    assert (report.files_with_pictures, report.unread_files, report.duplicates) == (0, 6, [])
    assert report.summary.endswith("6 file(s) have no picture record yet. Run scan_library.")


def test_the_mcp_tool_returns_the_report(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    _two_duplicates(music_dir)
    mcp_server.scan_library()

    payload = mcp_server.detect_picture_duplicates(limit=1)

    assert list(payload) == [
        "ok",
        "files_with_pictures",
        "distinct_pictures",
        "unread_files",
        "duplicates",
        "empty_pictures",
        "summary",
    ]
    duplicates = payload["duplicates"]
    assert isinstance(duplicates, list)
    assert [row["album_artists"] for row in duplicates] == [3]
    assert list(duplicates[0]["albums"][0]) == [
        "album_artist",
        "album",
        "folders",
        "files",
        "album_files",
        "file_ids",
        "cover_images",
    ]
