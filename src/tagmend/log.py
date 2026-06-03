"""Shared logging for TagMend.

Every module obtains its logger via :func:`get_logger`; nothing in the engine, CLI,
or server code calls :func:`print`. User-facing CLI output goes through Typer, which
is deliberately separate from logging.

Named ``log.py`` rather than ``logging.py`` to avoid shadowing the stdlib module.

MCP safety: when TagMend runs as an MCP server over stdio, stdout carries the
JSON-RPC protocol and must never be polluted. All log records therefore go to
**stderr** only.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Final

_ROOT_NAME: Final = "tagmend"
_DEFAULT_LEVEL: Final = "INFO"
_LOG_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def _configure_root() -> logging.Logger:
    """Configure the ``tagmend`` root logger exactly once and return it."""
    root = logging.getLogger(_ROOT_NAME)
    if root.handlers:
        return root

    level_name = os.environ.get("TAGMEND_LOG_LEVEL", _DEFAULT_LEVEL).upper()
    root.setLevel(logging.getLevelNamesMapping().get(level_name, logging.INFO))

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(handler)
    root.propagate = False
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a configured child logger of the ``tagmend`` root.

    Pass ``__name__`` from the calling module. Names already under ``tagmend`` are
    used as-is; bare names are nested beneath the root.
    """
    _configure_root()
    qualified = name if name.startswith(_ROOT_NAME) else f"{_ROOT_NAME}.{name}"
    return logging.getLogger(qualified)


def set_level(level: int | str) -> None:
    """Set the level of the whole ``tagmend`` logger tree (used by ``--verbose``)."""
    _configure_root().setLevel(level)
