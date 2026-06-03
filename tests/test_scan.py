"""Tests for library discovery."""

from __future__ import annotations

from pathlib import Path

from tagmend.engine.scan import count_audio_files, is_audio_file, iter_audio_files


def test_counts_only_audio_files(temp_library: Path) -> None:
    assert count_audio_files(temp_library) == 1


def test_iter_returns_the_audio_file(temp_library: Path) -> None:
    files = list(iter_audio_files(temp_library))
    assert len(files) == 1
    assert files[0].name == "01 Track.mp3"


def test_is_audio_file_ignores_sidecars(tmp_path: Path) -> None:
    audio = tmp_path / "song.FLAC"  # case-insensitive extension match
    audio.write_bytes(b"\x00")
    art = tmp_path / "cover.jpg"
    art.write_bytes(b"\x00")
    assert is_audio_file(audio)
    assert not is_audio_file(art)


def test_empty_library_counts_zero(tmp_path: Path) -> None:
    assert count_audio_files(tmp_path) == 0
