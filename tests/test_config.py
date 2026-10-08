"""Tests for settings loading and persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tagmend import __version__, config

# Config/data dirs are isolated by the autouse `_isolate_config` fixture in conftest.


def test_defaults_when_no_file() -> None:
    settings = config.load_settings()
    assert settings.music_path is None
    assert settings.lastfm_api_key is None
    assert settings.db_path == config.default_db_path()
    # M2 genre/Last.fm settings fall back to their built-in defaults.
    assert settings.genre_min_weight == 2
    assert settings.genre_max_count == 4
    assert settings.genre_use_album_tags is True
    assert settings.lastfm_rate_per_sec == 1.0
    assert settings.genre_stage_limit == 300


def test_genre_settings_parse_valid_values() -> None:
    config.set_setting("genre_min_weight", "5")
    config.set_setting("genre_max_count", "3")
    config.set_setting("genre_use_album_tags", "false")
    config.set_setting("lastfm_rate_per_sec", "2.5")
    config.set_setting("genre_stage_limit", "50")

    settings = config.load_settings()
    assert settings.genre_min_weight == 5
    assert settings.genre_max_count == 3
    assert settings.genre_use_album_tags is False
    assert settings.lastfm_rate_per_sec == 2.5
    assert settings.genre_stage_limit == 50


def test_malformed_int_falls_back_to_default() -> None:
    config.set_setting("genre_min_weight", "not-a-number")
    config.set_setting("genre_stage_limit", "abc")
    config.set_setting("lastfm_rate_per_sec", "fast")

    settings = config.load_settings()
    assert settings.genre_min_weight == 2
    assert settings.genre_stage_limit == 300
    assert settings.lastfm_rate_per_sec == 1.0


def test_negative_stage_limit_falls_back_to_default() -> None:
    # A stage limit caps a Python slice, where -1 would mean "all but the last one".
    defaults = config.load_settings()
    config.set_setting("genre_stage_limit", "-1")
    config.set_setting("year_stage_limit", "-5")

    settings = config.load_settings()
    assert settings.genre_stage_limit == defaults.genre_stage_limit
    assert settings.year_stage_limit == defaults.year_stage_limit


def test_year_stage_limit_defaults_to_300() -> None:
    assert config.load_settings().year_stage_limit == 300


def _write_raw_settings(raw: dict[str, str]) -> None:
    path = config.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_legacy_album_stage_limit_is_read_as_year_stage_limit() -> None:
    _write_raw_settings({"album_stage_limit": "40"})

    assert config.load_settings().year_stage_limit == 40


def test_save_rewrites_legacy_album_stage_limit() -> None:
    _write_raw_settings({"album_stage_limit": "40"})

    config.set_setting("genre_stage_limit", "5")

    saved = json.loads(config.settings_path().read_text(encoding="utf-8"))
    assert saved["year_stage_limit"] == "40"
    assert "album_stage_limit" not in saved


def test_new_key_wins_over_legacy_key() -> None:
    _write_raw_settings({"album_stage_limit": "40", "year_stage_limit": "70"})

    assert config.load_settings().year_stage_limit == 70


@pytest.mark.parametrize("token", ["", "0", "none", "NULL", "None"])
def test_genre_max_count_none_sentinel(token: str) -> None:
    config.set_setting("genre_max_count", token)
    assert config.load_settings().genre_max_count is None


@pytest.mark.parametrize("token", ["00", "+0", "-0"])
def test_a_genre_max_count_spelled_as_another_zero_means_no_cap(token: str) -> None:
    # A cap of 0 is a slice bound that would drop every genre.
    config.set_setting("genre_max_count", token)
    assert config.load_settings().genre_max_count is None


def test_genre_max_count_malformed_falls_back_to_default() -> None:
    config.set_setting("genre_max_count", "lots")
    assert config.load_settings().genre_max_count == 4


def test_a_negative_genre_max_count_falls_back_to_the_default_cap() -> None:
    # The cap is a slice bound, where -1 would drop the last matched genre.
    config.set_setting("genre_max_count", "-1")
    assert config.load_settings().genre_max_count == 4


@pytest.mark.parametrize("key", ["lastfm_rate_per_sec", "musicbrainz_rate_per_sec"])
@pytest.mark.parametrize("raw", ["0", "-1", "nan"])
def test_a_rate_that_would_disable_pacing_falls_back_to_the_default(key: str, raw: str) -> None:
    default = getattr(config.load_settings(), key)
    config.set_setting(key, raw)
    assert getattr(config.load_settings(), key) == default


@pytest.mark.parametrize(
    ("key", "cap"),
    [("lastfm_rate_per_sec", 5.0), ("musicbrainz_rate_per_sec", 1.0)],
)
def test_a_rate_above_the_published_limit_is_clamped(key: str, cap: float) -> None:
    config.set_setting(key, str(cap * 4))
    assert getattr(config.load_settings(), key) == cap


def test_an_unreadable_settings_file_refuses_a_save_and_keeps_its_bytes() -> None:
    # A hand edit with single backslashes in a Windows path is invalid JSON. Merging over the
    # empty map the tolerant reader returns would drop every stored key.
    path = config.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"music_path": "E:\\music", "lastfm_api_key": "keep-me"}')
    before = path.read_bytes()

    with pytest.raises(ValueError, match="could not be read"):
        config.set_setting("naming_pattern", "x")

    assert path.read_bytes() == before
    assert list(config.config_dir().glob(".settings-*")) == []
    # Loading stays tolerant: it warns and runs on the defaults.
    assert config.load_settings().music_path is None


def test_a_settings_file_holding_a_non_object_refuses_a_save() -> None:
    path = config.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('["music_path"]', encoding="utf-8")

    with pytest.raises(ValueError, match="not a JSON object"):
        config.set_setting("naming_pattern", "x")

    assert path.read_text(encoding="utf-8") == '["music_path"]'


@pytest.mark.parametrize("token", ["false", "0", "no", "off", "OFF", "No"])
def test_genre_use_album_tags_false_tokens(token: str) -> None:
    config.set_setting("genre_use_album_tags", token)
    assert config.load_settings().genre_use_album_tags is False


@pytest.mark.parametrize("token", ["true", "1", "yes", "on", "anything"])
def test_genre_use_album_tags_truthy_tokens(token: str) -> None:
    config.set_setting("genre_use_album_tags", token)
    assert config.load_settings().genre_use_album_tags is True


def test_env_overrides_genre_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    config.set_setting("genre_min_weight", "5")
    monkeypatch.setenv("TAGMEND_GENRE_MIN_WEIGHT", "9")
    assert config.load_settings().genre_min_weight == 9


def test_set_and_load_roundtrip(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    config.set_setting("music_path", str(library))
    settings = config.load_settings()
    assert settings.music_path == library


def test_set_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="unknown setting"):
        config.set_setting("bogus", "x")


def test_env_overrides_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config.set_setting("music_path", str(tmp_path / "from_file"))
    monkeypatch.setenv("TAGMEND_MUSIC_PATH", str(tmp_path / "from_env"))
    settings = config.load_settings()
    assert settings.music_path == tmp_path / "from_env"


def test_musicbrainz_contact_defaults_to_project_url() -> None:
    # The default contact is the public repo URL, never a personal address.
    assert config.load_settings().musicbrainz_contact == config._MUSICBRAINZ_CONTACT_DEFAULT
    assert "@" not in config._MUSICBRAINZ_CONTACT_DEFAULT


def test_musicbrainz_user_agent_composes_live_version_and_contact() -> None:
    config.set_setting("musicbrainz_contact", "me@example.com")
    settings = config.load_settings()
    # The version is supplied by the app, not stored, so it can never drift.
    assert settings.musicbrainz_user_agent == f"TagMend/{__version__} ( me@example.com )"


def test_build_user_agent_uses_live_version() -> None:
    assert config.build_user_agent("x") == f"TagMend/{__version__} ( x )"


def test_set_settings_batch_merges() -> None:
    config.set_setting("music_path", "/keep/me")
    config.set_settings({"genre_min_weight": "7", "genre_stage_limit": "11"})
    # A later batch must preserve untouched keys (merge, not replace).
    config.set_settings({"genre_max_count": "2"})

    settings = config.load_settings()
    assert str(settings.music_path) == str(Path("/keep/me"))
    assert settings.genre_min_weight == 7
    assert settings.genre_stage_limit == 11
    assert settings.genre_max_count == 2


def test_set_settings_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="unknown setting"):
        config.set_settings({"music_path": "/ok", "bogus": "x"})


def test_set_settings_writes_atomically_without_leftover_temp() -> None:
    config.set_settings({"genre_min_weight": "3"})
    leftovers = list(config.config_dir().glob(".settings-*"))
    assert leftovers == []
    assert config.settings_path().exists()


# --- container_folders (mismatch path-signal suppression) ---------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ()),
        ("", ()),
        ("Soundtracks", ("Soundtracks",)),
        ("a;b", ("a", "b")),
        ("  a ; b  ", ("a", "b")),
        ("a;;b;", ("a", "b")),
        (";", ()),
    ],
)
def test_coerce_folder_list(raw: str | None, expected: tuple[str, ...]) -> None:
    assert config._coerce_folder_list(raw) == expected


def test_container_folders_default_empty() -> None:
    assert config.load_settings().container_folders == ()


def test_container_folders_roundtrip() -> None:
    config.set_setting("container_folders", "Soundtracks;Compilations")
    assert config.load_settings().container_folders == ("Soundtracks", "Compilations")


def test_container_folders_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    config.set_setting("container_folders", "FromFile")
    monkeypatch.setenv("TAGMEND_CONTAINER_FOLDERS", "FromEnv;Other")
    assert config.load_settings().container_folders == ("FromEnv", "Other")


# --- id3_droppable_frames (the ID3 frames a tag write may drop) ------------------------


def test_id3_droppable_frames_default_empty() -> None:
    assert config.load_settings().id3_droppable_frames == ()


def test_id3_droppable_frames_coerce_to_unique_upper_case_ids(
    tagmend_warnings: pytest.LogCaptureFixture,
) -> None:
    config.set_setting("id3_droppable_frames", " rvad ; ncon;bad!;RVAD")

    assert config.load_settings().id3_droppable_frames == ("RVAD", "NCON")
    messages = [record.getMessage() for record in tagmend_warnings.records]
    assert len(messages) == 1
    assert "'bad!'" in messages[0]


@pytest.mark.parametrize("entry", ["RVA", "RVADX", "RV-D", "ÄBCD"])
def test_id3_droppable_frames_reject_an_entry_that_is_not_a_frame_id(entry: str) -> None:
    config.set_setting("id3_droppable_frames", f"{entry};;TXXX;")

    assert config.load_settings().id3_droppable_frames == ("TXXX",)


# --- naming_pattern (the path renderer's pattern) --------------------------------------


def test_naming_pattern_defaults_empty_and_roundtrips_stripped() -> None:
    assert config.load_settings().naming_pattern == ""
    config.set_setting("naming_pattern", " {albumartist}/{title} ")
    assert config.load_settings().naming_pattern == "{albumartist}/{title}"


# --- song-axis settings --------------------------------------------------------------


def test_song_settings_default_when_unset() -> None:
    settings = config.load_settings()
    assert settings.acoustid_api_key is None
    assert settings.fpcalc_path is None
    assert settings.acoustid_rate_per_sec == 2.0
    assert settings.song_stage_limit == 150


def test_acoustid_api_key_env_override_wins_over_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    config.set_setting("acoustid_api_key", "from-file")
    assert config.load_settings().acoustid_api_key == "from-file"
    monkeypatch.setenv("TAGMEND_ACOUSTID_API_KEY", "from-env")
    assert config.load_settings().acoustid_api_key == "from-env"


def test_an_empty_fpcalc_path_means_path_lookup() -> None:
    config.set_setting("fpcalc_path", "")
    assert config.load_settings().fpcalc_path is None
    config.set_setting("fpcalc_path", "C:/tools/fpcalc.exe")
    assert config.load_settings().fpcalc_path == "C:/tools/fpcalc.exe"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("5.0", 3.0), ("3", 3.0), ("2.5", 2.5), ("0", 2.0), ("-1", 2.0), ("nan", 2.0), ("x", 2.0)],
)
def test_acoustid_rate_is_held_inside_the_published_limit(raw: str, expected: float) -> None:
    config.set_setting("acoustid_rate_per_sec", raw)
    assert config.load_settings().acoustid_rate_per_sec == expected


@pytest.mark.parametrize(("raw", "expected"), [("10", 10), ("0", 0), ("-3", 150), ("abc", 150)])
def test_song_stage_limit_is_coerced_like_the_other_limits(raw: str, expected: int) -> None:
    config.set_setting("song_stage_limit", raw)
    assert config.load_settings().song_stage_limit == expected


@pytest.mark.parametrize(("raw", "expected"), [("10", 10), ("0", 0), ("-3", 300), ("abc", 300)])
def test_artist_stage_limit_is_coerced_like_the_other_limits(raw: str, expected: int) -> None:
    config.set_setting("artist_stage_limit", raw)
    assert config.load_settings().artist_stage_limit == expected


@pytest.mark.parametrize("key", sorted(config.SECRET_KEYS))
def test_every_api_key_is_redacted_from_the_settings_repr(key: str) -> None:
    config.set_setting(key, "secret-api-key")
    settings = config.load_settings()
    assert getattr(settings, key) == "secret-api-key"
    assert "secret-api-key" not in repr(settings)
