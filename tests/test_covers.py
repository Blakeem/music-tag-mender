"""Tests for the album cover detector (:mod:`tagmend.engine.covers`) and the picture probe."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING, Any

import mutagen
import pytest
from mutagen.flac import Picture
from mutagen.id3 import APIC  # type: ignore[attr-defined]
from mutagen.mp4 import MP4Cover

from conftest import make_track
from tagmend import config, mcp_server
from tagmend.engine import covers, db, schema
from tagmend.engine.library import scan_library
from tagmend.engine.tags import has_embedded_picture

if TYPE_CHECKING:
    from tagmend.config import Settings

_IMAGE = b"\xff\xd8\xff\xe0 not a real jpeg"


def _embed_picture(path: Path) -> None:
    """Embed a front picture in *path* the way its container stores one."""
    audio: Any = mutagen.File(path)  # type: ignore[attr-defined]
    picture: Any = Picture()  # type: ignore[no-untyped-call]
    picture.type = 3
    picture.mime = "image/jpeg"
    picture.data = _IMAGE
    if audio.tags is None:
        audio.add_tags()
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        frame = APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=_IMAGE)  # type: ignore[no-untyped-call]
        audio.tags.add(frame)
    elif suffix == ".flac":
        audio.add_picture(picture)
    elif suffix == ".m4a":
        audio["covr"] = [MP4Cover(_IMAGE, imageformat=MP4Cover.FORMAT_JPEG)]  # type: ignore[no-untyped-call]
    else:
        audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
    audio.save()


def _album(folder: Path, album: str, *, artist: str = "Band", count: int = 2) -> list[Path]:
    """Write *count* tracks of one album into *folder*."""
    tags = {"artist": [artist], "albumartist": [artist], "album": [album], "date": ["2001"]}
    return [make_track(folder / f"{index:02d}.mp3", tags) for index in range(1, count + 1)]


def _image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_IMAGE)


def _plans(settings: Settings) -> list[covers.AlbumCover]:
    """Scan the library and return every album's cover plan."""
    scan_library(settings)
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        return covers.plan_album_covers(connection, settings)
    finally:
        connection.close()


def _plan(settings: Settings) -> dict[str | None, covers.AlbumCover]:
    """Scan the library and return each album's cover plan keyed by album title."""
    return {album.album: album for album in _plans(settings)}


# --- the embedded-picture probe -----------------------------------------------------


@pytest.mark.parametrize("suffix", [".mp3", ".flac", ".m4a", ".ogg"])
def test_has_embedded_picture_reads_each_container(tmp_path: Path, suffix: str) -> None:
    track = make_track(tmp_path / f"t{suffix}", {"title": ["Song"]})
    bare = make_track(tmp_path / f"bare{suffix}")

    before = has_embedded_picture(track)
    _embed_picture(track)

    assert before is False
    assert has_embedded_picture(track) is True
    assert has_embedded_picture(bare) is False


def test_has_embedded_picture_raises_for_an_unreadable_file(tmp_path: Path) -> None:
    broken = tmp_path / "broken.flac"
    broken.write_bytes(b"not audio at all")

    with pytest.raises(mutagen.MutagenError):  # type: ignore[attr-defined]
        has_embedded_picture(broken)


# --- the status rules ---------------------------------------------------------------


def test_a_cover_file_in_the_album_folder_covers_it_under_any_case(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir / "Band" / "Blue", "Blue")
    _image(music_dir / "Band" / "Blue" / "Cover.JPG")

    plan = _plan(engine_settings)

    assert plan["Blue"].status == covers.STATUS_COVERED_BY_FILE
    assert plan["Blue"].target_folder is None


def test_an_embedded_picture_in_the_second_track_covers_the_album(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _album(music_dir / "Band" / "Green", "Green")
    _embed_picture(tracks[1])

    plan = _plan(engine_settings)

    assert plan["Green"].status == covers.STATUS_COVERED_BY_PICTURE


def _albums_in(settings: Settings, name: str) -> list[covers.AlbumCover]:
    """Scan the library and return every album plan titled *name*."""
    return [album for album in _plans(settings) if album.album == name]


def _dated_tracks(folder: Path) -> list[Path]:
    """Write three tracks of one album whose dates differ, one of them blank."""
    base = {"artist": ["Band"], "album": ["[Other]"]}
    return [
        make_track(folder / "a.mp3", {**base, "date": ["2001"]}),
        make_track(folder / "b.mp3", {**base, "date": ["2004"]}),
        make_track(folder / "c.mp3", base),
    ]


def test_mp3_tracks_differing_only_in_date_are_one_album(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _dated_tracks(music_dir / "Band" / "Other")

    [album] = _albums_in(engine_settings, "[Other]")

    assert album.status == covers.STATUS_GAP
    assert len(album.file_ids) == 3


@pytest.mark.parametrize(
    ("suffix", "field"),
    [(".flac", "year"), (".flac", "releasedate"), (".ogg", "year"), (".m4a", "date")],
)
def test_a_release_date_navidrome_keys_on_splits_the_album(
    engine_settings: Settings,
    music_dir: Path,
    suffix: str,
    field: str,
) -> None:
    folder = music_dir / "Band" / "Other"
    base = {"artist": ["Band"], "album": ["[Other]"]}
    make_track(folder / f"a{suffix}", {**base, field: ["2001"]})
    make_track(folder / f"b{suffix}", {**base, field: ["2004"]})

    albums = _albums_in(engine_settings, "[Other]")

    assert [album.status for album in albums] == [covers.STATUS_SHARED_FOLDER] * 2


def test_a_vorbis_releasedate_outranks_its_year(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = music_dir / "Band" / "Other"
    base = {"artist": ["Band"], "album": ["[Other]"], "releasedate": ["2001"]}
    make_track(folder / "a.flac", {**base, "year": ["2001"]})
    make_track(folder / "b.flac", {**base, "year": ["2004"]})

    [album] = _albums_in(engine_settings, "[Other]")

    assert album.status == covers.STATUS_GAP


def test_a_picture_on_one_dated_track_covers_the_whole_album(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tracks = _dated_tracks(music_dir / "Band" / "Other")
    _embed_picture(tracks[1])

    [album] = _albums_in(engine_settings, "[Other]")

    assert album.status == covers.STATUS_COVERED_BY_PICTURE


def test_an_image_no_pattern_matches_leaves_a_gap_and_is_listed(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = music_dir / "Weezer" / "Red Album"
    _album(folder, "Red Album", artist="Weezer")
    _image(folder / "WeezerRedAlbumFront.jpg")
    _image(folder / "Scans" / "back.png")
    (folder / "notes.txt").write_text("not an image", encoding="utf-8")

    album = _plan(engine_settings)["Red Album"]

    assert album.status == covers.STATUS_GAP
    assert album.target_folder == str(folder)
    assert album.images == (str(Path("Scans") / "back.png"), "WeezerRedAlbumFront.jpg")
    assert album.album_artist == "Weezer"
    assert len(album.file_ids) == 2


def test_disc_folders_take_the_cover_of_their_release_folder(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    release = music_dir / "Band" / "Double"
    _album(release / "CD1", "Double")
    _album(release / "CD2", "Double")
    _image(release / "cover.jpg")

    album = _plan(engine_settings)["Double"]

    assert album.status == covers.STATUS_COVERED_BY_FILE
    assert album.folders == (str(release / "CD1"), str(release / "CD2"))


def test_disc_folders_without_a_cover_target_their_release_folder(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    release = music_dir / "Band" / "Double"
    _album(release / "CD1", "Double")
    _album(release / "CD2", "Double")

    album = _plan(engine_settings)["Double"]

    assert album.status == covers.STATUS_GAP
    assert album.target_folder == str(release)


def test_disc_folders_beside_other_audio_are_scattered(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    release = music_dir / "Band" / "Double"
    _album(release / "CD1", "Double")
    _album(release / "CD2", "Double")
    _album(release / "Extras", "Bonus")

    plan = _plan(engine_settings)

    assert plan["Double"].status == covers.STATUS_SCATTERED
    assert plan["Double"].target_folder is None


def test_the_parent_cover_counts_only_while_the_parent_holds_no_other_audio(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    artist = music_dir / "Band"
    _album(artist / "Lone", "Lone")
    _image(artist / "cover.jpg")

    alone = _plan(engine_settings)["Lone"].status
    _album(artist / "Other", "Other")
    beside = _plan(engine_settings)["Lone"]

    assert alone == covers.STATUS_COVERED_BY_FILE
    assert beside.status == covers.STATUS_GAP
    assert beside.target_folder == str(artist / "Lone")


def test_a_lone_folder_holding_an_image_never_reads_its_parent(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    artist = music_dir / "Band"
    _album(artist / "Lone", "Lone")
    _image(artist / "Lone" / "back.jpg")
    _image(artist / "cover.jpg")

    album = _plan(engine_settings)["Lone"]

    assert album.status == covers.STATUS_GAP
    assert album.images == ("back.jpg",)


def test_a_folder_holding_two_albums_is_shared_and_a_split_album_is_scattered(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir / "Mixed", "First", count=1)
    make_track(music_dir / "Mixed" / "b.mp3", {"albumartist": ["Band"], "album": ["Second"]})
    _album(music_dir / "One" / "Split", "Split", count=1)
    make_track(
        music_dir / "Two" / "Elsewhere" / "b.mp3",
        {"albumartist": ["Band"], "album": ["Split"], "date": ["2001"]},
    )

    plan = _plan(engine_settings)

    assert plan["First"].status == covers.STATUS_SHARED_FOLDER
    assert plan["Second"].status == covers.STATUS_SHARED_FOLDER
    assert plan["Split"].status == covers.STATUS_SCATTERED
    assert plan["Split"].target_folder is None


def test_an_album_at_the_library_root_has_no_folder_of_its_own(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir, "Loose")

    album = _plan(engine_settings)["Loose"]

    assert album.status == covers.STATUS_LIBRARY_ROOT
    assert album.target_folder is None


def test_an_image_above_the_library_never_covers_a_root_album(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir, "Loose")
    _image(music_dir.parent / "cover.jpg")

    album = _plan(engine_settings)["Loose"]

    assert album.status == covers.STATUS_LIBRARY_ROOT


def test_the_release_ids_come_from_the_identity_and_the_shared_group(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    tags = {
        "album": ["Tagged"],
        "albumartist": ["Band"],
        "musicbrainz_albumid": ["rel-1"],
        "musicbrainz_releasegroupid": ["rg-1"],
    }
    make_track(music_dir / "Band" / "Tagged" / "01.mp3", tags)
    make_track(music_dir / "Band" / "Tagged" / "02.mp3", {**tags, "album": ["tagged!"]})

    album = _plan(engine_settings)["Tagged"]

    assert album.identity == ("release", "rel-1")
    assert album.release_mbid == "rel-1"
    assert album.release_group_mbid == "rg-1"
    assert len(album.file_ids) == 2


def test_an_unreadable_file_counts_as_no_picture_and_warns(
    engine_settings: Settings,
    music_dir: Path,
    tagmend_warnings: pytest.LogCaptureFixture,
) -> None:
    tracks = _album(music_dir / "Band" / "Broken", "Broken")
    scan_library(engine_settings)
    tracks[0].write_bytes(b"not audio at all")

    report = covers.detect_cover_gaps(engine_settings)

    assert [row.status for row in report.rows] == [covers.STATUS_GAP]
    assert "cover probe: cannot read" in tagmend_warnings.text


# --- the report and the tool ----------------------------------------------------------


def _report_library(music_dir: Path) -> None:
    _album(music_dir / "Band" / "Covered", "Covered")
    _image(music_dir / "Band" / "Covered" / "folder.png")
    _album(music_dir / "Band" / "Zeta", "Zeta")
    _album(music_dir / "Band" / "Alpha", "Alpha")
    _album(music_dir, "Loose")


def test_detect_cover_gaps_counts_every_album_and_lists_the_uncovered(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _report_library(music_dir)
    scan_library(engine_settings)

    report = covers.detect_cover_gaps(engine_settings)
    capped = covers.detect_cover_gaps(engine_settings, limit=1)

    assert (report.albums, report.covered_by_file, report.gap, report.library_root) == (4, 1, 2, 1)
    assert [row.album for row in report.rows] == ["Loose", "Alpha", "Zeta"]
    assert report.summary.startswith("3 of 4 album(s) show no cover")
    assert [row.album for row in capped.rows] == ["Loose"]
    assert capped.albums == 4


def test_detect_cover_gaps_keeps_the_albums_under_a_folder(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _report_library(music_dir)
    scan_library(engine_settings)

    report = covers.detect_cover_gaps(engine_settings, folder="Band/Alpha")

    assert report.albums == 1
    assert [row.album for row in report.rows] == ["Alpha"]
    with pytest.raises(ValueError, match="outside music_path"):
        covers.detect_cover_gaps(engine_settings, folder=str(music_dir.parent))


def test_detect_cover_gaps_reports_a_covered_library(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir / "Band" / "Covered", "Covered")
    _image(music_dir / "Band" / "Covered" / "front.webp")
    scan_library(engine_settings)

    report = covers.detect_cover_gaps(engine_settings)

    assert report.rows == []
    assert report.summary == "Every album shows a cover (1 album(s) checked)."


def test_the_mcp_tool_returns_the_counts_and_rows_and_honors_folder_and_limit(
    music_dir: Path,
) -> None:
    config.set_setting("music_path", str(music_dir))
    _report_library(music_dir)
    mcp_server.scan_library()

    payload = mcp_server.detect_cover_gaps()
    capped = mcp_server.detect_cover_gaps(limit=1)
    narrowed = mcp_server.detect_cover_gaps(folder="Band/Zeta")

    assert payload["ok"] is True
    assert (payload["albums"], payload["gap"], payload["library_root"]) == (4, 2, 1)
    rows = payload["rows"]
    assert isinstance(rows, list)
    assert rows[1]["target_folder"] == str(music_dir / "Band" / "Alpha")
    assert rows[1]["images"] == []
    assert len(capped["rows"]) == 1
    assert narrowed["albums"] == 1
