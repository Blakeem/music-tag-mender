"""A verified tag write carries the file's ``fingerprint_cache`` row to its new signature.

Only a write whose payload hash decides the decoded audio (ID3, FLAC) proves the stored
fingerprint still describes the file. MP4 and Ogg rows stay keyed to the old signature.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from conftest import make_track
from tagmend.engine import staging, store, versioning
from tagmend.engine.acoustid import Fingerprint, get_fingerprint, put_fingerprint
from tagmend.engine.db import connect
from tagmend.engine.library import scan_library
from tagmend.engine.schema import apply_schema

if TYPE_CHECKING:
    from pathlib import Path

    from tagmend.config import Settings

_FP = Fingerprint(fingerprint="AQADtEmUaEkS", duration=243)
_NOW = datetime(2026, 9, 1, tzinfo=UTC)
# A fixed old mtime, so any write moves the signature whatever the clock resolution.
_OLD_MTIME_NS = 1_000_000_000_000_000_000


def _scanned_track(settings: Settings, music_dir: Path, suffix: str) -> int:
    track = make_track(music_dir / f"song{suffix}", {"genre": ["Rock"]})
    os.utime(track, ns=(_OLD_MTIME_NS, _OLD_MTIME_NS))
    scan_library(settings)
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file(conn, str(music_dir), track.name)
        assert row is not None
        return row.id
    finally:
        conn.close()


def _signature(settings: Settings, file_id: int) -> tuple[int, int]:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file_by_id(conn, file_id)
        assert row is not None
        assert row.size_bytes is not None
        assert row.mtime_ns is not None
        return row.size_bytes, row.mtime_ns
    finally:
        conn.close()


def _put_row(settings: Settings, file_id: int, signature: tuple[int, int]) -> None:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        put_fingerprint(conn, file_id, *signature, fpcalc_exit=0, fingerprint=_FP, now=_NOW)
        conn.commit()
    finally:
        conn.close()


def _has_row(settings: Settings, file_id: int, signature: tuple[int, int]) -> bool:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return get_fingerprint(conn, file_id, *signature) is not None
    finally:
        conn.close()


def _commit_genre(settings: Settings, file_id: int, genre: str) -> None:
    assert staging.stage_tags(settings, file_id=file_id, tags={"genre": [genre]})
    assert staging.commit_tags(settings).committed == 1


@pytest.mark.parametrize(
    ("suffix", "rekeyed"),
    [(".mp3", True), (".flac", True), (".m4a", False), (".ogg", False)],
)
def test_a_tag_commit_rekeys_the_row_only_when_the_audio_is_proven(
    engine_settings: Settings, music_dir: Path, suffix: str, *, rekeyed: bool
) -> None:
    file_id = _scanned_track(engine_settings, music_dir, suffix)
    before = _signature(engine_settings, file_id)
    _put_row(engine_settings, file_id, before)

    _commit_genre(engine_settings, file_id, "Jazz")

    after = _signature(engine_settings, file_id)
    assert after != before
    assert _has_row(engine_settings, file_id, after) is rekeyed
    assert _has_row(engine_settings, file_id, before) is not rekeyed


def test_a_revert_rekeys_the_row_of_an_mp3(engine_settings: Settings, music_dir: Path) -> None:
    file_id = _scanned_track(engine_settings, music_dir, ".mp3")
    _commit_genre(engine_settings, file_id, "Jazz")
    committed = _signature(engine_settings, file_id)
    _put_row(engine_settings, file_id, committed)

    assert versioning.revert_tags(engine_settings, file_id, 0).status == "reverted"

    reverted = _signature(engine_settings, file_id)
    assert reverted != committed
    assert _has_row(engine_settings, file_id, reverted)


def test_a_row_already_stale_before_the_write_stays_untouched(
    engine_settings: Settings, music_dir: Path
) -> None:
    file_id = _scanned_track(engine_settings, music_dir, ".mp3")
    size, mtime_ns = _signature(engine_settings, file_id)
    stale = (size, mtime_ns - 1)
    _put_row(engine_settings, file_id, stale)

    _commit_genre(engine_settings, file_id, "Jazz")

    assert _has_row(engine_settings, file_id, stale)
    assert not _has_row(engine_settings, file_id, _signature(engine_settings, file_id))
