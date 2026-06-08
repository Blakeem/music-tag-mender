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
    },
)

# Defaults for the M2 genre-tagging settings (used by the coercion helpers below).
_GENRE_MIN_WEIGHT_DEFAULT: Final = 2
_LASTFM_RATE_PER_SEC_DEFAULT: Final = 1.0
_GENRE_STAGE_LIMIT_DEFAULT: Final = 300

# Tokens (case-insensitive) that mean "no limit" for ``genre_max_count``.
_NONE_TOKENS: Final[frozenset[str]] = frozenset({"", "0", "none", "null"})

# Tokens (case-insensitive) that mean ``False`` for a boolean setting.
_FALSE_TOKENS: Final[frozenset[str]] = frozenset({"false", "0", "no", "off"})

logger = get_logger(__name__)


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
    genre_max_count: int | None = None
    genre_use_album_tags: bool = True
    lastfm_rate_per_sec: float = _LASTFM_RATE_PER_SEC_DEFAULT
    genre_stage_limit: int = _GENRE_STAGE_LIMIT_DEFAULT


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
    """Parse ``genre_max_count``: a "none" sentinel → ``None`` (unlimited), else ``int``.

    The sentinel tokens (empty string, ``0``, ``none``, ``null``; case-insensitive) all
    mean "no cap". A malformed non-sentinel value warns and falls back to ``None``.
    """
    if value is None:
        return None
    if value.strip().lower() in _NONE_TOKENS:
        return None
    try:
        return int(value)
    except ValueError:
        logger.warning("invalid genre_max_count=%r; using default None", value)
        return None


def _coerce_bool(value: str | None, *, default: bool) -> bool:
    """Parse *value* as a bool; ``false``/``0``/``no``/``off`` (any case) → ``False``."""
    if value is None:
        return default
    return value.strip().lower() not in _FALSE_TOKENS


def set_setting(key: str, value: str) -> Path:
    """Persist a single key into ``settings.json`` and return the file path."""
    if key not in _KNOWN_KEYS:
        known = ", ".join(sorted(_KNOWN_KEYS))
        message = f"unknown setting {key!r}; known keys: {known}"
        raise ValueError(message)

    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    current = _read_raw_settings()
    current[key] = value
    serialized = json.dumps(current, indent=2, sort_keys=True) + "\n"
    path.write_text(serialized, encoding="utf-8")
    _restrict_permissions(path)

    logger.info("saved setting %r to %s", key, path)
    return path


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
