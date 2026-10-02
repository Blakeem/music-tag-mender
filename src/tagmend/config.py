"""Settings & configuration.

Both frontends need the same settings, but the MCP server runs as a subprocess of
its client and cannot see the shell environment the CLI was launched from. So
configuration lives in **one JSON file on disk** (in the OS config dir), not in
environment variables.

Precedence: ``TAGMEND_*`` env override  >  ``settings.json``  >  built-in defaults.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import platformdirs

from tagmend import __version__
from tagmend.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

_APP_NAME: Final = "tagmend"
_SETTINGS_FILENAME: Final = "settings.json"
_DB_FILENAME: Final = "tagmend.sqlite3"
_KNOWN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "music_path",
        "lastfm_api_key",
        "db_path",
        "genre_min_weight",
        "genre_max_count",
        "genre_use_album_tags",
        "lastfm_rate_per_sec",
        "genre_stage_limit",
        "musicbrainz_rate_per_sec",
        "musicbrainz_contact",
        "year_stage_limit",
        "container_folders",
        "naming_pattern",
        "acoustid_api_key",
        "fpcalc_path",
        "acoustid_rate_per_sec",
        "song_stage_limit",
    },
)

# The API keys. The config UI masks them and ``config-set`` prompts for them without echoing.
SECRET_KEYS: Final[frozenset[str]] = frozenset({"lastfm_api_key", "acoustid_api_key"})

# A settings file written before the album axis became the year axis still carries the old key.
_LEGACY_KEYS: Final[Mapping[str, str]] = {"album_stage_limit": "year_stage_limit"}

# Defaults for the M2 genre-tagging settings (used by the coercion helpers below).
_GENRE_MIN_WEIGHT_DEFAULT: Final = 2
_GENRE_MAX_COUNT_DEFAULT: Final = 4
_LASTFM_RATE_PER_SEC_DEFAULT: Final = 1.0
# Last.fm's API terms allow 5 requests per second per IP, averaged over 5 minutes.
_LASTFM_RATE_PER_SEC_MAX: Final = 5.0
_GENRE_STAGE_LIMIT_DEFAULT: Final = 300

# The public project URL, the contact any User-Agent may carry without naming a person.
PROJECT_URL: Final = "https://github.com/Blakeem/music-tag-mender"

# Defaults for the year-axis MusicBrainz settings. MusicBrainz's published rate limit is
# ~1 request/second and it REQUIRES a descriptive User-Agent identifying the application
# plus a contact (an email or URL). We compose ``TagMend/<live-version> ( <contact> )`` at
# request time so the version never drifts; only the contact is user-configurable, and it
# defaults to the public project URL rather than a personal address.
_MUSICBRAINZ_RATE_PER_SEC_DEFAULT: Final = 1.0
_MUSICBRAINZ_RATE_PER_SEC_MAX: Final = 1.0
_MUSICBRAINZ_CONTACT_DEFAULT: Final = PROJECT_URL
_YEAR_STAGE_LIMIT_DEFAULT: Final = 300

# Defaults for the song-axis AcoustID settings. AcoustID publishes a limit of 3 requests per
# second, so a configured rate above it is clamped rather than trusted.
_ACOUSTID_RATE_PER_SEC_DEFAULT: Final = 2.0
_ACOUSTID_RATE_PER_SEC_MAX: Final = 3.0
_SONG_STAGE_LIMIT_DEFAULT: Final = 150

# Tokens (case-insensitive) that mean "no limit" for ``genre_max_count``.
_NONE_TOKENS: Final[frozenset[str]] = frozenset({"", "0", "none", "null"})

# Tokens (case-insensitive) that mean ``False`` for a boolean setting.
_FALSE_TOKENS: Final[frozenset[str]] = frozenset({"false", "0", "no", "off"})

logger = get_logger(__name__)

# Serializes concurrent writers (the CLI, the config web UI) so a merge never loses data.
_WRITE_LOCK: Final = threading.Lock()


def config_dir() -> Path:
    """Directory that holds ``settings.json`` (platform-specific).

    ``appauthor=False`` avoids the doubled ``tagmend/tagmend`` nesting that
    platformdirs produces on Windows when no author is given.
    """
    return Path(platformdirs.user_config_dir(_APP_NAME, appauthor=False))


def data_dir() -> Path:
    """Directory that holds the SQLite ledger and other mutable state."""
    return Path(platformdirs.user_data_dir(_APP_NAME, appauthor=False))


def settings_path() -> Path:
    """Absolute path to ``settings.json``."""
    return config_dir() / _SETTINGS_FILENAME


def default_db_path() -> Path:
    """Default SQLite ledger path."""
    return data_dir() / _DB_FILENAME


def build_user_agent(contact: str) -> str:
    """Compose the MusicBrainz ``User-Agent`` from the live app version + *contact*.

    MusicBrainz requires ``App/Version ( contact )``; *contact* is an email or URL.
    """
    return f"TagMend/{__version__} ( {contact} )"


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved, typed settings with file + env overrides already applied."""

    music_path: Path | None
    lastfm_api_key: str | None
    db_path: Path
    # M2 genre/Last.fm settings carry defaults so direct construction (tests, fixtures)
    # needn't restate them; ``load_settings`` always passes the coerced values.
    genre_min_weight: int = _GENRE_MIN_WEIGHT_DEFAULT
    genre_max_count: int | None = _GENRE_MAX_COUNT_DEFAULT
    genre_use_album_tags: bool = True
    lastfm_rate_per_sec: float = _LASTFM_RATE_PER_SEC_DEFAULT
    genre_stage_limit: int = _GENRE_STAGE_LIMIT_DEFAULT
    # Year-axis MusicBrainz settings carry defaults so direct construction (tests,
    # fixtures) needn't restate them; ``load_settings`` always passes the coerced values.
    musicbrainz_rate_per_sec: float = _MUSICBRAINZ_RATE_PER_SEC_DEFAULT
    musicbrainz_contact: str = _MUSICBRAINZ_CONTACT_DEFAULT
    year_stage_limit: int = _YEAR_STAGE_LIMIT_DEFAULT
    # Top-level folders that collect releases rather than name an artist. A semicolon-delimited
    # string on disk, coerced to a tuple here.
    container_folders: tuple[str, ...] = ()
    # The path naming pattern. Empty means the built-in default the naming module defines.
    naming_pattern: str = ""
    # Song-axis settings. The key stays out of the repr so a logged Settings never leaks it.
    acoustid_api_key: str | None = field(default=None, repr=False)
    fpcalc_path: str | None = None
    acoustid_rate_per_sec: float = _ACOUSTID_RATE_PER_SEC_DEFAULT
    song_stage_limit: int = _SONG_STAGE_LIMIT_DEFAULT

    @property
    def musicbrainz_user_agent(self) -> str:
        """The MusicBrainz ``User-Agent`` header: app name + live version + contact.

        Composed at read time so the version tracks ``tagmend.__version__`` and never
        drifts the way a stored string would; only the contact half is configurable.
        """
        return build_user_agent(self.musicbrainz_contact)


def load_settings() -> Settings:
    """Load settings, applying env overrides over the on-disk file over defaults.

    The on-disk store and env overrides are string-only, so the typed genre/Last.fm
    settings are coerced here; a malformed value logs a lazy ``%``-warning and falls back
    to the built-in default rather than raising.
    """
    raw = _read_raw_settings()

    music = _env_override("music_path") or raw.get("music_path")
    api_key = _env_override("lastfm_api_key") or raw.get("lastfm_api_key")
    db = _env_override("db_path") or raw.get("db_path")

    return Settings(
        music_path=Path(music).expanduser() if music else None,
        lastfm_api_key=api_key or None,
        db_path=Path(db).expanduser() if db else default_db_path(),
        genre_min_weight=_coerce_int(
            "genre_min_weight",
            _resolve_raw("genre_min_weight", raw),
            _GENRE_MIN_WEIGHT_DEFAULT,
        ),
        genre_max_count=_coerce_max_count(_resolve_raw("genre_max_count", raw)),
        genre_use_album_tags=_coerce_bool(
            _resolve_raw("genre_use_album_tags", raw),
            default=True,
        ),
        lastfm_rate_per_sec=_coerce_capped_rate(
            "lastfm_rate_per_sec",
            _resolve_raw("lastfm_rate_per_sec", raw),
            _LASTFM_RATE_PER_SEC_DEFAULT,
            _LASTFM_RATE_PER_SEC_MAX,
        ),
        genre_stage_limit=_coerce_non_negative_int(
            "genre_stage_limit",
            _resolve_raw("genre_stage_limit", raw),
            _GENRE_STAGE_LIMIT_DEFAULT,
        ),
        musicbrainz_rate_per_sec=_coerce_capped_rate(
            "musicbrainz_rate_per_sec",
            _resolve_raw("musicbrainz_rate_per_sec", raw),
            _MUSICBRAINZ_RATE_PER_SEC_DEFAULT,
            _MUSICBRAINZ_RATE_PER_SEC_MAX,
        ),
        musicbrainz_contact=_resolve_raw("musicbrainz_contact", raw)
        or _MUSICBRAINZ_CONTACT_DEFAULT,
        year_stage_limit=_coerce_non_negative_int(
            "year_stage_limit",
            _resolve_raw("year_stage_limit", raw),
            _YEAR_STAGE_LIMIT_DEFAULT,
        ),
        container_folders=_coerce_folder_list(_resolve_raw("container_folders", raw)),
        naming_pattern=(_resolve_raw("naming_pattern", raw) or "").strip(),
        acoustid_api_key=_resolve_raw("acoustid_api_key", raw) or None,
        fpcalc_path=_resolve_raw("fpcalc_path", raw) or None,
        acoustid_rate_per_sec=_coerce_capped_rate(
            "acoustid_rate_per_sec",
            _resolve_raw("acoustid_rate_per_sec", raw),
            _ACOUSTID_RATE_PER_SEC_DEFAULT,
            _ACOUSTID_RATE_PER_SEC_MAX,
        ),
        song_stage_limit=_coerce_non_negative_int(
            "song_stage_limit",
            _resolve_raw("song_stage_limit", raw),
            _SONG_STAGE_LIMIT_DEFAULT,
        ),
    )


def _resolve_raw(key: str, raw: dict[str, str]) -> str | None:
    """Return the env override for *key*, falling back to the on-disk value (or None)."""
    return _env_override(key) or raw.get(key)


def _coerce_int(key: str, value: str | None, default: int) -> int:
    """Parse *value* as an ``int``; warn and use *default* when missing or malformed."""
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("invalid %s=%r; using default %d", key, value, default)
        return default


def _coerce_non_negative_int(key: str, value: str | None, default: int) -> int:
    """Parse *value* like :func:`_coerce_int`, and also warn and use *default* when negative.

    A stage limit caps a Python slice, where a negative value means "all but the last N".
    """
    parsed = _coerce_int(key, value, default)
    if parsed < 0:
        logger.warning("invalid %s=%r; using default %d", key, value, default)
        return default
    return parsed


def _coerce_float(key: str, value: str | None, default: float) -> float:
    """Parse *value* as a ``float``; warn and use *default* when missing or malformed."""
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("invalid %s=%r; using default %s", key, value, default)
        return default


def _coerce_capped_rate(key: str, value: str | None, default: float, cap: float) -> float:
    """Parse *value* like :func:`_coerce_float`, then hold it inside ``(0, cap]``.

    A rate at or below zero would disable pacing entirely, so it falls back to *default*.
    """
    parsed = _coerce_float(key, value, default)
    # Written as a negation so NaN, which compares false to everything, is rejected too.
    if not parsed > 0:
        logger.warning("invalid %s=%r; using default %s", key, value, default)
        return default
    if parsed > cap:
        logger.warning("%s=%r exceeds the published limit; using %s", key, value, cap)
        return cap
    return parsed


def _coerce_max_count(value: str | None) -> int | None:
    """Parse ``genre_max_count``: unset → default cap; a sentinel → ``None`` (unlimited).

    Unset (not configured) yields the default cap of ``_GENRE_MAX_COUNT_DEFAULT``. The
    sentinel tokens (empty string, ``0``, ``none``, ``null``; case-insensitive) explicitly
    mean "no cap" (``None``). Any other value is parsed as ``int``. A malformed or negative
    one warns and falls back to the default cap, since the cap is a slice bound where a
    negative value means "all but the last N". Any spelling that parses to 0 (``00``,
    ``-0``) also means no cap, since a slice bound of 0 would drop every genre.
    """
    parsed: int | None = None
    if value is None:
        return _GENRE_MAX_COUNT_DEFAULT
    if value.strip().lower() in _NONE_TOKENS:
        return None
    with contextlib.suppress(ValueError):
        parsed = int(value)
    if parsed is None or parsed < 0:
        logger.warning(
            "invalid genre_max_count=%r; using default %d",
            value,
            _GENRE_MAX_COUNT_DEFAULT,
        )
        return _GENRE_MAX_COUNT_DEFAULT
    if parsed == 0:
        return None
    return parsed


def _coerce_bool(value: str | None, *, default: bool) -> bool:
    """Parse *value* as a bool; ``false``/``0``/``no``/``off`` (any case) → ``False``."""
    if value is None:
        return default
    return value.strip().lower() not in _FALSE_TOKENS


def _coerce_folder_list(value: str | None) -> tuple[str, ...]:
    """Parse a semicolon-delimited folder list into a tuple; strip entries, drop empties.

    Unset (``None``) or all-empty yields ``()``. Each segment is stripped and blank segments
    (from leading/trailing/repeated ``;``) are dropped, so ``" a ; ;b; "`` → ``("a", "b")``.
    """
    if value is None:
        return ()
    return tuple(stripped for part in value.split(";") if (stripped := part.strip()))


def set_setting(key: str, value: str) -> Path:
    """Persist a single key into ``settings.json`` and return the file path.

    Thin wrapper over :func:`set_settings` so both the single- and batch-key writers share
    one atomic, lock-guarded merge path.
    """
    return set_settings({key: value})


def set_settings(mapping: dict[str, str]) -> Path:
    """Merge a batch of keys into ``settings.json`` atomically and return the file path.

    Every key must be in ``_KNOWN_KEYS`` (a ``ValueError`` lists any unknowns). The given
    *mapping* is **merged** over the current on-disk values — never a wholesale replace, so
    keys absent from *mapping* are preserved. A file that exists but cannot be read or parsed
    raises ``ValueError`` before anything is written. The write is serialized by a module-level
    lock and is atomic (temp file, fsync, restrict permissions, then rename) so a concurrent
    writer or a mid-write crash can never leave a partial file.
    """
    # Input: reject unknown keys before touching disk.
    unknown = sorted(key for key in mapping if key not in _KNOWN_KEYS)
    if unknown:
        known = ", ".join(sorted(_KNOWN_KEYS))
        message = f"unknown setting {', '.join(unknown)}; known keys: {known}"
        raise ValueError(message)

    path = settings_path()

    # Process + Output: merge under the lock, then write atomically.
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        current = _parse_settings_file(path)
        current.update(mapping)
        serialized = json.dumps(current, indent=2, sort_keys=True) + "\n"
        _atomic_write(path, serialized)

    logger.info("saved %d setting(s) to %s", len(mapping), path)
    return path


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* atomically: temp file → fsync → restrict perms → rename.

    The temp file lives in the destination directory so the final ``replace`` is an atomic
    rename on the same filesystem. On any failure the temp file is removed, so a crash never
    leaves a half-written ``settings.json`` behind.
    """
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".tmp")
    temp = Path(temp_name)
    succeeded = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_permissions(temp)
        temp.replace(path)
        succeeded = True
    finally:
        if not succeeded:
            temp.unlink(missing_ok=True)


def _env_override(key: str) -> str | None:
    """Read ``TAGMEND_<KEY>`` from the environment, if set."""
    return os.environ.get(f"{_APP_NAME.upper()}_{key.upper()}")


def _read_raw_settings() -> dict[str, str]:
    """Read ``settings.json`` into a flat string map; tolerate a missing/invalid file."""
    path = settings_path()
    if not path.exists():
        logger.debug("no settings file at %s; using defaults", path)
        return {}
    try:
        return _parse_settings_file(path)
    except ValueError as exc:
        logger.warning("%s; using defaults", exc)
        return {}


def _parse_settings_file(path: Path) -> dict[str, str]:
    """Read *path* into a flat string map, or ``{}`` when it is absent.

    Raises :class:`ValueError` when the file exists but cannot be read or parsed, or holds a
    non-object, so a writer never merges over a map that dropped every stored key.
    """
    if not path.exists():
        return {}
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(_unreadable_settings_message(path, exc)) from exc
    if isinstance(parsed, dict):
        raw = {str(k): str(v) for k, v in parsed.items() if v is not None}
        return _rename_legacy_keys(raw)
    # A parseable file holding the wrong shape is the same unusable settings file to the caller.
    raise ValueError(_unreadable_settings_message(path, "not a JSON object"))


def _unreadable_settings_message(path: Path, reason: object) -> str:
    """Return the refusal for a settings file that exists but holds no usable map."""
    return (
        f"settings file {path} could not be read ({reason}). "
        "Repair or remove it, then save again. Nothing was saved"
    )


def _rename_legacy_keys(raw: dict[str, str]) -> dict[str, str]:
    """Move each :data:`_LEGACY_KEYS` value to its new key unless that key is set, then drop it.

    :func:`set_settings` merges over this map, so the next save rewrites the file without the
    legacy key.
    """
    renamed = dict(raw)
    for legacy_key, new_key in _LEGACY_KEYS.items():
        legacy_value = renamed.pop(legacy_key, None)
        if legacy_value is not None and new_key not in renamed:
            renamed[new_key] = legacy_value
    return renamed


def _restrict_permissions(path: Path) -> None:
    """Best-effort: make the settings file user-readable/writable only (holds a key)."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:  # Windows ACLs differ; non-fatal
        logger.debug("could not restrict permissions on %s: %s", path, exc)
