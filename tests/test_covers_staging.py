"""Tests for the cover staging area and its commit: the image check, ``stage_covers``,
``unstage_covers``, ``diff_covers`` and ``commit_covers`` (:mod:`tagmend.engine.covers`), with a
fake Cover Art Archive source."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import zlib
from typing import TYPE_CHECKING

import pytest

from conftest import make_track
from tagmend import config, mcp_server
from tagmend.engine import commits, covers, db, genres, paths, schema, store, trash, versioning
from tagmend.engine.coverart import CoverArtError, CoverArtFront
from tagmend.engine.library import scan_library

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings
    from tagmend.engine.coverart import CoverArtKind


def _jpeg(width: int = 600, height: int = 400, *, frame: int = 0xC0) -> bytes:
    """Build a JPEG header: SOI, APP0, a DHT segment, the frame header, then the scan."""
    app0 = b"\xff\xe0" + (16).to_bytes(2) + b"JFIF\x00" + bytes(9)
    dht = b"\xff\xc4" + (5).to_bytes(2) + bytes(3)
    size = height.to_bytes(2) + width.to_bytes(2)
    sof = bytes([0xFF, frame]) + (11).to_bytes(2) + b"\x08" + size + bytes([1, 1, 0x11, 0])
    sos = b"\xff\xda" + (8).to_bytes(2) + bytes(6)
    return b"\xff\xd8" + app0 + dht + sof + sos + bytes(8) + b"\xff\xd9"


def _png(width: int = 500, height: int = 500) -> bytes:
    """Build a PNG signature and its IHDR chunk."""
    header = width.to_bytes(4) + height.to_bytes(4) + bytes([8, 2, 0, 0, 0])
    crc = zlib.crc32(b"IHDR" + header).to_bytes(4)
    return b"\x89PNG\r\n\x1a\n" + len(header).to_bytes(4) + b"IHDR" + header + crc


_GIF = b"GIF89a" + bytes(20)


def _front(kind: CoverArtKind, mbid: str, *, original: str = "front.jpg") -> CoverArtFront:
    base = f"https://caa.test/{kind}/{mbid}"
    return CoverArtFront(
        kind=kind,
        mbid=mbid,
        image=f"{base}/{original}",
        thumbnail_1200=f"{base}/front-1200.jpg",
        thumbnail_500=f"{base}/front-500.jpg",
        thumbnail_large=f"{base}/front-large.jpg",
    )


class _FakeCoverArt:
    """A CAA source answering from dicts and recording every call."""

    def __init__(
        self,
        fronts: dict[tuple[str, str], CoverArtFront] | None = None,
        images: dict[str, bytes] | None = None,
    ) -> None:
        self.fronts = fronts or {}
        self.images = images or {}
        self.listings: list[tuple[str, str]] = []
        self.downloads: list[str] = []

    def front_image(self, kind: CoverArtKind, mbid: str) -> CoverArtFront | None:
        self.listings.append((kind, mbid))
        return self.fronts.get((kind, mbid))

    def fetch_image(self, url: str) -> bytes:
        self.downloads.append(url)
        return self.images[url]


class _FailingCoverArt(_FakeCoverArt):
    def front_image(self, kind: CoverArtKind, mbid: str) -> CoverArtFront | None:
        message = f"Cover Art Archive HTTP 503 for {kind} {mbid} listing"
        raise CoverArtError(message)


def _album(folder: Path, album: str, *, count: int = 2, **ids: str) -> list[Path]:
    """Write *count* tracks of one album into *folder*, with optional MusicBrainz *ids*."""
    tags = {"artist": ["Band"], "albumartist": ["Band"], "album": [album], "date": ["2001"]}
    tags |= {name: [value] for name, value in ids.items()}
    return [make_track(folder / f"{index:02d}.mp3", tags) for index in range(1, count + 1)]


def _staged(settings: Settings) -> list[tuple[str, str, bytes]]:
    """Return every staged cover row as ``(target_path, origin, content)``."""
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = connection.execute(
            "SELECT target_path, origin, content FROM cover_writes_staged ORDER BY target_key"
        ).fetchall()
    finally:
        connection.close()
    return [(str(row[0]), str(row[1]), bytes(row[2])) for row in rows]


# --- the image check -------------------------------------------------------------------


@pytest.mark.parametrize("frame", [0xC0, 0xC2])
def test_inspect_image_reads_a_baseline_and_a_progressive_jpeg(frame: int) -> None:
    info = covers.inspect_image(_jpeg(640, 480, frame=frame))

    assert (info.format, info.width, info.height) == ("jpeg", 640, 480)


def test_inspect_image_reads_a_png() -> None:
    info = covers.inspect_image(_png(1200, 1100))

    assert (info.format, info.width, info.height) == ("png", 1200, 1100)


def test_inspect_image_skips_fill_bytes_before_a_marker() -> None:
    data = _jpeg(300, 200)
    padded = data[:2] + b"\xff" + data[2:]

    assert covers.inspect_image(padded).width == 300


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (_jpeg()[:30], "truncated"),
        (_GIF, "neither JPEG nor PNG"),
        (_png()[:20], "truncated"),
        (_jpeg(0, 400), "zero dimension"),
        (b"\xff\xd8\xff\xda" + bytes(10), "scan data before any frame header"),
    ],
    ids=["truncated_jpeg", "gif", "truncated_png", "zero_width", "scan_first"],
)
def test_inspect_image_rejects_what_is_not_a_whole_jpeg_or_png(data: bytes, problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        covers.inspect_image(data)


def test_inspect_image_rejects_an_image_over_the_cap() -> None:
    oversized = _jpeg() + bytes(covers.MAX_COVER_BYTES)

    with pytest.raises(ValueError, match="over the"):
        covers.inspect_image(oversized)


# --- the sources -----------------------------------------------------------------------


def test_a_front_image_in_the_folder_is_copied_and_the_owner_files_stay(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = music_dir / "Soundtracks" / "Hackers"
    _album(folder, "Hackers")
    front, back = _jpeg(800, 800), _jpeg(700, 700)
    (folder / "Hackers Soundtrack - Front.jpg").write_bytes(front)
    (folder / "Hackers Soundtrack - Back.jpg").write_bytes(back)
    scan_library(engine_settings)
    fake = _FakeCoverArt()

    result = covers.stage_covers(engine_settings, client=fake)

    staged = result.staged[0]
    assert staged.target_path == str(folder.relative_to(music_dir) / "cover.jpg")
    assert (staged.source_kind, staged.origin) == (covers.SOURCE_FOLDER_IMAGE, "auto")
    assert staged.source_ref.endswith("Hackers Soundtrack - Front.jpg")
    assert (staged.width, staged.size_bytes) == (800, len(front))
    assert _staged(engine_settings) == [(staged.target_path, "auto", front)]
    assert (folder / "Hackers Soundtrack - Front.jpg").read_bytes() == front
    assert (folder / "Hackers Soundtrack - Back.jpg").read_bytes() == back
    assert not (folder / "cover.jpg").exists()
    assert fake.listings == []


def test_two_images_naming_no_front_are_ambiguous_and_no_lookup_runs(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = music_dir / "Band" / "Pair"
    _album(folder, "Pair", musicbrainz_albumid="rel-1")
    (folder / "inlay.jpg").write_bytes(_jpeg())
    (folder / "disc.png").write_bytes(_png())
    scan_library(engine_settings)
    fake = _FakeCoverArt({("release", "rel-1"): _front("release", "rel-1")})

    result = covers.stage_covers(engine_settings, client=fake)

    assert result.staged == []
    [skip] = result.skipped
    assert skip.reason == covers.SKIP_AMBIGUOUS_IMAGES
    assert skip.detail == "disc.png, inlay.jpg"
    assert fake.listings == []
    assert _staged(engine_settings) == []


def test_a_release_front_stages_the_original_jpeg(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir / "Band" / "Tagged", "Tagged", musicbrainz_albumid="rel-1")
    scan_library(engine_settings)
    front = _front("release", "rel-1")
    original = _jpeg(1500, 1500)
    fake = _FakeCoverArt({("release", "rel-1"): front}, {front.image: original})

    result = covers.stage_covers(engine_settings, client=fake)

    staged = result.staged[0]
    assert (staged.source_kind, staged.source_ref) == (covers.SOURCE_RELEASE, front.image)
    assert (staged.format, staged.width, staged.height) == ("jpeg", 1500, 1500)
    assert fake.downloads == [front.image]
    assert _staged(engine_settings)[0][2] == original


def test_a_gif_original_gives_way_to_the_1200_thumbnail(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir / "Band" / "Animated", "Animated", musicbrainz_albumid="rel-2")
    scan_library(engine_settings)
    front = _front("release", "rel-2", original="front.gif")
    assert front.thumbnail_1200 is not None
    thumbnail = _jpeg(1200, 1200)
    fake = _FakeCoverArt(
        {("release", "rel-2"): front},
        {front.image: _GIF, front.thumbnail_1200: thumbnail},
    )

    result = covers.stage_covers(engine_settings, client=fake)

    assert result.staged[0].source_ref == front.thumbnail_1200
    assert result.staged[0].target_path.endswith("cover.jpg")
    assert fake.downloads == [front.image, front.thumbnail_1200]
    assert _staged(engine_settings)[0][2] == thumbnail


def test_no_valid_caa_image_skips_the_album_as_invalid(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir / "Band" / "Broken", "Broken", musicbrainz_albumid="rel-3")
    scan_library(engine_settings)
    front = _front("release", "rel-3", original="front.gif")
    urls = [url for url in (front.image, front.thumbnail_1200, front.thumbnail_large) if url]
    fake = _FakeCoverArt({("release", "rel-3"): front}, dict.fromkeys(urls, _GIF))

    result = covers.stage_covers(engine_settings, client=fake)

    assert [skip.reason for skip in result.skipped] == [covers.SKIP_INVALID_IMAGE]
    assert fake.downloads == urls


def test_a_release_group_front_serves_an_album_without_a_release_id(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir / "Band" / "Grouped", "Grouped", musicbrainz_releasegroupid="rg-1")
    _album(music_dir / "Band" / "Bare", "Bare")
    scan_library(engine_settings)
    front = _front("release-group", "rg-1", original="front.png")
    fake = _FakeCoverArt({("release-group", "rg-1"): front}, {front.image: _png()})

    result = covers.stage_covers(engine_settings, client=fake)

    [staged] = result.staged
    assert staged.source_kind == covers.SOURCE_RELEASE_GROUP
    assert staged.target_path.endswith("cover.png")
    assert [(skip.album, skip.reason) for skip in result.skipped] == [
        ("Band - Bare", covers.SKIP_NO_SOURCE),
    ]
    assert fake.listings == [("release-group", "rg-1")]


def test_a_lookup_failure_skips_the_album_with_the_message(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir / "Band" / "Tagged", "Tagged", musicbrainz_albumid="rel-1")
    scan_library(engine_settings)

    result = covers.stage_covers(engine_settings, client=_FailingCoverArt())

    [skip] = result.skipped
    assert skip.reason == covers.SKIP_LOOKUP_ERROR
    assert skip.detail is not None
    assert "HTTP 503" in skip.detail


def test_the_owner_image_stages_a_png_and_replaces_the_earlier_row(
    engine_settings: Settings,
    music_dir: Path,
    tmp_path: Path,
) -> None:
    folder = music_dir / "Band" / "Tagged"
    _album(folder, "Tagged", musicbrainz_albumid="rel-1")
    scan_library(engine_settings)
    front = _front("release", "rel-1")
    fake = _FakeCoverArt({("release", "rel-1"): front}, {front.image: _jpeg()})
    covers.stage_covers(engine_settings, client=fake)
    owner_png = tmp_path / "chosen.png"
    owner_png.write_bytes(_png(900, 900))

    again = covers.stage_covers(engine_settings, client=fake)
    result = covers.stage_covers(
        engine_settings, folder="Band/Tagged", image=owner_png, client=fake
    )

    assert [skip.reason for skip in again.skipped] == [covers.SKIP_ALREADY_STAGED]
    [staged] = result.staged
    assert (staged.source_kind, staged.origin) == (covers.SOURCE_OWNER_FILE, "manual")
    assert staged.source_ref == str(owner_png)
    rows = _staged(engine_settings)
    assert rows == [(str(folder.relative_to(music_dir) / "cover.png"), "manual", _png(900, 900))]


def test_the_owner_image_needs_a_folder_selecting_one_gap_album(
    engine_settings: Settings,
    music_dir: Path,
    tmp_path: Path,
) -> None:
    _album(music_dir / "Band" / "One", "One")
    _album(music_dir / "Band" / "Two", "Two")
    scan_library(engine_settings)
    owner_png = tmp_path / "chosen.png"
    owner_png.write_bytes(_png())

    with pytest.raises(ValueError, match="image requires folder"):
        covers.stage_covers(engine_settings, image=owner_png, client=_FakeCoverArt())
    with pytest.raises(ValueError, match="selects 2 album"):
        covers.stage_covers(engine_settings, folder="Band", image=owner_png, client=_FakeCoverArt())
    with pytest.raises(ValueError, match="not a regular file"):
        covers.stage_covers(
            engine_settings, folder="Band/One", image=tmp_path, client=_FakeCoverArt()
        )


def test_an_invalid_owner_image_skips_the_album(
    engine_settings: Settings,
    music_dir: Path,
    tmp_path: Path,
) -> None:
    _album(music_dir / "Band" / "One", "One")
    scan_library(engine_settings)
    owner_gif = tmp_path / "chosen.gif"
    owner_gif.write_bytes(_GIF)

    result = covers.stage_covers(
        engine_settings, folder="Band/One", image=owner_gif, client=_FakeCoverArt()
    )

    assert [skip.reason for skip in result.skipped] == [covers.SKIP_INVALID_IMAGE]


# --- scope, limit, target and dry run --------------------------------------------------


def test_every_other_status_is_skipped_and_the_limit_reports_more(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir / "Band" / "Covered", "Covered")
    (music_dir / "Band" / "Covered" / "folder.jpg").write_bytes(_jpeg())
    for name in ("Alpha", "Beta"):
        _album(music_dir / "Band" / name, name)
        (music_dir / "Band" / name / "scan.jpg").write_bytes(_jpeg())
    scan_library(engine_settings)

    result = covers.stage_covers(engine_settings, limit=1, client=_FakeCoverArt())

    assert len(result.staged) == 1
    assert result.more is True
    assert [(skip.album, skip.reason) for skip in result.skipped] == [
        ("Band - Covered", covers.STATUS_COVERED_BY_FILE),
    ]
    assert "More gap albums remain" in result.summary
    with pytest.raises(ValueError, match="limit must be >= 0"):
        covers.stage_covers(engine_settings, limit=-1, client=_FakeCoverArt())


def test_a_file_named_like_the_target_skips_the_album(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = music_dir / "Band" / "Taken"
    _album(folder, "Taken")
    (folder / "front scan.jpg").write_bytes(_jpeg())
    (folder / "COVER.JPG").mkdir()
    scan_library(engine_settings)

    result = covers.stage_covers(engine_settings, client=_FakeCoverArt())

    assert [(skip.reason, skip.detail) for skip in result.skipped] == [
        (covers.SKIP_TARGET_TAKEN, "cover.jpg"),
    ]


def test_a_dry_run_stages_nothing_and_downloads_nothing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir / "Band" / "Tagged", "Tagged", musicbrainz_albumid="rel-1")
    scan_library(engine_settings)
    front = _front("release", "rel-1")
    fake = _FakeCoverArt({("release", "rel-1"): front}, {front.image: _jpeg()})

    result = covers.stage_covers(engine_settings, dry_run=True, client=fake)

    [staged] = result.staged
    assert staged.source_ref == front.image
    assert (staged.format, staged.width, staged.height, staged.size_bytes) == (None,) * 4
    assert result.summary.startswith("Would stage 1 cover(s)")
    assert fake.listings == [("release", "rel-1")]
    assert fake.downloads == []
    assert _staged(engine_settings) == []


# --- the staging-area guards -----------------------------------------------------------


def test_a_real_run_is_refused_while_a_path_move_is_staged(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _album(music_dir / "Band" / "One", "One")
    scan_library(engine_settings)
    connection = db.connect(engine_settings.db_path)
    try:
        store.upsert_staged_path(
            connection,
            store.StagedPath(
                file_id=1,
                to_path="Band/One/moved.mp3",
                to_key="band/one/moved.mp3",
                origin="manual",
                note=None,
                staged_at="2026-10-02T00:00:00+00:00",
                base_size_bytes=1,
                base_mtime_ns=1,
                reverted_to_version=None,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    preview = covers.stage_covers(engine_settings, dry_run=True, client=_FakeCoverArt())

    assert preview.dry_run is True
    with pytest.raises(ValueError, match="move is staged"):
        covers.stage_covers(engine_settings, client=_FakeCoverArt())


def test_a_staged_cover_blocks_the_resolvers_and_revert_commit(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = music_dir / "Band" / "One"
    _album(folder, "One")
    (folder / "scan front.png").write_bytes(_png())
    scan_library(engine_settings)
    covers.stage_covers(engine_settings, client=_FakeCoverArt())
    connection = db.connect(engine_settings.db_path)
    try:
        connection.execute(
            "INSERT INTO commits (created_at, origin, status) "
            "VALUES ('2026-10-01T00:00:00+00:00', 'manual', 'applied')"
        )
        connection.commit()
        assert store.any_staged(connection) is True
    finally:
        connection.close()

    with pytest.raises(ValueError, match="commit or unstage pending changes first"):
        genres.resolve_genres(engine_settings)
    with pytest.raises(ValueError, match="staging area is not empty"):
        versioning.revert_commit(engine_settings, 1)


# --- diff_covers and unstage_covers ----------------------------------------------------


def _stage_one(settings: Settings, music_dir: Path, name: str) -> Path:
    folder = music_dir / "Band" / name
    _album(folder, name)
    (folder / f"{name} front.jpg").write_bytes(_jpeg())
    scan_library(settings)
    covers.stage_covers(settings, folder=f"Band/{name}", client=_FakeCoverArt())
    return folder


def test_diff_covers_reports_ready_then_target_taken(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _stage_one(engine_settings, music_dir, "One")

    [ready] = covers.diff_covers(engine_settings)
    (folder / "cover.jpg").write_bytes(b"someone else's")
    [taken] = covers.diff_covers(engine_settings)

    assert ready.state == covers.STATE_READY
    assert ready.sha256 == hashlib.sha256(_jpeg()).hexdigest()
    assert len(ready.file_ids) == 2
    assert taken.state == covers.STATE_TARGET_TAKEN


def test_diff_covers_reports_a_cover_that_appeared_since_staging(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _stage_one(engine_settings, music_dir, "One")
    (folder / "folder.png").write_bytes(_png())

    [view] = covers.diff_covers(engine_settings)

    assert view.state == covers.STATE_COVERED_SINCE_STAGE


def test_diff_covers_reports_an_album_moved_on_disk_and_rescanned(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _stage_one(engine_settings, music_dir, "One")
    elsewhere = music_dir / "Band" / "Elsewhere"
    elsewhere.mkdir()
    shutil.move(folder / "02.mp3", elsewhere / "02.mp3")
    scan_library(engine_settings)

    [view] = covers.diff_covers(engine_settings)

    assert view.state == covers.STATE_ALBUM_MOVED


def test_diff_covers_keeps_disc_folders_below_the_target(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    release = music_dir / "Band" / "Double"
    _album(release / "CD1", "Double", count=1)
    make_track(
        release / "CD2" / "01.mp3",
        {"artist": ["Band"], "albumartist": ["Band"], "album": ["Double"], "date": ["2001"]},
    )
    (release / "Double front.jpg").write_bytes(_jpeg())
    scan_library(engine_settings)

    result = covers.stage_covers(engine_settings, client=_FakeCoverArt())
    [view] = covers.diff_covers(engine_settings)

    assert result.staged[0].target_path == str(release.relative_to(music_dir) / "cover.jpg")
    assert view.state == covers.STATE_READY


def test_diff_and_unstage_scope_by_folder_and_unstage_clears_the_rows(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _stage_one(engine_settings, music_dir, "One")
    _stage_one(engine_settings, music_dir, "Two")

    scoped = covers.diff_covers(engine_settings, folder="Band/Two")
    capped = covers.diff_covers(engine_settings, limit=1)
    removed_one = covers.unstage_covers(engine_settings, folder="Band/One")
    removed_rest = covers.unstage_covers(engine_settings)

    assert [view.album for view in scoped] == ["Band - Two"]
    assert len(capped) == 1
    assert (removed_one, removed_rest) == (1, 1)
    assert covers.diff_covers(engine_settings) == []


# --- the MCP tools ---------------------------------------------------------------------


def test_the_mcp_tools_stage_diff_and_unstage(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    folder = music_dir / "Band" / "One"
    _album(folder, "One")
    (folder / "One - Front.jpg").write_bytes(_jpeg())
    mcp_server.scan_library()

    preview = mcp_server.stage_covers(dry_run=True)
    staged = mcp_server.stage_covers(folder="Band/One")
    diff = mcp_server.diff_covers()
    removed = mcp_server.unstage_covers(folder="Band")
    refused = mcp_server.stage_covers(limit=-1)

    assert preview["ok"] is True
    assert preview["dry_run"] is True
    assert staged["ok"] is True
    staged_rows = staged["staged"]
    assert isinstance(staged_rows, list)
    assert staged_rows[0]["source_kind"] == covers.SOURCE_FOLDER_IMAGE
    changes = diff["changes"]
    assert isinstance(changes, list)
    assert changes[0]["state"] == covers.STATE_READY
    assert "content" not in changes[0]
    assert removed == {"ok": True, "removed": 1}
    assert refused["ok"] is False


# --- the ledger tables -----------------------------------------------------------------


def test_cover_writes_staged_accepts_only_the_known_source_kinds(
    db_conn: sqlite3.Connection,
) -> None:
    insert = (
        "INSERT INTO cover_writes_staged (target_key, target_path, folder_key, album_label, "
        "file_ids, source_kind, source_ref, content, sha256, size_bytes, image_format, width, "
        "height, origin, staged_at) "
        "VALUES (?, 'A/cover.jpg', 'a', 'A', '[1]', ?, 'x', x'00', 's', 1, 'jpeg', 1, 1, "
        "'auto', '2026')"
    )
    db_conn.execute(insert, ("a/cover.jpg", "release"))

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        db_conn.execute(insert, ("b/cover.jpg", "lastfm"))


# --- commit_covers ---------------------------------------------------------------------


def _target(music_dir: Path, folder: Path) -> str:
    return str(folder.relative_to(music_dir) / "cover.jpg")


def _cover_writes(settings: Settings) -> list[tuple[int, str, str, str, bytes | None]]:
    """Return every ``cover_writes`` row as ``(commit_id, action, origin, path, content)``."""
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        rows = connection.execute(
            "SELECT commit_id, action, origin, path, content FROM cover_writes ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    return [(int(row[0]), str(row[1]), str(row[2]), str(row[3]), row[4]) for row in rows]


def test_commit_covers_writes_two_staged_covers_as_one_commit(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    one = _stage_one(engine_settings, music_dir, "One")
    two = _stage_one(engine_settings, music_dir, "Two")
    targets = [_target(music_dir, one), _target(music_dir, two)]

    result = covers.commit_covers(engine_settings)

    assert result.commit_id is not None
    assert [row.target_path for row in result.written] == targets
    assert result.errors == []
    assert (one / "cover.jpg").read_bytes() == _jpeg()
    assert (two / "cover.jpg").read_bytes() == _jpeg()
    assert _cover_writes(engine_settings) == [
        (result.commit_id, "create", "auto", target, _jpeg()) for target in targets
    ]
    assert _staged(engine_settings) == []
    assert versioning.commit_logs(engine_settings, result.commit_id) == {"cover_writes": 2}
    commit = commits.get_commit(engine_settings, result.commit_id)
    assert commit is not None
    assert (commit.origin, commit.status) == ("auto", "applied")


def test_an_owner_image_among_auto_covers_makes_the_commit_manual(
    engine_settings: Settings,
    music_dir: Path,
    tmp_path: Path,
) -> None:
    # The owner's cover sorts first, so a rule taking the first or the last row's origin fails.
    one = music_dir / "Band" / "One"
    _album(one, "One")
    scan_library(engine_settings)
    owner_png = tmp_path / "owner.png"
    owner_png.write_bytes(_png())
    covers.stage_covers(
        engine_settings, folder="Band/One", image=str(owner_png), client=_FakeCoverArt()
    )
    _stage_one(engine_settings, music_dir, "Two")

    result = covers.commit_covers(engine_settings)

    assert result.commit_id is not None
    assert len(result.written) == 2
    commit = commits.get_commit(engine_settings, result.commit_id)
    assert commit is not None
    assert commit.origin == "manual"
    assert sorted(row[2] for row in _cover_writes(engine_settings)) == ["auto", "manual"]


def test_a_file_placed_at_one_target_keeps_its_row_and_the_other_cover_writes(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    one = _stage_one(engine_settings, music_dir, "One")
    two = _stage_one(engine_settings, music_dir, "Two")
    (one / "cover.jpg").write_bytes(b"someone else's")

    result = covers.commit_covers(engine_settings)

    [error] = result.errors
    assert (error.target_path, error.reason) == (
        _target(music_dir, one),
        covers.STATE_TARGET_TAKEN,
    )
    assert "unstage_covers" in error.detail
    assert (one / "cover.jpg").read_bytes() == b"someone else's"
    assert (two / "cover.jpg").read_bytes() == _jpeg()
    assert [row[0] for row in _staged(engine_settings)] == [_target(music_dir, one)]
    assert [row[3] for row in _cover_writes(engine_settings)] == [_target(music_dir, two)]


def test_a_cover_that_appeared_since_staging_keeps_its_row(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _stage_one(engine_settings, music_dir, "One")
    (folder / "folder.png").write_bytes(_png())

    result = covers.commit_covers(engine_settings)

    assert [row.reason for row in result.errors] == [covers.STATE_COVERED_SINCE_STAGE]
    assert not (folder / "cover.jpg").exists()
    assert len(_staged(engine_settings)) == 1


def test_a_target_already_holding_the_staged_bytes_is_logged_as_landed(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _stage_one(engine_settings, music_dir, "One")
    (folder / "cover.jpg").write_bytes(_jpeg())
    landed_at = (folder / "cover.jpg").stat().st_mtime_ns

    result = covers.commit_covers(engine_settings)

    assert result.errors == []
    assert [row.target_path for row in result.written] == [_target(music_dir, folder)]
    assert (folder / "cover.jpg").stat().st_mtime_ns == landed_at
    assert [row[1] for row in _cover_writes(engine_settings)] == ["create"]
    assert _staged(engine_settings) == []


def test_a_leftover_temp_beside_the_target_is_replaced_and_removed(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _stage_one(engine_settings, music_dir, "One")
    (folder / "cover.jpg.tagmend.tmp").write_bytes(b"cut short")

    result = covers.commit_covers(engine_settings)

    assert result.errors == []
    assert (folder / "cover.jpg").read_bytes() == _jpeg()
    assert not (folder / "cover.jpg.tagmend.tmp").exists()


def test_an_album_moved_since_staging_writes_nothing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _stage_one(engine_settings, music_dir, "One")
    elsewhere = music_dir / "Band" / "Elsewhere"
    elsewhere.mkdir()
    shutil.move(folder / "02.mp3", elsewhere / "02.mp3")
    scan_library(engine_settings)

    result = covers.commit_covers(engine_settings)

    assert [row.reason for row in result.errors] == [covers.STATE_ALBUM_MOVED]
    assert result.written == []
    assert not (folder / "cover.jpg").exists()
    assert len(_staged(engine_settings)) == 1
    assert _cover_writes(engine_settings) == []


def test_a_failed_write_rolls_back_and_removes_the_temp(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = _stage_one(engine_settings, music_dir, "One")

    def refuse(_source: Path, _destination: Path) -> None:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr(paths, "move_no_clobber", refuse)

    result = covers.commit_covers(engine_settings)

    [error] = result.errors
    assert (error.reason, error.detail.startswith("disk full")) == (covers.COMMIT_ERROR, True)
    assert not (folder / "cover.jpg").exists()
    assert not (folder / "cover.jpg.tagmend.tmp").exists()
    assert len(_staged(engine_settings)) == 1
    assert _cover_writes(engine_settings) == []


def test_commit_covers_with_nothing_staged_recovers_and_creates_no_commit(
    engine_settings: Settings,
) -> None:
    connection = db.connect(engine_settings.db_path)
    try:
        schema.apply_schema(connection)
        stuck = commits.create_commit(
            connection, origin="manual", message=None, now="2026-10-02T00:00:00+00:00"
        )
        connection.commit()
    finally:
        connection.close()

    result = covers.commit_covers(engine_settings)

    assert (result.commit_id, result.written, result.errors) == (None, [], [])
    assert [(commit.id, commit.status) for commit in commits.list_commits(engine_settings)] == [
        (stuck, "interrupted")
    ]


def test_a_staged_move_refuses_the_commit_and_a_later_move_carries_the_cover(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    album = music_dir / "Artist" / "Album"
    tags = {"albumartist": ["Artist"], "artist": ["Artist"], "album": ["Album"]}
    tags |= {"date": ["2001"], "title": ["One"], "tracknumber": ["1"]}
    make_track(album / "01 One.flac", tags)
    (album / "Album front.jpg").write_bytes(_jpeg())
    scan_library(engine_settings)
    covers.stage_covers(engine_settings, client=_FakeCoverArt())
    paths.stage_paths(engine_settings, path="Artist")

    with pytest.raises(ValueError, match="move is staged"):
        covers.commit_covers(engine_settings)
    paths.unstage_paths(engine_settings, path="Artist")
    written = covers.commit_covers(engine_settings)
    paths.stage_paths(engine_settings, path="Artist")
    moved = paths.commit_paths(engine_settings)

    assert [row.target_path for row in written.written] == [_target(music_dir, album)]
    assert moved.commit_id is not None
    connection = db.connect(engine_settings.db_path)
    try:
        logged = store.sidecar_moves_for_commit(connection, moved.commit_id)
    finally:
        connection.close()
    [cover] = [row for row in logged if row.from_path == _target(music_dir, album)]
    assert (music_dir / cover.to_path).read_bytes() == _jpeg()
    assert not album.exists()


def test_the_mcp_tool_commits_the_staged_covers(music_dir: Path) -> None:
    config.set_setting("music_path", str(music_dir))
    folder = music_dir / "Band" / "One"
    _album(folder, "One")
    (folder / "One - Front.jpg").write_bytes(_jpeg())
    mcp_server.scan_library()
    mcp_server.stage_covers(folder="Band/One")

    committed = mcp_server.commit_covers()

    assert committed["ok"] is True
    commit_id = committed["commit_id"]
    assert isinstance(commit_id, int)
    assert committed["errors"] == []
    assert mcp_server.get_commit(commit_id)["logs"] == {"cover_writes": 1}
    assert (folder / "cover.jpg").read_bytes() == _jpeg()


# --- revert_commit of a cover commit ----------------------------------------------------


@pytest.fixture
def trashed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Path]:
    """Replace the OS trash with a folder under ``tmp_path`` and return each path sent to it."""
    bin_folder = tmp_path / "trash"
    bin_folder.mkdir()
    sent: list[Path] = []

    def fake(path: Path) -> None:
        sent.append(path)
        shutil.move(path, bin_folder / f"{len(sent)} {path.name}")

    monkeypatch.setattr(trash, "send_to_trash", fake)
    return sent


def _revert(
    settings: Settings,
    commit_id: int | None,
    *,
    note: str | None = None,
    dry_run: bool = False,
) -> paths.PathRevertCommitResult:
    """Run ``revert_commit`` on the cover commit *commit_id* and return its typed result."""
    assert commit_id is not None
    result = versioning.revert_commit(settings, commit_id, note=note, dry_run=dry_run)
    assert isinstance(result, paths.PathRevertCommitResult)
    return result


def _reverted_from(settings: Settings) -> list[int | None]:
    """Return the ``reverted_from`` of every ``cover_writes`` row, in id order."""
    connection = db.connect(settings.db_path)
    try:
        rows = connection.execute("SELECT reverted_from FROM cover_writes ORDER BY id").fetchall()
    finally:
        connection.close()
    return [None if row[0] is None else int(row[0]) for row in rows]


def test_reverting_two_covers_trashes_both_and_its_revert_writes_them_again(
    engine_settings: Settings,
    music_dir: Path,
    trashed: list[Path],
) -> None:
    one = _stage_one(engine_settings, music_dir, "One")
    two = _stage_one(engine_settings, music_dir, "Two")
    targets = [_target(music_dir, one), _target(music_dir, two)]
    written = covers.commit_covers(engine_settings)

    reverted = _revert(engine_settings, written.commit_id, note="undo covers")
    gone = [(one / "cover.jpg").exists(), (two / "cover.jpg").exists()]
    again = _revert(engine_settings, reverted.commit_id)

    assert trashed == [music_dir / target for target in targets]
    assert gone == [False, False]
    assert [(row.from_path, row.status) for row in reverted.sidecars] == [
        (target, "reverted") for target in targets
    ]
    assert reverted.to_dict()["sidecars_reverted"] == 2
    assert reverted.commit_id is not None
    commit = commits.get_commit(engine_settings, reverted.commit_id)
    assert commit is not None
    assert (commit.origin, commit.status, commit.reverted_from, commit.message) == (
        "revert",
        "applied",
        written.commit_id,
        "undo covers",
    )
    assert again.commit_id is not None
    assert (one / "cover.jpg").read_bytes() == _jpeg()
    assert (two / "cover.jpg").read_bytes() == _jpeg()
    assert not (one / "cover.jpg.tagmend.tmp").exists()
    assert _cover_writes(engine_settings) == [
        *[(written.commit_id, "create", "auto", target, _jpeg()) for target in targets],
        *[(reverted.commit_id, "remove", "revert", target, None) for target in targets],
        *[(again.commit_id, "create", "revert", target, _jpeg()) for target in targets],
    ]
    assert _reverted_from(engine_settings) == [None, None, 1, 2, 3, 4]


def test_a_dry_run_revert_of_a_cover_commit_changes_nothing(
    engine_settings: Settings,
    music_dir: Path,
    trashed: list[Path],
) -> None:
    folder = _stage_one(engine_settings, music_dir, "One")
    written = covers.commit_covers(engine_settings)

    preview = _revert(engine_settings, written.commit_id, dry_run=True)

    assert (preview.commit_id, preview.dry_run) == (None, True)
    assert [row.status for row in preview.sidecars] == ["reverted"]
    assert trashed == []
    assert (folder / "cover.jpg").read_bytes() == _jpeg()
    assert [row[1] for row in _cover_writes(engine_settings)] == ["create"]
    assert [commit.id for commit in commits.list_commits(engine_settings)] == [written.commit_id]


def test_a_cover_whose_bytes_changed_is_reported_changed_and_stays(
    engine_settings: Settings,
    music_dir: Path,
    trashed: list[Path],
) -> None:
    folder = _stage_one(engine_settings, music_dir, "One")
    written = covers.commit_covers(engine_settings)
    (folder / "cover.jpg").write_bytes(b"edited by hand")

    result = _revert(engine_settings, written.commit_id)

    [cover] = result.sidecars
    assert (cover.status, result.commit_id) == (covers.REVERT_CHANGED, None)
    assert trashed == []
    assert (folder / "cover.jpg").read_bytes() == b"edited by hand"
    assert [row[1] for row in _cover_writes(engine_settings)] == ["create"]


def test_a_cover_a_later_path_commit_moved_is_skipped(
    engine_settings: Settings,
    music_dir: Path,
    trashed: list[Path],
) -> None:
    album = music_dir / "Artist" / "Album"
    tags = {"albumartist": ["Artist"], "artist": ["Artist"], "album": ["Album"]}
    tags |= {"date": ["2001"], "title": ["One"], "tracknumber": ["1"]}
    make_track(album / "01 One.flac", tags)
    (album / "Album front.jpg").write_bytes(_jpeg())
    scan_library(engine_settings)
    covers.stage_covers(engine_settings, client=_FakeCoverArt())
    written = covers.commit_covers(engine_settings)
    paths.stage_paths(engine_settings, path="Artist")
    moved = paths.commit_paths(engine_settings)
    assert moved.commit_id is not None
    connection = db.connect(engine_settings.db_path)
    try:
        logged = store.sidecar_moves_for_commit(connection, moved.commit_id)
    finally:
        connection.close()
    [carried] = [row for row in logged if row.from_path == _target(music_dir, album)]

    result = _revert(engine_settings, written.commit_id)

    [cover] = result.sidecars
    assert (cover.from_path, cover.status) == (carried.from_path, "skipped_later_changes")
    assert result.commit_id is None
    assert trashed == []
    assert (music_dir / carried.to_path).read_bytes() == _jpeg()


def test_a_missing_cover_is_reported_and_the_revert_commit_records_the_other(
    engine_settings: Settings,
    music_dir: Path,
    trashed: list[Path],
) -> None:
    one = _stage_one(engine_settings, music_dir, "One")
    two = _stage_one(engine_settings, music_dir, "Two")
    written = covers.commit_covers(engine_settings)
    (one / "cover.jpg").unlink()

    result = _revert(engine_settings, written.commit_id)

    assert [(row.from_path, row.status) for row in result.sidecars] == [
        (_target(music_dir, one), "missing"),
        (_target(music_dir, two), "reverted"),
    ]
    assert result.commit_id is not None
    assert trashed == [two / "cover.jpg"]
    assert [(row[0], row[1], row[3]) for row in _cover_writes(engine_settings)] == [
        (written.commit_id, "create", _target(music_dir, one)),
        (written.commit_id, "create", _target(music_dir, two)),
        (result.commit_id, "remove", _target(music_dir, two)),
    ]


def test_a_trash_error_leaves_the_cover_and_logs_no_remove_row(
    engine_settings: Settings,
    music_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = _stage_one(engine_settings, music_dir, "One")
    written = covers.commit_covers(engine_settings)

    def refuse(path: Path) -> None:
        message = f"{path} sits on a network drive, which has no Recycle Bin"
        raise trash.TrashUnavailableError(message)

    monkeypatch.setattr(trash, "send_to_trash", refuse)

    result = _revert(engine_settings, written.commit_id)

    [cover] = result.sidecars
    assert cover.status == covers.COMMIT_ERROR
    assert cover.detail is not None
    assert "no Recycle Bin" in cover.detail
    assert (folder / "cover.jpg").read_bytes() == _jpeg()
    assert [row[1] for row in _cover_writes(engine_settings)] == ["create"]


def test_a_file_where_a_removed_cover_goes_back_is_an_error(
    engine_settings: Settings,
    music_dir: Path,
    trashed: list[Path],
) -> None:
    folder = _stage_one(engine_settings, music_dir, "One")
    written = covers.commit_covers(engine_settings)
    reverted = _revert(engine_settings, written.commit_id)
    (folder / "cover.jpg").write_bytes(b"someone else's")

    again = _revert(engine_settings, reverted.commit_id)

    [cover] = again.sidecars
    assert (cover.status, again.commit_id) == (covers.COMMIT_ERROR, None)
    assert (folder / "cover.jpg").read_bytes() == b"someone else's"
    assert [row[1] for row in _cover_writes(engine_settings)] == ["create", "remove"]


def test_a_cover_commit_revert_is_refused_while_a_cover_is_staged_or_scoped_by_path(
    engine_settings: Settings,
    music_dir: Path,
    trashed: list[Path],
) -> None:
    folder = _stage_one(engine_settings, music_dir, "One")
    written = covers.commit_covers(engine_settings)
    assert written.commit_id is not None

    with pytest.raises(ValueError, match="wrote covers, and path= selects"):
        versioning.revert_commit(engine_settings, written.commit_id, path="Band/One")
    _stage_one(engine_settings, music_dir, "Two")
    with pytest.raises(ValueError, match="staging area is not empty"):
        versioning.revert_commit(engine_settings, written.commit_id)

    assert trashed == []
    assert (folder / "cover.jpg").read_bytes() == _jpeg()
