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
_KNOWN_KEYS: Final[frozenset[str]] = frozenset({"music_path", "lastfm_api_key", "db_path"})

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


def load_settings() -> Settings:
    """Load settings, applying env overrides over the on-disk file over defaults."""
    raw = _read_raw_settings()

    music = _env_override("music_path") or raw.get("music_path")
    api_key = _env_override("lastfm_api_key") or raw.get("lastfm_api_key")
    db = _env_override("db_path") or raw.get("db_path")

    return Settings(
        music_path=Path(music).expanduser() if music else None,
        lastfm_api_key=api_key or None,
        db_path=Path(db).expanduser() if db else default_db_path(),
    )


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
