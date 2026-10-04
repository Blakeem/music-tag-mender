"""Tests for the embedded-picture writer :func:`tagmend.engine.tags.write_pictures`."""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import TYPE_CHECKING, Any

import mutagen
import pytest
from mutagen.id3 import APIC, ID3, RVAD, TIT2, TXXX, TYER, MakeID3v1  # type: ignore[attr-defined]
from mutagen.ogg import OggPage

from conftest import (
    PictureImage,
    embed_pictures,
    make_chunk_id3_track,
    make_track,
    vorbis_picture_value,
)
from tagmend.engine import tags
from tagmend.engine.tags import (
    MANAGED_TAGS,
    PictureData,
    TagWriteError,
    TagWriteResult,
    read_picture_data,
    read_tags,
    write_pictures,
)

if TYPE_CHECKING:
    from pathlib import Path

_FRONT: PictureImage = (3, "image/jpeg", "front", b"\xff\xd8\xff\xe0 the front")
_BACK: PictureImage = (4, "image/png", "back", b"\x89PNG\r\n\x1a\n the back")
_EMPTY: PictureImage = (0, "image/png", "empty", b"")
_SUFFIXES = [".mp3", ".flac", ".m4a", ".ogg"]
# A Vorbis stream opens with three header packets, its comment packet among them.
_VORBIS_HEADER_PACKETS = 3


def _audio_digest(track: Path) -> str:
    """Hash the audio of *track*: the verifier's payload range, or the Ogg audio packets."""
    if track.suffix == ".ogg":
        pages: list[Any] = []
        with track.open("rb") as handle:
            while True:
                try:
                    pages.append(OggPage(handle))  # type: ignore[no-untyped-call]
                except EOFError:
                    break
        packets = OggPage.to_packets(pages)  # type: ignore[no-untyped-call]
        return hashlib.sha256(b"".join(packets[_VORBIS_HEADER_PACKETS:])).hexdigest()
    audio = mutagen.File(track, easy=True)  # type: ignore[attr-defined]
    digest = tags._snapshot(track, tags._container_of(audio), type(audio)).audio_digest
    assert digest is not None
    return digest


def _add_unrelated_entry(track: Path) -> None:
    """Add an unmanaged entry that is no picture, holding ``bar``."""
    audio: Any = mutagen.File(track)  # type: ignore[attr-defined]
    if track.suffix == ".mp3":
        audio.tags.add(TXXX(encoding=3, desc="Foo", text=["bar"]))  # type: ignore[no-untyped-call]
    else:
        audio["cprt" if track.suffix == ".m4a" else "foo"] = ["bar"]
    audio.save()


def _unrelated_entry(track: Path) -> list[str]:
    audio: Any = mutagen.File(track)  # type: ignore[attr-defined]
    if track.suffix == ".mp3":
        return [str(text) for text in audio.tags["TXXX:Foo"].text]
    return [str(text) for text in audio["cprt" if track.suffix == ".m4a" else "foo"]]


def _managed(track: Path) -> dict[str, list[str]]:
    return {key: values for key, values in read_tags(track).tags.items() if key in MANAGED_TAGS}


def _track(tmp_path: Path, suffix: str, images: list[PictureImage]) -> Path:
    track = make_track(tmp_path / f"t{suffix}", {"title": ["Song"], "artist": ["Band"]})
    embed_pictures(track, images)
    _add_unrelated_entry(track)
    return track


def _picture_of(track: Path, image: PictureImage) -> PictureData:
    """Return the picture of *track* holding the bytes of *image*. MP4 keeps no description."""
    return next(picture for picture in read_picture_data(track) if picture.data == image[3])


def _same(track: Path, got: list[PictureData], wanted: list[PictureData]) -> bool:
    """Compare as the writer does: an ID3 save orders its frames itself."""
    if track.suffix == ".mp3":
        return Counter(got) == Counter(wanted)
    return got == wanted


def _assert_only_pictures_changed(
    track: Path,
    audio_before: str,
    managed_before: dict[str, list[str]],
    wanted: list[PictureData],
) -> None:
    assert _audio_digest(track) == audio_before
    assert _managed(track) == managed_before
    assert _unrelated_entry(track) == ["bar"]
    assert _same(track, read_picture_data(track), wanted)
    assert not list(track.parent.glob("*.tagmend.tmp"))


@pytest.mark.parametrize("suffix", _SUFFIXES)
def test_removing_one_of_two_pictures_changes_nothing_else(tmp_path: Path, suffix: str) -> None:
    track = _track(tmp_path, suffix, [_FRONT, _BACK])
    back = _picture_of(track, _BACK)
    audio_before, managed_before = _audio_digest(track), _managed(track)

    result = write_pictures(track, [back])

    assert result.written is True
    assert result.dropped_frames == ()
    _assert_only_pictures_changed(track, audio_before, managed_before, [back])


@pytest.mark.parametrize("suffix", _SUFFIXES)
def test_removing_the_only_picture_changes_nothing_else(tmp_path: Path, suffix: str) -> None:
    track = _track(tmp_path, suffix, [_FRONT])
    audio_before, managed_before = _audio_digest(track), _managed(track)

    assert write_pictures(track, []).written is True

    _assert_only_pictures_changed(track, audio_before, managed_before, [])


@pytest.mark.parametrize("suffix", _SUFFIXES)
def test_a_removed_picture_is_restored_at_its_old_position(tmp_path: Path, suffix: str) -> None:
    track = _track(tmp_path, suffix, [_FRONT, _BACK])
    original = read_picture_data(track)
    removed = [picture for picture in original if picture.data != _FRONT[3]]
    audio_before, managed_before = _audio_digest(track), _managed(track)
    write_pictures(track, removed)

    result = write_pictures(track, original)

    assert result.written is True
    _assert_only_pictures_changed(track, audio_before, managed_before, original)


@pytest.mark.parametrize("suffix", _SUFFIXES)
def test_writing_the_current_list_touches_nothing(tmp_path: Path, suffix: str) -> None:
    track = _track(tmp_path, suffix, [_FRONT, _BACK])
    before = track.read_bytes()

    result = write_pictures(track, read_picture_data(track))

    assert result == TagWriteResult(written=False, audio_proven=False)
    assert track.read_bytes() == before


def test_an_id3_list_in_another_order_is_the_current_list(tmp_path: Path) -> None:
    track = _track(tmp_path, ".mp3", [_FRONT, _BACK])
    before = track.read_bytes()

    result = write_pictures(track, list(reversed(read_picture_data(track))))

    assert result.written is False
    assert track.read_bytes() == before


@pytest.mark.parametrize("suffix", _SUFFIXES)
def test_a_zero_byte_picture_is_removable(tmp_path: Path, suffix: str) -> None:
    track = _track(tmp_path, suffix, [_FRONT, _EMPTY])
    front = _picture_of(track, _FRONT)
    assert _picture_of(track, _EMPTY).data == b""
    audio_before, managed_before = _audio_digest(track), _managed(track)

    assert write_pictures(track, [front]).written is True

    _assert_only_pictures_changed(track, audio_before, managed_before, [front])


@pytest.mark.parametrize("suffix", _SUFFIXES)
def test_picture_data_round_trips_through_its_json_form(tmp_path: Path, suffix: str) -> None:
    track = _track(tmp_path, suffix, [_FRONT, _BACK])

    pictures = read_picture_data(track)

    assert len(pictures) == 2
    for picture in pictures:
        assert PictureData.from_json(picture.to_json(), picture.data) == picture


@pytest.mark.parametrize("attributes", ['{"mime": "image/png"}', "[]", "not json"])
def test_picture_data_refuses_json_naming_other_attributes(attributes: str) -> None:
    with pytest.raises(ValueError, match=r"picture attributes|Expecting value"):
        PictureData.from_json(attributes, b"")


def test_read_picture_data_lists_the_pictures_read_pictures_reports(tmp_path: Path) -> None:
    track = _track(tmp_path, ".flac", [_FRONT, _BACK])

    data = read_picture_data(track)
    reported = tags.read_pictures(track)

    assert [(p.picture_type, p.mime, p.description, p.data) for p in data] == [
        (p.picture_type, p.mime, p.description, p.data) for p in reported
    ]


def _apic_only_over_id3v1_title(dest: Path) -> Path:
    """Write an MP3 whose ID3v2 tag holds one ``APIC`` frame and whose ID3v1 holds a title."""
    track = make_track(dest)
    frames = ID3()  # type: ignore[no-untyped-call]
    front = APIC(encoding=3, mime="image/jpeg", type=3, desc="front", data=_FRONT[3])  # type: ignore[no-untyped-call]
    frames.add(front)  # type: ignore[no-untyped-call]
    frames.save(track, v1=0)
    title = TIT2(encoding=0, text=["Short Title"])  # type: ignore[no-untyped-call]
    with track.open("ab") as handle:
        handle.write(MakeID3v1({"TIT2": title}))  # type: ignore[no-untyped-call]
    return track


def test_removing_the_last_id3v2_frame_over_id3v1_values_is_refused(tmp_path: Path) -> None:
    track = _apic_only_over_id3v1_title(tmp_path / "v1.mp3")
    before = track.read_bytes()
    assert read_tags(track).tags == {}

    with pytest.raises(TagWriteError, match="emptying ID3v2 exposes the values only ID3v1 holds"):
        write_pictures(track, [])

    assert track.read_bytes() == before
    assert not list(tmp_path.glob("*.tagmend.tmp"))


def test_adding_the_first_id3v2_frame_over_id3v1_values_is_refused(tmp_path: Path) -> None:
    track = make_track(tmp_path / "v1.mp3")
    ID3(track).delete()  # type: ignore[no-untyped-call]
    title = TIT2(encoding=0, text=["Short Title"])  # type: ignore[no-untyped-call]
    with track.open("ab") as handle:
        handle.write(MakeID3v1({"TIT2": title}))  # type: ignore[no-untyped-call]
    front = PictureData(
        picture_type=3, mime="image/jpeg", description="front", data=_FRONT[3], encoding=3
    )
    assert read_tags(track).tags == {"title": ["Short Title"]}
    before = track.read_bytes()

    with pytest.raises(TagWriteError, match="title reads back"):
        write_pictures(track, [front])

    assert track.read_bytes() == before


def test_a_v23_full_date_tyer_survives_a_picture_write(tmp_path: Path) -> None:
    track = _track(tmp_path, ".mp3", [_FRONT, _BACK])
    frames = ID3(track)  # type: ignore[no-untyped-call]
    frames.add(TYER(encoding=0, text=["2013-10-04T07:00:00Z"]))  # type: ignore[no-untyped-call]
    frames.save(track, v2_version=3)
    audio_before = _audio_digest(track)
    managed_before = _managed(track)
    wanted = [_picture_of(track, _FRONT)]
    assert managed_before["date"] == ["2013-10-04"]
    assert not ID3(track, translate=False).getall("TDRC")  # type: ignore[no-untyped-call]
    tags.ensure_pictures_writable(track)

    result = write_pictures(track, wanted)

    assert result.written is True
    _assert_only_pictures_changed(track, audio_before, managed_before, wanted)


def test_a_v23_rvad_frame_refuses_a_picture_write_unless_droppable(
    tmp_path: Path,
    tagmend_warnings: pytest.LogCaptureFixture,
) -> None:
    track = make_track(tmp_path / "loud.mp3", {"title": ["Loud"]})
    embed_pictures(track, [_FRONT])
    frames = ID3(track)  # type: ignore[no-untyped-call]
    frames.add(RVAD(adjustments=[1, 1], peaks=[1, 1]))  # type: ignore[no-untyped-call]
    frames.save(track, v2_version=3)
    before = track.read_bytes()
    rvad = frozenset({"RVAD"})

    with pytest.raises(TagWriteError) as written:
        write_pictures(track, [])
    with pytest.raises(TagWriteError) as staged:
        tags.ensure_pictures_writable(track)

    assert written.value.violations == ["dropped RVAD"]
    assert staged.value.violations == ["dropped RVAD"]
    assert track.read_bytes() == before
    tags.ensure_pictures_writable(track, droppable_frames=rvad)

    result = write_pictures(track, [], droppable_frames=rvad)

    assert result.written is True
    assert result.audio_proven is True
    assert result.dropped_frames == ("RVAD",)
    assert read_picture_data(track) == []
    assert read_tags(track).tags["title"] == ["Loud"]
    assert "RVAD" in tagmend_warnings.text


def test_an_undecodable_ogg_picture_refuses_the_write_and_the_stage_check(
    tmp_path: Path,
) -> None:
    track = make_track(tmp_path / "t.ogg", {"title": ["Song"]})
    audio: Any = mutagen.File(track)  # type: ignore[attr-defined]
    audio["metadata_block_picture"] = ["abc", vorbis_picture_value(_FRONT)]
    audio.save()
    before = track.read_bytes()

    with pytest.raises(TagWriteError, match="picture 0 does not decode"):
        write_pictures(track, [])
    with pytest.raises(TagWriteError, match="picture 0 does not decode"):
        tags.ensure_pictures_writable(track)

    assert track.read_bytes() == before


@pytest.mark.parametrize("suffix", _SUFFIXES)
def test_the_stage_check_accepts_a_plain_file(tmp_path: Path, suffix: str) -> None:
    tags.ensure_pictures_writable(_track(tmp_path, suffix, [_FRONT]))


def test_a_container_the_verifier_cannot_check_is_refused(tmp_path: Path) -> None:
    track = make_chunk_id3_track(tmp_path / "t.wav")
    before = track.read_bytes()

    with pytest.raises(TagWriteError, match="no layout for the WAVE container"):
        write_pictures(track, [])
    with pytest.raises(TagWriteError, match="no layout for the WAVE container"):
        tags.ensure_pictures_writable(track)

    assert track.read_bytes() == before


def test_a_picture_lacking_its_container_attributes_is_refused(tmp_path: Path) -> None:
    track = _track(tmp_path, ".mp3", [_FRONT])
    mp4_cover = PictureData(picture_type=None, mime="image/jpeg", description="", data=b"x")
    before = track.read_bytes()

    with pytest.raises(ValueError, match=r"pictures \[0\] lack an attribute"):
        write_pictures(track, [mp4_cover])

    assert track.read_bytes() == before


def test_a_picture_write_that_drops_another_entry_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = _track(tmp_path, ".mp3", [_FRONT, _BACK])
    back = _picture_of(track, _BACK)
    before = track.read_bytes()
    real_apply = tags._apply_pictures

    def lossy(
        path: Path,
        container: tags._Container,
        kind: type[Any],
        pictures: list[PictureData],
    ) -> None:
        real_apply(path, container, kind, pictures)
        frames = ID3(path)  # type: ignore[no-untyped-call]
        frames.delall("TXXX:Foo")  # type: ignore[no-untyped-call]
        frames.save()

    monkeypatch.setattr(tags, "_apply_pictures", lossy)

    with pytest.raises(TagWriteError, match="dropped TXXX:Foo"):
        write_pictures(track, [back])

    assert track.read_bytes() == before
    assert not list(tmp_path.glob("*.tagmend.tmp"))


@pytest.mark.parametrize("suffix", _SUFFIXES)
def test_a_picture_write_that_misses_its_target_is_refused(
    tmp_path: Path,
    suffix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = _track(tmp_path, suffix, [_FRONT, _BACK])
    back = _picture_of(track, _BACK)
    before = track.read_bytes()
    monkeypatch.setattr(tags, "_apply_pictures", lambda *_: None)

    with pytest.raises(TagWriteError, match="pictures read back as"):
        write_pictures(track, [back])

    assert track.read_bytes() == before
