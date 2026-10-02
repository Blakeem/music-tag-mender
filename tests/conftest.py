"""Shared pytest fixtures.

Tests never touch the real ``music/`` library or the real OS config/data dirs.
``temp_library`` builds a throwaway tree with one dummy audio file (and a non-audio
sidecar that must be ignored); the autouse ``_isolate_config`` fixture redirects the
config/data dirs into ``tmp_path`` and clears env overrides for every test.

``make_track`` (exposed via the same-named fixture) copies one of the real, silent,
public-domain audio templates under ``fixtures/templates`` and optionally writes tags
through mutagen's "easy" mode, so tag-reading tests run against genuine audio streams
rather than the dummy byte file ``temp_library`` produces.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import mutagen
import pytest
from mutagen.id3 import ID3, RVAD, TIT2  # type: ignore[attr-defined]

from tagmend import config
from tagmend.config import Settings
from tagmend.engine.schema import apply_schema

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

TEMPLATE_DIR = Path(__file__).parent / "fixtures" / "templates"

# Suffix -> template filename. Only these four formats have a prepared template.
_TEMPLATES: dict[str, str] = {
    ".mp3": "silence.mp3",
    ".flac": "silence.flac",
    ".m4a": "silence.m4a",
    ".ogg": "silence.ogg",
}


def make_track(
    dest: Path,
    tags: Mapping[str, Sequence[str]] | None = None,
) -> Path:
    """Copy a silent template to *dest* and optionally write *tags* via mutagen.

    The template is chosen by ``dest.suffix`` (case-insensitive). Parent directories
    are created. When *tags* is given, the file is opened in mutagen "easy" mode and
    each key is assigned ``list(values)`` before saving. Returns *dest*.

    Raises ``ValueError`` for a suffix with no prepared template.
    """
    suffix = dest.suffix.lower()
    template_name = _TEMPLATES.get(suffix)
    if template_name is None:
        message = f"no audio template for suffix {suffix!r}"
        raise ValueError(message)

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(TEMPLATE_DIR / template_name, dest)

    if tags:
        audio = mutagen.File(dest, easy=True)  # type: ignore[attr-defined]
        for key, values in tags.items():
            audio[key] = list(values)
        audio.save()

    return dest


def make_droppable_frames_mp3(dest: Path) -> Path:
    """Write an ID3v2.3 MP3 titled ``Loud`` holding an ``RVAD`` frame and an unknown ``NCON``.

    A v2.4 save drops both frames, so the writer refuses the file unless the caller names them.
    """
    track = make_track(dest)
    frames = ID3()  # type: ignore[no-untyped-call]
    frames.add(TIT2(encoding=3, text=["Loud"]))  # type: ignore[no-untyped-call]
    frames.add(RVAD(adjustments=[1, 1], peaks=[1, 1]))  # type: ignore[no-untyped-call]
    frames.save(track, v2_version=3)
    # mutagen never writes a frame it cannot parse, so NCON is spliced in after the tag header.
    data = track.read_bytes()
    ncon = b"NCON" + (4).to_bytes(4, "big") + b"\x00\x00" + b"\x00\x01\x02\x03"
    shifts = (21, 14, 7, 0)
    size = sum(byte << shift for byte, shift in zip(data[6:10], shifts, strict=True)) + len(ncon)
    header = data[:6] + bytes((size >> shift) & 0x7F for shift in shifts)
    track.write_bytes(header + ncon + data[10:])
    return track


# The ways a user can type one folder. Upper case names the same folder only where the
# filesystem ignores case, so that spelling runs on Windows alone.
FOLDER_SPELLINGS = (
    "exact",
    pytest.param(
        "upper",
        marks=pytest.mark.skipif(sys.platform != "win32", reason="NTFS is case-insensitive"),
    ),
    "slash",
)


def spell_folder(folder: Path, spelling: str) -> str:
    """Return *folder* as typed in one of the :data:`FOLDER_SPELLINGS`."""
    text = str(folder)
    if spelling == "upper":
        return text.upper()
    if spelling == "slash":
        return text.replace(os.sep, "/")
    return text


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that reaches a real host off this machine instead of an injected fake.

    Every network-facing engine builds its own real client when none is injected, so a
    fixture that merely grows a ``musicbrainz_artistid`` can silently start calling the live
    API from the unit suite. Loopback stays open because the config-UI tests drive a real
    local server over it; ``httpx.MockTransport`` is a different class and is untouched.
    """
    real_handle = httpx.HTTPTransport.handle_request

    def guarded(
        self: httpx.HTTPTransport,
        request: httpx.Request,
    ) -> httpx.Response:
        if request.url.host not in _LOOPBACK_HOSTS:
            message = (
                f"a test reached the real network ({request.url.host}); "
                f"inject a fake client or an httpx.MockTransport"
            )
            raise RuntimeError(message)
        return real_handle(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guarded)


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect config/data dirs into a temp location and clear env overrides."""
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path / "data")
    for var in (
        "TAGMEND_MUSIC_PATH",
        "TAGMEND_LASTFM_API_KEY",
        "TAGMEND_ACOUSTID_API_KEY",
        "TAGMEND_FPCALC_PATH",
        "TAGMEND_DB_PATH",
        "TAGMEND_NO_BROWSER",
        "TAGMEND_NO_CONFIG_UI",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def temp_library(tmp_path: Path) -> Path:
    """A throwaway music library: one audio file plus a non-audio sidecar."""
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "01 Track.mp3").write_bytes(b"\x00")
    (album / "cover.jpg").write_bytes(b"\x00")  # ignored: not an audio extension
    return tmp_path


@pytest.fixture
def tagmend_warnings(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Capture ``tagmend`` warnings, which the root ``caplog`` handler misses (propagate=False)."""
    logger = logging.getLogger("tagmend")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="tagmend"):
            yield caplog
    finally:
        logger.removeHandler(caplog.handler)


@pytest.fixture
def make_track_factory() -> Callable[..., Path]:
    """Expose :func:`make_track` as a fixture (cleanest for mypy/ruff in tests)."""
    return make_track


@pytest.fixture
def music_dir(tmp_path: Path) -> Path:
    """Create and return an empty ``music`` directory under ``tmp_path``."""
    path = tmp_path / "music"
    path.mkdir()
    return path


@pytest.fixture
def engine_settings(tmp_path: Path, music_dir: Path) -> Settings:
    """Settings pointing at the temp library and an isolated temp ledger."""
    return Settings(
        music_path=music_dir,
        lastfm_api_key=None,
        db_path=tmp_path / "ledger.sqlite3",
    )


@pytest.fixture
def db_conn() -> Iterator[sqlite3.Connection]:
    """An in-memory SQLite connection with foreign keys on and the M1 schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    apply_schema(conn)
    try:
        yield conn
    finally:
        conn.close()
