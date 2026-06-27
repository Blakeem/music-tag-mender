"""Settings & configuration.

Both frontends need the same settings, but the MCP server runs as a subprocess of
its client and cannot see the shell environment the CLI was launched from. So
configuration lives in **one JSON file on disk** (in the OS config dir), not in
environment variables.

Precedence: ``TAGMEND_*`` env override  >  ``settings.json``  >  built-in defaults.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import platformdirs

from tagmend.log import get_logger

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
        "musicbrainz_user_agent",
        "album_stage_limit",
    },
)

# Defaults for the M2 genre-tagging settings (used by the coercion helpers below).
_GENRE_MIN_WEIGHT_DEFAULT: Final = 2
_GENRE_MAX_COUNT_DEFAULT: Final = 4
_LASTFM_RATE_PER_SEC_DEFAULT: Final = 1.0
_GENRE_STAGE_LIMIT_DEFAULT: Final = 300

# Defaults for the album-axis MusicBrainz settings. MusicBrainz's published rate limit is
# ~1 request/second and it REQUIRES a descriptive User-Agent identifying the application.
_MUSICBRAINZ_RATE_PER_SEC_DEFAULT: Final = 1.0
_MUSICBRAINZ_USER_AGENT_DEFAULT: Final = "TagMend/0.1 ( blakeem@gmail.com )"
_ALBUM_STAGE_LIMIT_DEFAULT: Final = 300

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
    # Album-axis MusicBrainz settings carry defaults so direct construction (tests,
    # fixtures) needn't restate them; ``load_settings`` always passes the coerced values.
    musicbrainz_rate_per_sec: float = _MUSICBRAINZ_RATE_PER_SEC_DEFAULT
    musicbrainz_user_agent: str = _MUSICBRAINZ_USER_AGENT_DEFAULT
    album_stage_limit: int = _ALBUM_STAGE_LIMIT_DEFAULT


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
        lastfm_rate_per_sec=_coerce_float(
            "lastfm_rate_per_sec",
            _resolve_raw("lastfm_rate_per_sec", raw),
            _LASTFM_RATE_PER_SEC_DEFAULT,
        ),
        genre_stage_limit=_coerce_int(
            "genre_stage_limit",
            _resolve_raw("genre_stage_limit", raw),
            _GENRE_STAGE_LIMIT_DEFAULT,
        ),
        musicbrainz_rate_per_sec=_coerce_float(
            "musicbrainz_rate_per_sec",
            _resolve_raw("musicbrainz_rate_per_sec", raw),
            _MUSICBRAINZ_RATE_PER_SEC_DEFAULT,
        ),
        musicbrainz_user_agent=_resolve_raw("musicbrainz_user_agent", raw)
        or _MUSICBRAINZ_USER_AGENT_DEFAULT,
        album_stage_limit=_coerce_int(
            "album_stage_limit",
            _resolve_raw("album_stage_limit", raw),
            _ALBUM_STAGE_LIMIT_DEFAULT,
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


def _coerce_float(key: str, value: str | None, default: float) -> float:
    """Parse *value* as a ``float``; warn and use *default* when missing or malformed."""
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("invalid %s=%r; using default %s", key, value, default)
        return default


def _coerce_max_count(value: str | None) -> int | None:
    """Parse ``genre_max_count``: unset → default cap; a sentinel → ``None`` (unlimited).

    Unset (not configured) yields the default cap of ``_GENRE_MAX_COUNT_DEFAULT``. The
    sentinel tokens (empty string, ``0``, ``none``, ``null``; case-insensitive) explicitly
    mean "no cap" (``None``). Any other value is parsed as ``int``; a malformed one warns
    and falls back to the default cap.
    """
    if value is None:
        return _GENRE_MAX_COUNT_DEFAULT
    if value.strip().lower() in _NONE_TOKENS:
        return None
    try:
        return int(value)
    except ValueError:
        logger.warning(
            "invalid genre_max_count=%r; using default %d",
            value,
            _GENRE_MAX_COUNT_DEFAULT,
        )
        return _GENRE_MAX_COUNT_DEFAULT


def _coerce_bool(value: str | None, *, default: bool) -> bool:
    """Parse *value* as a bool; ``false``/``0``/``no``/``off`` (any case) → ``False``."""
    if value is None:
        return default
    return value.strip().lower() not in _FALSE_TOKENS


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
    keys absent from *mapping* are preserved. The write is serialized by a module-level lock
    and is atomic (temp file, fsync, restrict permissions, then rename) so a concurrent
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
        current = _read_raw_settings()
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
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not read settings at %s: %s", path, exc)
        return {}

    if not isinstance(parsed, dict):
        logger.warning("settings at %s is not a JSON object; ignoring", path)
        return {}

    return {str(k): str(v) for k, v in parsed.items() if v is not None}


def _restrict_permissions(path: Path) -> None:
    """Best-effort: make the settings file user-readable/writable only (holds a key)."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:  # Windows ACLs differ; non-fatal
        logger.debug("could not restrict permissions on %s: %s", path, exc)
