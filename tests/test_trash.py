"""Tests for ``send_to_trash``, with the drive probe and ``send2trash.send2trash`` replaced."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import send2trash

from tagmend.engine import trash

_WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="the drive probe and UNC paths are Windows only"
)


def _fake_send2trash(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Replace ``send2trash.send2trash`` and return each path handed to it."""
    sent: list[Path] = []
    monkeypatch.setattr(send2trash, "send2trash", sent.append)
    return sent


def _fake_probe(monkeypatch: pytest.MonkeyPatch, drive_type: int) -> list[str]:
    """Make every drive report *drive_type* and return each drive root probed."""
    probed: list[str] = []

    def probe(root: str) -> int:
        probed.append(root)
        return drive_type

    monkeypatch.setattr(trash, "_drive_type", probe)
    return probed


@_WINDOWS_ONLY
@pytest.mark.parametrize(("drive_type", "name"), [(2, "removable"), (4, "network")])
def test_a_drive_with_no_recycle_bin_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drive_type: int,
    name: str,
) -> None:
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"jpeg")
    sent = _fake_send2trash(monkeypatch)
    probed = _fake_probe(monkeypatch, drive_type)

    with pytest.raises(trash.TrashUnavailableError, match=f"sits on drive type {name}"):
        trash.send_to_trash(cover)

    assert (sent, probed) == ([], [cover.drive + "\\"])
    assert cover.read_bytes() == b"jpeg"


@_WINDOWS_ONLY
def test_a_unc_path_is_refused_without_a_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _fake_send2trash(monkeypatch)
    probed = _fake_probe(monkeypatch, trash.DRIVE_FIXED)

    with pytest.raises(trash.TrashUnavailableError, match="UNC"):
        trash.send_to_trash(Path(r"\\server\share\Album\cover.jpg"))

    assert (sent, probed) == ([], [])


def test_a_file_on_a_fixed_drive_goes_to_send2trash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cover = tmp_path / "cover.jpg"
    sent = _fake_send2trash(monkeypatch)
    _fake_probe(monkeypatch, trash.DRIVE_FIXED)

    trash.send_to_trash(cover)

    assert sent == [cover]


def test_a_send2trash_error_is_raised_as_trash_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cover = tmp_path / "cover.jpg"
    _fake_probe(monkeypatch, trash.DRIVE_FIXED)

    def fail(path: Path) -> None:
        raise PermissionError(13, "Access is denied", str(path))

    monkeypatch.setattr(send2trash, "send2trash", fail)

    with pytest.raises(trash.TrashUnavailableError, match="Access is denied") as raised:
        trash.send_to_trash(cover)

    assert isinstance(raised.value.__cause__, PermissionError)
